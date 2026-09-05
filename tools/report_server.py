#!/usr/bin/env python3
"""朝夕レポート自動生成パイプライン専用の自作MCPサーバー。

チャットから「レポートにまとめて」のように呼び出された場合(mode="manual")と、
スケジューラから定期実行された場合(mode="scheduled"/"weekly")の両方から、
このモジュールの generate_report() を1つの入り口として使う。

設計方針(ユーザーとの合意事項):
- ローカルモデルに40〜50個のツール呼び出しを自由に組み立てさせるのではなく、
  収集〜レポート作成〜送信までを決め打ちの手順(このファイル)にまとめる。
- 文章の要約・翻訳・重要度判断など「質」が必要な部分だけをGemini(gemini_server.pyと
  同じ_call_gemini方式)に委任する。
- 実Chromeの操作(FT/NYT/WSJ/Yahoo!ファイナンス)など、通常のチャットでは
  自動実行を許可していない操作も、このパイプライン専用としてここでは直接実行する
  (invocation_sourceのゲートを経由しない、専用パイプラインのみの許可)。

過去にNikkei Telecom(日経テレコン、図書館プロキシ経由)を情報源として使っていたが、
同時ログイン数超過エラーが頻発し安定運用できなかったため廃止し、ログイン不要で
無料公開されているYahoo!ファイナンスに置き換えた(2026-09-04)。当時のコードは
tools/nikkei_server.py・config/nikkei_credentials.json に残しているが、
generate_report()からは呼び出していない。
"""

import asyncio
import json
import logging
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

from mcp.server.mcpserver import MCPServer

TOOLS_DIR = Path(__file__).parent
sys.path.insert(0, str(TOOLS_DIR))

import finance_server as _finance  # noqa: E402
import gemini_server as _gemini  # noqa: E402
import gmail_server as _gmail  # noqa: E402

# 日本のマーケット・外国のマーケットの両セクションで、記事本文に頼らず確実に
# 数値を載せられるよう、主要指数・為替を直接取得する(yfinance、無料・APIキー不要)。
MARKET_INDICES = ["日経平均", "TOPIX", "ドル円", "ダウ平均", "NASDAQ", "S&P500"]

# レポート生成専用のGeminiモデル。チャット側(gemini_server.MODEL)とは別にしている。
# 無料枠の「1日あたりリクエスト数」はモデル単位の別枠(実測20回/日)なので、
# 同じモデルを共用すると、チャットでGeminiを数回使った日に翌朝のレポートが
# 枠切れで欠ける。分けることでレポート用の枠を丸ごと確保する。
REPORT_MODEL = "gemini-3.8-flash"

# 代替モデル。単一モデルに固定すると、そのモデルが落ちている日に配信が丸ごと空になる。
# 実際に gemini-3.8-flash は335字の小さなリクエストでも503を返す状態が観測されており
# (枠切れではなくモデル側の過負荷)、その時間帯でも 3.7-flash と 3.5-flash-lite は
# 正常に応答していた。品質順に並べ、上から順に試す。
REPORT_MODEL_FALLBACKS = ["gemini-3.7-flash", "gemini-3.5-flash-lite"]

# フォールバックがあるので、1モデルあたりのリトライは浅くする。
# 落ちているモデルを5回叩くより、次のモデルへ移る方が速く消費も少ない。
_RETRIES_PER_MODEL = 2

VAULT_DIR = Path.home() / "Documents" / "Obsidian Vault" / "朝夕レポート"
GMAIL_PROFILES = ["main", "lulu20173170", "4123146"]

mcp = MCPServer("report")


# ---------- Gmail(3アカウント横断) ----------

