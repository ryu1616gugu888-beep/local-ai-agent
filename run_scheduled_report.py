#!/usr/bin/env python3
"""launchdから呼び出される、朝夕レポート生成のエントリーポイント。

使い方: python3 run_scheduled_report.py <mode>
  mode: scheduled_morning / scheduled_evening / weekly

GEMINI_API_KEYはconfig/servers.jsonから読み込み、環境変数としてこのプロセス内だけに
設定する(launchdのplistにAPIキーを平文で書きたくないため)。
"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).parent
LOG_PATH = PROJECT_DIR / "data" / "report_run.log"

logging.basicConfig(
    filename=str(LOG_PATH),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


def _load_gemini_key() -> None:
    config = json.loads((PROJECT_DIR / "config" / "servers.json").read_text())
    os.environ["GEMINI_API_KEY"] = config["mcpServers"]["gemini"]["env"]["GEMINI_API_KEY"]


async def main(mode: str) -> None:
    _load_gemini_key()
    sys.path.insert(0, str(PROJECT_DIR / "tools"))
    import report_server as rs

    fn = rs.generate_report.fn if hasattr(rs.generate_report, "fn") else rs.generate_report
    logging.info("開始: mode=%s", mode)
    try:
        result = await fn(mode=mode)
        logging.info("完了: %s", result)
    except Exception:
        logging.exception("失敗: mode=%s", mode)
        raise


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("scheduled_morning", "scheduled_evening", "weekly"):
        print("使い方: python3 run_scheduled_report.py <scheduled_morning|scheduled_evening|weekly>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
