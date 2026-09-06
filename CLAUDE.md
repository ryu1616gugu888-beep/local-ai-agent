# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Detailed topic docs live in `docs/` and are not auto-loaded — read the relevant one when a task touches that area:
- [docs/architecture.md](docs/architecture.md) — `core.py`/`webapp.py`/`db.py`/frontend internals, the tool-calling loop, fake-tool-call recovery
- [docs/mcp-tools.md](docs/mcp-tools.md) — adding/registering MCP tools, the `invocation_source` safety gate, `/command` exposure gating
- [docs/model-config.md](docs/model-config.md) — model selection rationale, hardware constraints

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

## Guardrails (see linked docs for the reasoning)

- `core.py` is the shared brain for both `webapp.py` and `bridge.py` — don't duplicate logic between entry points; add shared behavior there. Full breakdown: [docs/architecture.md](docs/architecture.md).
- Any new tool with non-obvious usage rules or a safety gate needs an instruction added to `SYSTEM_PROMPT` in `core.py`, or the model won't use it reliably.
- New MCP tools with real-world side effects outside this Mac (send/post/irreversible delete) should gate on `invocation_source == "manual"` by default, like `computer_server.py` and `bash_server.py` already do. Details: [docs/mcp-tools.md](docs/mcp-tools.md).
- `config/servers.json` holds real secrets and is gitignored — keep `config/servers.example.json` in sync (placeholder values) whenever a server or `env` key changes.
- Filesystem MCP tool access is deliberately scoped to `/Users/luca` only — not root.
- `static/index.html`'s double-Enter-to-send is a deliberate, non-default UX choice — don't "fix" it to single-Enter.
- Models are intentionally uncensored/abliterated builds per explicit user requirement — don't "fix" toward a standard safety-aligned model without being asked. Rationale and hardware constraints: [docs/model-config.md](docs/model-config.md).
