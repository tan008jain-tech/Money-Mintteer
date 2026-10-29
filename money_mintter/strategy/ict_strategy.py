from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, date as dt_date
from zoneinfo import ZoneInfo
from dataclasses import dataclass

# Ensure ICT Strategy folder is in sys.path for importing reference modules
_ICT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "ICT Strategy"))
if _ICT_DIR not in sys.path:
    sys.path.insert(0, _ICT_DIR)

from v106_dynamic_rr_zone_entry import (
    get_liquidity_levels,
    detect_sweep_at,
    cisd_5m,
    structure_15m,
    sweep_15m,
    in_kz,
    KZ,
    NS_MIN,
)
from backtest_entry_modes import (
    gen_sweep_entries_enriched,
    apply_entry_mode,
    MODE_CLOSE_ENTRY,
)

from money_mintter.domain import Side, TradeSignal
from money_mintter.strategy.candles import Candle
from money_mintter.strategy.timeframes import MultiTimeframeSnapshot, Timeframe
from money_mintter.strategy.liquidity_reversal import SetupAnalysis

CT = ZoneInfo("America/Chicago")
UTC = ZoneInfo("UTC")

def _bar_trading_date(ts_ns: int) -> dt_date:
    t = datetime.fromtimestamp(ts_ns / 1e9, tz=CT)
    d = t.date()
    if t.hour >= 17:
        d = d + timedelta(days=1)
    return d

@dataclass(frozen=True, slots=True)
class ICTStrategyConfig:
    entry_timeframe: str = "5m"
    flat_rr: float = 1.1
    allow_5m_fallback: bool = False

