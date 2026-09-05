"""
ローカルAIエージェントCLI版。

config/servers.json にMCPサーバーを登録すると、起動時に自動接続してツールとして
モデルに公開する。サーバー未登録の状態でも、素のチャットとして動作する。
"""

import asyncio

import ollama

from core import SYSTEM_PROMPT, DEFAULT_MODEL, MCPBridge, run_turn


async def chat_loop(bridge: MCPBridge):
    client = ollama.AsyncClient()
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    print(f"[bridge] モデル: {DEFAULT_MODEL}")
    print("[bridge] 準備完了。終了するには exit または quit。\n")

    while True:
        try:
            user_input = input("あなた> ").strip()
        except EOFError:
            break
        if user_input.lower() in ("exit", "quit"):
            break
        if not user_input:
            continue

        messages.append({"role": "user", "content": user_input})
        new_messages = await run_turn(bridge, client, messages)
        messages.extend(new_messages)

        for msg in new_messages:
            if msg.get("tool_calls"):
                for call in msg["tool_calls"]:
                    fn = call["function"]
                    print(f"[bridge] ツール呼び出し: {fn['name']}({fn.get('arguments', {})})")
            elif msg.get("role") == "assistant" and msg.get("content"):
                print(f"AI> {msg['content']}\n")


async def main():
    bridge = MCPBridge()
    try:
        n = await bridge.connect_all()
        if n:
            print(f"[bridge] MCPサーバー {n}個に接続しました")
        else:
            print("[bridge] MCPサーバー未接続(config/servers.json が空)。プレーンチャットのみで起動します。")
        await chat_loop(bridge)
    finally:
        await bridge.close()


if __name__ == "__main__":
    asyncio.run(main())
