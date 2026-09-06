# local-ai-agent

Ollama + 自作MCPサーバー群 + Claude Code風のWeb UI で構成される、完全ローカル・オフライン対応のAIエージェントです。

## これは何か

ローカルLLM(Ollama)を「頭脳」として、Claude Codeのようなツール実行型AIエージェント体験を、1台のMac上だけで再現することを目指したプロジェクトです。ファイルシステム・シェル・Web検索・ブラウザ操作・画面操作・記憶・外部API連携といった各機能は、LangChainやOpen Interpreterのような既存フレームワークではなく、それぞれ小さな自作MCP(Model Context Protocol)サーバーとして実装しています。

ビルドステップやテストスイートは存在しません。ライブラリではなく、常駐して動かすランタイムサービスです。

- `webapp.py` — FastAPI製のWeb UI(ブラウザから使う本体)
- `bridge.py` — Web UIなしのCLI版
- `core.py` — 両エントリーポイント共通のロジック(ツール呼び出しループなど)
- `tools/*.py` — 自作MCPサーバー群(bash・web・memory・browser・gemini・computerなど)
- `static/index.html` — フロントエンド

詳細な内部構造は [docs/architecture.md](docs/architecture.md)、MCPツールの追加方法は [docs/mcp-tools.md](docs/mcp-tools.md)、モデル選定の経緯は [docs/model-config.md](docs/model-config.md) を参照してください。

## 必要なもの

- macOS
- Python 3.10以上
- [Ollama](https://ollama.com/) と、使用するモデル(ローカルで動かせるサイズのもの)
- Node.js / npx(ファイルシステムMCPサーバー `@modelcontextprotocol/server-filesystem` の実行に必要)

## セットアップ

```bash
git clone https://github.com/ryu1616gugu888-beep/local-ai-agent.git
cd local-ai-agent

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

`config/servers.example.json` を `config/servers.json` としてコピーし、使うMCPサーバーのAPIキー・トークンなどを埋めてください(`config/servers.json` は秘密情報を含むため gitignore 対象です)。

```bash
cp config/servers.example.json config/servers.json
```

最低限、`filesystem`・`bash`・`web`・`memory` サーバーはAPIキー不要で動きます。Gemini・Notion・Perplexityなど外部サービス連携を使う場合のみ、該当するキー/トークンを設定してください。`filesystem` サーバーの `args` にあるアクセス先パスは、自分の環境に合わせて書き換えてください(デフォルトはリポジトリ作者のホームディレクトリを指しています)。

Ollamaを起動し、使用するモデルを取得しておきます。

```bash
ollama serve
ollama pull <モデル名>
```

## 起動方法

### Web UI

```bash
source venv/bin/activate
uvicorn webapp:app --host 0.0.0.0 --reload --port 8420
```

`http://127.0.0.1:8420` をブラウザで開くとチャットUIが使えます。`--host 0.0.0.0` を指定しないと、Tailscaleなど経由でのリモートアクセスができなくなるので注意してください(アクセス制限自体は `webapp.py` の `restrict_to_localhost_and_tailscale` ミドルウェアが行います)。

### CLI版(Web UIなし)

```bash
python3 bridge.py
```

## 注意事項

- 個人のMac 1台での利用を想定しており、複数人での同時利用や本番運用は想定していません。
- 使用モデルはあえて無検閲(abliterated)系のビルドを想定しています。標準的な安全志向モデルへの変更は、用途に応じて自身の判断で行ってください。
- ファイルシステムMCPツールのアクセス範囲は `config/servers.json` の `filesystem` エントリで明示的にスコープしてください(ルート `/` 全体へのアクセスは推奨しません)。
