#!/usr/bin/env python3
"""web検索・ダウンロード用の自作MCPサーバー。

検索はDuckDuckGo(APIキー不要)を使用。ダウンロードはホームディレクトリ配下にのみ許可し、
ファイルサイズに上限を設ける。
"""

import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from ddgs import DDGS
from mcp.server.mcpserver import MCPServer

HOME = Path.home().resolve()
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024  # 200MB
DOWNLOAD_TIMEOUT_SEC = 60
FETCH_TIMEOUT_SEC = 20
MAX_RESULT_CHARS = 300
MAX_PAGE_CHARS = 6000

# ddgsのデフォルト(backend="auto")は毎回DuckDuckGo/Wikipedia/Grokipedia/Brave/Google/
# Mojeek/Startpage/Yahooを並列で叩くメタサーチ仕様で遅い。かといって単体固定は
# レート制限に弱く"No results found"で即失敗しやすいことが実測で判明したため、
# 段階的にエンジン数を増やしながら再試行する(普段は軽量、詰まった時だけ手を広げる)。
SEARCH_BACKEND_STAGES = ["duckduckgo,startpage", "mojeek,brave", "auto"]
SEARCH_RETRY_WAIT_SEC = 2

mcp = MCPServer("web")


@mcp.tool()
def web_search(query: str, max_results: int = 5) -> str:
    """DuckDuckGoでweb検索する。タイトル・URL・概要を返す。"""
    max_results = max(1, min(max_results, 10))
    results = None
    last_error = None
    for i, backend in enumerate(SEARCH_BACKEND_STAGES):
        try:
            results = list(DDGS().text(query, max_results=max_results, backend=backend))
            if results:
                break
        except Exception as e:
            last_error = e
        if i < len(SEARCH_BACKEND_STAGES) - 1:
            time.sleep(SEARCH_RETRY_WAIT_SEC)

    if results is None:
        return f"検索エラー: {last_error}"
    if not results:
        return "検索結果はありませんでした。"

    lines = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "")
        href = r.get("href", "")
        body = (r.get("body", "") or "")[:MAX_RESULT_CHARS]
        lines.append(f"{i}. {title}\n   {href}\n   {body}")
    return "\n\n".join(lines)


@mcp.tool()
def fetch_page(url: str) -> str:
    """指定URLのページを取得し、HTMLタグを除いた本文テキストを返す(web_searchで見つけたページの中身を詳しく読みたい時に使う)。"""
    try:
        resp = requests.get(
            url,
            timeout=FETCH_TIMEOUT_SEC,
            headers={"User-Agent": "Mozilla/5.0 (local-ai-agent)"},
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        return f"取得エラー: {e}"

    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "noscript"]):
        tag.decompose()
    text = " ".join(soup.get_text(separator=" ").split())
    if not text:
        return "本文テキストを抽出できませんでした。"
    truncated = text[:MAX_PAGE_CHARS]
    if len(text) > MAX_PAGE_CHARS:
        truncated += f"\n\n...(以下省略、全{len(text):,}文字中先頭{MAX_PAGE_CHARS:,}文字)"
    return truncated


@mcp.tool()
def download_file(url: str, dest_path: str) -> str:
    """指定URLの内容をダウンロードし、ホームディレクトリ配下の指定パスに保存する。"""
    dest = Path(dest_path).expanduser()
    if not dest.is_absolute():
        dest = HOME / dest
    dest = dest.resolve()

    try:
        dest.relative_to(HOME)
    except ValueError:
        return f"エラー: 保存先はホームディレクトリ({HOME})配下である必要があります: {dest}"

    try:
        with requests.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT_SEC) as resp:
            resp.raise_for_status()
            content_length = resp.headers.get("Content-Length")
            if content_length and int(content_length) > MAX_DOWNLOAD_BYTES:
                return f"エラー: ファイルサイズが上限({MAX_DOWNLOAD_BYTES // (1024*1024)}MB)を超えています。"

            dest.parent.mkdir(parents=True, exist_ok=True)
            total = 0
            with open(dest, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 64):
                    total += len(chunk)
                    if total > MAX_DOWNLOAD_BYTES:
                        f.close()
                        dest.unlink(missing_ok=True)
                        return f"エラー: ファイルサイズが上限({MAX_DOWNLOAD_BYTES // (1024*1024)}MB)を超えたため中止しました。"
                    f.write(chunk)
    except requests.RequestException as e:
        return f"ダウンロードエラー: {e}"

    return f"ダウンロード完了: {dest} ({total:,} bytes)"


if __name__ == "__main__":
    mcp.run()
