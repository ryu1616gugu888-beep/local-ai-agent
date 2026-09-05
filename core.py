"""
共有コア: OllamaとMCPサーバー群を仲介するブリッジ本体。
CLI版(bridge.py)・Web版(webapp.py)から共通で使う。
"""

import json
import re
from contextlib import AsyncExitStack
from pathlib import Path

import ollama
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

DEFAULT_MODEL = "huihui_ai/gemma-4-abliterated:26b"
# モデルの理論上限は32768だが、24GBメモリのMacでKVキャッシュが圧迫しすぎないよう16384に設定。
CONTEXT_WINDOW = 16384
CONFIG_PATH = Path(__file__).parent / "config" / "servers.json"

# 既知の外部MCPサーバー(英語で説明を返すもの)を日本語に置き換えるための対訳表。
# 未知のツールは原文の説明をそのまま使う。
TOOL_DESCRIPTIONS_JA = {
    "read_file": "ファイルの内容をテキストとして読み込む(非推奨: read_text_fileを使用してください)",
    "read_text_file": "ファイルの内容をテキストとして読み込む。様々な文字エンコーディングに対応し、"
    "先頭/末尾の行数を指定した部分読み込みにも対応する",
    "read_media_file": "画像や音声などのファイルをBase64エンコードされたデータとMIMEタイプで読み込む",
    "read_multiple_files": "複数のファイルを一度に読み込む。1つずつ読み込むより効率的",
    "write_file": "新規ファイルを作成、または既存ファイルを完全に上書きする。上書きは注意して使用すること",
    "edit_file": "テキストファイルに行単位の編集を加える。指定した行の並びを新しい内容に置き換える",
    "create_directory": "新しいディレクトリを作成する(既に存在する場合はそのまま)。ネストしたディレクトリも一度に作成可能",
    "list_directory": "指定パス内のファイル・ディレクトリの詳細な一覧を取得する",
    "list_directory_with_sizes": "指定パス内のファイル・ディレクトリの一覧をサイズ付きで取得する",
    "directory_tree": "ファイル・ディレクトリの階層構造を再帰的にJSON形式で取得する",
    "move_file": "ファイルやディレクトリを移動・リネームする",
    "search_files": "指定パターン(glob形式)に一致するファイル・ディレクトリを再帰的に検索する",
    "get_file_info": "ファイルやディレクトリの詳細なメタデータ(サイズ・更新日時など)を取得する",
    "list_allowed_directories": "このサーバーがアクセスを許可されているディレクトリの一覧を返す",
}
SYSTEM_PROMPT = (
    "あなたはユーザーのMac上で動くローカルAIエージェントです。"
    "回答は結論から簡潔に述べ、不要な前置きや過度な丁寧語、絵文字は使わないでください。"
    "技術者向けに、必要な時だけ見出し・表・コードブロックで情報を整理してください。"
    "ツールは本当に必要な時(最新情報・正確な数値・ファイル操作など、自分の知識だけでは"
    "答えられない場合)だけ使ってください。既に知っている一般的な知識で十分に答えられる"
    "質問には、ツールを使わずそのまま簡潔に答えてください。"
    "ユーザーの質問と同じ言語で回答してください(日本語で聞かれたら日本語、英語で聞かれたら"
    "英語)。"
    "検索結果や自分の知識から具体的な答えが出せる場合は、一般論やはぐらかしで濁さず、"
    "はっきりと結論を述べてください。"
    "web_searchやfetch_pageの結果は、ツールの戻り値としてその場で直接渡されます。"
    "検索結果が何らかのファイルに保存されていると仮定して読み込もうとしないでください。"
    "web_searchのスニペットが抽象的・宣伝的で具体的な数値や事実を含まない場合、"
    "検索結果を並べただけの曖昧な回答で済ませず、最も関連性の高い1〜2件についてfetch_pageで"
    "本文を実際に取得し、具体的な情報を抽出してから回答してください。"
    "fetch_pageに渡すURLは、必ずweb_searchの結果に実際に含まれていたものをそのままコピーして"
    "使ってください。自分で推測・生成したURLを使わないでください。fetch_pageが失敗した場合は、"
    "別の推測URLを試すのではなく、web_searchの結果だけを使って回答してください。"
    "web_searchが「検索エラー」や「検索結果はありませんでした」を繰り返し返す場合(検索エンジン側の"
    "レート制限が原因であることが多い)、同じ検索を何度もリトライせず、代わりにbrowser_openで"
    "検索エンジン(例: https://duckduckgo.com/html/?q=検索語)や関連ページを直接開き、"
    "browser_get_text/browser_list_linksで情報を取得してください。"
    "ask_geminiツールが使える場合、web_search/fetch_pageを使っても正確な事実確認が難しい質問"
    "(時事性の高い数値、複雑な計算・推論、正確性が特に重要な判断)は、無理に自分で答えを"
    "作らず、ask_geminiに委任してください。ただしask_geminiは質問に答えるだけで、"
    "ブラウザやアプリを操作する力はありません。動画再生やクリックなどの操作には使わないこと。"
    "ユーザーのメッセージに「[添付ファイル(画像): ...]」または「[添付ファイル(PDF): ...]」と"
    "パスが含まれている場合、自分自身のvision機能で内容を推測しようとせず、必ずgeminiサーバーの"
    "read_documentツールにそのパスを渡して呼び出し、その結果(正確な書き起こし)を根拠にして"
    "回答してください。スクリーンショット内の細かい文字やエラーメッセージ、PDFの本文は、"
    "read_documentを使わない自己判断だと誤読しやすいため、パスが渡された添付ファイルについては"
    "必ずread_documentを経由すること。特定の観点(例: 「このエラーメッセージだけ知りたい」"
    "「3ページ目の表だけ教えて」)があれば、read_documentのquestion引数にその観点を渡すと、"
    "より的確な回答を引き出せます。"
    "動画・記事など特定の項目を開きたい時は、browser_clickでセレクタを推測せず、"
    "必ず先にbrowser_list_linksでリンク一覧を取得し、該当するURLへbrowser_openで"
    "直接移動してください。"
    "browser_openは画面に表示されない非公開のブラウザで開くツールで、AIが内容を読むためだけの"
    "ものです。ユーザーが「Chromeで開いて」「ブラウザで表示して」のように、画面に実際に見える形で"
    "開くことを明示的に求めている場合は、browser_openではなくbashツールで`open <URL>`コマンドを"
    "実行してください(Macのデフォルトブラウザで実際に開き、画面に表示されます)。"
    "ユーザーについての重要な情報(好み・進行中の作業・繰り返し使う設定など)を知った場合は、"
    "rememberツールで記録してください。過去に記録された情報はシステムプロンプトの末尾に含まれます。"
    "bashツールで「ブロックされました」と返ってきた場合(ゴミ箱を完全に空にする操作のみが対象)は、"
    "再試行せず、その内容をそのままユーザーに伝えてください。それ以外のインストールや削除を"
    "含むコマンドは、確認を挟まずそのまま実行して構いません。"
    "gmail_list_recentでメールを確認する時は、見落としを防ぐため、必ず取得した全件を"
    "「差出人・件名」だけの一覧としてまず提示してください(絞り込んで一部だけ表示し他を"
    "隠すことはしないでください)。その上で、重要と判断したものだけ内容の要約を追加してください。"
    "重要と判断する基準は、実在の個人からの返信・直接的な連絡、お金・支払い・契約に関するもの、"
    "締め切りや予定に関するもの、セキュリティ・アカウントに関する警告です。"
    "メルマガ・プロモーション・ニュースレターは全件一覧には含めますが、重要なものとしては"
    "扱わないでください。「重要度の高いものをまとめて」のように聞かれ、該当するものが"
    "無かった場合は、無理に何かを重要扱いにせず「特にありません」と答えてください。"
    "ユーザーが特定の送信元・企業名・話題(例: 「〇〇社からのメール」「デロイトのインターン」)"
    "について聞いてきた場合、既に会話に出ている一覧を目で追って「見当たらない」と判断せず、"
    "必ずgmail_list_recentをquery引数にその名前・キーワードを入れて呼び出し直し、"
    "実際に検索した結果に基づいて答えてください。件名・送信元だけでなく本文にも一致する"
    "全文検索になるため、送信元名が分からない話題でもキーワードだけで検索できます。"
    "「直近◯時間」「今日」「過去◯日間」のように期間を指定された場合は、queryに手動でafter:を"
    "書かず、gmail_list_recentのdaysパラメータを使ってください(例: 24時間以内ならdays=1)。"
    "空引数で再実行しても対象期間を正しく絞り込めず、デフォルトの件数上限に収まる直近分しか"
    "取得できないため、期間指定の意図を無視した検索にならないよう注意してください。"
    "generate_reportツールが使える場合(/newsコマンド、または「レポートにまとめて」"
    "「アップデートをレポートで」のような依頼を受けた時)は、必ずmode=\"manual\"を指定して"
    "呼び出してください。他のmode(scheduled_morning/scheduled_evening/weekly)は定期実行"
    "専用なので、チャットから呼び出す時は使わないでください。ユーザーが「外国為替に関する"
    "ニュースのみ」「AI関連だけ」のように特定のテーマを指定した場合は、そのテーマの文言を"
    "そのままtopic引数に渡してください(例: topic=\"外国為替に関するニュース\")。"
    "テーマ指定が無い通常の依頼ではtopicを省略し、全セクションを含む通常のレポートに"
    "してください。generate_reportの戻り値がレポート本文そのものなので、それをそのまま"
    "ユーザーへの回答として提示し、自分で内容を要約し直したり付け加えたりしないでください。"
    "ツールを呼び出す時は、同じ応答の中で自分の推測や答えを絶対に書かないでください。"
    "ツール呼び出しのみを出力し、結果が返ってきた後の次の応答で初めて、"
    "最終的な回答を1回だけ述べてください(答えを2回書かない)。"
    "今の会話の内容が、今使っているモデルより別のモデル(画像理解が必要ならVL系、"
    "コード生成が中心ならCoder系など)の方が適していると判断した場合は、"
    "その理由と切り替え候補を簡潔に提案してください。"
    "ただし、モデルの切り替えやダウンロードを実行する権限はありません。"
    "提案するだけに留め、実行はユーザーの明示的な指示を待ってください。"
    "ユーザーが次に聞きたくなりそうな具体的な質問を提示すると役立つ場合(毎回である必要はない)は、"
    "回答本文の後に1行空けて、次の形式で候補を追加してください:\n"
    "SUGGESTED_QUESTIONS:\n"
    "- 質問候補1\n"
    "- 質問候補2\n"
    "この見出し文字列・形式は正確に守り、それ以外の用途では使わないでください。"
    "不要な場合はこのブロック自体を省略してください。"
)


