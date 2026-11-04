from __future__ import annotations

import os
import sys
import json
import asyncio
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestBarRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import DataFeed

import websockets

def print_banner(title: str):
    print("=" * 60)
    print(f" {title}")
    print("=" * 60)

async def test_websocket_strict(feed_name: str, url: str, api_key: str, secret_key: str):
    print_banner(f"Strict WebSocket Test: {feed_name} ({url})")
    auth_status = "PENDING"
    sub_status = "PENDING"
    delivery_status = "PENDING_ACTIVE_SESSION"
    error_msg = None
    received_count = 0

    try:
        async with websockets.connect(url) as ws:
            # 1. Welcome frame
            welcome = await asyncio.wait_for(ws.recv(), timeout=5.0)
            print(f"[{feed_name}] Welcome received.")

            # 2. Authentication (Zero credential output)
            auth_msg = {
                "action": "auth",
                "key": api_key,
                "secret": secret_key,
            }
            await ws.send(json.dumps(auth_msg))
            auth_resp = await asyncio.wait_for(ws.recv(), timeout=5.0)
            
            auth_parsed = json.loads(auth_resp)
            authenticated = False
            if isinstance(auth_parsed, list):
                for m in auth_parsed:
                    if m.get("status") == "authorized" or m.get("T") == "success":
                        authenticated = True
                    elif m.get("status") == "unauthorized" or m.get("T") == "error":
                        error_msg = m.get("message", "Unauthorized")
            elif isinstance(auth_parsed, dict):
                if auth_parsed.get("status") == "authorized" or auth_parsed.get("T") == "success":
                    authenticated = True
                else:
                    error_msg = auth_parsed.get("message", "Unauthorized")

            if authenticated:
                auth_status = "VERIFIED"
                print(f"[{feed_name}] Authentication: VERIFIED")
            else:
                auth_status = "AUTHENTICATION_DENIED"
                print(f"[{feed_name}] Authentication: DENIED")
                return {
                    "auth": auth_status,
                    "subscription": "SUBSCRIPTION_DENIED",
                    "data_delivery": "PENDING_ACTIVE_SESSION",
                    "error": error_msg or "Authentication failed",
                }

            # 3. Subscription
            sub_msg = {
                "action": "subscribe",
                "bars": ["AAPL", "SPY"],
            }
            await ws.send(json.dumps(sub_msg))
            sub_resp = await asyncio.wait_for(ws.recv(), timeout=5.0)

            sub_parsed = json.loads(sub_resp)
            subscription_confirmed = False
            if isinstance(sub_parsed, list):
                for m in sub_parsed:
                    if m.get("T") == "subscription":
                        bars_sub = m.get("bars", [])
                        if "AAPL" in bars_sub and "SPY" in bars_sub:
                            subscription_confirmed = True
                    elif m.get("T") == "error":
                        error_msg = m.get("msg", "Subscription error")
            elif isinstance(sub_parsed, dict):
                if sub_parsed.get("T") == "subscription":
                    bars_sub = sub_parsed.get("bars", [])
                    if "AAPL" in bars_sub and "SPY" in bars_sub:
                        subscription_confirmed = True
                elif sub_parsed.get("T") == "error":
                    error_msg = sub_parsed.get("msg", "Subscription error")

            if subscription_confirmed:
                sub_status = "VERIFIED"
                print(f"[{feed_name}] Subscription: VERIFIED")
            else:
                sub_status = "SUBSCRIPTION_DENIED"
                print(f"[{feed_name}] Subscription: DENIED")
                return {
                    "auth": auth_status,
                    "subscription": sub_status,
                    "data_delivery": "PENDING_ACTIVE_SESSION",
                    "error": error_msg or "Subscription confirmation failed",
                }

            # 4. Message delivery listening (will timeout if market is closed)
            try:
                while received_count < 3:
                    m = await asyncio.wait_for(ws.recv(), timeout=2.0)
                    received_count += 1
                delivery_status = "VERIFIED"
            except asyncio.TimeoutError:
                print(f"[{feed_name}] Data delivery timeout (market closed - marked PENDING_ACTIVE_SESSION).")
                delivery_status = "PENDING_ACTIVE_SESSION"

    except Exception as exc:
        print(f"[{feed_name}] WebSocket error: {exc}")
        error_msg = str(exc)
        if "403" in str(exc) or "Forbidden" in str(exc):
            auth_status = "SUBSCRIPTION_DENIED"
        elif auth_status == "PENDING":
            auth_status = "REQUEST_DENIED"

    return {
        "auth": auth_status,
        "subscription": sub_status,
        "data_delivery": delivery_status,
        "error": error_msg,
    }

