from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, date as dt_date
from typing import Any, Sequence
from zoneinfo import ZoneInfo

from money_mintter.domain import Side
from money_mintter.strategy.candles import Candle
from money_mintter.strategy.ict.adapter import adapt_market_data, bar_trading_date, ICTAdapterResult, ICTAcquisitionResult
from money_mintter.strategy.ict.v106_dynamic_rr_zone_entry import generate_all, get_liquidity_levels, _G, INST
from money_mintter.strategy.ict.backtest_entry_modes import gen_sweep_entries_enriched, apply_entry_mode, MODE_CLOSE_ENTRY
import money_mintter.strategy.ict.backtest_entry_modes as bem


@dataclass(frozen=True, slots=True)
class DiagnosticICTSignal:
    """Diagnostic-only ICT signal. Never executable as TradeSignal."""
    symbol: str
    date: dt_date
    side: str
    timestamp_ns: int
    entry_price: float
    stop_loss: float
    risk: float
    score: int
    zone_type: str
    rejection: bool
    hour: int
    cisd_passed: bool
    structure_15m_passed: bool
    sweep_15m: bool
    is_diagnostic: bool = field(default=True, metadata={"description": "Diagnostic only, non-executable"})


# Using RLock to support safe re-entrant wrapper calls.
# Note: This lock only protects calls made through run_ict_integration().
# Direct external calls to the underlying original strategy bypass this lock;
# universal thread safety is not claimed.
_ICT_LOCK = threading.RLock()


def run_ict_integration(
    acquisition_result: ICTAcquisitionResult,
    as_of: datetime,
    evaluation_dates: Sequence[dt_date],
) -> tuple[ICTAdapterResult, tuple[DiagnosticICTSignal, ...]]:
    """
    Run single-symbol ICT integration in a strictly controlled, thread-safe (via RLock),
    state-isolated manner. Produces diagnostic-only signals.
    Consumes validated ICTAcquisitionResult (rejecting non-IEX or invalid data).
    """
    adapter_result = adapt_market_data(
        acquisition_result=acquisition_result,
        as_of=as_of,
    )

    with _ICT_LOCK:
        prev_nq = _G.get("NQ", None)
        has_prev_nq = "NQ" in _G

        # Correct original _G tuple structure:
        # Index 0: tp_ (tick prices) -> None (tick simulation not invoked)
        # Index 1: tt_ (tick timestamps) -> None (tick simulation not invoked)
        # Index 2: candle dictionary {"1m": ..., "5m": ..., "15m": ...}
        # Index 3: dr5
        # Index 4: auxiliary data -> None
        # Index 5: all_dates
        # Index 6: auxiliary data -> None
        # Index 7: dr15
        mock_nq_data = (
            None,
            None,
            {
                "1m": adapter_result.b1,
                "5m": adapter_result.b5,
                "15m": adapter_result.b15,
            },
            adapter_result.dr5,
            None,
            adapter_result.all_dates,
            None,
            adapter_result.dr15,
        )

        _G["NQ"] = mock_nq_data

        try:
            raw_signals = generate_all(set(evaluation_dates))
        finally:
            if has_prev_nq:
                _G["NQ"] = prev_nq
            else:
                _G.pop("NQ", None)

    diagnostic_signals = []
    for sig in raw_signals:
        diagnostic_signals.append(
            DiagnosticICTSignal(
                symbol=acquisition_result.symbol,
                date=sig["date"],
                side=sig["side"],
                timestamp_ns=sig["t"],
                entry_price=sig["e"],
                stop_loss=sig["sp"],
                risk=sig["risk"],
                score=sig["score"],
                zone_type=sig["zt"],
                rejection=sig["rej"],
                hour=sig["hour"],
                cisd_passed=sig["cisd"],
                structure_15m_passed=sig["struct_15m"],
                sweep_15m=sig["sw15"],
                is_diagnostic=True,
            )
        )

    return adapter_result, tuple(diagnostic_signals)


