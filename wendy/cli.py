"""Claude CLI subprocess manager.

Spawns the ``claude`` CLI subprocess, manages session resolution,
and streams output. Responses flow through the internal HTTP API, not stdout.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any

from . import sessions
from .config import (
    CLAUDE_CLI_IDLE_TIMEOUT,
    CLAUDE_CLI_MAX_RUNTIME,
    CLI_SUBPROCESS_UID,
    MAX_STREAM_LOG_LINES,
    PROXY_PORT,
    SENSITIVE_ENV_VARS,
    resolve_model,
)
from .paths import (
    STREAM_LOG_FILE,
    WENDY_BASE,
    channel_dir,
    ensure_channel_dirs,
    ensure_shared_dirs,
    session_dir,
)

_LOG = logging.getLogger(__name__)

TOOL_INSTRUCTIONS_TEMPLATE = """
---
REAL-TIME CHANNEL TOOLS (Channel ID: {channel_id})

1. SEND A MESSAGE:
   curl -X POST http://localhost:{proxy_port}/api/send_message -H "Content-Type: application/json" -d '{{"channel_id": "{channel_id}", "content": "your message here"}}'

   With attachment (file under /data/wendy/ or /tmp/):
   curl -X POST http://localhost:{proxy_port}/api/send_message -H "Content-Type: application/json" -d '{{"channel_id": "{channel_id}", "content": "check this out", "attachment": "/data/wendy/channels/{channel_name}/output.png"}}'

   Reply to a specific message:
   curl -X POST http://localhost:{proxy_port}/api/send_message -H "Content-Type: application/json" -d '{{"channel_id": "{channel_id}", "content": "great point", "reply_to": MESSAGE_ID}}'

   The response includes a "new_messages" array. If there are new messages, respond to them before finishing.

   If the API returns an error about new messages, check them and incorporate into your reply:
   curl -X POST http://localhost:{proxy_port}/api/send_message -H "Content-Type: application/json" -d '{{"channel_id": "{channel_id}", "content": "your message", "force": true}}'

2. ADD EMOJI REACTION:
   curl -X POST http://localhost:{proxy_port}/api/send_message -H "Content-Type: application/json" -d '{{"channel_id": "{channel_id}", "actions": [{{"type": "add_reaction", "message_id": MESSAGE_ID, "emoji": "thumbsup"}}]}}'

ATTACHMENTS:
When users upload files, check_messages includes "attachments" with file paths.
You MUST call Read on each path to see the content.

WORKSPACE:
Your workspace is /data/wendy/channels/{channel_name}/ - files persist between conversations.

MESSAGE HISTORY:
  sqlite3 /data/wendy/shared/wendy.db "SELECT * FROM message_history WHERE content LIKE '%keyword%' LIMIT 20"
