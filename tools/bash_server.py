#!/usr/bin/env python3
"""bashコマンド実行用の自作MCPサーバー。

install/削除系コマンドを含め、通常のコマンドはAIによる自動呼び出しでもそのまま実行される。
唯一「ゴミ箱を完全に空にする」操作(復元不可能な完全削除)だけは、手動実行かどうかに関わらず
常にブロックされる。本当に必要な場合はFinderから手動で行うこと。
"""

import re
import subprocess
from pathlib import Path

from mcp.server.mcpserver import MCPServer

HOME = str(Path.home())
TIMEOUT_SEC = 30
MAX_OUTPUT_CHARS = 8000

# ゴミ箱を完全に空にする操作、およびホーム/ルート直下を丸ごと吹き飛ばすような
# 広範囲削除は、常にブロックする。
ALWAYS_BLOCKED_PATTERNS = [
    r"empty\s+trash",              # osascriptのFinder「ゴミ箱を空にする」等
    r"\.Trash\b[^\n]*\b(rm|delete|purge)\b",
    r"\b(rm|delete|purge)\b[^\n]*\.Trash\b",
    # rm -rf ~ / rm -rf ~/* / rm -rf $HOME / rm -rf / など、ホームディレクトリや
    # ルート直下そのものを対象にした広範囲削除(サブフォルダ指定のない削除)。
    r"\brm\s+(-\w*\s+)*(~|\$HOME|/)(\s|/\*|\*)?\s*($|&&|;|\|)",
    r"\brm\s+(-\w*\s+)*~/\*",
]

mcp = MCPServer("bash")


@mcp.tool()
def run_command(command: str, cwd: str = "", invocation_source: str = "auto") -> str:
    """シェルコマンドを実行する。

    install/uninstall/削除(rm, brew install, pip installなど)を含め、通常のコマンドは
    そのまま自動実行される。ただし「ゴミ箱を完全に空にする」操作(復元不可能な完全削除)は
    常にブロックされる。
    """
    if any(re.search(p, command, re.IGNORECASE) for p in ALWAYS_BLOCKED_PATTERNS):
        return (
            "ブロックされました: ゴミ箱を完全に空にする(復元不可能な)操作は実行できません。\n"
            f"コマンド: {command}\n"
            "本当に必要な場合は、Finderから手動で行ってください。"
        )

    work_dir = cwd or HOME
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        return f"エラー: {TIMEOUT_SEC}秒でタイムアウトしました。"
    except Exception as e:
        return f"エラー: {e}"

    parts = [f"$ {command}", f"(cwd: {work_dir})", ""]
    if result.stdout:
        parts.append(f"[stdout]\n{result.stdout[:MAX_OUTPUT_CHARS]}")
    if result.stderr:
        parts.append(f"[stderr]\n{result.stderr[:MAX_OUTPUT_CHARS]}")
    parts.append(f"[exit code] {result.returncode}")
    return "\n".join(parts)


if __name__ == "__main__":
    mcp.run()