@dataclass
class ICTDeduplicationTracker:
    """Persistent per-symbol deduplication and execution lifecycle tracker."""
    generated_timestamps: set[tuple[str, int]] = field(default_factory=set)
    executed_zones: set[tuple[str, str, str, float, float]] = field(default_factory=set)
    pending_symbols: set[str] = field(default_factory=set)
    pending_zones: set[tuple[str, str, str, float, float]] = field(default_factory=set)

    def is_generated(self, symbol: str, raw_ns: int) -> bool:
        return (symbol, raw_ns) in self.generated_timestamps

    def mark_generated(self, symbol: str, raw_ns: int) -> None:
        self.generated_timestamps.add((symbol, raw_ns))

    def is_executed(self, symbol: str, side: str, zone_type: str, zone_top: float, zone_bot: float) -> bool:
        zone_key = (symbol, side, zone_type, round(zone_top, 4), round(zone_bot, 4))
        return zone_key in self.executed_zones

    def mark_executed(self, symbol: str, side: str, zone_type: str, zone_top: float, zone_bot: float) -> None:
        zone_key = (symbol, side, zone_type, round(zone_top, 4), round(zone_bot, 4))
        self.executed_zones.add(zone_key)
        self.clear_pending(symbol, side, zone_type, zone_top, zone_bot)

    def is_pending(self, symbol: str, side: str, zone_type: str, zone_top: float, zone_bot: float) -> bool:
        zone_key = (symbol, side, zone_type, round(zone_top, 4), round(zone_bot, 4))
        norm_sym = symbol.upper().replace("-", "").replace("/", "").strip()
        return norm_sym in self.pending_symbols or zone_key in self.pending_zones

    def mark_pending(self, symbol: str, side: str, zone_type: str, zone_top: float, zone_bot: float) -> None:
        zone_key = (symbol, side, zone_type, round(zone_top, 4), round(zone_bot, 4))
        norm_sym = symbol.upper().replace("-", "").replace("/", "").strip()
        self.pending_symbols.add(norm_sym)
        self.pending_zones.add(zone_key)

    def clear_pending(self, symbol: str, side: str, zone_type: str, zone_top: float, zone_bot: float) -> None:
        zone_key = (symbol, side, zone_type, round(zone_top, 4), round(zone_bot, 4))
        norm_sym = symbol.upper().replace("-", "").replace("/", "").strip()
        self.pending_symbols.discard(norm_sym)
        self.pending_zones.discard(zone_key)

    def resolve_pending_to_executed_for_symbol(self, symbol: str) -> None:
        norm_sym = symbol.upper().replace("-", "").replace("/", "").strip()
        self.pending_symbols.discard(norm_sym)
        to_move = [z for z in self.pending_zones if z[0].upper().replace("-", "").replace("/", "").strip() == norm_sym]
        for z in to_move:
            self.pending_zones.discard(z)
            self.executed_zones.add(z)


@dataclass(frozen=True, slots=True)
class ICTExecutionInstruction:
    """Executable production ICT trading instruction."""
    symbol: str
    side: Side
    entry_price: float
    stop_price: float
    target_price: float
    risk_pts: float
    timestamp: datetime
    timestamp_ns: int
    zone_type: str
    score: int
    zone_top: float
    zone_bot: float
    reason: str
    is_diagnostic: bool = field(default=False)