async def main_async():
    api_key = os.getenv("APCA_API_KEY_ID") or os.getenv("APCA_API_KEY")
    secret_key = os.getenv("APCA_API_SECRET_KEY") or os.getenv("APCA_API_SECRET")

    if not api_key or not secret_key:
        print("ERROR: Alpaca credentials not found in environment.")
        sys.exit(1)

    print("API credentials loaded securely (zero credential printing enforced).")

    client = StockHistoricalDataClient(api_key=api_key, secret_key=secret_key)
    symbols = ["AAPL", "SPY", "MS", "XLI", "BSX"]

    # Explicitly set request boundaries to exact regular trading session: 14:30:00Z to 20:59:00Z
    start_dt = datetime(2026, 11, 3, 14, 30, tzinfo=timezone.utc)
    end_dt = datetime(2026, 11, 3, 20, 59, tzinfo=timezone.utc)

    results = {}

    for feed_key, feed_enum in [("historical_sip", DataFeed.SIP), ("historical_iex", DataFeed.IEX)]:
        print_banner(f"Detailed Analysis: {feed_key.upper()}")
        feed_summary = {}
        for sym in symbols:
            try:
                req = StockBarsRequest(
                    symbol_or_symbols=sym,
                    timeframe=TimeFrame(1, TimeFrameUnit.Minute),
                    start=start_dt,
                    end=end_dt,
                    feed=feed_enum,
                    adjustment="raw",
                )
                resp = client.get_stock_bars(req)
                bars = list(getattr(resp, "data", {}).get(sym, []))
                
                timestamps = [b.timestamp for b in bars]
                unique_timestamps = set(timestamps)
                duplicate_count = len(timestamps) - len(unique_timestamps)
                
                out_of_range = [
                    t for t in timestamps 
                    if t < start_dt or t > end_dt
                ]
                
                total_vol = sum(float(b.volume) for b in bars)
                total_trades = sum(int(getattr(b, "trade_count", 0) or 0) for b in bars)
                vwap_list = [float(b.vwap) for b in bars if getattr(b, "vwap", None) is not None]
                avg_vwap = sum(vwap_list) / len(vwap_list) if vwap_list else 0.0

                feed_summary[sym] = {
                    "total_records": len(bars),
                    "unique_timestamps": len(unique_timestamps),
                    "duplicate_timestamps": duplicate_count,
                    "first_timestamp": timestamps[0].isoformat() if timestamps else None,
                    "last_timestamp": timestamps[-1].isoformat() if timestamps else None,
                    "out_of_range_count": len(out_of_range),
                    "total_volume": total_vol,
                    "total_trade_count": total_trades,
                    "average_vwap": avg_vwap,
                }
                print(f"[{sym}] Records: {len(bars)}, First: {feed_summary[sym]['first_timestamp']}, Last: {feed_summary[sym]['last_timestamp']}, Vol: {total_vol:,.0f}")
            except Exception as exc:
                print(f"Error querying {sym} for {feed_key}: {exc}")
                feed_summary[sym] = {"error": str(exc)}
        results[feed_key] = feed_summary

    # Strict WebSocket Tests
    delayed_sip_url = "wss://stream.data.alpaca.markets/v2/delayed_sip"
    ws_delayed_res = await test_websocket_strict("Delayed SIP", delayed_sip_url, api_key, secret_key)
    results["delayed_sip_ws_strict"] = ws_delayed_res

    iex_url = "wss://stream.data.alpaca.markets/v2/iex"
    ws_iex_res = await test_websocket_strict("Real-time IEX", iex_url, api_key, secret_key)
    results["iex_ws_strict"] = ws_iex_res

    output_path = os.path.join(os.path.dirname(__file__), "phase0_reconciliation_results.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    print(f"\nSaved reconciliation results to {output_path}")

def main():
    asyncio.run(main_async())

if __name__ == "__main__":
    main()
