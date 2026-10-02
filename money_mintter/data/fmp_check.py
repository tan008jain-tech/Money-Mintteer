from __future__ import annotations

import os

from dotenv import load_dotenv

from money_mintter.data.fmp import FmpApiError, FmpClient


def main() -> int:
    load_dotenv()
    api_key = os.getenv("FMP_API_KEY", "").strip()
    if not api_key:
        print("FMP_API_KEY is missing from the local environment.")
        return 2

    try:
        payload = FmpClient(
            api_key=api_key,
            base_url=os.getenv(
                "FMP_BASE_URL",
                "https://financialmodelingprep.com/stable",
            ),
        ).get_json("most-actives")
    except FmpApiError as exc:
        print(f"FMP connection failed: {exc}")
        return 1

    if not isinstance(payload, list):
        print("FMP connection failed: unexpected response shape.")
        return 1

    symbols = [
        str(row["symbol"])
        for row in payload
        if isinstance(row, dict) and row.get("symbol")
    ]

    print("FMP connection OK.")
    print(f"Most-active rows received: {len(payload)}")
    print("Sample symbols:", ", ".join(symbols[:5]) or "none")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
