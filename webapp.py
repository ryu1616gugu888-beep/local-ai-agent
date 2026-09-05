"""ローカルAIエージェントのWeb UI。すべてlocalhost内で完結する(オフライン動作)。"""

import ipaddress
import json
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import ollama
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import db
from core import CONFIG_PATH, DEFAULT_MODEL, SYSTEM_PROMPT, MCPBridge, generate_title, run_turn_stream

STATIC_DIR = Path(__file__).parent / "static"
UPLOAD_DIR = Path(__file__).parent / "data" / "uploads"
TEXT_EXTENSIONS = {
    ".txt", ".md", ".csv", ".tsv", ".json", ".py", ".js", ".ts",
    ".log", ".yaml", ".yml", ".html", ".css", ".xml",
}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
PDF_EXTENSIONS = {".pdf"}
GEMINI_READABLE_EXTENSIONS = IMAGE_EXTENSIONS | PDF_EXTENSIONS
MAX_TEXT_CHARS = 8000
MEMORY_PATH = Path(__file__).parent / "data" / "memories.json"

# 常時公開するMCPサーバー(基本的なテキスト作業に必要な最小限)。
# それ以外は /コマンドで明示的に呼び出した時だけそのターンに限り公開される
# (接続中の全ツールを毎回モデルに見せると、ツール数が多いほど暴走しやすいことが
# 実測で分かっているため)。
ALWAYS_ON_SERVERS = {"filesystem", "bash", "web", "memory", "gemini", "browser"}

COMMAND_RE = re.compile(r"(?:^|\s)/([a-zA-Z_]+)\b")


def resolve_active_tools(user_text: str) -> list[dict]:
    """直近のユーザーメッセージ本文から /コマンドを抽出し、公開するツール一覧を絞り込む。

    コマンド名は config/servers.json に接続中のサーバー名とそのまま対応する
    (例: /notion で notion サーバーのツールが有効化される)。特定のサーバーを
    個別に優遇/除外する判断はここでは行わない、完全に機械的な対応関係。
    コマンド無しの場合は ALWAYS_ON_SERVERS のみが公開される。
    """
    commands = {m.group(1).lower() for m in COMMAND_RE.finditer(user_text)}
    connected_servers = set(bridge.sessions.keys())
    active_servers = ALWAYS_ON_SERVERS | (commands & connected_servers)
    return [
        t for t in bridge.tools_for_model
        if bridge.tool_to_server.get(t["function"]["name"]) in active_servers
    ]


def build_on_demand_commands_block() -> str:
    """/コマンドで有効化できるオンデマンドサーバーの一覧を、モデルに常に伝える。

    モデルには /コマンド無しの状態のツールしか見えていないため、これが無いと
    「そもそもそのツール自体が存在しない」と誤解し、外部サービスを勧めるなど
    誤った案内をしてしまう。実際にはユーザーが該当の/コマンドを付けて再送すれば
    使えることを、モデル自身が案内できるようにする。
    """
    on_demand = sorted(name for name in bridge.sessions if name not in ALWAYS_ON_SERVERS)
    if not on_demand:
        return ""
    lines = []
    for name in on_demand:
        tool_names = [
            t["function"]["name"] for t in bridge.tools_for_model
            if bridge.tool_to_server.get(t["function"]["name"]) == name
        ]
        lines.append(f"- /{name}: {', '.join(tool_names[:5])}")
    return (
        "\n\n以下は、ユーザーがメッセージに該当の/コマンドを付けた場合にのみ、そのターンだけ"
        "追加で使えるようになるツール群です(通常はあなたには見えていません)。"
        "ユーザーの依頼がこれらの範囲に該当するのに今は該当ツールが見当たらない場合、"
        "「ツール自体が存在しない」「このAIエージェントにはできない」と誤って結論づけたり、"
        "外部サービスを代わりに勧めたりせず、該当する/コマンドをメッセージの先頭に付けて"
        "もう一度送るようユーザーに案内してください:\n" + "\n".join(lines)
    )


def build_system_prompt() -> str:
    """会話をまたぐ記憶を、モデルのツール呼び出しに頼らず常にシステムプロンプトへ含める。"""
    prompt = SYSTEM_PROMPT + build_on_demand_commands_block()
    if not MEMORY_PATH.exists():
        return prompt
    try:
        memories = json.loads(MEMORY_PATH.read_text()).get("memories", [])
    except Exception:
        return prompt
    if not memories:
        return prompt
    recent = memories[-30:]
    memory_block = "\n".join(f"- {m['content']}" for m in recent)
    return f"{prompt}\n\n過去の会話から記録されているユーザーの情報:\n{memory_block}"


bridge = MCPBridge()
ollama_client = ollama.AsyncClient()


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    await bridge.connect_all()
    yield
    await bridge.close()


