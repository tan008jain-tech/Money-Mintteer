# HF Data Library research integration

Money Mintter uses the HF Data Library as a temporary historical 1-minute research source.

## What it supplies

The current HF release provides 1-minute OHLCV for 1,391 U.S. stocks and ETFs, with data reaching back to 2002 for most tickers. The clean version is the default research input because it removes documented bad bars while preserving genuine gaps.

The source has an important boundary: pre-March 2022 data is based on consolidated-tape-derived PiTrading data, while post-March 2022 data is based on IEX historical data. IEX is only a small share of consolidated volume, so recent-period tests should treat the feed as a research approximation rather than a full consolidated-tape replay.

The library also warns that its ticker universe is a fixed snapshot around 2022-2023, so long historical tests are survivorship-biased. For Money Mintter's first validation pass, prefer a recent 2022-present window and report the limitation.

## Local workflow

1. Register for a free HF Data Library API key and complete the required account profile.
2. Put the key in the local `.env` file:

```text
HF_API_KEY=your_key_here
HF_DATA_BASE_URL=https://api.hfdatalibrary.com/v1
```

3. Check the connection without downloading market data:

```text
python -m money_mintter.data.hf_check
```

4. Download a specific ticker through the provider adapter.
5. Load the downloaded CSV into validated `Candle` objects and run the existing research/backtest stack.

The API returns a whole-ticker file rather than server-side date ranges, so download cost is paid once per ticker and the requested test window is sliced locally.

## Research policy

For the first strategy-validation report, use the clean version and a recent 2022-present window. Do not report the resulting performance as a universal live-market expectancy. Repeat later on other data sources and out-of-sample periods.
