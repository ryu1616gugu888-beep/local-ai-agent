# MCPツールの追加・安全ゲーティング

新しいMCPサーバー/ツールを追加する、既存ツールの公開範囲や安全ゲートを変更するときに参照する。システム全体の構造は [architecture.md](architecture.md) を参照。

## `invocation_source` 安全パターン

複数のツールで使われているパターン：`MCPBridge.call_tool(name, arguments, source=...)` はサーバー側で無条件に `arguments["invocation_source"]` を上書きする — モデルは自分のツール呼び出し引数に含めることで `"manual"` を偽装できない。このパターンでゲートしているツール：

- `tools/computer_server.py`: `computer_click`/`computer_type`/`computer_key` は `"manual"` を要求する（画面・キーボード操作は人間の確認が必須）。
- `tools/bash_server.py`: sourceに関わらずブロックするが、狭いパターン集合のみが対象（`ALWAYS_BLOCKED_PATTERNS`: ゴミ箱を空にする、または `~`・`$HOME`・`/` を直接対象にする `rm -rf`）— 一般的なインストール・削除コマンドは、自動でも手動でも、ユーザーの明示的な指示通りに変更なく実行される。このパターンを編集する際は、ホームディレクトリ全体/ルート全体の消去にスコープを限定し続けること（例えば `rm -rf ./build` や `rm -rf node_modules` はブロックしない）。

Macの外側の何かに実世界の副作用を持つ新しいMCPツール（何かを送信する、何かを投稿する、不可逆な削除）を追加するときは、同様に `invocation_source == "manual"` でゲートすることをデフォルトとし、`SYSTEM_PROMPT` にそのゲートを説明すること。そうしないと、モデルはブロックされた際にただ黙って再試行するだけになる。

## MCPサーバーの実装規約

`tools/*_server.py` は `mcp.server.mcpserver.MCPServer` を使った独立した単一ファイルスクリプトで、`@mcp.tool()` デコレータ付き関数を持つ。関数のdocstringは、モデルに送られるツールの説明とUIのツールパネルに表示される説明の両方になる — ここでは日本語のdocstringが慣習。新しいサーバーは、読み込まれるために `config/servers.json`（`command`/`args`/`env`）に登録する必要がある。`POST /api/mcp-servers` でも再起動なしにライブで登録・接続できる。

`config/servers.json` には実際のシークレット（APIキー、OAuthトークン、セッションcookie）が平文で入っており、gitignore対象 — `config/servers.example.json` がリポジトリにチェックインされているサニタイズ済みの参照コピー。サーバーの追加・削除や `env` キーの変更をするたびに、プレースホルダー値でこちらも同期させること。APIキーが必要なサーバーは、その設定の `env` フィールド経由で渡される環境変数から読み込む（ハードコードしない）— 例: `gemini_server.py` は `GEMINI_API_KEY` を読む。

`tools/finance_server.py`（Yahoo Financeの株価）は単独のMCPサーバーとしては登録されておらず、`generate_report()`（`news` サーバーとして登録されている `tools/report_server.py`）から直接importされている。`tools/nikkei_server.py` + `config/nikkei_credentials.json` は意図的に残しているが未使用（Nikkei Telecomのログインが同時セッション数制限下で不安定になったため、`report_server.py` はログイン不要のYahoo Finance株価取得に切り替えた — 詳細は `report_server.py` 冒頭のコメントを参照）。

## ツール公開範囲は `/command` でゲートされる（`servers.json` への登録だけではない）

`webapp.py` の `ALWAYS_ON_SERVERS`（`filesystem`, `bash`, `web`, `memory`, `gemini`, `browser`）は毎ターンモデルに公開される。`servers.json` に登録されているそれ以外（`notebooklm`, `notion`, `perplexity`, `slides`, `gmail`, `news`, …）は、そのターンのユーザーメッセージに対応する `/name` トークンが含まれない限り非表示（`resolve_active_tools()` が最新のユーザーメッセージから `/command` を解析する）。モデルは自分が知らないツールを見ることができないため、`build_on_demand_commands_block()` がこれらオンデマンドサーバー（とそのツール名）のリストを毎リクエストのシステムプロンプトに追加し、モデルがユーザーに「その機能がない」と誤って伝える代わりに、どの `/command` を追加すべきか伝えられるようにしている。

## 外部MCPツールの説明文（日本語化）

標準の `@modelcontextprotocol/server-filesystem` ツールは英語の説明文を返す。`core.py` の `TOOL_DESCRIPTIONS_JA` 辞書がそれをUI/プロンプト全体との一貫性のために日本語に上書きしている。英語のみの説明文を持つ別のサードパーティMCPサーバーを組み込む際は、ここに追加すること。

## ファイルシステムツールのアクセス範囲

`/Users/luca` のみにスコープされている（`config/servers.json` の `filesystem` エントリの `args`）— これはルート `/` 全体へのフルアクセスより意図的に選んだ制限。