class ICTStrategy:
    """ICT V106 Multi-Timeframe Strategy replacing LiquiditySweepReversalStrategy."""

    VERSION = "ict.v106.mtf"

    def __init__(self, config: ICTStrategyConfig | None = None) -> None:
        self.config = config or ICTStrategyConfig()
        self._used_zones = set()
        self._last_trading_date = None

    def export_state(self) -> dict[str, Any]:
        return {
            "used_zones": [list(item) for item in self._used_zones],
            "last_trading_date": self._last_trading_date.isoformat() if self._last_trading_date else None,
        }

    def import_state(self, state: dict[str, Any] | list | tuple | None) -> None:
        if not state:
            return
        if isinstance(state, dict):
            zones = state.get("used_zones", [])
            self._used_zones = set(tuple(item) for item in zones)
            dt_str = state.get("last_trading_date")
            if dt_str:
                try:
                    self._last_trading_date = dt_date.fromisoformat(dt_str)
                except Exception:
                    self._last_trading_date = None
        elif isinstance(state, (list, tuple)):
            self._used_zones = set(tuple(item) for item in state)
            self._last_trading_date = None

    def _convert_candles(self, candles: tuple[Candle, ...]) -> list[dict]:
        bars = []
        for c in candles:
            dt = c.timestamp
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            dt_ct = dt.astimezone(CT)
            time_ns = int(dt.timestamp() * 1e9)
            bars.append({
                "time_ns": time_ns,
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
                "volume": c.volume,
                "hour": dt_ct.hour,
                "minute": dt_ct.minute,
                "date": _bar_trading_date(time_ns),
            })
        return bars

    def _build_dr(self, bars: list[dict]) -> dict:
        dr = {}
        for i, bar in enumerate(bars):
            d = bar["date"]
            if d not in dr:
                dr[d] = (i, i + 1)
            else:
                dr[d] = (dr[d][0], i + 1)
        return dr

    def analyze(self, snapshot: MultiTimeframeSnapshot) -> SetupAnalysis:
        b5_raw = self._convert_candles(snapshot.entry)
        if not b5_raw:
            return SetupAnalysis(False, 0.0, "no entry candles", side=None)

        if not snapshot.one_minute:
            if not self.config.allow_5m_fallback:
                raise ValueError(
                    "ICTStrategy requires genuine 1-minute bars in snapshot.one_minute; "
                    "5-minute fallback is disabled in production."
                )
            b1_raw = b5_raw
        else:
            b1_raw = self._convert_candles(snapshot.one_minute)

        dr5 = self._build_dr(b5_raw)
        all_dates = sorted(dr5.keys())
        if not all_dates:
            return SetupAnalysis(False, 0.0, "no trading dates", side=None)

        current_date = _bar_trading_date(int(snapshot.as_of.timestamp() * 1e9))
        if current_date not in dr5:
            current_date = all_dates[-1]

        if self._last_trading_date is not None and self._last_trading_date != current_date:
            self._used_zones.clear()
        self._last_trading_date = current_date

        ds5, de5 = dr5[current_date]
        liq_levels = get_liquidity_levels(b5_raw, dr5, current_date, all_dates)

        b15_raw = self._convert_candles(snapshot.fifteen_minute)
        dr15 = self._build_dr(b15_raw) if b15_raw else None

        raw_entries = gen_sweep_entries_enriched(
            b5_raw, b1_raw, ds5, de5, current_date, liq_levels, stop_b5=b5_raw, kz=KZ
        )

        if not raw_entries:
            return SetupAnalysis(False, 0.0, "no ICT sweep entries found", side=None)

        # Sort entries by newest timestamp first, then zone priority (ifvg > disp_fvg)
        sorted_entries = sorted(
            raw_entries,
            key=lambda x: (x["ns"], {"ifvg": 2, "disp_fvg": 1}.get(x["zt"], 0)),
            reverse=True,
        )

        best_entry = None
        side = None
        sig = None

        for e in sorted_entries:
            e_side = Side.BUY if e["side"] == "bull" else Side.SELL
            zone_key = (
                e_side,
                e["zt"],
                round(e.get("zone_top", 0.0), 2),
                round(e.get("zone_bot", 0.0), 2),
            )
            if zone_key in self._used_zones:
                continue

            candidate_sig = apply_entry_mode(
                e,
                MODE_CLOSE_ENTRY,
                b1_raw,
                b5_raw,
                ds5,
                liq_levels,
                dr15,
                b15_raw,
                current_date,
            )
            if candidate_sig:
                best_entry = e
                side = e_side
                sig = candidate_sig
                self._used_zones.add(zone_key)
                break

        if not sig or not best_entry or not side:
            return SetupAnalysis(False, 0.0, "ICT entry mode filtering or deduplication failed", side=None)

        score = sig.get("score", 1)
        normalized_confidence = min(1.0, score / 8.0)
        ep = sig["entry"]
        sp = sig["stop"]
        rr = self.config.flat_rr
        risk = abs(ep - sp)
        target = ep + (risk * rr) if side == Side.BUY else ep - (risk * rr)

        return SetupAnalysis(
            passed=True,
            score=normalized_confidence,
            reason=f"ICT V106 setup: {best_entry['zt']} sweep at {best_entry.get('swept', 'level')} (score {score})",
            entry_price=ep,
            stop_price=sp,
            target_price=target,
            risk_per_share=risk,
            reward_risk=rr,
            side=side,
            confluences=(best_entry['zt'], f"score_{score}"),
        )

    def signal(self, snapshot: MultiTimeframeSnapshot) -> TradeSignal | None:
        result = self.analyze(snapshot)
        if not result.passed or result.side is None or result.entry_price is None or result.stop_price is None or result.target_price is None:
            return None
        symbol = snapshot.entry[-1].symbol if snapshot.entry and hasattr(snapshot.entry[-1], "symbol") else "NQ"
        return TradeSignal(
            symbol=symbol,
            side=result.side,
            entry_price=result.entry_price,
            stop_price=result.stop_price,
            target_price=result.target_price,
            confidence=result.score,
            timestamp=snapshot.as_of,
            reason=result.reason,
            strategy_version=self.VERSION,
        )
