"""Unified SQLite state manager."""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from pathlib import Path

from .models import SessionInfo
from .paths import DB_PATH as DEFAULT_DB_PATH

_LOG = logging.getLogger(__name__)

_env_db_path = os.getenv("WENDY_DB_PATH")
_DEFAULT_DB_PATH = Path(_env_db_path) if _env_db_path else DEFAULT_DB_PATH


class StateManager:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or _DEFAULT_DB_PATH
        self._local = threading.local()
        self._lock = threading.Lock()
        self._initialized = False

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._local.conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30.0)
            self._local.conn.row_factory = sqlite3.Row
            self._local.conn.execute("PRAGMA journal_mode=WAL")

        if not self._initialized:
            with self._lock:
                if not self._initialized:
                    self._init_schema(self._local.conn)
                    self._initialized = True
        return self._local.conn

    def _init_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS channel_sessions (
                channel_id INTEGER PRIMARY KEY,
                session_id TEXT NOT NULL,
                folder TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                last_used_at INTEGER,
                message_count INTEGER DEFAULT 0,
                total_input_tokens INTEGER DEFAULT 0,
                total_output_tokens INTEGER DEFAULT 0,
                total_cache_read_tokens INTEGER DEFAULT 0,
                total_cache_create_tokens INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS channel_last_seen (
                channel_id INTEGER PRIMARY KEY,
                last_message_id INTEGER NOT NULL,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS message_history (
                message_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                guild_id INTEGER,
                author_id INTEGER,
                author_nickname TEXT,
                is_bot INTEGER DEFAULT 0,
                is_webhook INTEGER DEFAULT 0,
                content TEXT,
                timestamp INTEGER,
                attachment_urls TEXT,
                reply_to_id INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_message_history_channel
                ON message_history(channel_id, message_id);

            CREATE TABLE IF NOT EXISTS thread_registry (
                thread_id INTEGER PRIMARY KEY,
                parent_channel_id INTEGER NOT NULL,
                folder_name TEXT NOT NULL,
                thread_name TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS session_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER NOT NULL,
                session_id TEXT NOT NULL,
                folder TEXT NOT NULL,
                started_at INTEGER NOT NULL,
                ended_at INTEGER,
                message_count INTEGER DEFAULT 0,
                total_input_tokens INTEGER DEFAULT 0,
                total_output_tokens INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_session_history_channel
                ON session_history(channel_id, started_at);
        """)
        conn.commit()
        _LOG.info("Schema initialized at %s", self.db_path)

    # -- Session Management --

    def get_session(self, channel_id: int) -> SessionInfo | None:
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM channel_sessions WHERE channel_id = ?", (channel_id,)).fetchone()
        if not row:
            return None
        return SessionInfo(
            channel_id=row["channel_id"], session_id=row["session_id"], folder=row["folder"],
            created_at=row["created_at"], last_used_at=row["last_used_at"],
            message_count=row["message_count"],
            total_input_tokens=row["total_input_tokens"], total_output_tokens=row["total_output_tokens"],
            total_cache_read_tokens=row["total_cache_read_tokens"],
            total_cache_create_tokens=row["total_cache_create_tokens"],
        )

    def create_session(self, channel_id: int, session_id: str, folder: str) -> None:
        conn = self._get_conn()
        now = int(time.time())
        existing = conn.execute("SELECT * FROM channel_sessions WHERE channel_id = ?", (channel_id,)).fetchone()
        if existing:
            conn.execute(
                """INSERT OR IGNORE INTO session_history
                    (channel_id, session_id, folder, started_at, ended_at, message_count, total_input_tokens, total_output_tokens)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (existing["channel_id"], existing["session_id"], existing["folder"],
                 existing["created_at"], now, existing["message_count"],
                 existing["total_input_tokens"], existing["total_output_tokens"]),
            )
        conn.execute(
            """INSERT OR REPLACE INTO channel_sessions
                (channel_id, session_id, folder, created_at, message_count,
                 total_input_tokens, total_output_tokens, total_cache_read_tokens, total_cache_create_tokens)
            VALUES (?, ?, ?, ?, 0, 0, 0, 0, 0)""",
            (channel_id, session_id, folder, now),
        )
        conn.commit()
        _LOG.info("Created session %s for channel %d (folder=%s)", session_id[:8], channel_id, folder)

    def update_session_stats(self, channel_id: int, input_tokens: int = 0, output_tokens: int = 0,
                             cache_read_tokens: int = 0, cache_create_tokens: int = 0) -> None:
        conn = self._get_conn()
        conn.execute(
            """UPDATE channel_sessions SET message_count = message_count + 1,
                total_input_tokens = total_input_tokens + ?, total_output_tokens = total_output_tokens + ?,
                total_cache_read_tokens = total_cache_read_tokens + ?,
                total_cache_create_tokens = total_cache_create_tokens + ?, last_used_at = ?
            WHERE channel_id = ?""",
            (input_tokens, output_tokens, cache_read_tokens, cache_create_tokens, int(time.time()), channel_id),
        )
        conn.commit()

    def get_session_stats(self, channel_id: int) -> dict | None:
        session = self.get_session(channel_id)
        if not session:
            return None
        return {
            "session_id": session.session_id, "folder": session.folder,
            "created_at": session.created_at, "last_used_at": session.last_used_at,
            "message_count": session.message_count,
            "total_input_tokens": session.total_input_tokens,
            "total_output_tokens": session.total_output_tokens,
        }

    # -- Last Seen --

    def get_last_seen(self, channel_id: int) -> int | None:
        row = self._get_conn().execute(
            "SELECT last_message_id FROM channel_last_seen WHERE channel_id = ?", (channel_id,)
        ).fetchone()
        return row["last_message_id"] if row else None

    def update_last_seen(self, channel_id: int, message_id: int) -> None:
        conn = self._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO channel_last_seen (channel_id, last_message_id, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
            (channel_id, message_id),
        )
        conn.commit()

    # -- Message History --

    def insert_message(self, message_id: int, channel_id: int, guild_id: int | None,
                       author_id: int | None, author_nickname: str | None, is_bot: bool,
                       content: str | None, timestamp: int | None, attachment_urls: str | None = None,
                       reply_to_id: int | None = None, is_webhook: bool = False) -> None:
        conn = self._get_conn()
        conn.execute(
            """INSERT OR IGNORE INTO message_history
                (message_id, channel_id, guild_id, author_id, author_nickname,
                 is_bot, is_webhook, content, timestamp, attachment_urls, reply_to_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (message_id, channel_id, guild_id, author_id, author_nickname,
             int(is_bot), int(is_webhook), content, timestamp, attachment_urls, reply_to_id),
        )
        conn.commit()

    def update_message_content(self, message_id: int, content: str) -> None:
        conn = self._get_conn()
        conn.execute("UPDATE message_history SET content = ? WHERE message_id = ?", (content, message_id))
        conn.commit()

    def delete_messages(self, message_ids: list[int]) -> None:
        if not message_ids:
            return
        conn = self._get_conn()
        placeholders = ",".join("?" * len(message_ids))
        conn.execute(f"DELETE FROM message_history WHERE message_id IN ({placeholders})", message_ids)
        conn.commit()

    _MESSAGE_QUERY_COLUMNS = """
        m.message_id, m.author_id, m.is_bot, m.author_nickname, m.content, m.timestamp,
        m.reply_to_id, r.author_nickname as reply_author, r.content as reply_content
    """

    _MESSAGE_QUERY_BASE = f"""
        SELECT {_MESSAGE_QUERY_COLUMNS}
        FROM message_history m
        LEFT JOIN message_history r ON m.reply_to_id = r.message_id
        WHERE m.channel_id = ?
        AND (m.content IS NULL OR (m.content NOT LIKE '!%' AND m.content NOT LIKE '-%'))
    """

    @staticmethod
    def _row_to_message_dict(row: sqlite3.Row, *, attachment_paths: list[str] | None = None) -> dict:
        msg: dict = {
            "message_id": row["message_id"], "author": row["author_nickname"],
            "is_bot": bool(row["is_bot"]), "content": row["content"], "timestamp": row["timestamp"],
        }
        if attachment_paths:
            msg["attachments"] = attachment_paths
        if row["reply_to_id"] and row["reply_author"]:
            msg["reply_to"] = {"message_id": row["reply_to_id"], "author": row["reply_author"],
                               "content": row["reply_content"] or ""}
        return msg

    def fetch_messages(self, channel_id: int, *, since_id: int | None = None, limit: int = 10,
                       synthetic_threshold: int = 9_000_000_000_000_000_000) -> list[sqlite3.Row]:
        conn = self._get_conn()
        if since_id is not None:
            real_query = self._MESSAGE_QUERY_BASE + " AND m.message_id > ? AND m.message_id < ? ORDER BY m.message_id DESC LIMIT ?"
            real_params = (channel_id, since_id, synthetic_threshold, limit)
        else:
            real_query = self._MESSAGE_QUERY_BASE + " AND m.message_id < ? ORDER BY m.message_id DESC LIMIT ?"
            real_params = (channel_id, synthetic_threshold, limit)
        real_rows = conn.execute(real_query, real_params).fetchall()

        synth_query = self._MESSAGE_QUERY_BASE + " AND m.message_id >= ? ORDER BY m.message_id ASC"
        synth_rows = conn.execute(synth_query, (channel_id, synthetic_threshold)).fetchall()
        return list(synth_rows)[::-1] + list(real_rows)

    def check_for_new_messages(self, channel_id: int, bot_user_id: int,
                               synthetic_id_threshold: int, max_limit: int) -> list[dict]:
        last_seen = self.get_last_seen(channel_id)
        if last_seen is None:
            return []
        query = (self._MESSAGE_QUERY_BASE + " AND m.message_id > ? AND m.author_id != ? ORDER BY m.message_id DESC LIMIT ?")
        rows = self._get_conn().execute(query, (channel_id, last_seen, bot_user_id, max_limit)).fetchall()
        if not rows:
            return []
        return [self._row_to_message_dict(r) for r in reversed(rows)]

    def has_pending_messages(self, channel_id: int, bot_user_id: int) -> bool:
        try:
            conn = self._get_conn()
            last_seen = self.get_last_seen(channel_id)
            if last_seen:
                query = """SELECT EXISTS(SELECT 1 FROM message_history
                    WHERE channel_id = ? AND message_id > ? AND author_id != ?
                    AND (content IS NULL OR (content NOT LIKE '!%' AND content NOT LIKE '-%')) LIMIT 1)"""
                params: tuple = (channel_id, last_seen, bot_user_id)
            else:
                query = """SELECT EXISTS(SELECT 1 FROM message_history
                    WHERE channel_id = ? AND author_id != ?
                    AND (content IS NULL OR (content NOT LIKE '!%' AND content NOT LIKE '-%')) LIMIT 1)"""
                params = (channel_id, bot_user_id)
            return bool(conn.execute(query, params).fetchone()[0])
        except Exception as e:
            _LOG.error("Error checking pending messages: %s", e)
            return True

    # -- Thread Registry --

    def register_thread(self, thread_id: int, parent_channel_id: int, folder_name: str, thread_name: str | None = None) -> None:
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO thread_registry (thread_id, parent_channel_id, folder_name, thread_name)
            VALUES (?, ?, ?, ?) ON CONFLICT(thread_id) DO UPDATE SET thread_name = excluded.thread_name""",
            (thread_id, parent_channel_id, folder_name, thread_name),
        )
        conn.commit()

    def get_thread_folder(self, thread_id: int) -> str | None:
        row = self._get_conn().execute(
            "SELECT folder_name FROM thread_registry WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        return row["folder_name"] if row else None

    def get_session_by_id(self, session_id_prefix: str) -> dict | None:
        conn = self._get_conn()
        for query, params in [
            ("SELECT * FROM session_history WHERE session_id = ?", (session_id_prefix,)),
            ("SELECT * FROM session_history WHERE session_id LIKE ?", (session_id_prefix + "%",)),
            ("SELECT channel_id, session_id, folder, created_at AS started_at FROM channel_sessions WHERE session_id LIKE ?",
             (session_id_prefix + "%",)),
        ]:
            row = conn.execute(query, params).fetchone()
            if row:
                return dict(row)
        return None


state = StateManager()
