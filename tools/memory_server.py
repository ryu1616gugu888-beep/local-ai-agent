#!/usr/bin/env python3
"""会話をまたいだ記憶用の自作MCPサーバー。

data/memories.json に追記していく、シンプルな永続メモリ。
webapp.py側がこのファイルを読み、システムプロンプトに含めることで
「思い出す」を実現する(モデルの自動ツール呼び出しに頼らない設計)。
このサーバー自体は「覚える」書き込み操作だけを提供する。
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.mcpserver import MCPServer

MEMORY_PATH = Path(__file__).parent.parent / "data" / "memories.json"
MAX_MEMORIES = 200

mcp = MCPServer("memory")


def _load() -> list[dict]:
    if not MEMORY_PATH.exists():
        return []
    try:
        return json.loads(MEMORY_PATH.read_text()).get("memories", [])
    except Exception:
        return []


def _save(memories: list[dict]):
    MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    MEMORY_PATH.write_text(json.dumps({"memories": memories}, ensure_ascii=False, indent=2))


@mcp.tool()
def remember(content: str) -> str:
    """ユーザーについての重要な情報(好み・進行中の作業・繰り返し使う設定など)を、
    今後の会話でも思い出せるように記録する。"""
    memories = _load()
    memories.append({"content": content, "created_at": datetime.now(timezone.utc).isoformat()})
    memories = memories[-MAX_MEMORIES:]
    _save(memories)
    return f"記録しました: {content}"


@mcp.tool()
def list_memories() -> str:
    """これまでに記録された記憶の一覧を確認する。"""
    memories = _load()
    if not memories:
        return "記録されている記憶はありません。"
    return "\n".join(f"- {m['content']}" for m in memories)


@mcp.tool()
def forget(content_contains: str) -> str:
    """指定した文字列を含む記憶を削除する。"""
    memories = _load()
    kept = [m for m in memories if content_contains not in m["content"]]
    removed = len(memories) - len(kept)
    _save(kept)
    return f"{removed}件の記憶を削除しました。"


if __name__ == "__main__":
    mcp.run()