app = FastAPI(lifespan=lifespan)

# 0.0.0.0でLAN内複数インターフェースを待ち受けつつ、実際に応答するのはlocalhostと
# TailscaleネットワークからのリクエストだけにOSのファイアウォールではなくアプリ側で
# 制限する(大学など共有Wi-Fiに接続していても、同じWi-Fi上の他デバイスからは届かない)。
# TailscaleのIPv4は100.64.0.0/10(CGNAT帯域)を使う。
ALLOWED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("100.64.0.0/10"),
]


@app.middleware("http")
async def restrict_to_localhost_and_tailscale(request: Request, call_next):
    client_host = request.client.host if request.client else None
    try:
        client_ip = ipaddress.ip_address(client_host)
    except (TypeError, ValueError):
        return JSONResponse({"detail": "許可されていない接続元です"}, status_code=403)
    if not any(client_ip in net for net in ALLOWED_NETWORKS):
        return JSONResponse({"detail": "許可されていない接続元です"}, status_code=403)
    return await call_next(request)


class Attachment(BaseModel):
    filename: str
    is_text: bool
    preview: str | None = None
    path: str | None = None  # 画像添付の場合、read_imageツールに渡せる保存先の絶対パス


class NewMessage(BaseModel):
    content: str
    attachments: list[Attachment] = []


class NewConversation(BaseModel):
    model: str | None = None


class ModelChange(BaseModel):
    model: str


class NewMCPServer(BaseModel):
    name: str
    command: str
    args: list[str] = []
    env: dict[str, str] | None = None
    tools: list[str] | None = None  # 省略時は全ツール公開。指定時はこのツール名だけに絞り込む


@app.get("/")
async def index():
    # スマホSafariがindex.htmlをキャッシュして更新に気づかないことがあったため、
    # 明示的にno-cacheを指定する(頻繁に編集するファイルなので)。
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


@app.get("/api/tools")
async def api_list_tools():
    return [
        {"name": t["function"]["name"], "description": t["function"]["description"]}
        for t in bridge.tools_for_model
    ]


@app.get("/api/commands")
async def api_list_commands():
    """/コマンドとして呼び出せるオンデマンドサーバー一覧(常時公開サーバーは除く)。"""
    return sorted(name for name in bridge.sessions if name not in ALWAYS_ON_SERVERS)


@app.post("/api/mcp-servers")
async def api_add_mcp_server(body: NewMCPServer):
    if body.name in bridge.sessions:
        raise HTTPException(400, f"'{body.name}' は既に接続済みです")

    config = json.loads(CONFIG_PATH.read_text())
    config.setdefault("mcpServers", {})[body.name] = {
        "command": body.command,
        "args": body.args,
        **({"env": body.env} if body.env else {}),
        **({"tools": body.tools} if body.tools else {}),
    }

    try:
        await bridge._connect_one(body.name, config["mcpServers"][body.name])
    except Exception as e:
        raise HTTPException(400, f"接続に失敗しました: {e}")

    CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2))
    added = [t for t in bridge.tools_for_model if bridge.tool_to_server.get(t["function"]["name"]) == body.name]
    return {"ok": True, "tools_added": [t["function"]["name"] for t in added]}


@app.get("/api/models")
async def api_list_models():
    result = await ollama_client.list()
    return [
        {"name": m.model, "default": m.model == DEFAULT_MODEL}
        for m in result.models
    ]


@app.get("/api/conversations")
async def api_list_conversations():
    return db.list_conversations()


@app.post("/api/conversations/{conv_id}/retitle")
async def api_retitle_conversation(conv_id: str):
    conv = db.get_conversation(conv_id)
    if not conv:
        raise HTTPException(404, "会話が見つかりません")
    history = db.get_messages(conv_id)
    if not history:
        return {"title": conv["title"]}
    if conv["titled_count"] == len(history):
        return {"title": conv["title"], "skipped": True}  # 前回生成時から変化なし
    model = conv["model"] or DEFAULT_MODEL
    title = await generate_title(ollama_client, model, history)
    db.rename_conversation(conv_id, title, titled_count=len(history))
    return {"title": title}


@app.post("/api/conversations")
async def api_create_conversation(body: NewConversation):
    conv_id = str(uuid.uuid4())
    model = body.model or DEFAULT_MODEL
    db.create_conversation(conv_id, "新しいチャット", model)
    return {"id": conv_id, "model": model}


@app.post("/api/conversations/{conv_id}/model")
async def api_set_conversation_model(conv_id: str, body: ModelChange):
    db.set_conversation_model(conv_id, body.model)
    return {"ok": True}


@app.delete("/api/conversations/{conv_id}")
async def api_delete_conversation(conv_id: str):
    db.delete_conversation(conv_id)
    return {"ok": True}