def to_dict(obj) -> dict:
    if hasattr(obj, "model_dump"):
        return obj.model_dump(exclude_none=True)
    return dict(obj)


class MCPBridge:
    def __init__(self):
        self.exit_stack = AsyncExitStack()
        self.sessions: dict[str, ClientSession] = {}
        self.tool_to_server: dict[str, str] = {}
        self.tools_for_model: list[dict] = []

    async def connect_all(self) -> int:
        config = json.loads(CONFIG_PATH.read_text())
        servers = config.get("mcpServers", {})

        connected = 0

        for name, spec in servers.items():
            try:
                await self._connect_one(name, spec)
                connected += 1
                print(f"[bridge] ✓ {name}")
            except Exception as e:
                print(f"[bridge] ✗ {name}: {type(e).__name__}: {e}")

        return connected

    async def _connect_one(self, name: str, spec: dict):
        """spec の 'tools' キー(省略可)でツールを絞り込める。

        大量のツールを持つ外部MCPサーバー(例: notebooklm-mcp の45個)をそのまま全部
        公開すると、以前の実測(31個超でモデルが暴走)通りモデルの安定性が悪化するため、
        必要なツールだけを許可リストとして明示する。省略時は従来通り全ツールを公開する。
        """
        params = StdioServerParameters(
            command=spec["command"],
            args=spec.get("args", []),
            env=spec.get("env"),
        )
        read, write = await self.exit_stack.enter_async_context(stdio_client(params))
        session = await self.exit_stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        self.sessions[name] = session

        allowed = spec.get("tools")
        listed = await session.list_tools()
        for tool in listed.tools:
            if allowed is not None and tool.name not in allowed:
                continue
            self.tool_to_server[tool.name] = name
            self.tools_for_model.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": TOOL_DESCRIPTIONS_JA.get(tool.name, tool.description or ""),
                        "parameters": tool.input_schema,
                    },
                }
            )

    async def call_tool(self, name: str, arguments: dict, source: str = "auto") -> str:
        """source: 'auto'(モデルによる自動呼び出し) / 'manual'(ユーザーが手動実行パネルから実行)。

        'invocation_source' はここで必ず上書きするため、モデルが引数に含めても偽装できない。
        危険操作の可否判定はツール側(例: bashサーバー)がこの値を見て行う。
        ただし、この引数を受け付けない設計の外部ツール(サードパーティ製MCPサーバー)に渡すと
        スキーマ検証エラーで一切動作しなくなるため、ツールのinput_schemaに実際に
        'invocation_source' プロパティが定義されている場合のみ注入する。
        """
        server_name = self.tool_to_server.get(name)
        if server_name is None:
            return f"エラー: 未知のツール '{name}'"
        session = self.sessions[server_name]
        tool_def = next((t for t in self.tools_for_model if t["function"]["name"] == name), None)
        schema_props = (tool_def or {}).get("function", {}).get("parameters", {}).get("properties", {}) or {}
        if "invocation_source" in schema_props:
            arguments = {**arguments, "invocation_source": source}
        result = await session.call_tool(name, arguments)
        return "\n".join(getattr(c, "text", str(c)) for c in result.content)

    async def close(self):
        await self.exit_stack.aclose()


