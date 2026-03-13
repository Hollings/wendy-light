"""System prompt assembly.

Loads a single system prompt file and appends tool instructions.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from .cli import TOOL_INSTRUCTIONS_TEMPLATE
from .config import PROXY_PORT, BOT_NAME

_LOG = logging.getLogger(__name__)


def build_system_prompt(channel_id: int, channel_config: dict) -> str:
    channel_name = channel_config.get("_folder", channel_config.get("name", "default"))

    # Load base system prompt
    prompt = _get_base_system_prompt(channel_name)

    # Append tool instructions
    prompt += TOOL_INSTRUCTIONS_TEMPLATE.format(
        channel_id=channel_id, channel_name=channel_name, proxy_port=PROXY_PORT,
    )

    # Thread context
    is_thread = channel_config.get("_is_thread", False)
    thread_name = channel_config.get("_thread_name")
    thread_folder = channel_config.get("_folder") if is_thread else None
    parent_folder = channel_config.get("_parent_folder")

    if is_thread and thread_name and thread_folder and parent_folder:
        prompt += f"""
---
THREAD CONTEXT:
You are in a Discord thread called "{thread_name}" (not the main channel).
This thread has its own separate conversation history and session.
Messages you send here stay in this thread.
Your workspace: /data/wendy/channels/{thread_folder}/
Parent channel workspace: /data/wendy/channels/{parent_folder}/ (read-only reference)
---
"""

    return prompt


def _get_base_system_prompt(channel_name: str) -> str:
    system_prompt_file = os.getenv("SYSTEM_PROMPT_FILE", "/app/config/system_prompt.txt")
    if not Path(system_prompt_file).exists():
        return ""
    try:
        content = Path(system_prompt_file).read_text().strip()
        content = content.replace("{folder}", channel_name)
        content = content.replace("{bot_name}", BOT_NAME)
        return content
    except Exception as e:
        _LOG.warning("Failed to read system prompt file: %s", e)
        return ""
