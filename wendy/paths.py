"""Centralized path definitions. Leaf module -- zero internal imports."""
from __future__ import annotations

import os
import re
from pathlib import Path

WENDY_BASE: Path = Path(os.getenv("WENDY_BASE_DIR", "/data/wendy"))
CHANNELS_DIR: Path = WENDY_BASE / "channels"
SHARED_DIR: Path = WENDY_BASE / "shared"
DB_PATH: Path = SHARED_DIR / "wendy.db"
STREAM_LOG_FILE: Path = WENDY_BASE / "stream.jsonl"

CLAUDE_PROJECTS_DIR: Path = Path(os.getenv("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))) / "projects"

CHANNEL_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")


def _encode_path_for_claude(path: Path) -> str:
    return str(path).replace("/", "-")


def validate_channel_name(name: str) -> bool:
    if not name:
        return False
    return bool(CHANNEL_NAME_PATTERN.match(name))


def channel_dir(name: str) -> Path:
    return CHANNELS_DIR / name


def session_dir(channel_name: str) -> Path:
    channel_path = channel_dir(channel_name)
    encoded = _encode_path_for_claude(channel_path)
    return CLAUDE_PROJECTS_DIR / encoded


def current_session_file(channel_name: str) -> Path:
    return channel_dir(channel_name) / ".current_session"


def attachments_dir(channel_name: str) -> Path:
    return channel_dir(channel_name) / "attachments"


def ensure_channel_dirs(channel_name: str) -> None:
    dirs = [channel_dir(channel_name), attachments_dir(channel_name)]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
    if os.name == "posix" and os.getuid() == 0:
        for d in dirs:
            try:
                os.chown(d, 1000, 1000)
            except OSError:
                pass


def find_attachments_for_message(message_id: int, channel_name: str | None = None) -> list[str]:
    if not channel_name:
        return []
    att_dir = attachments_dir(channel_name)
    if not att_dir.exists():
        return []
    return sorted(str(f) for f in att_dir.glob(f"msg_{message_id}_*"))


def ensure_shared_dirs() -> None:
    SHARED_DIR.mkdir(parents=True, exist_ok=True)
