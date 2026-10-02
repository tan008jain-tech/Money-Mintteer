from __future__ import annotations

import os

from dotenv import load_dotenv

from money_mintter.data.hf import HfDataClient, HfDataError


def main() -> int:
    load_dotenv()
    api_key = os.getenv("HF_API_KEY", "").strip()
    if not api_key:
        print("HF_API_KEY is missing from the local environment.")
        return 2

    try:
        client = HfDataClient(
            api_key=api_key,
            base_url=os.getenv(
                "HF_DATA_BASE_URL",
                "https://api.hfdatalibrary.com/v1",
            ),
        )
        symbols = client.symbols()
    except HfDataError as exc:
        print(f"HF Data Library connection failed: {exc}")
        return 1

    print("HF Data Library connection OK.")
    print(f"Available symbols: {len(symbols)}")
    print(
        "Sample symbols:",
        ", ".join(str(row.get("ticker")) for row in symbols[:10]),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