MAX_TOOL_ITERATIONS = 8  # 同じ失敗を繰り返す等の暴走ループを防ぐ上限
MAX_FAKE_CALL_RETRIES = 2  # 復元できない「実行したふりのテキスト」を再試行する回数の上限

# 応答全体が「関数名(引数)」のような形だけで構成されている場合、実際のツール呼び出しではなく
# テキストとして書いてしまった「実行したふり」の可能性が高いと判定する。
FAKE_TOOL_CALL_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\((.*)\)\s*[.。]?\s*$", re.DOTALL)


def _positional_args(name: str, value, tools_for_model: list[dict]) -> dict | None:
    """位置引数っぽい値(例: "東京の天気")を、ツールの必須パラメータ名に割り当てる。"""
    tool_def = next((t for t in tools_for_model if t["function"]["name"] == name), None)
    if tool_def is None:
        return None
    schema = tool_def["function"].get("parameters", {}) or {}
    required = schema.get("required") or list(schema.get("properties", {}).keys())
    if not required:
        return None
    return {required[0]: value}


def _recover_fake_tool_call(raw_content: str, tools_for_model: list[dict]) -> dict | None:
    """「実行したふり」のテキストを解析し、本物のツール呼び出しとして復元できるか試みる。

    モデルが実際には呼び出さず、次のようなテキストを書いてしまうことがある:
    - 'web_search({"query": "東京の天気"})' / 'web_search("東京の天気")' (関数呼び出し風)
    - '{"name": "web_search", "arguments": {"query": "東京の天気"}}' (生のJSON風)
    復元できなければ None を返す。

    'tools_for_model' には、接続中の全ツール(bridge.tools_for_model)ではなく、
    このターンで実際にモデルへ公開したツール一覧を渡すこと。/コマンドで絞り込んだ
    ツールセット外の呼び出しをここで復元してしまうと、/notionを付けずに書いた
    メッセージでも「実行したふり」のテキスト経由でNotionへの書き込みが実行できて
    しまい、絞り込みの意味がなくなるため。
    """
    tool_names = {t["function"]["name"] for t in tools_for_model}
    stripped = raw_content.strip()

    # パターン1: 関数呼び出し風 funcname(args)
    m = FAKE_TOOL_CALL_RE.match(stripped)
    if m:
        name, arg_str = m.group(1), m.group(2).strip()
        if name not in tool_names:
            return None

        parsed = None
        for candidate in (arg_str, arg_str.replace("'", '"')):
            try:
                parsed = json.loads(candidate)
                break
            except json.JSONDecodeError:
                continue

        if isinstance(parsed, dict):
            args = parsed
        else:
            value = parsed if parsed is not None else arg_str.strip("\"'")
            args = _positional_args(name, value, tools_for_model)
            if args is None:
                return None
        return {"function": {"name": name, "arguments": args}}

    # パターン2: 生のJSONオブジェクト風({"name": ..., "arguments": {...}} など)
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None

    inner = parsed.get("function") if isinstance(parsed.get("function"), dict) else parsed
    name = inner.get("name") or inner.get("tool")
    args = inner.get("arguments") if isinstance(inner.get("arguments"), dict) else inner.get("parameters")
    if not name or name not in tool_names:
        return None
    if not isinstance(args, dict):
        return None
    return {"function": {"name": name, "arguments": args}}


