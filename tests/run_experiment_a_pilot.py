from __future__ import annotations

import os
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Any

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import DataFeed

from money_mintter.config import Settings
from money_mintter.calendar.service import ExchangeCalendarService

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ExperimentAPilot")

def evaluate_symbol_point_in_time(
    sym: str,
    all_bars: list[Any],
    eval_date: datetime.date,
    settings: Settings,
    feed_name: str,
) -> dict[str, Any]:
    """Evaluate Layer 2 metrics point-in-time for a symbol up to eval_date."""
    # Filter completed bars strictly before or on eval_date's session close (or strictly prior sessions)
    # Following DailySuitabilityEngine convention: b_dt.date() < eval_date (completed sessions)
    completed_bars = []
    for b in all_bars:
        b_dt = b.timestamp.replace(tzinfo=timezone.utc) if b.timestamp.tzinfo is None else b.timestamp
        if b_dt.date() < eval_date:
            completed_bars.append((b_dt, b))

    completed_bars.sort(key=lambda x: x[0])
    lookback_n = settings.lookback_sessions
    window_bars = [b for _, b in completed_bars[-lookback_n:]]

    valid_bar_count = len(window_bars)
    if valid_bar_count < settings.min_valid_daily_bars:
        return {
            "symbol": sym,
            "eval_date": eval_date.isoformat(),
            "feed": feed_name,
            "status": "excluded",
            "exclusion_reason": "insufficient_history",
            "valid_bar_count": valid_bar_count,
            "latest_close": float(window_bars[-1].close) if window_bars else 0.0,
            "median_close_5": 0.0,
            "median_dollar_volume_20": 0.0,
            "atr_14": 0.0,
            "atr_pct": 0.0,
        }

    closes = [float(b.close) for b in window_bars]
    latest_close = closes[-1]

    if latest_close < settings.min_latest_close:
        return {
            "symbol": sym,
            "eval_date": eval_date.isoformat(),
            "feed": feed_name,
            "status": "excluded",
            "exclusion_reason": "low_price",
            "valid_bar_count": valid_bar_count,
            "latest_close": latest_close,
            "median_close_5": 0.0,
            "median_dollar_volume_20": 0.0,
            "atr_14": 0.0,
            "atr_pct": 0.0,
        }

    recent_5_closes = closes[-5:] if len(closes) >= 5 else closes
    sorted_5 = sorted(recent_5_closes)
    mid_idx = len(sorted_5) // 2
    median_close_5 = sorted_5[mid_idx] if len(sorted_5) % 2 != 0 else (sorted_5[mid_idx - 1] + sorted_5[mid_idx]) / 2.0

    if median_close_5 < settings.min_median_close_5:
        return {
            "symbol": sym,
            "eval_date": eval_date.isoformat(),
            "feed": feed_name,
            "status": "excluded",
            "exclusion_reason": "low_median_price",
            "valid_bar_count": valid_bar_count,
            "latest_close": latest_close,
            "median_close_5": median_close_5,
            "median_dollar_volume_20": 0.0,
            "atr_14": 0.0,
            "atr_pct": 0.0,
        }

    dollar_volumes = []
    for b in window_bars:
        c = float(b.close)
        v = float(b.volume)
        vwap = float(b.vwap) if hasattr(b, "vwap") and b.vwap is not None else 0.0
        price_to_use = vwap if vwap > 0 else c
        dollar_volumes.append(price_to_use * v)

    sorted_dv = sorted(dollar_volumes)
    dv_mid = len(sorted_dv) // 2
    median_dv = sorted_dv[dv_mid] if len(sorted_dv) % 2 != 0 else (sorted_dv[dv_mid - 1] + sorted_dv[dv_mid]) / 2.0

    if median_dv < settings.min_median_dollar_volume:
        return {
            "symbol": sym,
            "eval_date": eval_date.isoformat(),
            "feed": feed_name,
            "status": "excluded",
            "exclusion_reason": "low_liquidity",
            "valid_bar_count": valid_bar_count,
            "latest_close": latest_close,
            "median_close_5": median_close_5,
            "median_dollar_volume_20": median_dv,
            "atr_14": 0.0,
            "atr_pct": 0.0,
        }

    tr_list = []
    prev_c = None
    for b in window_bars:
        h = float(b.high)
        l = float(b.low)
        c = float(b.close)
        if prev_c is None:
            tr = h - l
        else:
            tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        tr_list.append(tr)
        prev_c = c

    atr_period = 14
    if len(tr_list) >= atr_period:
        initial_atr = sum(tr_list[:atr_period]) / atr_period
        current_atr = initial_atr
        for tr_val in tr_list[atr_period:]:
            current_atr = (current_atr * (atr_period - 1) + tr_val) / atr_period
        atr_14 = current_atr
    else:
        atr_14 = sum(tr_list) / len(tr_list) if tr_list else 0.0

    atr_pct = atr_14 / latest_close if latest_close > 0 else 0.0

    if atr_pct < settings.min_atr_pct:
        return {
            "symbol": sym,
            "eval_date": eval_date.isoformat(),
            "feed": feed_name,
            "status": "excluded",
            "exclusion_reason": "low_volatility",
            "valid_bar_count": valid_bar_count,
            "latest_close": latest_close,
            "median_close_5": median_close_5,
            "median_dollar_volume_20": median_dv,
            "atr_14": atr_14,
            "atr_pct": atr_pct,
        }

    if atr_pct > settings.max_atr_pct:
        return {
            "symbol": sym,
            "eval_date": eval_date.isoformat(),
            "feed": feed_name,
            "status": "quarantined",
            "exclusion_reason": "high_volatility_quarantine",
            "valid_bar_count": valid_bar_count,
            "latest_close": latest_close,
            "median_close_5": median_close_5,
            "median_dollar_volume_20": median_dv,
            "atr_14": atr_14,
            "atr_pct": atr_pct,
        }

    return {
        "symbol": sym,
        "eval_date": eval_date.isoformat(),
        "feed": feed_name,
        "status": "eligible",
        "exclusion_reason": "",
        "valid_bar_count": valid_bar_count,
        "latest_close": latest_close,
        "median_close_5": median_close_5,
        "median_dollar_volume_20": median_dv,
        "atr_14": atr_14,
        "atr_pct": atr_pct,
    }


