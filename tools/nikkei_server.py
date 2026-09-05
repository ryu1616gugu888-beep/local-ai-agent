#!/usr/bin/env python3
"""日経テレコム(大学図書館プロキシ経由)へのアクセス用の自作MCPサーバー(Playwright)。

ユーザー自身のID・パスワードでログインする。認証情報は config/nikkei_credentials.json
から読む(パスワードはユーザー本人がファイルに直接書き込む — このサーバーのコードは
パスワードを一切ハードコードしない)。

重要: 永続プロファイル(Instagramツールと同じ launch_persistent_context 方式)は
あえて使わない。実際に検証した結果、通常のブラウザプロファイルに溜まった古い
セッションCookieが残っていると、日経側に「同時ログイン数超過(OGH_0005)」と
誤認され続けることが分かった(プライベートブラウジングウィンドウ=Cookie無しの
状態でだけ正常にログインできることで確認済み)。そのため毎回まっさらな
非永続コンテキスト(プライベートブラウジング相当)を新規作成し、ログインもその都度
やり直す設計にしている。1日数回しか呼ばない用途なので、毎回ログインし直す
オーバーヘッドは許容する。

このサーバーは自動要約レポート生成パイプライン専用として想定しており、通常のチャット
からの自由な呼び出しは意図していない(ログイン処理を含むため)。
"""

import json
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from playwright.async_api import async_playwright

CONFIG_PATH = Path(__file__).parent.parent / "config" / "nikkei_credentials.json"
NIKKEI_HOME_URL = "https://t21-nikkei-co-jp.hit-u.idm.oclc.org/g3/CMNDF11.do"
MAX_TEXT_CHARS = 8000

mcp = MCPServer("nikkei")


def _load_credentials() -> dict:
    if not CONFIG_PATH.exists():
        raise RuntimeError(f"{CONFIG_PATH} が見つかりません。")
    data = json.loads(CONFIG_PATH.read_text())
    if not data.get("password") or data["password"] == "ここにパスワードを入力":
        raise RuntimeError(
            f"{CONFIG_PATH} のpasswordが未設定です。プレースホルダーのままなので、"
            "実際のパスワードに書き換えてください。"
        )
    return data


async def _read_all_text(page) -> str:
    """本文がフレーム内にあるページ(日経テレコンの記事画面など)にも対応するため、
    メインフレームと全ての子フレームのテキストを連結して返す。"""
    parts = []
    for frame in page.frames:
        try:
            t = await frame.inner_text("body")
            if t.strip():
                parts.append(t)
        except Exception:
            continue
    return " ".join(" ".join(parts).split())


def _looks_like_login_form(text: str) -> bool:
    return "UserID" in text and "Password" in text


def _looks_like_session_limit_error(text: str) -> bool:
    return "OGH_0005" in text or "同時ログイン数" in text


async def _perform_logout(page) -> bool:
    """ホーム画面右上の「ログアウト」ボタンをクリックしてセッションを正しく終了する。

    ユーザーが実機のスクリーンショットで確認したところ、独立したボタン要素として
    「ログアウト」というテキストが表示される(通常フレーム内)。ここを経由せず
    ブラウザ/コンテキストを閉じるだけだと、日経側にセッションが残留し続け、
    次回ログイン時の同時ログイン数超過(OGH_0005)エラーの一因になっているとみられる
    (日経の公式FAQにも同様の説明がある)。押せたかどうかをbool で返す。"""
    for frame in page.frames:
        try:
            btn = frame.locator("text=ログアウト").first
            if await btn.count() > 0:
                await btn.click(timeout=5000)
                await page.wait_for_timeout(1500)
                return True
        except Exception:
            continue
    return False


async def _perform_sso_login(page, creds: dict) -> None:
    """一橋大学SSO(OpenAM)のログインフォームを送信する。

    日経側の中継ページ(meta refresh + onload submitの自動遷移だが、タイミングに
    よってはヘッドレスで一度で完了しないことがある)に出る「接続」ボタンは、
    固定時間待つのではなく最大10秒間ポーリングして、出現し次第クリックする。
    """
    id_input = page.locator('input[type="text"]').first
    pw_input = page.locator('input[type="password"]').first
    if await id_input.count() == 0 or await pw_input.count() == 0:
        return  # 既にログインフォームではない(想定外のページ)
    await id_input.fill(creds["id"])
    await pw_input.fill(creds["password"])
    await page.locator('input[type="submit"]').first.click()

    for _ in range(5):
        await page.wait_for_timeout(2000)
        connect_btn = page.locator('input[type="submit"][value="接続"]')
        if await connect_btn.count() > 0:
            try:
                await connect_btn.click(timeout=3000)
            except Exception:
                pass
            continue
        text = " ".join((await page.inner_text("body")).split())
        if "接続します" not in text and "UserID" not in text:
            break  # ログインフォームでも中継ページでもない = 最終ページに到達


@mcp.tool()
async def nikkei_fetch_page(url: str = "") -> str:
    """日経テレコム経由でページを開き、本文テキストを取得する(毎回ログインし直す)。

    url を省略すると日経テレコンのトップページを開く。プライベートブラウジング相当の
    まっさらなブラウザコンテキストを毎回新規作成するため、Cookie起因の
    「同時ログイン数超過」誤検知を避けられる(実地検証済み)。
    """
    try:
        creds = _load_credentials()
    except RuntimeError as e:
        return f"エラー: {e}"

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()  # 非永続 = プライベートブラウジング相当
        page = await context.new_page()
        try:
            await page.goto(creds["login_url"], timeout=20000, wait_until="domcontentloaded")
            await page.wait_for_timeout(1500)
            text = " ".join((await page.inner_text("body")).split())
            if _looks_like_session_limit_error(text):
                return (
                    "エラー: 同時ログイン数超過です。他のブラウザ/デバイスで日経テレコンに"
                    "ログインしたままになっていないか確認し、ログアウトしてから"
                    "再試行してください。"
                )
            if _looks_like_login_form(text):
                await _perform_sso_login(page, creds)

            # ログイン完了後、実際のポータル画面は新規ポップアップウィンドウで開き、
            # 元のページ(page)は自己終了用の空ページ(「日経テレコン 閉じる」のみ)に
            # なることが実機確認で判明した。ポップアップが開いていれば、以降は
            # そちらを本体のページとして扱う。
            await page.wait_for_timeout(1000)
            if len(context.pages) > 1:
                page = context.pages[-1]
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=10000)
                except Exception:
                    pass
                await page.wait_for_timeout(1000)

            if url:
                await page.goto(url, timeout=20000, wait_until="domcontentloaded")
                await page.wait_for_timeout(1500)

            text = " ".join((await page.inner_text("body")).split())
            if _looks_like_session_limit_error(text):
                return "エラー: 同時ログイン数超過です。しばらく時間を置いてから再試行してください。"
            if _looks_like_login_form(text):
                return "エラー: ログインに失敗しました(ログインフォームのままです)。IDまたはパスワードを確認してください。"

            text = await _read_all_text(page)  # 本文がフレーム内にある場合に対応
            if len(text) > MAX_TEXT_CHARS:
                text = text[:MAX_TEXT_CHARS] + f"\n\n...(全{len(text):,}文字中先頭{MAX_TEXT_CHARS:,}文字)"

            try:
                await _perform_logout(page)
            except Exception:
                pass  # ログアウト失敗は記事取得の成否に影響させない(ベストエフォート)

            return text or "(内容が空です)"
        except Exception as e:
            return f"エラー: {e}"
        finally:
            await context.close()
            await browser.close()


if __name__ == "__main__":
    mcp.run()