"""


class ClaudeCliError(Exception):
    pass


def find_cli_path() -> str:
    cli_path = os.getenv("CLAUDE_CLI_PATH")
    if cli_path and Path(cli_path).exists():
        return cli_path
    candidates = [
        str(Path.home() / ".local" / "bin" / "claude"),
        str(Path.home() / ".claude" / "local" / "claude"),
        shutil.which("claude"),
    ]
    for path in candidates:
        if path and Path(path).exists():
            return path
    raise ClaudeCliError("Claude CLI not found. Install it or set CLAUDE_CLI_PATH env var.")


def build_cli_command(cli_path: str, session_id: str, is_new_session: bool,
                      system_prompt: str, model: str, fork_mode: bool = False,
                      max_turns: int | None = None) -> list[str]:
    cmd = [cli_path, "-p", "--output-format", "stream-json", "--verbose", "--model", model]

    if fork_mode:
        cmd.extend(["--resume", session_id, "--fork-session"])
    elif is_new_session:
        cmd.extend(["--session-id", session_id])
    else:
        cmd.extend(["--resume", session_id])

    if max_turns is not None:
        cmd.extend(["--max-turns", str(max_turns)])
    if system_prompt:
        cmd.extend(["--append-system-prompt", system_prompt])
    return cmd


def build_nudge_prompt(channel_id: int, is_thread: bool = False, thread_name: str | None = None,
                       was_compacted: bool = False) -> str:
    if is_thread:
        base = (
            f'<you\'ve been forked into a Discord thread: "{thread_name}". '
            f"Your conversation history from the parent channel has been preserved. "
            f"You MUST call curl -s http://localhost:{PROXY_PORT}/api/check_messages/{channel_id} "
            f"before any other action. Do not assume what the messages contain.>"
        )
    else:
        base = (
            f"<new messages - you MUST call curl -s http://localhost:{PROXY_PORT}/api/check_messages/{channel_id} "
            f"before any other action. Do not assume what the messages contain.>"
        )
    if was_compacted:
        base += (
            f"\n<your session was auto-compacted since your last turn. "
            f"Use count=20 to restore context: "
            f"curl -s 'http://localhost:{PROXY_PORT}/api/check_messages/{channel_id}?count=20'>"
        )
    return base


def setup_channel_folder(channel_name: str) -> None:
    ensure_channel_dirs(channel_name)
    chan_dir = channel_dir(channel_name)
    claude_settings_src = Path("/app/config/claude_settings.json")
    claude_dir = chan_dir / ".claude"
    claude_dir.mkdir(exist_ok=True)
    settings_dest = claude_dir / "settings.json"
    if claude_settings_src.exists():
        if not settings_dest.exists() or settings_dest.stat().st_mtime < claude_settings_src.stat().st_mtime:
            shutil.copy2(claude_settings_src, settings_dest)


def append_to_stream_log(event: dict, channel_id: int | None) -> None:
    try:
        STREAM_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        enriched = {"ts": int(time.time() * 1000), "channel_id": str(channel_id) if channel_id else None, "event": event}
        with open(STREAM_LOG_FILE, "a") as f:
            f.write(json.dumps(enriched) + "\n")
    except Exception as e:
        _LOG.error("Failed to append to stream log: %s", e)


def trim_stream_log() -> None:
    try:
        if not STREAM_LOG_FILE.exists():
            return
        with open(STREAM_LOG_FILE) as f:
            lines = f.readlines()
        if len(lines) > MAX_STREAM_LOG_LINES:
            with open(STREAM_LOG_FILE, "w") as f:
                f.writelines(lines[-MAX_STREAM_LOG_LINES:])
    except Exception as e:
        _LOG.error("Failed to trim stream log: %s", e)


def get_recent_cli_error() -> str | None:
    debug_dir = Path.home() / ".claude" / "debug"
    if not debug_dir.exists():
        return None
    try:
        debug_files = sorted(debug_dir.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not debug_files:
            return None
        content = debug_files[0].read_text(errors="replace")
        if "OAuth token has expired" in content:
            return "OAuth token has expired"
        for line in reversed(content.strip().split("\n")[-20:]):
            if "[ERROR]" in line:
                if "Error:" in line:
                    return line.split("Error:", 1)[-1].strip()[:200]
                return line.split("[ERROR]", 1)[-1].strip()[:200]
    except Exception as e:
        _LOG.warning("Failed to read CLI debug files: %s", e)
    return None


def extract_forked_session_id(events: list[dict], session_cwd_folder: str) -> str | None:
    for event in reversed(events):
        if event.get("type") == "result" and event.get("session_id"):
            return event["session_id"]
    for event in events:
        if event.get("type") == "system" and event.get("session_id"):
            return event["session_id"]
    try:
        index_path = session_dir(session_cwd_folder) / "sessions-index.json"
        if index_path.exists():
            index = json.loads(index_path.read_text())
            entries = index.get("entries", [])
            if entries:
                entries.sort(key=lambda e: e.get("modified", ""), reverse=True)
                return entries[0].get("sessionId")
    except Exception as e:
        _LOG.warning("Failed to read sessions-index.json: %s", e)
    return None


def _resolve_session(channel_id: int, channel_config: dict, session_cwd_folder: str,
                     force_new_session: bool) -> tuple[str, bool, bool]:
    is_thread = channel_config.get("_is_thread", False)
    parent_folder = channel_config.get("_parent_folder")
    session_info = sessions.get_session(channel_id)

    channel_changed = session_info is not None and session_info.folder != session_cwd_folder
    is_new_session = session_info is None or force_new_session or channel_changed

    if not is_new_session and session_info:
        sess_file = session_dir(session_cwd_folder) / f"{session_info.session_id}.jsonl"
        if not sess_file.exists():
            is_new_session = True

    fork_mode = False
    session_id = ""
    if is_new_session and is_thread and parent_folder:
        parent_channel_id = int(channel_config.get("_parent_channel_id", 0))
        parent_session = sessions.get_session(parent_channel_id)
        if parent_session:
            parent_sess_file = session_dir(session_cwd_folder) / f"{parent_session.session_id}.jsonl"
            if parent_sess_file.exists():
                session_id = parent_session.session_id
                fork_mode = True

    if is_new_session and not fork_mode:
        session_id = sessions.create_session(channel_id, session_cwd_folder)
    elif not is_new_session:
        session_id = session_info.session_id

    return session_id, is_new_session, fork_mode


def _build_cli_env(channel_name: str) -> dict[str, str]:
    cli_env = {k: v for k, v in os.environ.items() if k not in SENSITIVE_ENV_VARS}
    if oauth_token := os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        cli_env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token
    if CLI_SUBPROCESS_UID is not None:
        cli_env["HOME"] = "/home/wendy"
    return cli_env


def _is_session_resume_error(cmd: list[str], error_text: str) -> bool:
    if "--resume" not in cmd:
        return False
    lower = error_text.lower()
    return "session" in lower or "no conversation found" in lower


async def _stream_cli_output(proc: asyncio.subprocess.Process, channel_id: int,
                             idle_timeout: int, max_runtime: int) -> tuple[list[dict], dict[str, Any]]:
    events: list[dict] = []
    usage: dict[str, Any] = {}
    start = time.monotonic()

    while True:
        elapsed = time.monotonic() - start
        remaining = max_runtime - elapsed
        if remaining <= 0:
            raise TimeoutError(f"hit max runtime ({max_runtime}s)")
        try:
            raw = await asyncio.wait_for(proc.stdout.readline(), timeout=min(idle_timeout, remaining))
        except TimeoutError:
            elapsed = time.monotonic() - start
            msg = f"hit max runtime ({max_runtime}s)" if elapsed >= max_runtime - 1 else f"idle for {idle_timeout}s"
            raise TimeoutError(msg)
        if not raw:
            break
        decoded = raw.decode("utf-8").strip()
        if not decoded:
            continue
        try:
            event = json.loads(decoded)
            events.append(event)
            append_to_stream_log(event, channel_id)
            if event.get("type") == "result":
                usage = event.get("usage", {})
        except json.JSONDecodeError:
            continue
    return events, usage


def _kill_process(proc: asyncio.subprocess.Process | None) -> None:
    if proc is None:
        return
    if proc.returncode is None:
        try:
            proc.kill()
        except Exception:
            pass


async def run_cli(channel_id: int, channel_config: dict, system_prompt: str,
                  model_override: str | None = None, force_new_session: bool = False,
                  nudge_override: str | None = None, timeout_override: int | None = None) -> None:
    cli_path = find_cli_path()
    channel_name = channel_config.get("_folder", channel_config.get("name", "default"))

    is_thread = channel_config.get("_is_thread", False)
    parent_folder = channel_config.get("_parent_folder")
    thread_name = channel_config.get("_thread_name")
    session_cwd_folder = parent_folder if (is_thread and parent_folder) else channel_name

    session_id, is_new_session, fork_mode = _resolve_session(
        channel_id, channel_config, session_cwd_folder, force_new_session,
    )

    effective_model = resolve_model(model_override or channel_config.get("model"))

    # Build tool permissions
    allowed = (
        f"Read,WebSearch,WebFetch,Bash,"
        f"Edit(//data/wendy/channels/{channel_name}/**),Write(//data/wendy/channels/{channel_name}/**),"
        f"Write(//tmp/**)"
    )
    disallowed = "Edit(//app/**),Write(//app/**),Skill,TodoWrite,TodoRead"

    cmd = build_cli_command(cli_path, session_id, is_new_session, system_prompt, effective_model, fork_mode=fork_mode)
    cmd.extend(["--allowedTools", allowed, "--disallowedTools", disallowed])

    compacted_flag = channel_dir(channel_name) / ".compacted"
    was_compacted = compacted_flag.exists()
    if was_compacted:
        compacted_flag.unlink(missing_ok=True)

    nudge_prompt = nudge_override or build_nudge_prompt(
        channel_id, is_thread=is_thread, thread_name=thread_name, was_compacted=was_compacted,
    )

    WENDY_BASE.mkdir(parents=True, exist_ok=True)
    ensure_shared_dirs()
    setup_channel_folder(channel_name)

    _LOG.info("CLI: %s session %s for channel %d (model=%s)",
              "starting new" if is_new_session else "resuming", session_id[:8], channel_id, effective_model)

    proc = None
    idle_timeout = CLAUDE_CLI_IDLE_TIMEOUT
    max_runtime = timeout_override if timeout_override is not None else CLAUDE_CLI_MAX_RUNTIME
    try:
        user_kwargs = {"user": CLI_SUBPROCESS_UID} if CLI_SUBPROCESS_UID else {}
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, limit=10 * 1024 * 1024,
            cwd=channel_dir(session_cwd_folder), env=_build_cli_env(channel_name), **user_kwargs,
        )

        proc.stdin.write(nudge_prompt.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()
        await proc.stdin.wait_closed()

        events, usage = await _stream_cli_output(proc, channel_id, idle_timeout, max_runtime)
        await proc.wait()

        if proc.returncode != 0:
            error_detail = get_recent_cli_error() or "unknown error"
            _LOG.error("CLI failed (code %d): %s", proc.returncode, error_detail)
            if _is_session_resume_error(cmd, error_detail) and not force_new_session:
                return await run_cli(channel_id, channel_config, system_prompt,
                                     model_override=model_override, force_new_session=True,
                                     nudge_override=nudge_override, timeout_override=timeout_override)
            raise ClaudeCliError(f"CLI failed (code {proc.returncode}): {error_detail}")

        trim_stream_log()

        if fork_mode:
            forked_id = extract_forked_session_id(events, session_cwd_folder)
            if forked_id:
                sessions.create_session(channel_id, session_cwd_folder, session_id=forked_id)

        if usage:
            sessions.update_stats(channel_id, usage)

        _LOG.info("CLI: completed, events_streamed=%d", len(events))

    except TimeoutError as exc:
        _kill_process(proc)
        raise ClaudeCliError(f"Timed out: {exc}") from None
    except asyncio.CancelledError:
        _kill_process(proc)
        raise