def main():
    settings = Settings()
    client = StockHistoricalDataClient(
        api_key=settings.alpaca_api_key or "mock_key",
        secret_key=settings.alpaca_secret_key or "mock_secret",
    )

    # 1. Define stratified sample of 30 symbols with rationale
    sample_symbols = [
        # Stratum 1: Comfortably above threshold (High Liquidity)
        ("AAPL", "High Liquidity"), ("MSFT", "High Liquidity"), ("NVDA", "High Liquidity"),
        ("SPY", "High Liquidity"), ("QQQ", "High Liquidity"), ("AMZN", "High Liquidity"),
        ("GOOGL", "High Liquidity"), ("META", "High Liquidity"), ("TSLA", "High Liquidity"), ("AMD", "High Liquidity"),
        # Stratum 2: Around threshold (Medium Liquidity / Price)
        ("INTC", "Medium Liquidity"), ("PFE", "Medium Liquidity"), ("F", "Medium Liquidity"),
        ("BAC", "Medium Liquidity"), ("T", "Medium Liquidity"), ("VALE", "Medium Liquidity"),
        ("NIO", "Medium Liquidity"), ("PLTR", "Medium Liquidity"), ("SNAP", "Medium Liquidity"), ("SOFI", "Medium Liquidity"),
        # Stratum 3: Excluded or borderline under IEX
        ("XLI", "Borderline/Exile"), ("MS", "Borderline/Exile"), ("BSX", "Borderline/Exile"),
        ("AA", "Borderline/Exile"), ("AAL", "Borderline/Exile"), ("AER", "Borderline/Exile"),
        ("AFRM", "Borderline/Exile"), ("AGCO", "Borderline/Exile"), ("AIG", "Borderline/Exile"), ("AKAM", "Borderline/Exile")
    ]
    symbols = [s[0] for s in sample_symbols]
    stratum_map = {s[0]: s[1] for s in sample_symbols}

    logger.info(f"Selected {len(symbols)} symbols for Experiment A pilot across 3 strata.")

    # 2. Determine date range: 30 evaluation sessions + 20 warm-up sessions (~75 calendar days)
    calendar_service = ExchangeCalendarService({"US": "XNYS"})
    calendar = calendar_service._calendar("US")

    now_utc = datetime.now(timezone.utc)
    end_date = now_utc.date() - timedelta(days=1)
    start_date = end_date - timedelta(days=100) # Ensure enough sessions

    schedule = calendar.schedule.loc[start_date.isoformat():end_date.isoformat()]
    session_dates = [row["close"].to_pydatetime().astimezone(timezone.utc).date() for _, row in schedule.iterrows()]

    if len(session_dates) < 50:
        raise RuntimeError(f"Insufficient trading sessions found in schedule: {len(session_dates)}")

    # Take last 50 session dates: 20 warm-up, 30 evaluation
    warmup_dates = session_dates[:-30]
    eval_dates = session_dates[-30:]

    fetch_start_dt = datetime.combine(warmup_dates[0], datetime.min.time(), tzinfo=timezone.utc) - timedelta(days=5)
    fetch_end_dt = datetime.combine(eval_dates[-1], datetime.max.time(), tzinfo=timezone.utc)

    logger.info(f"Fetching daily bars from {fetch_start_dt.date()} to {fetch_end_dt.date()} ({len(eval_dates)} evaluation sessions).")

    # 3. Retrieve SIP and IEX bars separately
    sip_bars_data = {}
    iex_bars_data = {}

    for feed_enum, feed_label, target_dict in [
        (DataFeed.SIP, "SIP", sip_bars_data),
        (DataFeed.IEX, "IEX", iex_bars_data)
    ]:
        logger.info(f"Retrieving daily bars for feed: {feed_label}")
        try:
            req = StockBarsRequest(
                symbol_or_symbols=symbols,
                timeframe=TimeFrame(1, TimeFrameUnit.Day),
                start=fetch_start_dt,
                end=fetch_end_dt,
                feed=feed_enum,
            )
            resp = client.get_stock_bars(req)
            resp_data = getattr(resp, "data", {}) or {}
            for sym_key, bars in resp_data.items():
                if isinstance(sym_key, (list, tuple)):
                    for s in sym_key:
                        target_dict.setdefault(str(s), []).extend(bars)
                else:
                    target_dict.setdefault(str(sym_key), []).extend(bars)
        except Exception as exc:
            logger.error(f"Failed to retrieve {feed_label} bars: {exc}")
            raise

    # Save raw retrieved bars to isolated research storage
    os.makedirs("data/cache/experiment_a", exist_ok=True)
    with open("data/cache/experiment_a/sip_raw_bars.json", "w", encoding="utf-8") as f:
        json.dump({sym: [{"timestamp": b.timestamp.isoformat(), "open": b.open, "high": b.high, "low": b.low, "close": b.close, "volume": b.volume, "vwap": getattr(b, "vwap", None)} for b in bars] for sym, bars in sip_bars_data.items()}, f, indent=2)

    with open("data/cache/experiment_a/iex_raw_bars.json", "w", encoding="utf-8") as f:
        json.dump({sym: [{"timestamp": b.timestamp.isoformat(), "open": b.open, "high": b.high, "low": b.low, "close": b.close, "volume": b.volume, "vwap": getattr(b, "vwap", None)} for b in bars] for sym, bars in iex_bars_data.items()}, f, indent=2)

    # 4. Point-in-time evaluation across 30 eval dates
    results = []
    classification_changes_count = 0

    for eval_date in eval_dates:
        for sym in symbols:
            sip_bars = sip_bars_data.get(sym, [])
            iex_bars = iex_bars_data.get(sym, [])

            sip_res = evaluate_symbol_point_in_time(sym, sip_bars, eval_date, settings, "SIP")
            iex_res = evaluate_symbol_point_in_time(sym, iex_bars, eval_date, settings, "IEX")

            # Compare metrics
            status_match = sip_res["status"] == iex_res["status"]
            if not status_match:
                classification_changes_count += 1

            results.append({
                "eval_date": eval_date.isoformat(),
                "symbol": sym,
                "stratum": stratum_map[sym],
                "sip": sip_res,
                "iex": iex_res,
                "status_match": status_match,
            })

    output_payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "evaluation_sessions_count": len(eval_dates),
        "symbols_count": len(symbols),
        "classification_changes_count": classification_changes_count,
        "results": results,
    }

    results_path = "tests/experiment_a_pilot_results.json"
    os.makedirs(os.path.dirname(results_path), exist_ok=True)
    with open(results_path, "w", encoding="utf-8") as f:
        json.dump(output_payload, f, indent=2)

    logger.info(f"Experiment A pilot complete. Results saved to {results_path}. Total classification mismatches: {classification_changes_count}")


if __name__ == "__main__":
    main()
