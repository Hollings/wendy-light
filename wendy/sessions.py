"""Session lifecycle: create, resume, recover."""
from __future__ import annotations

import logging
import uuid

from .state import state as state_manager

_LOG = logging.getLogger(__name__)


def create_session(channel_id: int, folder: str, session_id: str | None = None) -> str:
    sid = session_id or str(uuid.uuid4())
    state_manager.create_session(channel_id, sid, folder)
    return sid


def get_session(channel_id: int):
    return state_manager.get_session(channel_id)


def reset_session(channel_id: int, folder: str) -> tuple[str | None, str]:
    old = get_session(channel_id)
    old_id = old.session_id if old else None
    new_id = create_session(channel_id, folder)
    return old_id, new_id


def resume_session(channel_id: int, session_id: str, folder: str) -> None:
    state_manager.create_session(channel_id, session_id, folder)


def update_stats(channel_id: int, usage: dict) -> None:
    if not state_manager.get_session(channel_id):
        return
    state_manager.update_session_stats(
        channel_id,
        input_tokens=usage.get("input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
        cache_read_tokens=usage.get("cache_read_input_tokens", 0),
        cache_create_tokens=usage.get("cache_creation_input_tokens", 0),
    )
