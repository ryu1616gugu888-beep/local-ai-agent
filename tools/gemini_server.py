#!/usr/bin/env python3
"""Gemini API(無料枠)への委任用の自作MCPサーバー。

ローカルモデルが精度・正確性に自信が持てない時(複雑な判断、事実確認が重要な質問など)に、
無料のGemini 3.5 Flashへ委任する。画像・PDFの高精度な読み取り(read_document)にも使う。
APIキーは環境変数 GEMINI_API_KEY から読む。
モデル名は無料枠のRPD(1日あたりの上限)が大きい安定版を選ぶこと — プレビュー版
snapshotタグ(例: gemini-3.6-flash)はRPDが極端に小さい場合がある。
"""

import base64
import mimetypes
import os
import time
from pathlib import Path

import requests
from mcp.server.mcpserver import MCPServer

API_KEY = os.environ.get("GEMINI_API_KEY", "")
MODEL = "gemini-3.5-flash"
API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
TIMEOUT_SEC = 30
MAX_ANSWER_CHARS = 6000
MAX_RETRIES = 5  # 無料枠は「高負荷」で503が出やすいため、自動リトライする
RETRY_WAIT_SEC = 4
# レート制限(429)は分単位の枠が回復するまで待つ必要があるため、503より大幅に長く取る。
# attemptごとに 30 → 60 → 120 → 240秒 と倍増させる。
RATE_LIMIT_WAIT_SEC = 30
RATE_LIMIT_MAX_WAIT_SEC = 90
DEFAULT_READ_PROMPT = (
    "この画像/PDFに写っている内容を正確に説明してください。"
    "文字(エラーメッセージ・UIテキスト・本文など)が含まれる場合は、省略や意訳をせず、"
    "見えている通りに一字一句正確に書き起こしてください。"
)
MAX_INLINE_FILE_BYTES = 15 * 1024 * 1024  # inline_dataはbase64化で約1.33倍に膨らむため、
# Gemini側の実質上限(約20MB)に収まるよう安全マージンを取って15MBで打ち切る

mcp = MCPServer("gemini")


def _call_gemini(parts: list[dict], timeout: int = TIMEOUT_SEC, max_answer_chars: int = MAX_ANSWER_CHARS) -> str:
    if not API_KEY:
        return "エラー: GEMINI_API_KEYが設定されていません。"

    resp = None
    for attempt in range(1, MAX_RETRIES + 1):
        is_last = attempt == MAX_RETRIES
        try:
            resp = requests.post(
                API_URL,
                # APIキーはURLクエリ(?key=)ではなくヘッダで渡す。クエリに載せると
                # requestsの例外メッセージにURLごと含まれ、その文字列がレポート本文に
                # 書き出されてObsidian経由でiPhoneまで同期されてしまうため
                # (429エラー時に実際に露出しているのを確認した)。
                headers={"x-goog-api-key": API_KEY},
                json={
                    "contents": [{"parts": parts}],
                    # generationConfigを省略すると出力トークン上限がデフォルト値になり、
                    # Gemini 3.5系はthinking(内部思考)トークンがこの上限を消費するため、
                    # 長文レポート生成時に回答が短く打ち切られる事故が起きた。
                    # モデルの最大値(65536)を明示的に指定してこれを防ぐ。
                    "generationConfig": {"maxOutputTokens": 65536},
                },
                timeout=timeout,
            )
            # 429(レート制限)は無料枠のRPM/TPM超過で出る。4秒待って再送しても
            # 同じ分の枠を叩くだけなので、503とは別に指数バックオフで待つ。
            if resp.status_code == 429 and not is_last:
                # 倍増させるが上限を設ける。上限なしにすると 30→60→120→240秒 で
                # 1回の失敗に7分以上かかり、レポート全体が1時間近くかかってしまう。
                time.sleep(min(RATE_LIMIT_WAIT_SEC * (2 ** (attempt - 1)), RATE_LIMIT_MAX_WAIT_SEC))
                continue
            if resp.status_code == 503 and not is_last:
                time.sleep(RETRY_WAIT_SEC)
                continue
            resp.raise_for_status()
            break
        except requests.RequestException as e:
            if not is_last:
                time.sleep(RETRY_WAIT_SEC)
                continue
            detail = ""
            if getattr(e, "response", None) is not None:
                detail = f" ({e.response.text[:300]})"
            # ヘッダ認証に変えた後もURL以外の経路でキーが混ざる可能性を潰しておく。
            # このメッセージはレポート本文としてObsidianに書き出され得るため。
            message = f"Gemini APIエラー({MAX_RETRIES}回試行): {e}{detail}"
            if API_KEY:
                message = message.replace(API_KEY, "***APIキー***")
            return message

    data = resp.json()
    try:
        answer_parts = data["candidates"][0]["content"]["parts"]
        answer = "".join(p.get("text", "") for p in answer_parts)
    except (KeyError, IndexError):
        return f"予期しない応答形式: {data}"

    return answer[:max_answer_chars] or "(空の応答でした)"


@mcp.tool()
def ask_gemini(question: str) -> str:
    """無料枠のGemini 3.5 Flashに質問を委任する。ローカルモデルより精度・事実確認能力が高い。
    複雑な判断や、正確性が重要な質問(時事情報の要約、複雑な計算・推論など)に使う。"""
    return _call_gemini([{"text": question}])


@mcp.tool()
def read_document(file_path: str, question: str = "") -> str:
    """画像またはPDFファイルをGemini 3.5 Flashに渡し、内容を高精度に読み取る。

    ローカルモデル自身のvision機能より、スクリーンショット内の細かい文字・エラーメッセージ・
    密な文章や、PDFの本文の読み取り精度が高い。file_pathにはこのMac上の画像/PDFファイルの
    絶対パスを渡す。questionを省略すると、内容全般(文字も含む)を正確に書き起こす。
    「このエラーメッセージを読んで」「3ページ目の表だけ教えて」のように特定の観点で
    読み取りたい場合はquestionで指定する。
    """
    path = Path(file_path).expanduser()
    if not path.exists():
        return f"エラー: ファイルが見つかりません: {file_path}"

    mime_type, _ = mimetypes.guess_type(path.name)
    if not mime_type or not (mime_type.startswith("image/") or mime_type == "application/pdf"):
        return f"エラー: 画像/PDFとして認識できない拡張子です: {path.name}"

    size = path.stat().st_size
    if size > MAX_INLINE_FILE_BYTES:
        return (
            f"エラー: ファイルサイズが大きすぎます({size / 1024 / 1024:.1f}MB)。"
            f"{MAX_INLINE_FILE_BYTES / 1024 / 1024:.0f}MB以下のファイルにしてください。"
        )

    data_b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    parts = [
        {"inline_data": {"mime_type": mime_type, "data": data_b64}},
        {"text": question or DEFAULT_READ_PROMPT},
    ]
    return _call_gemini(parts)


if __name__ == "__main__":
    mcp.run()
