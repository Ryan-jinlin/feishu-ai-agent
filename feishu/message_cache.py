"""
本地消息缓存模块。

使用 SQLite 实时缓存所有进入 Bot 的消息（群聊 + P2P），
解决两个场景：
1. Bot 被移出群后仍能查询该群历史记录
2. P2P 联系人映射在 bot 重启后依然可用
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_DB = os.path.join(os.path.dirname(__file__), "..", ".message_cache.db")
_P2P_IDS_FILE = os.path.join(os.path.dirname(__file__), "..", ".p2p_chat_ids.json")
RETENTION_DAYS = 14


class MessageCache:
    """实时消息缓存，SQLite 后端，自动保留最近 RETENTION_DAYS 天数据。"""

    def __init__(self, db_path: str = _DEFAULT_DB) -> None:
        self.db_path = str(Path(db_path).resolve())
        self._init_db()

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------
    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    message_id  TEXT PRIMARY KEY,
                    chat_id     TEXT NOT NULL,
                    chat_name   TEXT DEFAULT '',
                    chat_type   TEXT DEFAULT 'group',
                    sender_id   TEXT DEFAULT '',
                    sender_name TEXT DEFAULT '',
                    text        TEXT DEFAULT '',
                    ts          INTEGER NOT NULL
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_chat_ts ON messages(chat_id, ts)"
            )
            conn.commit()

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def store(
        self,
        message_id: str,
        chat_id: str,
        chat_name: str,
        chat_type: str,
        sender_id: str,
        sender_name: str,
        text: str,
        ts: int,
    ) -> None:
        if not text.strip():
            return
        cutoff = int(time.time()) - RETENTION_DAYS * 86400
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """INSERT OR IGNORE INTO messages
                       (message_id, chat_id, chat_name, chat_type,
                        sender_id, sender_name, text, ts)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (message_id, chat_id, chat_name, chat_type,
                     sender_id, sender_name, text, ts),
                )
                # 顺手清理过期数据（低频，约每百条触发一次）
                conn.execute("DELETE FROM messages WHERE ts < ?", (cutoff,))
                conn.commit()
        except Exception as exc:
            logger.warning("MessageCache.store 失败: %s", exc)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def get_messages(
        self, chat_id: str, start_ts: int, end_ts: int, max_msgs: int = 300
    ) -> list[dict]:
        try:
            with sqlite3.connect(self.db_path) as conn:
                rows = conn.execute(
                    """SELECT sender_name, text, ts, message_id
                       FROM messages
                       WHERE chat_id = ? AND ts >= ? AND ts <= ?
                       ORDER BY ts ASC LIMIT ?""",
                    (chat_id, start_ts, end_ts, max_msgs),
                ).fetchall()
            return [
                {"sender_name": r[0], "text": r[1], "ts": r[2], "message_id": r[3]}
                for r in rows
            ]
        except Exception as exc:
            logger.warning("MessageCache.get_messages 失败: %s", exc)
            return []

    def get_known_chats(self) -> list[dict]:
        """返回所有曾出现过消息的群/P2P，按最近消息时间倒序。"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                rows = conn.execute(
                    """SELECT chat_id, chat_name, chat_type, MAX(ts) as last_ts
                       FROM messages
                       GROUP BY chat_id
                       ORDER BY last_ts DESC"""
                ).fetchall()
            return [
                {"chat_id": r[0], "name": r[1], "chat_type": r[2]}
                for r in rows
            ]
        except Exception as exc:
            logger.warning("MessageCache.get_known_chats 失败: %s", exc)
            return []

    # ------------------------------------------------------------------
    # P2P chat_id 持久化（附加在此模块便于统一管理）
    # ------------------------------------------------------------------
    @staticmethod
    def load_p2p_chat_ids() -> dict[str, str]:
        try:
            if os.path.exists(_P2P_IDS_FILE):
                with open(_P2P_IDS_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as exc:
            logger.warning("load_p2p_chat_ids 失败: %s", exc)
        return {}

    @staticmethod
    def save_p2p_chat_ids(mapping: dict[str, str]) -> None:
        try:
            with open(_P2P_IDS_FILE, "w", encoding="utf-8") as f:
                json.dump(mapping, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.warning("save_p2p_chat_ids 失败: %s", exc)
