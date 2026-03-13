"""Discord gateway -- message handling, CLI orchestration.

Each configured channel gets a persistent Claude CLI session.
Incoming messages trigger CLI invocations that respond via the internal HTTP API.
"""
from __future__ import annotations

import asyncio
import datetime
import io
import json
import logging
import subprocess
import time
from pathlib import Path

import discord
from discord.ext import commands

from . import api_server, sessions
from .cli import ClaudeCliError, run_cli
from .config import PROXY_PORT, parse_channel_configs
from .paths import attachments_dir, ensure_channel_dirs, session_dir
from .state import state as state_manager

_LOG = logging.getLogger(__name__)

_synthetic_counter = 0

_MAX_TIMEOUT_CONTINUATIONS = 2


def _folder_for_config(config: dict) -> str:
    return config.get("_folder") or config.get("name", "default")


class GenerationJob:
    def __init__(self) -> None:
        self.task: asyncio.Task | None = None
        self.new_message_pending: bool = False
        self.timed_out: bool = False
        self.continuation_count: int = 0


class Bot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True

        super().__init__(command_prefix="!", intents=intents)

        self.channel_configs: dict[int, dict] = parse_channel_configs()
        self.whitelist_channels: set[int] = set(self.channel_configs.keys())
        self._active_generations: dict[int, GenerationJob] = {}
        self._api_runner = None

        from .paths import ensure_shared_dirs
        ensure_shared_dirs()
        self._register_commands()

        _LOG.info("Bot initialized with %d channels", len(self.whitelist_channels))

    def _register_commands(self) -> None:
        @self.command(name="version")
        async def cmd_version(ctx: commands.Context) -> None:
            try:
                sha = subprocess.check_output(
                    ["git", "rev-parse", "--short", "HEAD"], cwd="/app", stderr=subprocess.DEVNULL,
                ).decode().strip()
                msg = subprocess.check_output(
                    ["git", "log", "-1", "--format=%s"], cwd="/app", stderr=subprocess.DEVNULL,
                ).decode().strip()
                await ctx.send(f"`{sha}` {msg}")
            except Exception:
                await ctx.send("version unknown")

        @self.command(name="system")
        async def cmd_system(ctx: commands.Context) -> None:
            channel_config = self.channel_configs.get(ctx.channel.id)
            if channel_config is None:
                await ctx.send("no config for this channel")
                return
            try:
                from .prompt import build_system_prompt
                prompt = build_system_prompt(ctx.channel.id, channel_config)
                buf = io.BytesIO(prompt.encode("utf-8"))
                await ctx.send(file=discord.File(buf, filename="system_prompt.txt"))
            except Exception as e:
                await ctx.send(f"error: {e}")

        @self.command(name="clear")
        async def cmd_clear(ctx: commands.Context) -> None:
            channel_config = self.channel_configs.get(ctx.channel.id)
            if channel_config is None:
                await ctx.send("not a configured channel")
                return
            folder = _folder_for_config(channel_config)
            old_id, new_id = sessions.reset_session(ctx.channel.id, folder)
            if old_id:
                await ctx.send(f"session cleared. old: `{old_id[:8]}` new: `{new_id[:8]}`")
            else:
                await ctx.send(f"new session started: `{new_id[:8]}`")

        @self.command(name="resume")
        async def cmd_resume(ctx: commands.Context, *, session_id_prefix: str = "") -> None:
            if not session_id_prefix:
                await ctx.send("usage: `!resume <session_id>`")
                return
            channel_config = self.channel_configs.get(ctx.channel.id)
            if channel_config is None:
                await ctx.send("not a configured channel")
                return
            folder = _folder_for_config(channel_config)
            row = state_manager.get_session_by_id(session_id_prefix)
            if not row:
                await ctx.send("session not found")
                return
            full_id = row["session_id"]
            sessions.resume_session(ctx.channel.id, full_id, folder)
            await ctx.send(f"resumed session `{full_id[:8]}`")

        @self.command(name="session")
        async def cmd_session(ctx: commands.Context) -> None:
            sess = sessions.get_session(ctx.channel.id)
            if not sess:
                await ctx.send("no active session")
                return
            started = datetime.datetime.fromtimestamp(sess.created_at, tz=datetime.UTC)
            started_str = started.strftime("%Y-%m-%d %H:%M UTC")
            total_tokens = sess.total_input_tokens + sess.total_output_tokens
            lines = [
                f"session: `{sess.session_id[:8]}`",
                f"started: {started_str}",
                f"turns: {sess.message_count}",
                f"tokens: {total_tokens:,}",
            ]
            await ctx.send("\n".join(lines))

    async def setup_hook(self) -> None:
        api_server.set_discord_bot(self)
        api_server.set_channel_configs(self.channel_configs)
        self._api_runner = await api_server.start_server(int(PROXY_PORT))
        self._cache_emojis_task = self.loop.create_task(self._cache_emojis())

    async def close(self) -> None:
        if self._api_runner:
            await self._api_runner.cleanup()
        await super().close()

    async def on_ready(self) -> None:
        _LOG.info("Logged in as %s (id=%d)", self.user.name, self.user.id)
        from . import config as _config
        _config.BOT_USER_ID = self.user.id

        from .cli import setup_channel_folder
        for cfg in self.channel_configs.values():
            folder = _folder_for_config(cfg)
            ensure_channel_dirs(folder)
            setup_channel_folder(folder)

    async def on_message(self, message: discord.Message) -> None:
        if message.author.id == self.user.id or not message.guild:
            return

        if not self._channel_allowed(message):
            return

        self._ensure_thread_config(message)
        channel_config = self.channel_configs.get(message.channel.id, {})
        channel_name = _folder_for_config(channel_config)

        if message.content.startswith(("!", "-", "/")):
            await self.process_commands(message)
            return

        if not message.content.strip() and not message.attachments:
            return

        self._cache_message(message)
        await self._save_attachments(message, channel_name)

        _LOG.info("Processing message from %s: %s...", message.author.display_name, message.content[:50])

        # Interrupt: bot name in all caps cancels the running generation.
        existing_job = self._active_generations.get(message.channel.id)
        from .config import BOT_NAME
        if message.content.strip().upper() == BOT_NAME.upper() and self._job_is_running(existing_job):
            self._interrupt_channel(message, existing_job, channel_config)
            return

        if self._job_is_running(existing_job):
            existing_job.new_message_pending = True
            _LOG.info("CLI already running in channel %s, marked pending", message.channel.id)
            return

        self._start_generation(message.channel, channel_config)

    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        if not payload.guild_id:
            return
        if payload.channel_id not in self.whitelist_channels:
            return
        if "content" not in payload.data:
            return
        try:
            state_manager.update_message_content(payload.message_id, payload.data["content"])
        except Exception as e:
            _LOG.error("Failed to update edited message %s: %s", payload.message_id, e)

    # -- Channel / thread helpers --

    def _channel_allowed(self, message: discord.Message) -> bool:
        if self.user in message.mentions:
            return True
        if message.channel.id in self.whitelist_channels:
            return True
        if isinstance(message.channel, discord.Thread):
            return message.channel.parent_id in self.whitelist_channels
        return False

    def _ensure_thread_config(self, message: discord.Message) -> None:
        if message.channel.id in self.channel_configs:
            return
        thread_config = self._resolve_thread_config(message)
        if thread_config:
            self.channel_configs[message.channel.id] = thread_config
            api_server.set_channel_configs(self.channel_configs)

    @staticmethod
    def _resolve_mentions(message: discord.Message) -> str:
        content = message.content
        for member in message.mentions:
            display = member.display_name
            replacement = f"@{display} (id:{member.id})"
            content = content.replace(f"<@{member.id}>", replacement)
            content = content.replace(f"<@!{member.id}>", replacement)
        return content

    def _cache_message(self, message: discord.Message) -> None:
        attachment_urls = json.dumps([a.url for a in message.attachments]) if message.attachments else None
        reply_to_id = message.reference.message_id if message.reference and message.reference.message_id else None
        state_manager.insert_message(
            message_id=message.id, channel_id=message.channel.id,
            guild_id=message.guild.id if message.guild else None,
            author_id=message.author.id, author_nickname=message.author.display_name,
            is_bot=message.author.bot, content=self._resolve_mentions(message),
            timestamp=int(message.created_at.timestamp()),
            attachment_urls=attachment_urls, reply_to_id=reply_to_id,
            is_webhook=bool(message.webhook_id),
        )

    async def _save_attachments(self, message: discord.Message, channel_name: str) -> list[str]:
        if not message.attachments:
            return []
        att_dir = attachments_dir(channel_name)
        att_dir.mkdir(parents=True, exist_ok=True)
        saved: list[str] = []
        for i, attachment in enumerate(message.attachments):
            try:
                filepath = att_dir / f"msg_{message.id}_{i}_{attachment.filename}"
                data = await attachment.read()
                filepath.write_bytes(data)
                saved.append(str(filepath))
            except Exception as e:
                _LOG.error("Failed to save attachment %s: %s", attachment.filename, e)
        return saved

    def _resolve_thread_config(self, message: discord.Message) -> dict | None:
        if not isinstance(message.channel, discord.Thread):
            return None
        parent_config = self.channel_configs.get(message.channel.parent_id)
        if not parent_config:
            return None
        parent_folder = _folder_for_config(parent_config)
        thread_id = message.channel.id
        folder_name = f"{parent_folder}_t_{thread_id}"
        thread_name = message.channel.name or "unknown-thread"
        config = {
            "id": str(thread_id), "name": thread_name,
            "mode": parent_config.get("mode", "full"), "model": parent_config.get("model"),
            "_folder": folder_name, "_is_thread": True,
            "_parent_folder": parent_folder, "_parent_channel_id": message.channel.parent_id,
            "_thread_name": thread_name,
        }
        state_manager.register_thread(thread_id, message.channel.parent_id, folder_name, thread_name)
        return config

    # -- Generation lifecycle --

    @staticmethod
    def _job_is_running(job: GenerationJob | None) -> bool:
        return job is not None and job.task is not None and not job.task.done()

    def _start_generation(self, channel, channel_config: dict) -> None:
        job = GenerationJob()
        task = self.loop.create_task(self._generate_response(channel, job, model_override=channel_config.get("model")))
        job.task = task
        self._active_generations[channel.id] = job

    def _interrupt_channel(self, message: discord.Message, existing_job: GenerationJob, channel_config: dict) -> None:
        channel_id = message.channel.id
        new_job = GenerationJob()
        self._active_generations[channel_id] = new_job
        existing_job.task.cancel()
        _LOG.info("Interrupted generation for channel %s", channel_id)

        self._insert_synthetic_message(
            channel_id, "System",
            f"[{message.author.display_name} interrupted you. Whatever you were doing may not be finished.]",
        )

        new_task = self.loop.create_task(
            self._generate_response(message.channel, new_job, model_override=channel_config.get("model"))
        )
        new_job.task = new_task

    async def _generate_response(self, channel, job: GenerationJob, model_override: str | None = None) -> None:
        channel_config = self.channel_configs.get(channel.id, {})
        try:
            from .prompt import build_system_prompt
            system_prompt = build_system_prompt(channel.id, channel_config)
            await run_cli(
                channel_id=channel.id, channel_config=channel_config,
                system_prompt=system_prompt, model_override=model_override,
            )
            _LOG.info("CLI completed for channel %s", channel.id)
        except ClaudeCliError as e:
            if "timed out" in str(e).lower():
                job.timed_out = True
            self._handle_cli_error(channel, e)
        except Exception:
            _LOG.exception("Generation failed")
        finally:
            self._finalize_generation(channel, job)

    def _handle_cli_error(self, channel, error: ClaudeCliError) -> None:
        error_str = str(error).lower()
        if "oauth" in error_str and "expired" in error_str:
            asyncio.ensure_future(channel.send(
                "my claude cli token expired - someone needs to run "
                "`docker exec -it wendy claude login` to fix me"
            ))
        else:
            _LOG.error("Claude CLI error: %s", error)

    def _finalize_generation(self, channel, job: GenerationJob) -> None:
        if self._active_generations.get(channel.id) is not job:
            return

        # Auto-continue on timeout
        if job.timed_out and job.continuation_count < _MAX_TIMEOUT_CONTINUATIONS:
            self._insert_synthetic_message(
                channel.id, "System",
                "[Your CLI session hit the time limit. Pick up where you left off -- check messages first.]",
            )
            channel_config = self.channel_configs.get(channel.id, {})
            new_job = GenerationJob()
            new_job.continuation_count = job.continuation_count + 1
            new_job.new_message_pending = job.new_message_pending
            new_task = self.loop.create_task(
                self._generate_response(channel, new_job, model_override=channel_config.get("model"))
            )
            new_job.task = new_task
            self._active_generations[channel.id] = new_job
            return

        if job.new_message_pending and state_manager.has_pending_messages(channel.id, self.user.id):
            new_job = GenerationJob()
            new_task = self.loop.create_task(self._generate_response(channel, new_job))
            new_job.task = new_task
            self._active_generations[channel.id] = new_job
        else:
            self._active_generations.pop(channel.id, None)

    def _insert_synthetic_message(self, channel_id: int, author: str, content: str) -> None:
        global _synthetic_counter
        _synthetic_counter += 1
        synthetic_id = 9_000_000_000_000_000_000 + int(time.time_ns() // 1000) + _synthetic_counter
        state_manager.insert_message(
            message_id=synthetic_id, channel_id=channel_id, guild_id=None,
            author_id=0, author_nickname=author, is_bot=False,
            content=content, timestamp=int(time.time()),
        )

    async def _cache_emojis(self) -> None:
        await self.wait_until_ready()
        try:
            all_emojis = [
                {"name": emoji.name, "id": str(emoji.id), "animated": emoji.animated,
                 "usage": f"<{'a' if emoji.animated else ''}:{emoji.name}:{emoji.id}>"}
                for guild in self.guilds for emoji in guild.emojis
            ]
            from .paths import SHARED_DIR
            (SHARED_DIR / "emojis.json").write_text(json.dumps(all_emojis))
            _LOG.info("Cached %d emojis", len(all_emojis))
        except Exception as e:
            _LOG.error("Failed to cache emojis: %s", e)
