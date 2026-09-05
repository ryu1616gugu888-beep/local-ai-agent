#!/usr/bin/env python3
"""株価・指数の取得用の自作MCPサーバー(Yahoo Finance、APIキー不要・完全無料)。

web_searchの弱点(数値をモデルが検索スニペットから推測して誤る)を補うため、
数値だけは必ずこのツールで確定させる。因果関係の説明は引き続きweb_search/fetch_pageを使う。
"""

import yfinance as yf
from mcp.server.mcpserver import MCPServer

# よく使う名称 -> Yahoo Financeのティッカーシンボル
ALIASES = {
    "日経平均": "^N225",
    "日経225": "^N225",
    "nikkei": "^N225",
    # ^TOPX(生の指数シンボル)はyfinance/Yahoo Finance側でデータが取得できないことが
    # 確認されたため、代わりに連動性の高い上場ETF(NEXT FUNDS TOPIX ETF)を使う。
    "topix": "1306.T",
    "ダウ": "^DJI",
    "ダウ平均": "^DJI",
    "nasdaq": "^IXIC",
    "ナスダック": "^IXIC",
    "s&p500": "^GSPC",
    "sp500": "^GSPC",
    "ドル円": "JPY=X",
    "usdjpy": "JPY=X",
}

mcp = MCPServer("finance")


@mcp.tool()
def stock_quote(symbol_or_name: str) -> str:
    """株価・株価指数・為替の最新の値を取得する(日経平均、TOPIX、ダウ、ドル円など日本語名も可)。
    数値はYahoo Financeから直接取得するため、web_searchより正確。"""
    key = symbol_or_name.strip().lower()
    ticker_symbol = ALIASES.get(key, ALIASES.get(symbol_or_name.strip(), symbol_or_name.strip()))

    try:
        ticker = yf.Ticker(ticker_symbol)
        info = ticker.fast_info
        last = info.get("lastPrice")
        prev = info.get("previousClose")
    except Exception as e:
        return f"取得エラー: {e}"

    if last is None:
        return f"'{symbol_or_name}'(シンボル: {ticker_symbol})のデータが見つかりませんでした。"

    note = "  ※TOPIX指数連動ETFの価格(指数そのものの水準とは異なる)" if ticker_symbol == "1306.T" else ""
    result = f"{symbol_or_name}({ticker_symbol}): {last:,.2f}{note}"
    if prev:
        change = last - prev
        pct = (change / prev) * 100
        sign = "+" if change >= 0 else ""
        result += f"  前日比 {sign}{change:,.2f} ({sign}{pct:.2f}%)  前日終値: {prev:,.2f}"
    return result


if __name__ == "__main__":
    mcp.run()