async def run_turn_stream(
    bridge: MCPBridge,
    client: ollama.AsyncClient,
    messages: list[dict],
    model: str = DEFAULT_MODEL,
    tools: list[dict] | None = None,
):
    """1ターン分の推論をストリーミングで実行する(トークン単位でイベントをyield)。

    messages には既存の会話履歴 + 直近のuserメッセージまでを渡す(呼び出し側で追加済みとする)。
    tools を省略すると bridge.tools_for_model(接続中の全ツール)を使う。呼び出し側で
    /コマンドなどにより絞り込んだサブセットを渡すと、そのターンだけ公開ツールを制限できる
    (ツール数が多いとモデルが不安定になるための対策)。
    イベント種別: token(逐次テキスト) / tool_call(呼び出し開始) / tool_result(結果)
    最後に done イベントで、DB保存用の確定した new_messages 一覧を返す。
    ツール呼び出しが MAX_TOOL_ITERATIONS 回を超えたら、暴走防止のため打ち切る。
    """
    tools_for_model = bridge.tools_for_model if tools is None else tools
    new_messages: list[dict] = []
    working = list(messages)
    iterations = 0
    fake_call_retries = 0

    while True:
        iterations += 1
        if iterations > MAX_TOOL_ITERATIONS:
            cutoff_msg = {
                "role": "assistant",
                "content": (
                    f"(同じ操作の繰り返しが{MAX_TOOL_ITERATIONS}回を超えたため、暴走防止のため打ち切りました。"
                    "状況を確認してもう一度試してください。)"
                ),
            }
            working.append(cutoff_msg)
            new_messages.append(cutoff_msg)
            yield {"type": "token", "text": cutoff_msg["content"]}
            break

        stream = await client.chat(
            model=model,
            messages=working,
            tools=tools_for_model or None,
            stream=True,
            options={"num_ctx": CONTEXT_WINDOW},
        )

        content_parts: list[str] = []
        tool_calls: list[dict] | None = None
        async for chunk in stream:
            delta = chunk.message
            if delta.content:
                content_parts.append(delta.content)
                yield {"type": "token", "text": delta.content}
            if delta.tool_calls:
                tool_calls = to_dict(chunk.message).get("tool_calls")

        raw_content = "".join(content_parts)
        msg: dict = {"role": "assistant", "content": raw_content}

        if not tool_calls and raw_content.strip():
            recovered = _recover_fake_tool_call(raw_content, tools_for_model)
            if recovered is not None:
                # 「実行したふり」だったが、内容を解析できたので本物のツール呼び出しとして実行する。
                tool_calls = [recovered]
            elif (
                fake_call_retries < MAX_FAKE_CALL_RETRIES
                and FAKE_TOOL_CALL_RE.match(raw_content.strip())
            ):
                # 復元もできない未知の形式。モデルに見せた上で、実際に呼ぶかテキストで
                # 直接答えるよう促して再試行する。
                fake_call_retries += 1
                working.append(msg)
                working.append(
                    {
                        "role": "user",
                        "content": (
                            "(システム注記: 直前の応答はツールを実際に呼び出しておらず、"
                            "関数呼び出しのように見える文字列を回答として書いただけでした。"
                            "実際にツールを呼び出すか、ツールを使わないなら通常の文章で"
                            "直接回答してください。)"
                        ),
                    }
                )
                yield {"type": "retry"}
                continue

        if tool_calls:
            msg["tool_calls"] = tool_calls
        working.append(msg)
        new_messages.append(msg)

        if not tool_calls:
            break

        active_tool_names = {t["function"]["name"] for t in tools_for_model}
        for call in tool_calls:
            fn = call["function"]
            name = fn["name"]
            args = fn.get("arguments", {})
            yield {"type": "tool_call", "name": name, "arguments": args}
            if name not in active_tool_names:
                # bridge.call_toolは接続中の全ツールを基準に実行してしまい、/コマンドによる
                # このターンの絞り込みを無視してしまうため、ここで明示的に弾く。正規の
                # tool_calls経由でも、モデルが公開されていないツール名を出力する(学習データ由来
                # の記憶や過去の文脈からの類推など)ことがあると実測で確認されたため、
                # 「実行したふり」の復元処理だけでなく、正規のtool_callsに対しても同じ絞り込みを
                # 必ず適用する。
                result_text = (
                    f"エラー: '{name}' は今このターンでは公開されていないツールです。"
                    "対応する/コマンドをユーザーのメッセージに含めるよう案内してください。"
                )
            else:
                result_text = await bridge.call_tool(name, args)
            tool_msg = {"role": "tool", "content": result_text, "tool_name": name}
            working.append(tool_msg)
            new_messages.append(tool_msg)
            yield {"type": "tool_result", "name": name, "content": result_text}

    yield {"type": "done", "messages": new_messages}


