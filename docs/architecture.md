# アーキテクチャ詳細

システムのコア構造、`core.py`/`webapp.py`/`db.py`/フロントエンドの実装詳細。system core・tool-calling loop・エントリポイントを触るときに参照する。MCPツールの登録・安全ゲーティングは [mcp-tools.md](mcp-tools.md) を、モデル選定は [model-config.md](model-config.md) を参照。

## core.py — 共有の脳

`core.py` は `webapp.py`（FastAPI）と `bridge.py`（CLI）の両方が使う共有ロジック。**2つのエントリポイント間でロジックを重複させず、新しい共有の振る舞いは `core.py` に追加すること。**

- `MCPBridge` は `config/servers.json` に列挙された各サーバーに stdio 経由（`mcp.client.stdio`）で接続し、全サーバーのツールを1つの OpenAI 形式 `tools_for_model` リストにマージし、`call_tool()` で名前によりディスパッチする。
- `SYSTEM_PROMPT` はモデルの振る舞いを統括する唯一の場所（言語のマッチング、ツール使用とそのまま回答する場合の判断基準、`fetch_page`/`browser_open` のURL忠実性ルール、`ask_gemini` への委譲判断、メモリ/`remember` の慣習、`bash` の安全ブロックの説明）。非自明な使用ルールや安全ゲートを持つ新しいツールは、対応する指示をここに追加しないと、モデルが正しく使えない。
- `run_turn_stream()` はツール呼び出しループ本体：トークンをストリームし、モデルが出した `tool_calls` を実行し、結果をモデルに返し、モデルがツール呼び出しをやめるか `MAX_TOOL_ITERATIONS`（8）に達するまで繰り返す（暴走ループ防止）。
- **偽のツール呼び出し復元**: ローカルモデル（特に小型モデル）は、構造化されたツール呼び出しの代わりに、プレーンテキストとしてツール呼び出しを出力することがある（`web_search({"query": "..."})` や生の `{"name": ..., "arguments": ...}` JSON）。`_recover_fake_tool_call()` はこの両パターンを実際のツール呼び出しにパースし直してから、範囲を絞った修正リトライ（`MAX_FAKE_CALL_RETRIES`）にフォールバックする。ツール呼び出しループを変更する際は、この仕組みを保持すること — これは実際に観測されたモデルの挙動への対処であり、推測による実装ではない。

## webapp.py

`core.py` + `db.py` の上に立つ薄い FastAPI 層：会話のCRUD、2段階送信（`POST .../user-message` がユーザーメッセージを即座に永続化し、生成が遅くてもキャンセル時にメッセージが失われないようにする。別の `POST .../generate` が実際の返信をNDJSONでストリームする）、手動ツール実行（`POST .../tools/{tool_name}`、常に `source="manual"`）、初回のやり取り後のタイトル自動生成。

会話をまたぐメモリ（`remember`/`data/memories.json`）は `build_system_prompt()` によって毎回のリクエストで直接システムプロンプトに注入される — モデルが `list_memories` を呼ぶかどうかには依存しない。

## db.py

生の `sqlite3`（ORMなし）— `conversations` と `messages` の2テーブル。`conversations` の `titled_count` は、最後にタイトル自動生成をした時点で存在していたメッセージ数を追跡しており、新しい発言がなければ再タイトル付けはスキップされる。

## static/index.html

ビルドステップなし・CDN依存なし（オフライン要件）の単一ファイル vanilla JS/HTML/CSS フロントエンド — 独自のmarkdownレンダラー、`fetch()` + `ReadableStream` によるNDJSONストリーム消費、`AbortController` によるキャンセル・編集。Enterキーは改行を挿入し、送信にはダブルEnterが必要（意図的な非デフォルトUX選択 — これを単一Enter送信に「修正」しないこと）。
