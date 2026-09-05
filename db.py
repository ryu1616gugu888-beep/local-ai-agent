"""会話履歴の永続化(SQLite)。"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "conversations.db"


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = _connect()
    conn.execute(
        """CREATE TABLE IF NOT EXISTS conversations (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            model TEXT,
            titled_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            tool_name TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (conversation_id) REFERENCES conversations(id)
        )"""
    )
    for ddl in (
        "ALTER TABLE conversations ADD COLUMN model TEXT",
        "ALTER TABLE conversations ADD COLUMN titled_count INTEGER NOT NULL DEFAULT 0",
    ):
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass  # 既に列がある(新規DBではCREATE TABLEで作成済み)
    conn.commit()
    conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_conversation(conv_id: str, title: str, model: str):
    conn = _connect()
    conn.execute(
        "INSERT INTO conversations (id, title, model, created_at) VALUES (?, ?, ?, ?)",
        (conv_id, title, model, _now()),
    )
    conn.commit()
    conn.close()


def rename_conversation(conv_id: str, title: str, titled_count: int | None = None):
    conn = _connect()
    if titled_count is None:
        conn.execute("UPDATE conversations SET title = ? WHERE id = ?", (title, conv_id))
    else:
        conn.execute(
            "UPDATE conversations SET title = ?, titled_count = ? WHERE id = ?",
            (title, titled_count, conv_id),
        )
    conn.commit()
    conn.close()


def set_conversation_model(conv_id: str, model: str):
    conn = _connect()
    conn.execute("UPDATE conversations SET model = ? WHERE id = ?", (model, conv_id))
    conn.commit()
    conn.close()


def get_conversation(conv_id: str) -> dict | None:
    conn = _connect()
    row = conn.execute(
        "SELECT id, title, model, titled_count, created_at FROM conversations WHERE id = ?", (conv_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def list_conversations() -> list[dict]:
    conn = _connect()
    rows = conn.execute(
        "SELECT id, title, model, titled_count, created_at FROM conversations ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_message(conv_id: str, role: str, content: str, tool_name: str | None = None) -> int:
    conn = _connect()
    cur = conn.execute(
        "INSERT INTO messages (conversation_id, role, content, tool_name, created_at) VALUES (?, ?, ?, ?, ?)",
        (conv_id, role, content, tool_name, _now()),
    )
    conn.commit()
    msg_id = cur.lastrowid
    conn.close()
    return msg_id


def delete_message(message_id: int):
    conn = _connect()
    conn.execute("DELETE FROM messages WHERE id = ?", (message_id,))
    conn.commit()
    conn.close()


def get_messages(conv_id: str) -> list[dict]:
    conn = _connect()
    rows = conn.execute(
        "SELECT role, content, tool_name, created_at FROM messages WHERE conversation_id = ? ORDER BY id ASC",
        (conv_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_conversation(conv_id: str):
    conn = _connect()
    conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conv_id,))
    conn.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
    conn.commit()
    conn.close()