async def run_turn(
    bridge: MCPBridge,
    client: ollama.AsyncClient,
    messages: list[dict],
    model: str = DEFAULT_MODEL,
) -> list[dict]:
    """非ストリーミング版(CLI向け)。run_turn_streamを消費して最終結果だけ返す。"""
    async for event in run_turn_stream(bridge, client, messages, model=model):
        if event["type"] == "done":
            return event["messages"]
    return []


async def generate_title(client: ollama.AsyncClient, model: str, messages: list[dict]) -> str:
    """会話全体(直近部分含む)を踏まえてタイトルを生成/更新する(Claudeの自動タイトル付けと同じ考え方)。"""
    convo_text = "\n".join(
        f"{'ユーザー' if m.get('role') == 'user' else 'アシスタント'}: {m['content']}"
        for m in messages
        if m.get("role") in ("user", "assistant") and m.get("content")
    )
    convo_text = convo_text[-4000:]  # 直近を優先(古すぎる文脈より最新の話題を反映)
    if not convo_text.strip():
        return "新しいチャット"

    prompt = (
        "次の会話全体の内容を踏まえて、10〜15文字程度の簡潔な日本語タイトルに要約してください。"
        "タイトルのみを出力し、句読点・引用符・「タイトル:」などの接頭辞は付けないでください。\n\n"
        f"{convo_text}"
    )
    response = await client.chat(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        options={"num_predict": 30, "num_ctx": CONTEXT_WINDOW},
    )
    title = to_dict(response.message).get("content", "").strip()
    title = title.strip("「」\"'` \n")
    return title[:30] or "新しいチャット"