def _collect_gmail(hours: int) -> str:
    """3つのGmailプロフィールから、指定時間内のメールを横断的に取得する。"""
    days = max(1, -(-hours // 24))  # 時間をGmailのafter:粒度(日単位)に切り上げ
    sections = []
    for profile in GMAIL_PROFILES:
        service = _gmail._get_service(profile)
        if service is None:
            sections.append(f"[{profile}] 未認証のためスキップ")
            continue
        since = (datetime.now() - timedelta(days=days)).strftime("%Y/%m/%d")
        try:
            results = service.users().messages().list(
                userId="me", q=f"after:{since}", maxResults=50
            ).execute()
        except Exception as e:
            sections.append(f"[{profile}] エラー: {e}")
            continue
        messages = results.get("messages", [])
        if not messages:
            continue
        lines = [f"[{profile}]"]
        for m in messages:
            msg = service.users().messages().get(
                userId="me", id=m["id"], format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            ).execute()
            headers = msg["payload"]["headers"]
            sender = _gmail._header(headers, "From")
            subject = _gmail._header(headers, "Subject") or "(件名なし)"
            date = _gmail._header(headers, "Date")
            snippet = msg.get("snippet", "")[:300]
            lines.append(f"差出人: {sender} / 件名: {subject} / 日時: {date} / 冒頭: {snippet}")
        sections.append("\n".join(lines))
    return "\n\n".join(sections) if sections else "(対象期間のメールはありませんでした)"


# ---------- 実Chrome操作(FT/NYT/WSJ) ----------

def _chrome_open_and_read(url: str, click_texts: list[str] | None = None) -> str:
    """実際のGoogle Chromeで指定URLを開き、必要なら順番にボタン/リンクをクリックしてから
    本文テキストを取得する。ユーザー自身の既存ログインセッション(Google/図書館/NYT/WSJ)を
    引き継ぐために、Playwrightの独立ブラウザではなくAppleScriptで実Chromeを操作する。

    click_texts: ページ内でクリックしたいボタン/リンクのテキストを順番に指定する
    (例: ["Access outside the Library", "Redeem"])。見つからない場合はスキップする。
    """
    click_texts = click_texts or []
    click_steps = ""
    for text in click_texts:
        escaped = text.replace('"', '\\"')
        click_steps += f'''
        set clicked to execute targetTab javascript "(function(){{
          var els = Array.from(document.querySelectorAll('a,button,input[type=submit]'));
          var el = els.find(e => (e.innerText||e.value||'').includes('{escaped}'));
          if (el) {{ el.click(); return true; }}
          return false;
        }})()"
        delay 2
'''
    # 記事の公開時刻を、よくあるmetaタグ/time要素から推測して取得する(サイトによって
    # 無い場合もあるので、あくまでベストエフォート)。時間帯フィルタリングに使う。
    timestamp_js = r"""
      (function(){
        var metas = Array.from(document.querySelectorAll('meta'));
        var el = metas.find(m => m.getAttribute('property') === 'article:published_time')
          || metas.find(m => m.getAttribute('name') === 'date')
          || document.querySelector('time[datetime]');
        if (!el) return '';
        return el.getAttribute('content') || el.getAttribute('datetime') || '';
      })()
    """.replace("\n", " ")
    script = f'''
    tell application "Google Chrome"
      activate
      set targetTab to make new tab at end of tabs of window 1 with properties {{URL:"{url}"}}
      delay 3
      {click_steps}
      set publishedAt to execute targetTab javascript "{timestamp_js}"
      set pageText to execute targetTab javascript "document.body.innerText"
      close targetTab
      return publishedAt & "|||" & pageText
    end tell
    '''
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        return f"エラー: {result.stderr.strip()}"
    raw = result.stdout
    published_at, _, body = raw.partition("|||")
    text = " ".join(body.split())
    header = f"[記事URL: {url}] [公開時刻: {published_at.strip() or '不明'}]\n"
    return header + text[:8000]


def _discover_links(url: str, href_pattern: str, limit: int = 5, delay: int = 3) -> list[str]:
    """指定ページを開き、href_patternに正規表現マッチするリンクを最大limit件抽出する
    (ニュースサイトのトップ/セクションページから、今読むべき記事URLを見つけるため)。

    単純な部分文字列(CSS属性セレクタのhref*=)ではなく正規表現マッチにしているのは、
    WSJの記事URL(例: /business/autos/volkswagen-board-...-ac933812 のように末尾が
    8桁16進数IDで終わる形式)のようにパスの固定位置に手がかり文字列が無いサイトにも
    対応するため。クエリ文字列やアンカー違いの重複はhrefのpath部分で除去する。"""
    escaped_pattern = href_pattern.replace("\\", "\\\\").replace('"', '\\"')
    script = f'''
    tell application "Google Chrome"
      activate
      set targetTab to make new tab at end of tabs of window 1 with properties {{URL:"{url}"}}
      delay {delay}
      set linksJson to execute targetTab javascript "(function(){{
        var re = new RegExp('{escaped_pattern}');
        var seen = new Set();
        var out = [];
        Array.from(document.querySelectorAll('a[href]')).forEach(function(a){{
          if (!re.test(a.href)) return;
          var key = a.href.split('#')[0].split('?')[0];
          if (seen.has(key)) return;
          seen.add(key);
          out.push(key);
        }});
        return JSON.stringify(out.slice(0, {limit}));
      }})()"
      close targetTab
      return linksJson
    end tell
    '''
    result = subprocess.run(
        ["osascript", "-e", script], capture_output=True, text=True, timeout=30 + delay
    )
    urls: list[str] = []
    if result.returncode == 0:
        try:
            urls = json.loads(result.stdout.strip())
        except Exception:
            urls = []

    # 0件は「本当に該当記事が無い」のか「まだ描画が終わっていない」のか区別できず、
    # 呼び出し側は静かに「記事を取得できませんでした」を生データに入れるだけなので、
    # 情報源が丸ごと欠落してもレポートは正常に見えてしまう。実際、FTはこの経路で
    # 長期間0件になっていた(3秒では描画が間に合っていなかった)。
    # そのため0件のときは一度だけ待ち時間を延ばして取り直す。
    if not urls and delay < 12:
        return _discover_links(url, href_pattern, limit, delay=12)
    return urls


def _fetch_ft(limit: int = 5) -> str:
    urls = _discover_links("https://www.ft.com/world", "/content/", limit=limit)
    sections = [_chrome_open_and_read(u) for u in urls]
    return "\n\n---\n\n".join(sections) if sections else "(記事リンクを取得できませんでした)"


def _fetch_nyt(limit: int = 5) -> str:
    home_text = _fetch_via_library("nyt", "https://www.nytimes.com/")
    urls = _discover_links("https://www.nytimes.com/", "2026/09/", limit=limit)
    sections = [home_text[:3000]]
    for u in urls:
        sections.append(_chrome_open_and_read(u))
    return "\n\n---\n\n".join(sections)


def _fetch_yahoo_finance(limit: int = 5) -> str:
    """Yahoo!ファイナンスのニュースを取得する(ログイン不要、無料公開)。

    日本の市況・経済ニュース中心という点で、Nikkei Telecomが担っていた
    「日本国内の情報源」の役割を引き継ぐ。日経テレコンと違い図書館プロキシ経由の
    ログインが不要なため、同時ログイン数超過などの認証まわりの不安定さが無い。"""
    urls = _discover_links("https://finance.yahoo.co.jp/news", "/news/detail/", limit=limit)
    sections = [_chrome_open_and_read(u) for u in urls]
    return "\n\n---\n\n".join(sections) if sections else "(記事リンクを取得できませんでした)"


def _fetch_nhk(limit: int = 5) -> str:
    """NHKニュースを取得する(ログイン不要、無料公開)。

    既存の情報源(FT/NYT/WSJ/Yahoo!ファイナンス)はいずれも金融・国際中心で、
    Yahoo!ファイナンスも市況専門のため、「日本ニュース(マーケット関連以外)」に
    渡せる素材が事実上存在しなかった。その結果、モデルが不足分を受信メール
    (求人・宣伝メール)で埋めてしまう事故が起きていたため、日本の一般ニュースを
    扱う情報源としてNHKを追加した。
    記事URLは https://news.web.nhk/newsweb/na/nd-<YYYYMMDD><ID> の形式。
    かつての www3.nhk.or.jp/news/html/<日付>/k<記事ID>.html は廃止されており、
    そちらのパターンでは1件も取れない(実測で確認済み)。
    """
    urls = _discover_links(
        "https://news.web.nhk/newsweb", "/newsweb/na/nd-", limit=limit
    )
    sections = [_chrome_open_and_read(u) for u in urls]
    return "\n\n---\n\n".join(sections) if sections else "(記事リンクを取得できませんでした)"


def _fetch_wsj(limit: int = 5) -> str:
    # 1回目のredeemフローでセッションが確立されれば、以降は直接URLを開くだけで読める
    # (NYTと同じ挙動である前提。ズレがあれば各記事もfetch_via_libraryに戻すこと)。
    home_text = _fetch_via_library("wsj", "https://www.wsj.com/")
    urls = _discover_links("https://www.wsj.com/", "-[0-9a-f]{8}", limit=limit)
    sections = [home_text[:3000]]
    for u in urls:
        sections.append(_chrome_open_and_read(u))
    return "\n\n---\n\n".join(sections)


LIBRARY_URL = "https://sjcpls.org/digital-newspapers/"


def _fetch_via_library(kind: str, article_url: str) -> str:
    """SJCPLS図書館経由でNYT/WSJの記事を開く。kind: 'nyt' または 'wsj'。

    実地検証済みのフロー(NYT):
    1. 図書館ページで "Access Outside the Library" をクリック
    2. redeemページで "REDEEM" をクリック
    3. "Continue with Google" でGoogleの既存セッションを使う
       (このアカウントは既にredeem済みだったため「Code already redeemed」画面になったが、
       これは想定内 — 一度でも認証さえ通れば、以降はnytimes.com/wsj.comへの直接アクセスで
       記事が読める状態になる)
    4. まれにGoogleのアカウント選択画面が挟まることがあるので、ユーザー本人のアカウント
       (ryu.1616...)をクリックするステップも保険として入れておく(該当要素が無ければ
       何もしないだけなので、通常フロー時は無害)。パスワード入力は発生しない想定
       (ユーザー本人の説明による)。このパイプラインは既存のGoogleログインセッションを
       クリックするだけで、ID/パスワードの入力欄には一切触れない。
    5. 最後に記事URLへ直接遷移して本文を取得する

    WSJのクリックテキストはユーザーからの説明に基づく未検証の推測。実際に試して
    ズレがあれば調整すること。
    """
    click_sequence = (
        ["Access Outside the Library", "REDEEM", "Continue with Google", "ryu.1616"]
        if kind == "nyt"
        else ["Access the Wall Street Journal", "Redeem", "OK let's go", "Continue with Google", "ryu.1616"]
    )
    click_steps = ""
    for text in click_sequence:
        escaped = text.replace('"', '\\"')
        # アカウント選択の保険ステップ(ryu.1616)だけは、Googleのアカウント選択画面が
        # div/li/spanベースのUIであることが多いため広めのセレクタを使う。それ以外の
        # 検証済みステップは元のセレクタのまま(誤クリックのリスクを増やさないため)。
        selector = "a,button,input[type=submit],div,li,span" if "ryu.1616" in text else "a,button,input[type=submit]"
        click_steps += f'''
        execute targetTab javascript "(function(){{
          var els = Array.from(document.querySelectorAll('{selector}'));
          var el = els.find(e => (e.innerText||e.value||'').trim().toLowerCase().includes('{escaped.lower()}'));
          if (el) {{ el.click(); return true; }}
          return false;
        }})()"
        delay 3
'''
    script = f'''
    tell application "Google Chrome"
      activate
      set targetTab to make new tab at end of tabs of window 1 with properties {{URL:"{LIBRARY_URL}"}}
      delay 3
      {click_steps}
      delay 3
      set URL of targetTab to "{article_url}"
      delay 3
      set pageText to execute targetTab javascript "document.body.innerText"
      close targetTab
      return pageText
    end tell
    '''
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=90)
    if result.returncode != 0:
        return f"エラー: {result.stderr.strip()}"
    text = " ".join(result.stdout.split())
    return text[:8000]


# ---------- Gemini委任(要約・翻訳・重要度判断) ----------

# 自動配信レポートのセクション定義。
# 以前は全6セクションを1回のGemini呼び出しで生成していたが、その方式では分量が
# 19,000〜26,000字で頭打ちになっていた。原因は出力側の二重の上限:
#   - maxOutputTokens=65536 が thinking(内部思考)トークンと共有であること
#   - _call_gemini の max_answer_chars による切り捨て
# セクションごとに独立した呼び出しへ分割することで、1回あたりの出力予算を
# セクション数ぶん確保し、全体で2〜3倍の分量を切り捨て無しで得られるようにした。
# (heading, guidance) の順。headingはそのまま出力の見出しになる。
# 自動配信レポートの生成単位。当初は6セクションを個別に呼ぶ設計にしたが、
# 無料枠では6回の連続呼び出しが429/503で高確率に失敗することを実測したため、
# 関連するセクションをまとめた4グループに減らしてある。
#
# needs は渡す生データの種類。生データは "=== Gmail ===" "=== NHK ===" のような
# ラベル付きブロックの連結なので、_select_material でグループ別に切り出せる。
# ニュース系グループにGmailを渡さないのは、入力トークンを減らすためだけでなく、
# 受信メール(特に求人・宣伝メール)がニュース記事として扱われるのを構造的に
# 防ぐためでもある。実際、以前のレポートでは「AI関連」セクションの中身が
# RWS TrainAI・ZipRecruiterからの求人メールで埋まる事故が起きていた。
#
# target は分量の目安。ニュースに厚く、Gmailに薄く配分している
# (Gmailが長くなりニュースが浅いという指摘を受けた配分見直し)。
_SCHEDULED_SECTION_GROUPS: list[dict] = [
    {
        "name": "Gmail",
        "needs": "gmail",
        "target": "1,500〜2,500字",
        "headings": ["1. Gmail(重要なメールのみ)"],
        "guidance": (
            "重要と判断したメールだけを、差出人・要件・取るべき対応が分かる形で簡潔に"
            "まとめてください。このセクションは短くて構いません。"
            "宣伝メール・求人メール・ニュースレターの内容を、一般ニュースや業界動向として"
            "解説することは絶対にしないでください。それらは「こういう案内が届いた」という"
            "事実として1〜2文で触れるにとどめてください。"
        ),
    },
    # 日本・外国のニュースは当初1グループにまとめ、合計16,000〜20,000字を狙ったが、
    # 1回の呼び出しでその量を書かせるとタイムアウトするまで返ってこないことを実測した
    # (成功した他グループが2,000〜9,000字だったのに対し、このグループだけ失敗した)。
    # 1回あたり9,000字前後が現実的な上限なので、日本と外国で呼び出しを分けている。
    {
        "name": "日本ニュース",
        "needs": "news",
        "target": "8,000〜10,000字",
        "headings": ["2. 日本ニュース(マーケット関連以外)"],
        "guidance": (
            "日本国内の話題のうち、株価・為替・金融政策そのものを扱うものは除き"
            "(別セクションで扱う)、政治・社会・企業・技術・事件事故などを扱ってください。"
            "1件ごとに、何が起きたかだけで終わらせず、そこに至る経緯、関係する当事者、"
            "今後どうなり得るか、読者にとって何を意味するかまで踏み込んで解説してください。"
            "1件あたり1,500字以上を目安に厚く書いてください。"
        ),
    },
    {
        "name": "外国ニュース",
        "needs": "news",
        "target": "8,000〜10,000字",
        "headings": ["3. 外国ニュース(マーケット関連以外)"],
        "guidance": (
            "海外の話題のうち、株価・為替・金融政策そのものを扱うものは除き"
            "(別セクションで扱う)、政治・社会・企業・技術・国際情勢などを扱ってください。"
            "1件ごとに、そこに至る経緯、関係する当事者、今後どうなり得るか、"
            "日本の読者にとって何を意味するかまで踏み込んで解説してください。"
            "1件あたり1,500字以上を目安に厚く書いてください。"
        ),
    },
    {
        "name": "マーケット",
        "needs": "news",
        "target": "合計10,000〜14,000字(2つの見出しでそれぞれ5,000〜7,000字)",
        "headings": ["4. 日本のマーケット(為替、株価動向、経済政策など)",
                     "5. 外国のマーケット(外国株、海外の経済政策など)"],
        "guidance": (
            "「市場データ:日本」「市場データ:海外」として渡している数値を、"
            "各セクションで必ず具体的に引用してください。"
            "数値を並べるだけで終わらせず、その水準・前日比が何を反映しているのか、"
            "背景にどの政策・経済指標・企業業績があるのかを、生データの記事に"
            "書かれている範囲で結びつけて解説してください。"
        ),
    },
    {
        "name": "AI関連",
        "needs": "news",
        "target": "6,000〜8,000字",
        "headings": ["6. AI関連(日本国内・国外を問わない)"],
        "guidance": (
            "AI・機械学習に関する話題を、国内外を問わず扱ってください。"
            "モデル・製品・規制・投資・研究など、報道記事に基づく内容だけを扱い、"
            "求人情報やサービスの宣伝をAI業界の動向として扱わないでください。"
        ),
    },
]

# 1グループの出力に許す最大文字数。目標の上限を大きく超える値にして切り捨てを防ぐ。
_SECTION_MAX_CHARS = 30000

# グループ間に空ける秒数。無料枠は連続した大きなリクエストで429/503を返すため、
# 逐次実行の上で必ず間隔を空ける。4グループなので全体で3回ぶん待つ。
_SECTION_INTERVAL_SEC = 30


def _select_material(raw_material: str, needs: str) -> str:
    """生データのラベル付きブロックから、そのセクションに必要なものだけを取り出す。

    needs="gmail" なら「=== Gmail ===」ブロックだけ、"news" ならそれ以外
    (市場データ・各メディアの記事)を返す。ブロック構造が想定と違って
    切り出せなかった場合は、分量を減らして事故を起こすより全量を渡す方を選ぶ。
    """
    # 「=」は正規表現では特別な意味を持たないが、(?=== ) と書くと先読み (?= の直後が
    # 「== 」と解釈されて一度も分割されないため、明示的にエスケープして区別する。
    blocks = re.split(r"\n\n(?=\=\=\= )", raw_material)
    if len(blocks) < 2:
        return raw_material
    is_gmail = [b.lstrip().startswith("=== Gmail ===") for b in blocks]
    picked = [b for b, g in zip(blocks, is_gmail) if (g if needs == "gmail" else not g)]
    return "\n\n".join(picked) if picked else raw_material

# セクション間に空ける秒数。当初は3並列で投げる設計にしたが、生データを渡す
# リクエストを短時間に重ねると無料枠のTPM超過で429が返ることを実測したため、
# 逐次実行 + 待機に変更した。6セクションで全体7〜9分程度を見込む
# (朝7時の自動配信には十分間に合う)。
_SECTION_INTERVAL_SEC = 20


def _synthesize_report(
    raw_material: str,
    period_label: str,
    include_gmail: bool,
    news_included: bool,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    mode: str = "manual",
    topic: str = "",
    section: dict | None = None,
) -> str:
    """生データをGeminiに渡してレポート文章を作らせる。

    section: (見出し, 個別指示) を渡すと、レポート全体ではなくそのセクション1つだけを
    生成する。自動配信レポートを分割生成して分量を確保するために使う
    (_synthesize_report_by_section から呼ばれる)。

    重要: 「生データに実際に含まれている内容だけ」を扱わせ、ニュース記事データが
    含まれていない場合はそれを正直に明記させる(データが無い部分を創作させない)。
    以前、ニュース未実装のプレースホルダー文字列しか渡していないのに「日経・FT・
    NYT・WSJの記事です」と嘘の前提を与えてしまい、Geminiが実在しないニュースを
    丸ごと創作する事故が実際に起きたため、この注意書きは省略しないこと。

    window_start/window_end: このレポートが対象とする期間。各記事には
    _chrome_open_and_read が付与した「[公開時刻: ...]」ヘッダーが付いているので、
    この期間の外側にある記事は本文から除外させる(サイト側のトップページには
    期間外の記事も混ざって表示されるため、収集側だけでなくここでも絞り込む)。
    """
    is_weekly = mode == "weekly"
    is_scheduled = mode.startswith("scheduled")
    news_note = (
        "ニュース記事の生データが含まれています。"
        if news_included
        else "今回はニュース記事の収集がまだ実装されていないため、ニュース記事の生データは"
        "含まれていません。ニュースセクションは「本バージョンではニュース収集は未実装です」"
        "とだけ明記し、絶対に内容を創作しないでください。"
    )
    window_note = ""
    if is_weekly:
        window_note = """
生データは、過去1週間分の日次レポート(Markdownノート)そのものです。各ノートには
Gmail(受信メール)のセクションも含まれていますが、週次まとめではGmail・メールに
関する内容は一切含めないでください(ニュース・市場動向のみを対象とする)。
同じ話題が複数日にまたがって出てくる場合は重複させず、時系列の推移として
まとめてください。
"""
    elif news_included and window_start and window_end:
        window_note = f"""
このレポートが対象とする期間は {window_start.strftime('%Y-%m-%d %H:%M')} 〜
{window_end.strftime('%Y-%m-%d %H:%M')} です。各ニュース記事には
「[公開時刻: ...]」というヘッダーが付いています。この公開時刻がこの対象期間の
外側にある記事は、たとえ内容が興味深くても本レポートには含めないでください
(ニュースサイトのトップページには期間外の記事も混在しているため)。
公開時刻が「不明」または取得できていない記事は、本文の内容から判断できる範囲で
明らかに対象期間外だと分かる場合を除き、参考情報として含めても構いません。
"""
    if section is not None:
        headings = section["headings"]
        heading_list = "、".join(f"「{h}」" for h in headings)
        length_rule = (
            f"あなたが今書くのは、レポート全体のうち{heading_list}のセクションだけです。"
            "これ以外のセクションに属する内容は絶対に含めないでください。"
            f"{section['guidance']}"
            f"分量の目安は{section['target']}です。"
            "生データに書かれている背景・経緯・影響、複数メディアで見解が異なる場合は"
            "その違いを丁寧に掘り下げることで、事実を一切創作せずにこの分量を"
            "確保してください。"
            "ただし、該当する生データがそもそも乏しい場合に、分量の目安を満たすために"
            "事実・数値・固有名詞を創作することは絶対に禁止です。"
            "その場合は書ける範囲だけを書き、無理に伸ばさないでください。"
        )
        heading_lines = "\n".join(f"## {h}" for h in headings)
        section_rule = (
            "出力は次の見出しだけで構成してください(この順番、この表記のまま):\n"
            f"{heading_lines}\n"
            "レポート全体のタイトル、前書き、これ以外のセクションの見出しは"
            "書かないでください。各見出しの内部は「### 」の小見出しで整理してください。"
            "該当する内容が無い見出しも省略せず、その下に"
            "「特筆すべき情報はありませんでした」と明記してください。"
        )
    elif topic:
        length_rule = (
            f"「{topic}」というテーマに関連する内容だけを扱ってください。関連しない"
            "ニュース・話題は一切含めないでください。分量はこのテーマで実際に見つかった"
            "情報量に応じて自然に決めてください(無理に長くも短くもしないこと)。"
        )
        section_rule = (
            f"見出し・小見出しは固定のセクション分けにこだわらず、「{topic}」というテーマに"
            "沿って自然に整理してください。"
        )
    elif is_scheduled:
        length_rule = (
            "読了時間15〜30分程度の分量に必ずしてください。これは自動配信レポートの必須要件です。"
            "収集できた生データの範囲内で、各ニュースの背景・経緯・影響・複数メディアの見解の"
            "違いなどを丁寧に掘り下げて説明することで、事実を創作せずに十分な分量を確保して"
            "ください(生データに無い事実の水増しは禁止ですが、生データにある内容を簡潔に"
            "済ませすぎないよう、実際に書かれている背景情報は積極的に盛り込んでください)。"
        )
        section_rule = """以下の見出しの順番・構成で必ず整理してください(該当する内容が
無いセクションは「特筆すべき情報はありませんでした」と明記し、省略しないでください)。
4と5は「市場データ:日本」「市場データ:海外」として渡している数値データが必ず存在するため、
この2つのセクションは絶対に省略しないでください:
1. Gmail(重要なメールのみ)
2. 日本ニュース(マーケット関連以外)
3. 外国ニュース(マーケット関連以外)
4. 日本のマーケット(為替、株価動向、経済政策など。「市場データ:日本」の数値を必ず使う)
5. 外国のマーケット(外国株、海外の経済政策など。「市場データ:海外」の数値を必ず使う)
6. AI関連(日本国内・国外を問わない)"""
    else:
        length_rule = "分量は収集できた情報量に応じて自然に決めてください(無理に水増ししないこと)。"
        section_rule = """以下の見出しの順番・構成で整理してください。4と5は「市場データ:日本」
「市場データ:海外」として渡している数値データが必ず存在するため、この2つのセクションは
省略せず、その数値を使って書いてください。それ以外のセクションは該当する内容が
無ければ省略して構いません:
1. Gmail(重要なメールのみ、含まれている場合)
2. 日本ニュース(マーケット関連以外)
3. 外国ニュース(マーケット関連以外)
4. 日本のマーケット(為替、株価動向、経済政策など)
5. 外国のマーケット(外国株、海外の経済政策など)
6. AI関連(日本国内・国外を問わない)"""

    prompt = f"""以下は{period_label}に収集した生データです。{news_note}
{"Gmailの情報が含まれています。" if include_gmail else ""}
{window_note}

最重要ルール: 生データに実際に書かれていない事実・数値・固有名詞・出来事を
絶対に創作しないでください。生データに存在しない情報でレポートを埋めるくらいなら、
「この期間に取得できた情報はありませんでした」と正直に書いてください。

これを元に、以下の条件でレポートを作成してください:
- {length_rule}
- 英語記事は日本語に翻訳する
- 専門用語には大学生が理解できる簡単な説明を添える
- 重要度の基準: 各メディアには特性がある(例: Yahoo!ファイナンスは日本国内の
  市況・経済ニュース中心、FTは金融・経済中心、NYT/WSJは国際政治・ビジネス中心)。
  この特性を踏まえた上で、(1)複数メディアで共通して報じられている内容(メディア
  ごとに見解が異なる場合はその違いも明記)、(2)特定の1媒体だけが報じている内容でも、
  その媒体の得意分野に照らしてAIが重要度が高いと判断したもの、の両方を含めてください。
  単に他媒体と重複しないという理由だけで、日本のニュース(Yahoo!ファイナンス)や
  金融の専門的なニュース(FT)を軽視しないでください。
- 株価・為替・金融ニュース・政治ニュースを扱う。「市場データ」として渡している主要指数の
  数値は、日本のマーケット/外国のマーケットの各セクションで具体的な数値として引用してください。
- {section_rule}
- 出典の明記: 個別のニュース項目ごとに、どのメディアから得た情報かを明記してください
  (例: 見出しの末尾や項目の冒頭に「(FT)」「(NYT)」「(Yahoo!ファイナンス)」のように)。
  特に、同じ話題について複数メディアで見解や報じ方が異なる場合は、それぞれの文の
  直後に出典を付けて(例: 「〜と指摘している(FT)。一方、〜という見方もある(NYT)。」)、
  どの見解がどのメディアのものか読者が一目で分かるようにしてください。

生データ:
{raw_material[:400000]}
"""
    # 以前は[:100000]で切り詰めており、記事取得順(FT→NYT→WSJ→Yahoo)の後方にある
    # WSJ・Yahoo!ファイナンス(金融・日本市場に強い媒体)が実際に丸ごと切り捨てられ、
    # 「株価・finance情報が少ない」という結果につながっていたことが判明した。
    # gemini-3.5-flashの入力コンテキスト上限は約100万トークン(日本語混在でも
    # 数十万文字は十分収まる)なので、400,000文字まで余裕を持って引き上げてある。
    #
    # 自動配信は _synthesize_report_by_section からセクション単位で呼ばれ、1回あたり
    # 7,000〜10,000字を生成する(6セクションで合計42,000〜60,000字)。1回でも
    # thinking+本文に時間がかかるため、timeoutは長めに取ったままにしてある。
    # 一度300秒まで短縮したが、9,000字規模の生成がそれを超えて失敗したため戻した。
    # 短いグループは30〜120秒で返るので、長い方に合わせても実害はない。
    max_chars = _SECTION_MAX_CHARS if section is not None else 40000
    return _call_with_fallback(prompt, timeout=480, max_answer_chars=max_chars)


def _call_with_fallback(prompt: str, *, timeout: int, max_answer_chars: int) -> str:
    """レポート用モデルを優先順に試し、最初に成功したものの結果を返す。

    どのモデルを使ったかは必ずログに残す。品質が落ちる代替モデルに切り替わったことに
    気づけないまま「なぜか今日のレポートは薄い」と悩む事態を避けるため。
    """
    last = ""
    for i, model in enumerate([REPORT_MODEL, *REPORT_MODEL_FALLBACKS]):
        out = _gemini._call_gemini(
            [{"text": prompt}], timeout=timeout, max_answer_chars=max_answer_chars,
            model=model, max_retries=_RETRIES_PER_MODEL,
        )
        if not out.startswith("Gemini APIエラー"):
            if i:
                logging.warning("代替モデル %s で生成しました(本来は %s)", model, REPORT_MODEL)
            return out
        last = out
        logging.warning("モデル %s での生成に失敗: %s", model, out[:150])
    return last


async def _synthesize_report_by_section(
    raw_material: str,
    period_label: str,
    include_gmail: bool,
    news_included: bool,
    window_start: datetime | None,
    window_end: datetime | None,
    mode: str,
) -> str:
    """自動配信レポートを、セクションごとの個別Gemini呼び出しに分割して生成する。

    1回の呼び出しで全セクションを書かせると、maxOutputTokens(thinkingと共有)と
    max_answer_charsの二重の上限に阻まれて19,000〜26,000字で頭打ちになっていた。
    セクション単位に分けることで各呼び出しが独立した出力予算を持てるようになり、
    全体で従来の2〜3倍の分量を切り捨て無しで確保できる。

    生データはメディア別のラベル付きブロックで並んでおり、ニュースの内容がどの
    セクションに属するかは事前に仕分けできない。そのため各呼び出しには
    _select_material で絞ったブロックを渡した上で、「今回はこのセクションだけ書け」と
    指示している(Gmailセクションだけはメール部分に完全に切り分けられる)。

    当初は3並列で投げる設計にしたが、生データを渡すリクエストを短時間に重ねると
    無料枠のTPM(分あたりトークン数)を超えて429が返ることを実測で確認したため、
    逐次実行 + _SECTION_INTERVAL_SEC の待機に変更した。

    1セクションが失敗しても残りは配信したいので、例外は握りつぶさずその旨を本文に残す。
    """
    parts: list[str] = []
    for i, group in enumerate(_SCHEDULED_SECTION_GROUPS):
        if i:
            # 無料枠は大きなリクエストを連続で投げると429/503を返すため、必ず間隔を空ける。
            await asyncio.sleep(_SECTION_INTERVAL_SEC)
        material = _select_material(raw_material, group["needs"])
        reason = ""
        try:
            text = await asyncio.to_thread(
                _synthesize_report,
                material, period_label, include_gmail, news_included,
                window_start, window_end, mode, "", group,
            )
        except Exception as e:
            text, reason = "", f"{type(e).__name__}: {e}"
        # 生成に失敗した場合、Geminiのエラー文をそのまま本文に残すとレポートが
        # エラーメッセージで埋まってしまうため、見出しだけを残して事実を明記する。
        # ただし理由を完全に捨てるとタイムアウトなのかレート制限なのか後から
        # 判別できなくなるため(実際に切り分けに手間取った)、ログには必ず残す。
        if not text.strip() or "Gemini APIエラー" in text[:200]:
            reason = reason or text.strip()[:300] or "(空の応答)"
            logging.warning("レポートのグループ生成に失敗: %s / %s", group["name"], reason)
            text = "\n\n".join(
                f"## {h}\n\n(このセクションの生成に失敗しました)" for h in group["headings"]
            )
        parts.append(text)
    return "\n\n".join(part.strip() for part in parts if part.strip())


# ---------- 配信時刻の制御 ----------

# 自動配信をこの時刻ちょうどにVaultへ書き出す(launchdはこれより前に起動させる)。
# 収集とGemini生成に十数分かかるようになったため、生成完了と配信時刻を分離した。
# 生成が早く終わってもここまで待ってから書き出すので、Obsidian(LiveSync)経由で
# iPhoneに届くタイミングが毎回ほぼ一定になる。
_DELIVERY_TIME = {
    "scheduled_morning": (8, 0),
    "scheduled_evening": (20, 0),
    "weekly": (20, 30),
}

# 待機時間の上限。想定より大幅に早く起動した場合(手動実行など)に何時間も
# 待ち続けないための安全弁。これを超える場合は待たずに即書き出す。
_MAX_DELIVERY_WAIT_SEC = 2 * 60 * 60


async def _wait_until_delivery(mode: str) -> None:
    """配信予定時刻まで待つ。既に過ぎていれば待たずに戻る。"""
    target = _DELIVERY_TIME.get(mode)
    if target is None:
        return
    hour, minute = target
    now = datetime.now()
    deliver_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    wait = (deliver_at - now).total_seconds()
    if wait <= 0:
        logging.info("配信時刻 %02d:%02d を過ぎているため即座に書き出します", hour, minute)
        return
    if wait > _MAX_DELIVERY_WAIT_SEC:
        logging.warning(
            "配信時刻まで%.0f分あり上限を超えるため待機せず書き出します", wait / 60
        )
        return
    logging.info("生成完了。配信時刻 %02d:%02d まで %.0f秒待機します", hour, minute, wait)
    await asyncio.sleep(wait)


# ---------- Obsidian Vaultへの書き出し ----------

def _write_note(date: datetime, title: str, body_text: str, is_weekly: bool = False) -> Path:
    """Obsidian Vault(実体はただのフォルダ)にMarkdownノートを書き出す。

    Word/PDF生成やiMessage送信は行わない(iMessageは同一Apple ID同士の自己送信が
    Appleの仕様上サポートされないため断念し、Obsidian Vaultへの書き出しに一本化した)。
    Vaultフォルダを一度Obsidianアプリで開いておけば、以降はここに書き出すだけで
    自動的にノートとして認識される。

    蓄積されたレポートが散らからないよう、年/月のサブフォルダに整理し、
    ファイル名は「日付 タイトル.md」の形式にする(例: 2026-09-04 朝刊レポート.md)。
    is_weekly=Trueの場合、日次レポートと混ざらないよう別の「週次まとめ」サブフォルダ
    (VAULT_DIR/週次まとめ/{年}/)に保存する。
    """
    if is_weekly:
        subdir = VAULT_DIR / "週次まとめ" / f"{date.year}"
    else:
        subdir = VAULT_DIR / f"{date.year}" / f"{date.month:02d}"
    subdir.mkdir(parents=True, exist_ok=True)
    date_str = date.strftime("%Y-%m-%d")
    dest = subdir / f"{date_str} {title}.md"
    dest.write_text(f"# {date_str} {title}\n\n{body_text}\n", encoding="utf-8")
    return dest


# ---------- メインエントリーポイント ----------

@mcp.tool()
async def generate_report(mode: str = "manual", topic: str = "") -> str:
    """朝夕レポートを生成する(レポート生成パイプライン専用ツール)。

    mode:
    - "manual": ユーザーが随時リクエストした場合。Obsidian Vaultには保存せず、
      レポート本文をそのまま返す(チャットにそのまま表示される)。
    - "scheduled_morning": 8:00の定期実行。前日19:00〜当日7:00を対象、Gmail含む。
      Obsidian Vault(~/Documents/朝夕レポート)にMarkdownノートとして保存する。
    - "scheduled_evening": 20:00の定期実行。当日7:00〜19:00を対象、Gmail含む。同様に保存する。
    - "weekly": 日曜の週次まとめ。過去1週間の保存済みノートを要約、Gmail含まない。同様に保存する。

    topic: mode="manual"の時だけ意味を持つ、任意のテーマ絞り込み文字列。
    例えば「外国為替に関するニュースのみ」のようにユーザーがテーマを指定した場合、
    このテーマに関連する内容だけのレポートを生成する(それ以外の話題は含めない)。
    指定しない場合は通常通り全セクションを含む一般的なレポートを生成する。
    scheduled_*/weeklyでは無視される(定期配信は常にテーマ限定しない全体レポート)。
    """
    include_gmail = mode != "weekly"
    hours = 12

    material_parts = []
    news_included = False

    if mode == "weekly":
        # 週次まとめは新規にニュースを取りに行かず、過去1週間分に保存済みの
        # 日次ノート(Obsidian Vault内)を読み返して要約する。Gmail内容は
        # ユーザーの指示により週次まとめには含めないため、プロンプト側で除外を指示する。
        cutoff = datetime.now() - timedelta(days=7)
        past_notes = []
        if VAULT_DIR.exists():
            for note_path in sorted(VAULT_DIR.glob("*/*/*.md")):
                if "週次まとめ" in note_path.stem:
                    continue  # 過去の週次まとめ自体は要約対象に含めない
                try:
                    note_date = datetime.strptime(note_path.stem[:10], "%Y-%m-%d")
                except ValueError:
                    continue
                if note_date >= cutoff:
                    past_notes.append(f"[{note_path.name}]\n{note_path.read_text(encoding='utf-8')}")
        if past_notes:
            material_parts.append("=== 過去1週間の日次レポート ===\n" + "\n\n---\n\n".join(past_notes))
            news_included = True
        else:
            material_parts.append("=== 過去1週間の日次レポート ===\n(保存済みのレポートが見つかりませんでした)")
    else:
        if include_gmail:
            material_parts.append("=== Gmail ===\n" + _collect_gmail(hours))

        # 市場データ(主要指数)はGmailの直後、記事本文より前に入れる。記事本文
        # (FT/NYT/WSJ/Yahoo)は分量が多く、_synthesize_report側でraw_materialを
        # 先頭100,000文字に切り詰めるため、後ろに置くと株価データが切り捨てられて
        # 「日本のマーケット」セクションが空になる事故が実際に起きた。必ず記事より
        # 前に置くことで、この切り詰めの影響を受けないようにする。
        def _quotes_block(label: str, names: list[str]) -> None:
            lines = []
            for name in names:
                try:
                    lines.append(_finance.stock_quote(name))
                except Exception as e:
                    lines.append(f"{name}: 取得エラー({e})")
            material_parts.append(f"=== 市場データ:{label}(直近値) ===\n" + "\n".join(lines))

        _quotes_block("日本(日経平均・TOPIX・ドル円)", ["日経平均", "TOPIX", "ドル円"])
        _quotes_block("海外(ダウ・NASDAQ・S&P500)", ["ダウ平均", "NASDAQ", "S&P500"])

        # 自動配信(scheduled_*)は分量を確保するため、各媒体からより多くの記事を
        # 取得する。手動(manual)はオンデマンドなので控えめでよい。
        article_limit = 10 if mode.startswith("scheduled") else 6

        # NHKだけ本数を多く取る。1記事あたりの実質的な本文量が他媒体よりかなり少なく
        # (実測: NHK 約1,900字/本に対しFTは約6,250字/本。しかもNHK側はナビゲーションの
        # 定型文がかなりの割合を占める)、同じ本数では「日本ニュース」セクションだけが
        # 薄くなるため。トップページには常時30本以上のリンクがある。
        nhk_limit = article_limit * 2

        for label, fetch_fn in [
            ("NHK", lambda: _fetch_nhk(limit=nhk_limit)),
            ("FT", lambda: _fetch_ft(limit=article_limit)),
            ("NYT", lambda: _fetch_nyt(limit=article_limit)),
            ("WSJ", lambda: _fetch_wsj(limit=article_limit)),
            ("Yahoo!ファイナンス", lambda: _fetch_yahoo_finance(limit=article_limit)),
        ]:
            try:
                content = fetch_fn()
                if content and content.strip():
                    material_parts.append(f"=== {label} ===\n{content}")
                    news_included = True
            except Exception as e:
                material_parts.append(f"=== {label} ===\n(取得エラー: {e})")

    raw_material = "\n\n".join(material_parts) if material_parts else "(データなし)"

    now = datetime.now()
    today_7am = now.replace(hour=7, minute=0, second=0, microsecond=0)
    today_7pm = now.replace(hour=19, minute=0, second=0, microsecond=0)
    window_start, window_end = {
        "manual": (now - timedelta(hours=hours), now),
        "scheduled_morning": (today_7am - timedelta(hours=12), today_7am),  # 前日19:00〜当日7:00
        "scheduled_evening": (today_7am, today_7pm),
        "weekly": (now - timedelta(days=7), now),
    }.get(mode, (now - timedelta(hours=hours), now))
    period_label = {
        "manual": "現時点まで",
        "scheduled_morning": "前日19:00〜当日7:00",
        "scheduled_evening": "当日7:00〜19:00",
        "weekly": "今週",
    }.get(mode, "対象期間")

    if mode.startswith("scheduled"):
        # 自動配信(朝刊・夕刊)はセクション分割で生成し、分量を確保する。
        # manual(チャットからの随時リクエスト)は即答性を優先して従来通り1回の呼び出し、
        # weeklyは日次レポートの再要約が目的で分量を求めていないため同じく1回のまま。
        report_text = await _synthesize_report_by_section(
            raw_material, period_label, include_gmail, news_included,
            window_start, window_end, mode,
        )
    else:
        report_text = _synthesize_report(
            raw_material, period_label, include_gmail, news_included,
            window_start, window_end, mode=mode,
            topic=(topic if mode == "manual" else ""),
        )

    if mode == "manual":
        # 随時リクエスト(オンデマンド)はVaultに保存せず、チャットにそのまま返すだけにする
        # (元の指示通り。自動配信の分だけをVaultに記録として残す)。
        return report_text

    title = {
        "scheduled_morning": "朝刊レポート",
        "scheduled_evening": "夕刊レポート",
        "weekly": "週次まとめ",
    }.get(mode, mode)
    # 生成の所要時間に関わらず、配信時刻ちょうどにVaultへ現れるようにする。
    await _wait_until_delivery(mode)
    dest = _write_note(datetime.now(), title, report_text, is_weekly=(mode == "weekly"))
    return f"レポートを生成しました: {dest}"


if __name__ == "__main__":
    mcp.run()
