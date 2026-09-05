#!/usr/bin/env python3
"""アプリ操作全般(画面クリック・キー入力)用の自作MCPサーバー。

macOSの「アクセシビリティ」「画面収録」権限が必要。初回利用時にmacOSが
許可ダイアログを出すか、System Settings > Privacy & Security から
このPythonプロセスに手動で許可を与える必要がある。

クリック座標だけからは危険な操作かどうかを機械的に判定できないため、
computer_click/computer_type/computer_key は「手動実行パネル」からの
実行(invocation_source=manual)でのみ動作する。AIによる自動呼び出しは常に
ブロックされる。screenshotとopen_appは情報取得・起動のみなので自動実行を許可する。
"""

import subprocess
from pathlib import Path

import pyautogui
from mcp.server.mcpserver import MCPServer

SCREENSHOT_DIR = Path(__file__).parent.parent / "data" / "computer-screenshots"

mcp = MCPServer("computer")


@mcp.tool()
def computer_screenshot() -> str:
    """画面全体のスクリーンショットを撮影し、ファイルに保存する。"""
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    dest = SCREENSHOT_DIR / "latest.png"
    try:
        img = pyautogui.screenshot()
        img.save(dest)
    except Exception as e:
        return f"エラー: {e}(macOSの「画面収録」権限が必要な場合があります)"
    return f"保存しました: {dest} (サイズ: {img.size[0]}x{img.size[1]})"


def _require_manual(invocation_source: str) -> str | None:
    if invocation_source != "manual":
        return (
            "ブロックされました: この操作は画面上の実際の効果を機械的に判定できないため、"
            "ツールパネルの「手動実行」からユーザー自身が実行した場合にのみ実行できます。"
        )
    return None


@mcp.tool()
def computer_click(x: int, y: int, invocation_source: str = "auto") -> str:
    """画面上の指定座標(x, y)をクリックする。事前にcomputer_screenshotで座標を確認すること。
    AIによる自動呼び出しでは常にブロックされ、ユーザーの手動実行でのみ動作する。"""
    blocked = _require_manual(invocation_source)
    if blocked:
        return blocked
    try:
        pyautogui.click(x, y)
    except Exception as e:
        return f"エラー: {e}(macOSの「アクセシビリティ」権限が必要な場合があります)"
    return f"クリックしました: ({x}, {y})"


@mcp.tool()
def computer_type(text: str, invocation_source: str = "auto") -> str:
    """現在フォーカスされている場所にテキストを入力する。
    AIによる自動呼び出しでは常にブロックされ、ユーザーの手動実行でのみ動作する。"""
    blocked = _require_manual(invocation_source)
    if blocked:
        return blocked
    try:
        pyautogui.write(text, interval=0.02)
    except Exception as e:
        return f"エラー: {e}(macOSの「アクセシビリティ」権限が必要な場合があります)"
    return f"入力しました: {text!r}"


@mcp.tool()
def computer_key(key: str, invocation_source: str = "auto") -> str:
    """特定のキーを押す(例: 'enter', 'escape', 'cmd+c', 'cmd+tab')。
    AIによる自動呼び出しでは常にブロックされ、ユーザーの手動実行でのみ動作する。"""
    blocked = _require_manual(invocation_source)
    if blocked:
        return blocked
    try:
        if "+" in key:
            pyautogui.hotkey(*key.split("+"))
        else:
            pyautogui.press(key)
    except Exception as e:
        return f"エラー: {e}"
    return f"キーを押しました: {key}"


@mcp.tool()
def computer_open_app(name: str) -> str:
    """指定した名前のアプリを起動・前面表示する。"""
    result = subprocess.run(["open", "-a", name], capture_output=True, text=True)
    if result.returncode != 0:
        return f"エラー: {result.stderr.strip()}"
    return f"起動しました: {name}"


if __name__ == "__main__":
    mcp.run()
