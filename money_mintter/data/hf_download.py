from __future__ import annotations

import argparse
import os
from pathlib import Path

from dotenv import load_dotenv

from money_mintter.data.hf import HfDataClient, HfDataError


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Download a clean HF Data Library 1-minute CSV."
    )
    parser.add_argument("symbol", help="Ticker symbol, e.g. AAPL")
    parser.add_argument(
        "--output",
        default=None,
        help="Output CSV path (default: data/raw/hf/<SYMBOL>_1min_clean.csv)",
    )
    args = parser.parse_args()

    load_dotenv()
    api_key = os.getenv("HF_API_KEY", "").strip()
    if not api_key:
        parser.error("HF_API_KEY is missing from the local .env")

    symbol = args.symbol.strip().upper()
    output = (
        Path(args.output)
        if args.output
        else Path("data") / "raw" / "hf" / f"{symbol}_1min_clean.csv"
    )

    client = HfDataClient(
        api_key=api_key,
        base_url=os.getenv(
            "HF_DATA_BASE_URL",
            "https://api.hfdatalibrary.com/v1",
        ),
    )

    try:
        path = client.download_1min_csv(symbol, output, version="clean")
    except HfDataError as exc:
        print(f"HF download failed: {exc}")
        return 1

    print(f"Downloaded {symbol} 1-minute clean data to: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