@app.get("/api/conversations/{conv_id}/messages")
async def api_get_messages(conv_id: str):
    return db.get_messages(conv_id)


@app.post("/api/conversations/{conv_id}/tools/{tool_name}")
async def api_call_tool(conv_id: str, tool_name: str, body: dict):
    """ツールパネルからの手動実行。ユーザー自身の操作なので source='manual' を渡す。"""
    arguments = body.get("arguments", {})
    result_text = await bridge.call_tool(tool_name, arguments, source="manual")
    db.add_message(conv_id, "tool", result_text, tool_name)
    return {"result": result_text}


@app.post("/api/conversations/{conv_id}/attachments")
async def api_upload_attachment(conv_id: str, file: UploadFile = File(...)):
    conv_dir = UPLOAD_DIR / conv_id
    conv_dir.mkdir(parents=True, exist_ok=True)
    safe_name = f"{uuid.uuid4().hex[:8]}_{file.filename}"
    dest = conv_dir / safe_name
    data = await file.read()
    dest.write_bytes(data)

    ext = Path(file.filename).suffix.lower()
    is_text = ext in TEXT_EXTENSIONS
    preview = None
    if is_text:
        try:
            preview = data.decode("utf-8", errors="replace")[:MAX_TEXT_CHARS]
        except Exception:
            is_text = False

    path = str(dest.resolve()) if ext in GEMINI_READABLE_EXTENSIONS else None
    return {"filename": file.filename, "is_text": is_text, "preview": preview, "path": path}


@app.post("/api/conversations/{conv_id}/user-message")
async def api_add_user_message(conv_id: str, body: NewMessage):
    """ユーザーのメッセージだけを先に保存する(生成前に中断・編集できるようにするため)。"""
    full_content = body.content
    for att in body.attachments:
        if att.is_text and att.preview is not None:
            full_content += f"\n\n[添付ファイル: {att.filename}]\n```\n{att.preview}\n```"
        elif att.path:
            ext = Path(att.filename).suffix.lower()
            kind_label = "PDF" if ext in PDF_EXTENSIONS else "画像"
            full_content += (
                f"\n\n[添付ファイル({kind_label}): {att.filename}]\nパス: {att.path}\n"
                f"(この{kind_label}の内容を正確に読み取るには、geminiサーバーのread_documentツールに"
                "上記パスを渡して呼び出してください)"
            )
        else:
            full_content += f"\n\n[添付ファイル(画像/バイナリ、内容は読めません): {att.filename}]"

    msg_id = db.add_message(conv_id, "user", full_content)
    return {"id": msg_id, "content": full_content}


@app.delete("/api/conversations/{conv_id}/messages/{message_id}")
async def api_delete_message(conv_id: str, message_id: int):
    db.delete_message(message_id)
    return {"ok": True}


@app.post("/api/conversations/{conv_id}/generate")
async def api_generate(conv_id: str):
    """直前に保存済みのユーザーメッセージまでの履歴をもとに、応答をストリーミング生成する(NDJSON)。"""
    conv = db.get_conversation(conv_id)
    model = (conv and conv["model"]) or DEFAULT_MODEL
    history = db.get_messages(conv_id)
    # titled_count==0(=一度もタイトル生成に成功していない)を条件にすることで、
    # 1ターン目でタイトル生成が失敗しても(Ollama側のモデル未検出など)、
    # 以降のターンで自動的に再試行される(単純に「1ターン目かどうか」だと
    # 一度失敗しただけで永久に「新しいチャット」のまま固定されてしまうため)。
    needs_title = (conv is None) or (conv["titled_count"] == 0)

    messages = [{"role": "system", "content": build_system_prompt()}]
    for m in history:
        messages.append({"role": m["role"], "content": m["content"]})

    last_user_text = next((m["content"] for m in reversed(history) if m["role"] == "user"), "")
    active_tools = resolve_active_tools(last_user_text)

    async def event_stream():
        async for event in run_turn_stream(bridge, ollama_client, messages, model=model, tools=active_tools):
            if event["type"] == "done":
                for m in event["messages"]:
                    db.add_message(conv_id, m.get("role", "assistant"), m.get("content", ""), m.get("tool_name"))
                if needs_title:
                    try:
                        full_history = db.get_messages(conv_id)
                        title = await generate_title(ollama_client, model, full_history)
                        db.rename_conversation(conv_id, title, titled_count=len(full_history))
                        event["title"] = title
                    except Exception:
                        # タイトル生成の失敗(モデル未検出など)で応答ストリーム全体を
                        # 落とさない。titled_countを更新しないため次のターンで再試行される。
                        import traceback
                        traceback.print_exc()
            yield json.dumps(event, ensure_ascii=False) + "\n"

    return StreamingResponse(event_stream(), media_type="application/x-ndjson")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