def generate_production_ict_signals(
    acquisition_result: ICTAcquisitionResult,
    as_of: datetime,
    evaluation_dates: Sequence[dt_date] | None = None,
    dedup_tracker: ICTDeduplicationTracker | None = None,
    _allow_historical_evaluation: bool = False,
    diagnostics: dict | None = None,
) -> tuple[ICTAdapterResult, tuple[ICTExecutionInstruction, ...]]:
    """
    Run production ICT signal generation bridge:
    - Adapts IEX market data with lookahead protection.
    - Resolves current session using bar_trading_date semantics (fails closed if unresolvable in production; historical evaluation restricted to testing via evaluation_dates and _allow_historical_evaluation).
    - Computes liquidity levels using get_liquidity_levels().
    - Calls gen_sweep_entries_enriched() ONCE per session with b5[:de5] boundary.
    - Applies MODE_CLOSE_ENTRY with zero slip (0.0) and infinite max risk (inf) without mutating globals.
    - Enforces IFVG time restriction (skip before 9:00 CT) based on final signal time.
    - Forces production baseline RR = 1.1 and calculates target price.
    - Enforces persistent timestamp and zone lifecycle deduplication via dedup_tracker (distinguishing discovery/generation from execution).
    """
    adapter_result = adapt_market_data(
        acquisition_result=acquisition_result,
        as_of=as_of,
    )

    if dedup_tracker is None:
        dedup_tracker = ICTDeduplicationTracker()

    if evaluation_dates is not None:
        if not _allow_historical_evaluation:
            raise ValueError("Historical multi-date evaluation via evaluation_dates is restricted to testing-only and cannot be used in production.")
        target_dates = [d for d in evaluation_dates if d in adapter_result.dr5]
    else:
        # Production: resolve and process only the current ICT strategy session using upstream trading-date semantics
        chicago_now = as_of.astimezone(ZoneInfo("America/Chicago"))
        curr_date = bar_trading_date(chicago_now)
        if curr_date not in adapter_result.dr5:
            raise ValueError(f"Current session date {curr_date} cannot be resolved or validated in adapter data. Failing closed.")
        target_dates = [curr_date]

    instructions: list[ICTExecutionInstruction] = []
    symbol = acquisition_result.symbol
    raw_entries_count = 0
    entry_mode_passes_count = 0

    with _ICT_LOCK:
        for d in target_dates:
            if d not in adapter_result.dr5:
                continue
            ds5, de5 = adapter_result.dr5[d]
            liq = get_liquidity_levels(adapter_result.b5, adapter_result.dr5, d, adapter_result.all_dates)

            # Reproduce upstream production data boundary b5[:de5]
            raw_entries = gen_sweep_entries_enriched(
                adapter_result.b5[:de5],
                adapter_result.b1,
                ds5,
                de5,
                d,
                liq,
            )
            raw_entries_count += len(raw_entries)

            # Sort entries using original upstream ordering: (ns, zone priority) with IFVG priority over displacement Fvg
            sorted_entries = sorted(
                raw_entries,
                key=lambda x: (x["ns"], -{"ifvg": 2, "disp_fvg": 1}.get(x["zt"], 0))
            )

            for raw in sorted_entries:
                raw_ns = raw["ns"]
                zone_type = raw["zt"]
                side_str = raw["side"]  # "bull" or "bear"

                sig = apply_entry_mode(
                    raw,
                    MODE_CLOSE_ENTRY,
                    adapter_result.b1,
                    adapter_result.b5[:de5],
                    ds5,
                    liq,
                    adapter_result.dr15,
                    adapter_result.b15,
                    d,
                    slip=0.0,
                    max_risk=float("inf"),
                )
                if sig is None:
                    continue
                entry_mode_passes_count += 1

                # Inspect resulting signal's time for IFVG time restriction (skip before 9:00 CT)
                sig_time = sig["time"]
                if zone_type == "ifvg" and (sig_time.hour < 9 or (sig_time.hour == 9 and sig_time.minute < 0)):
                    continue

                zone_top = sig["zone_top"]
                zone_bot = sig["zone_bot"]

                if dedup_tracker.is_executed(symbol, side_str, zone_type, zone_top, zone_bot):
                    continue

                if side_str == "bull":
                    side = Side.BUY
                    direction = 1.0
                else:
                    side = Side.SELL
                    direction = -1.0

                risk_pts = sig["risk_pts"]
                entry_price = sig["entry"]
                stop_price = sig["stop"]
                # Fixed production RR = 1.1 regardless of diagnostic score
                rr = 1.1
                target_price = entry_price + risk_pts * rr * direction

                instructions.append(
                    ICTExecutionInstruction(
                        symbol=symbol,
                        side=side,
                        entry_price=entry_price,
                        stop_price=stop_price,
                        target_price=target_price,
                        risk_pts=risk_pts,
                        timestamp=sig_time,
                        timestamp_ns=raw_ns,
                        zone_type=zone_type,
                        score=sig["score"],
                        zone_top=zone_top,
                        zone_bot=zone_bot,
                        reason=f"ict_{zone_type}_sweep_enriched",
                        is_diagnostic=False,
                    )
                )

    if diagnostics is not None:
        diagnostics["raw_entries_count"] = raw_entries_count
        diagnostics["entry_mode_passes_count"] = entry_mode_passes_count

    return adapter_result, tuple(instructions)
