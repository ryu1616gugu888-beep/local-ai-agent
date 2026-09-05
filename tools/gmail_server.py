#!/usr/bin/env python3
"""Gmail(読み取り専用・公式API)のメール確認用の自作MCPサーバー。複数アカウント対応。

Google Cloud ConsoleでGmail APIを有効化し、OAuthクライアント(デスクトップアプリ)の
認証情報を config/gmail_credentials.json に保存しておく必要がある(全アカウント共通)。
アカウントごとに 'profile' 名(例: "main", "work")を指定して個別にログインすると、
config/gmail_token_<profile>.json にトークンが保存され、以降はプロフィール名だけで
そのアカウントのメールを読み取れる。読み取り専用スコープのみを要求するため、
メールの送信・削除は一切行えない。
"""

from datetime import datetime, timedelta
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from mcp.server.mcpserver import MCPServer

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
CONFIG_DIR = Path(__file__).parent.parent / "config"
CREDENTIALS_PATH = CONFIG_DIR / "gmail_credentials.json"
MAX_SNIPPET_CHARS = 300

mcp = MCPServer("gmail")


def _token_path(profile: str) -> Path:
    safe = "".join(c for c in profile if c.isalnum() or c in "_-") or "default"
    return CONFIG_DIR / f"gmail_token_{safe}.json"


def _get_service(profile: str):
    token_path = _token_path(profile)
    if not token_path.exists():
        return None
    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def _header(headers: list, name: str) -> str:
    return next((h["value"] for h in headers if h["name"].lower() == name.lower()), "")


@mcp.tool()
def gmail_authenticate(profile: str = "main", invocation_source: str = "auto") -> str:
    """Gmailアカウントへの初回ログインを行う(ブラウザが開き、Googleアカウントでの許可が必要)。

    'profile' には好きな名前(例: "main", "work", "personal2")を付けられ、
    複数のGoogleアカウントをそれぞれ別のプロフィールとしてログインできる。
    読み取り専用の権限のみを要求する(送信・削除は不可)。一度許可すれば、
    以降はそのプロフィール名を指定するだけで再ログイン不要になる。
    画面が開く操作のため手動実行限定。
    """
    if invocation_source != "manual":
        return "ブロックされました: 初回ログインは、ブラウザでの許可操作が必要なため手動実行してください。"
    if not CREDENTIALS_PATH.exists():
        return (
            f"エラー: {CREDENTIALS_PATH} が見つかりません。"
            "Google Cloud ConsoleでGmail APIを有効化し、OAuthクライアント(デスクトップアプリ)の"
            "認証情報JSONをこのパスに保存してください。"
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)
    creds = flow.run_local_server(port=0)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _token_path(profile).write_text(creds.to_json())
    return f"プロフィール「{profile}」として認証に成功しました。gmail_list_recent(profile=\"{profile}\")でメールを確認できます。"


@mcp.tool()
def gmail_list_recent(
    profile: str = "main", max_results: int = 15, query: str = "", days: int = 0
) -> str:
    """指定したプロフィール(アカウント)の受信メールを一覧取得する
    (送信元・件名・日時・本文の冒頭)。読み取り専用。

    query を指定すると、Gmailの検索構文でその条件に絞り込める。例:
    "from:marubeni.com"(特定の送信元ドメイン)、"from:教務課"(送信元に含まれる文字列)、
    "subject:履修"(件名に含まれる文字列)、"deloitte"(本文・件名・送信元を横断したキーワード検索)。
    query を空にすると、通常の受信トレイを新着順で取得する。

    「直近24時間」「今日」「過去3日間」のように期間を指定された場合は、queryに手動で
    after:を書くのではなく days パラメータを使うこと(例: 24時間以内なら days=1)。
    Gmailのafter:は日単位の粒度のため、daysを指定すると自動的に該当日数分の開始日を
    計算してクエリに追加し、同時に max_results もその期間を取りこぼさない件数まで
    自動的に引き上げる(指定した max_results が上回っていればそちらを優先)。"""
    service = _get_service(profile)
    if service is None:
        return f"プロフィール「{profile}」は未認証です。gmail_authenticate(profile=\"{profile}\")を手動実行してログインしてください。"

    if days > 0:
        since = (datetime.now() - timedelta(days=days)).strftime("%Y/%m/%d")
        date_query = f"after:{since}"
        query = f"{query} {date_query}".strip() if query else date_query
        max_results = max(max_results, 50)

    list_kwargs = {"userId": "me", "maxResults": max_results}
    if query:
        list_kwargs["q"] = query
    else:
        list_kwargs["labelIds"] = ["INBOX"]

    try:
        results = service.users().messages().list(**list_kwargs).execute()
    except Exception as e:
        return f"エラー: {e}"

    messages = results.get("messages", [])
    if not messages:
        used_query = f"query=\"{query}\"" if query else "受信トレイ新着順"
        return f"該当するメールが見つかりませんでした({used_query}、max_results={max_results})。"

    lines = []
    for m in messages:
        msg = service.users().messages().get(
            userId="me", id=m["id"], format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        ).execute()
        headers = msg["payload"]["headers"]
        sender = _header(headers, "From")
        subject = _header(headers, "Subject") or "(件名なし)"
        date = _header(headers, "Date")
        snippet = msg.get("snippet", "")[:MAX_SNIPPET_CHARS]
        lines.append(f"差出人: {sender}\n件名: {subject}\n日時: {date}\n冒頭: {snippet}")

    return "\n\n".join(lines)


if __name__ == "__main__":
    mcp.run()
