"""Configuration parsing and constants. Leaf module -- zero internal imports."""
from __future__ import annotations

import json
import logging
import os
import re

_LOG = logging.getLogger(__name__)

# CLI subprocess user (non-root isolation)
CLI_SUBPROCESS_UID: int | None = None
if os.name == "posix" and os.getuid() == 0:
    CLI_SUBPROCESS_UID = 1000

MODEL_MAP: dict[str, str] = {
    "opus": "claude-opus-4-6",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5-20251001",
}

MAX_STREAM_LOG_LINES: int = 5000
PROXY_PORT: str = os.getenv("WENDY_PROXY_PORT", "8945")
CLAUDE_CLI_IDLE_TIMEOUT: int = int(os.getenv("CLAUDE_CLI_IDLE_TIMEOUT", "300"))
CLAUDE_CLI_MAX_RUNTIME: int = int(os.getenv("CLAUDE_CLI_MAX_RUNTIME", "1800"))
DISCORD_MAX_MESSAGE_LENGTH: int = 2000
BOT_USER_ID: int = int(os.getenv("BOT_USER_ID", "0"))
BOT_NAME: str = os.getenv("BOT_NAME", "Bot")
SYNTHETIC_ID_THRESHOLD: int = 9_000_000_000_000_000_000
MAX_MESSAGE_LIMIT: int = 200

SENSITIVE_ENV_VARS: set[str] = {
    "DISCORD_TOKEN",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "GITHUB_PAT",
}

CHANNEL_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")


def _validate_name(name: str) -> bool:
    return bool(name and CHANNEL_NAME_PATTERN.match(name))


def parse_channel_configs() -> dict[int, dict]:
    """Parse CHANNEL_CONFIG env var into a dict of channel_id -> config."""
    configs: dict[int, dict] = {}
    config_json = os.getenv("CHANNEL_CONFIG", "")
    if not config_json:
        return configs

    try:
        raw_configs = json.loads(config_json)
    except (json.JSONDecodeError, ValueError) as e:
        _LOG.error("Failed to parse CHANNEL_CONFIG: %s", e)
        return configs

    for cfg in raw_configs:
        if "id" not in cfg or "name" not in cfg:
            _LOG.error("Channel config missing required fields: %s", cfg)
            continue

        name = cfg["name"]
        if not _validate_name(name):
            _LOG.error("Invalid channel name '%s'", name)
            continue

        folder = cfg.get("folder", name)
        if not _validate_name(folder):
            folder = name

        try:
            channel_id = int(cfg["id"])
        except (ValueError, TypeError):
            _LOG.error("Invalid channel ID '%s' in config", cfg["id"])
            continue
        configs[channel_id] = {
            "id": str(cfg["id"]),
            "name": name,
            "mode": cfg.get("mode", "full"),
            "model": cfg.get("model"),
            "_folder": folder,
        }

    _LOG.info("Loaded %d channel configs", len(configs))
    return configs


def resolve_model(model_shorthand: str | None) -> str:
    if not model_shorthand:
        return MODEL_MAP["sonnet"]
    return MODEL_MAP.get(model_shorthand, model_shorthand)
