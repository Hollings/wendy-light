"""Internal HTTP API server for the Claude CLI subprocess.

Claude CLI calls these endpoints via curl to send Discord messages
and read message history.

Routes:
    POST /api/send_message          -- send or batch-send Discord messages
    GET  /api/check_messages/:id    -- fetch recent messages from SQLite
    GET  /api/emojis                -- search custom server emojis
    GET  /health                    -- liveness check
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

from . import config as _config
from .config import DISCORD_MAX_MESSAGE_LENGTH, MAX_MESSAGE_LIMIT, SYNTHETIC_ID_THRESHOLD
from .paths import SHARED_DIR, WENDY_BASE, find_attachments_for_message
from .state import state as state_manager

if TYPE_CHECKING:
    import discord

_LOG = logging.getLogger(__name__)

_channel_configs: dict[int, dict] = {}
_discord_bot: discord.Client | None = None


def set_discord_bot(bot: discord.Client) -> None:
    global _discord_bot
    _discord_bot = bot


def set_channel_configs(configs: dict[int, dict]) -> None:
    global _channel_configs
    _channel_configs = configs


def get_channel_name(channel_id: int) -> str | None:
    cfg = _channel_configs.get(channel_id)
    if cfg:
        return cfg.get("_folder") or cfg.get("name")
    return state_manager.get_thread_folder(channel_id)


def check_for_new_messages(channel_id: int) -> list[dict]:
    return state_manager.check_for_new_messages(
        channel_id, bot_user_id=_config.BOT_USER_ID,
        synthetic_id_threshold=SYNTHETIC_ID_THRESHOLD, max_limit=MAX_MESSAGE_LIMIT,
    )


def _save_bot_message(msg, channel_id: int) -> None:
    if not msg:
        return
    try:
        state_manager.insert_message(
            message_id=msg.id, channel_id=channel_id,
            guild_id=msg.guild.id if msg.guild else None,
            author_id=msg.author.id, author_nickname=msg.author.display_name,
            is_bot=True, content=msg.content or "", timestamp=int(msg.created_at.timestamp()),
        )
    except Exception as e:
        _LOG.warning("Failed to save bot message %s: %s", msg.id, e)


def _validate_attachment_path(path_str: str) -> str | None:
    att_path = Path(path_str).resolve()
    allowed_parents = [WENDY_BASE.resolve(), Path("/tmp").resolve()]
    if not any(att_path == parent or parent in att_path.parents for parent in allowed_parents):
        return f"Attachment must be in {WENDY_BASE}/ or /tmp/, got: {path_str}"
    if not att_path.exists():
        return f"Attachment file not found: {path_str}"
    return None


def _build_discord_send_kwargs(body: dict, channel_id: int) -> tuple[dict, str | None]:
    import discord as _discord

    text = body.get("content") or body.get("message") or ""
    if len(text) > DISCORD_MAX_MESSAGE_LENGTH:
        return {}, f"Message too long ({len(text)} chars). Discord limit is {DISCORD_MAX_MESSAGE_LENGTH}."

    att_path = body.get("file_path") or body.get("attachment")
    if att_path:
        err = _validate_attachment_path(att_path)
        if err:
            return {}, err

    kwargs: dict = {"content": text or None}
    if att_path:
        kwargs["file"] = _discord.File(att_path)

    reply_to = body.get("reply_to")
    if reply_to:
        kwargs["reference"] = _discord.MessageReference(message_id=int(reply_to), channel_id=channel_id)

    return kwargs, None


def _parse_channel_id(body: dict) -> tuple[int | None, web.Response | None]:
    raw = body.get("channel_id")
    if not raw:
        return None, web.json_response({"error": "channel_id required"}, status=400)
    try:
        return int(raw), None
    except ValueError:
        return None, web.json_response({"error": "Invalid channel_id"}, status=400)


async def _execute_batch_actions(actions: list[dict], channel, channel_id: int) -> web.Response:
    results: list[dict] = []
    for i, action in enumerate(actions):
        action_type = action.get("type")
        if action_type == "send_message":
            kwargs, err = _build_discord_send_kwargs(action, channel_id)
            if err:
                return web.json_response({"error": f"Action {i}: {err}"}, status=400)
            sent_msg = await channel.send(**kwargs)
            _save_bot_message(sent_msg, channel_id)
            results.append({"action": i, "type": "send_message", "success": True})
        elif action_type == "add_reaction":
            msg_id = action.get("message_id")
            emoji = action.get("emoji")
            if not msg_id or not emoji:
                return web.json_response({"error": f"Action {i}: add_reaction requires message_id and emoji"}, status=400)
            try:
                msg = await channel.fetch_message(int(msg_id))
                await msg.add_reaction(emoji)
                results.append({"action": i, "type": "add_reaction", "success": True})
            except Exception as e:
                results.append({"action": i, "type": "add_reaction", "error": str(e)})
        else:
            return web.json_response({"error": f"Action {i}: unknown type '{action_type}'"}, status=400)

    new_messages = check_for_new_messages(channel_id)
    return web.json_response({"success": True, "results": results, "new_messages": new_messages})


async def handle_send_message(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    channel_id, err_resp = _parse_channel_id(body)
    if err_resp:
        return err_resp

    # Interrupt system: surface unseen messages before allowing a send.
    if not body.get("force", False):
        new_messages = check_for_new_messages(channel_id)
        if new_messages:
            return web.json_response({
                "error": "New messages received since your last check. Review them and retry.",
                "new_messages": new_messages,
                "guidance": "Respond to all users at once, then retry.",
            })

    if not _discord_bot:
        return web.json_response({"error": "Discord bot not ready"}, status=503)

    channel = _discord_bot.get_channel(channel_id)
    if not channel:
        return web.json_response({"error": f"Channel {channel_id} not found"}, status=404)

    actions = body.get("actions")
    if actions:
        return await _execute_batch_actions(actions, channel, channel_id)

    kwargs, err = _build_discord_send_kwargs(body, channel_id)
    if err:
        return web.json_response({"error": err}, status=400)

    sent_msg = await channel.send(**kwargs)
    _save_bot_message(sent_msg, channel_id)
    new_messages = check_for_new_messages(channel_id)
    return web.json_response({"success": True, "message": "Message sent", "new_messages": new_messages})


async def handle_check_messages(request: web.Request) -> web.Response:
    try:
        channel_id = int(request.match_info["channel_id"])
    except (KeyError, ValueError):
        return web.json_response({"error": "Invalid channel_id"}, status=400)

    limit = min(int(request.query.get("limit", "10")), MAX_MESSAGE_LIMIT)
    all_messages = request.query.get("all_messages", "").lower() == "true"
    count_param = request.query.get("count")
    count = min(int(count_param), MAX_MESSAGE_LIMIT) if count_param else None

    channel_name = get_channel_name(channel_id)
    messages: list[dict] = []

    try:
        if count is not None:
            since_id = None
            limit = count
        else:
            since_id = None if all_messages else state_manager.get_last_seen(channel_id)

        rows = state_manager.fetch_messages(channel_id, since_id=since_id, limit=limit)
        messages = [
            state_manager._row_to_message_dict(
                r, attachment_paths=find_attachments_for_message(r["message_id"], channel_name),
            )
            for r in rows
        ]
        messages.reverse()

        synthetic_ids = [m["message_id"] for m in messages if m["message_id"] >= SYNTHETIC_ID_THRESHOLD]
        real_messages = [m for m in messages if m["message_id"] < SYNTHETIC_ID_THRESHOLD]
        if real_messages:
            state_manager.update_last_seen(channel_id, max(m["message_id"] for m in real_messages))
        state_manager.delete_messages(synthetic_ids)
    except Exception as e:
        _LOG.error("Error reading messages: %s", e)

    return web.json_response({"messages": messages})


async def handle_emojis(request: web.Request) -> web.Response:
    emoji_cache = SHARED_DIR / "emojis.json"
    if not emoji_cache.exists():
        return web.json_response({"custom": []})
    try:
        emojis = json.loads(emoji_cache.read_text())
    except (json.JSONDecodeError, OSError):
        return web.json_response({"custom": []})
    search = request.query.get("search")
    if search:
        term = search.lower()
        emojis = [e for e in emojis if term in e.get("name", "").lower()]
    return web.json_response({"custom": emojis})


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


def create_app() -> web.Application:
    app = web.Application(client_max_size=30 * 1024 * 1024)
    app.router.add_post("/api/send_message", handle_send_message)
    app.router.add_get("/api/check_messages/{channel_id}", handle_check_messages)
    app.router.add_get("/api/emojis", handle_emojis)
    app.router.add_get("/health", handle_health)
    return app


async def start_server(port: int) -> web.AppRunner:
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    _LOG.info("API server listening on port %d", port)
    return runner
