# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A fully local, offline-capable AI agent (Ollama + custom MCP tools + a Claude-Code-style web UI) running on a single Mac. Goal: approximate Claude Code's tool-use capabilities using a local LLM as the "brain," with each capability (filesystem, shell, web, browser, screen control, memory, external API delegation) implemented as a small self-written MCP server rather than an existing agent framework (LangChain/Open Interpreter were explicitly rejected).

There is no build step, no bundler, and no test suite — this is a runtime service, not a library.

## Running it

```bash
source venv/bin/activate
uvicorn webapp:app --host 0.0.0.0 --reload --port 8420
```

**`--host 0.0.0.0` is required, not optional** — the app is reached remotely over Tailscale from the user's iPhone (see `restrict_to_localhost_and_tailscale` middleware in `webapp.py`, which is what actually restricts access, not the bind address). Omitting `--host` silently falls back to `127.0.0.1`-only and breaks iPhone/remote access with no error — this has happened before. Always pass it explicitly on every restart.

Open `http://127.0.0.1:8420` locally, or `http://100.95.113.70:8420` (Mac's Tailscale IP) from the iPhone. The CLI-only variant (no web UI) is `python3 bridge.py`.

Ollama must already be running locally (`ollama serve`) with the target model pulled (`ollama pull <model>`).

There is also a Dock launcher (`LocalAIAgent.app` / `~/Applications/LocalAIAgent.app`) whose `Contents/MacOS/LocalAIAgent` script starts the server if not already running, then opens the Safari "Add to Dock" chromeless window pointed at it.

## Architecture

**`core.py`** is the shared brain used by both `webapp.py` (FastAPI) and `bridge.py` (CLI) — do not duplicate logic between the two entry points; add new shared behavior to `core.py`.

- `MCPBridge` connects to every server listed in `config/servers.json` over stdio (`mcp.client.stdio`), merges all their tools into one OpenAI-style `tools_for_model` list, and dispatches `call_tool()` by name.
- `SYSTEM_PROMPT` is the single place that governs model behavior (language matching, when to use tools vs. answer directly, URL-fidelity rules for `fetch_page`/`browser_open`, when to delegate to `ask_gemini`, the memory/`remember` convention, and the safety-block explanations for `bash`). Any new tool with non-obvious usage rules or a safety gate needs a corresponding instruction added here, or the model won't reliably use it correctly.
- `run_turn_stream()` is the tool-calling loop: it streams tokens, executes any `tool_calls` the model emits, feeds results back, and repeats until the model stops calling tools or `MAX_TOOL_ITERATIONS` (8) is hit (runaway-loop guard).
- **Fake tool-call recovery**: local models (especially smaller ones) sometimes emit a tool call as plain text (`web_search({"query": "..."})` or raw `{"name": ..., "arguments": ...}` JSON) instead of a real structured tool call. `_recover_fake_tool_call()` parses both patterns back into a real call before falling back to a bounded corrective retry (`MAX_FAKE_CALL_RETRIES`). If you touch the tool-calling loop, preserve this — it's compensating for real, observed model behavior, not speculative.

**The `invocation_source` safety pattern** (used across several tools): `MCPBridge.call_tool(name, arguments, source=...)` unconditionally overwrites `arguments["invocation_source"]` server-side — the model cannot spoof `"manual"` by including it in its own tool-call arguments. Tools that gate on this:
- `tools/computer_server.py`: `computer_click`/`computer_type`/`computer_key` require `"manual"` (screen/keyboard actions must be human-confirmed).
- `tools/bash_server.py`: blocks regardless of source, but only for a narrow pattern set (`ALWAYS_BLOCKED_PATTERNS`: emptying the Trash, or `rm -rf` targeting `~`, `$HOME`, or `/` directly) — general install/delete commands run unmodified, auto or manual, per explicit user instruction. When editing these patterns, keep them scoped to whole-home/root wipes; don't block e.g. `rm -rf ./build` or `rm -rf node_modules`.

When adding a new MCP tool that has real-world side effects on anything outside this Mac (sending something, posting something, irreversible deletion), default to gating it on `invocation_source == "manual"` the same way, and explain the gate in `SYSTEM_PROMPT` so the model doesn't just retry silently when blocked.

**MCP servers** (`tools/*_server.py`) are independent single-file scripts using `mcp.server.mcpserver.MCPServer` with `@mcp.tool()`-decorated functions; the function's docstring becomes both the tool description sent to the model and the description shown in the UI's tools panel — Japanese-language docstrings are the convention here. New servers must be registered in `config/servers.json` (`command`/`args`/`env`) to be loaded; `POST /api/mcp-servers` can also register+connect one live without a restart. `config/servers.json` holds real secrets in plaintext (API keys, OAuth tokens, session cookies) and is gitignored — `config/servers.example.json` is the sanitized reference copy checked into the repo; keep it in sync (with placeholder values) whenever you add/remove a server or an `env` key. Servers that need an API key read it from an environment variable (never hardcoded) supplied via that config's `env` field — e.g. `gemini_server.py` reads `GEMINI_API_KEY`. `tools/finance_server.py` (Yahoo Finance quotes) is not registered as its own MCP server — it's imported directly by `tools/report_server.py` for `generate_report()` (registered as the `news` server). `tools/nikkei_server.py` + `config/nikkei_credentials.json` are deliberately kept but unused (Nikkei Telecom login became unreliable under concurrent-session limits; `report_server.py` switched to the login-free Yahoo Finance quotes it now uses instead — see the comment near the top of `report_server.py`).

**Tool exposure is gated by `/command`, not just by registration in `servers.json`** (`webapp.py`): `ALWAYS_ON_SERVERS` (`filesystem`, `bash`, `web`, `memory`, `gemini`, `browser`) are exposed to the model on every turn. Everything else registered in `servers.json` (`notebooklm`, `notion`, `perplexity`, `slides`, `gmail`, `news`, …) is hidden unless the user's message contains the matching `/name` token that turn (`resolve_active_tools()` parses `/command`s out of the latest user message). Since the model can't see a tool it doesn't know exists, `build_on_demand_commands_block()` appends a list of these on-demand servers (and their tool names) to the system prompt on every request, so the model can tell the user which `/command` to add instead of claiming the capability doesn't exist.

**External MCP tool descriptions**: the stock `@modelcontextprotocol/server-filesystem` tools return English descriptions; `core.py`'s `TOOL_DESCRIPTIONS_JA` dict overrides them with Japanese equivalents for consistency with the rest of the UI/prompt. Add here when wiring in another third-party MCP server with English-only descriptions.

**Filesystem tool access is scoped to `/Users/luca` only** (the `args` in `config/servers.json`'s `filesystem` entry) — this was a deliberate choice over full-`/` root access.

**`webapp.py`** is a thin FastAPI layer over `core.py` + `db.py`: conversation CRUD, a two-phase send (`POST .../user-message` persists the user's message immediately so a slow generation can be cancelled without losing it, then a separate `POST .../generate` streams the actual reply as NDJSON), manual tool execution (`POST .../tools/{tool_name}`, always `source="manual"`), and title auto-generation after the first exchange. Cross-conversation memory (`remember`/`data/memories.json`) is injected directly into the system prompt by `build_system_prompt()` on every request — it does *not* rely on the model choosing to call `list_memories`.

**`db.py`** is raw `sqlite3` (no ORM) — two tables, `conversations` and `messages`. `titled_count` on `conversations` tracks how many messages existed at last auto-title generation, so retitling is skipped if nothing new has been said.

**`static/index.html`** is a single-file vanilla JS/HTML/CSS frontend with no build step and no CDN dependencies (offline requirement) — custom markdown renderer, NDJSON stream consumption via `fetch()` + `ReadableStream`, `AbortController`-based cancel-and-edit. Enter inserts a newline; sending requires a double-Enter (an explicit, non-default UX choice — don't "fix" this to single-Enter-to-send).

## Model configuration

`DEFAULT_MODEL` and `CONTEXT_WINDOW` are set at the top of `core.py`. Models in use are Ollama-abliterated (uncensored) community builds, e.g. `huihui_ai/gemma-4-abliterated:26b` — this is intentional per the user's explicit requirement for an unrestricted local model; don't "fix" this toward a standard safety-aligned model without being asked. `CONTEXT_WINDOW` is deliberately capped below the model's theoretical max to avoid over-pressuring KV-cache memory on this machine's unified memory budget.

This machine (16" MacBook Pro, M4, 24GB unified memory) is memory-constrained for large local models: a dense ~27B model was measured at ~3x slower wall-clock than a similarly-sized MoE model (~26B total/~4B active) on identical tasks, because the dense model partially offloads to CPU. Prefer MoE architectures over dense ones at this size class when evaluating new models for this machine, and sanity-check any new model choice with `ollama ps` (look at the CPU/GPU split, not just whether it loads).
