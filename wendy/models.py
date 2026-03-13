"""Data structures. Leaf module -- zero internal imports."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class ChannelConfig:
    id: int
    name: str
    mode: str = "chat"
    model: str | None = None
    folder: str = ""

    # Thread-specific
    is_thread: bool = False
    parent_folder: str | None = None
    parent_channel_id: int | None = None
    thread_name: str | None = None

    def __post_init__(self):
        if not self.folder:
            self.folder = self.name


@dataclass(slots=True)
class SessionInfo:
    channel_id: int
    session_id: str
    folder: str
    created_at: int
    last_used_at: int | None
    message_count: int
    total_input_tokens: int
    total_output_tokens: int
    total_cache_read_tokens: int
    total_cache_create_tokens: int


@dataclass(slots=True)
class ConversationMessage:
    message_id: int
    author: str
    content: str
    timestamp: int | str
    attachments: list[str] = field(default_factory=list)
    reply_to_id: int | None = None
    reply_author: str | None = None
    reply_content: str | None = None
