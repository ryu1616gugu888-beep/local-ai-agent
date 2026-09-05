#!/usr/bin/env python3
"""ブラウザ自動操作用の自作MCPサーバー(Playwright)。

このサーバーが起動する専用のChromiumインスタンスのみを操作する。
ユーザーの普段使いのSafari/Chromeには一切影響しない。
"""

from pathlib import Path

from mcp.server.mcpserver import MCPServer
from playwright.async_api import Browser, Page, async_playwright

SCREENSHOT_DIR = Path(__file__).parent.parent / "data" / "browser-screenshots"
MAX_TEXT_CHARS = 6000
MAX_LINKS = 40

mcp = MCPServer("browser")

_playwright = None
_browser: Browser | None = None
_page: Page | None = None


async def _ensure_page() -> Page:
    global _playwright, _browser, _page
    if _page is not None and not _page.is_closed():
        return _page
    if _playwright is None:
        _playwright = await async_playwright().start()
    if _browser is None:
        _browser = await _playwright.chromium.launch(headless=True)
    _page = await _browser.new_page()
    return _page


@mcp.tool()
async def browser_open(url: str) -> str:
    """専用ブラウザで指定URLを開く。以後のbrowser_*ツールはこのページに対して操作する。"""
    page = await _ensure_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
    except Exception as e:
        return f"ページを開けませんでした: {e}"
    return f"開きました: {page.url} (タイトル: {await page.title()})"


@mcp.tool()
async def browser_get_text() -> str:
    """現在開いているページの本文テキストを取得する。"""
    page = await _ensure_page()
    text = await page.inner_text("body")
    text = " ".join(text.split())
    if len(text) > MAX_TEXT_CHARS:
        return text[:MAX_TEXT_CHARS] + f"\n\n...(全{len(text):,}文字中先頭{MAX_TEXT_CHARS:,}文字)"
    return text or "(本文が空です)"


@mcp.tool()
async def browser_list_links() -> str:
    """現在開いているページ内のリンク(表示テキストとURL)の一覧を取得する。
    「特定の項目(動画・記事など)を開きたい」時は、browser_clickでセレクタを推測するより先に
    まずこれでリンク一覧を取得し、該当するURLへbrowser_openで直接移動する方が確実。"""
    page = await _ensure_page()
    links = await page.eval_on_selector_all(
        "a[href]",
        "els => els.map(e => ({text: e.innerText.trim(), href: e.href}))"
        ".filter(l => l.text && l.href.startsWith('http'))",
    )
    seen = set()
    lines = []
    for link in links:
        key = (link["text"], link["href"])
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"- {link['text'][:80]}\n  {link['href']}")
        if len(lines) >= MAX_LINKS:
            break
    if not lines:
        return "リンクが見つかりませんでした。"
    return "\n".join(lines)


@mcp.tool()
async def browser_click(selector: str) -> str:
    """CSSセレクタ、またはテキストで指定した要素をクリックする。
    テキストで指定する場合は text=ボタン名 の形式を使う。
    特定のリンク先に移動したいだけなら、browser_list_linksでURLを取得して
    browser_openで直接移動する方が確実。"""
    page = await _ensure_page()
    try:
        await page.click(selector, timeout=10000)
    except Exception as e:
        return f"クリックできませんでした: {e}"
    return f"クリックしました: {selector}"


@mcp.tool()
async def browser_type(selector: str, text: str) -> str:
    """指定した入力欄にテキストを入力する。"""
    page = await _ensure_page()
    try:
        await page.fill(selector, text, timeout=10000)
    except Exception as e:
        return f"入力できませんでした: {e}"
    return f"入力しました: {selector} <- {text!r}"

@mcp.tool()
async def browser_screenshot() -> str:
    """現在のページのスクリーンショットを撮影し、ファイルに保存する。"""
    page = await _ensure_page()
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    dest = SCREENSHOT_DIR / "latest.png"
    await page.screenshot(path=str(dest))
    return f"保存しました: {dest}"


if __name__ == "__main__":
    mcp.run()
