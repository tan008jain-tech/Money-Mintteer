from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from money_mintter.brokers.base import Broker
from money_mintter.core.candidates import CandidateRanker, RankedCandidate
from money_mintter.core.sessions import SessionEngine
from money_mintter.core.trade_manager import TradeEvent, TradeManager
from money_mintter.domain import Candidate, Quote, TradeSignal


@dataclass(frozen=True, slots=True)
class CycleResult:
    open_markets: tuple[str, ...]
    deep_candidates: tuple[RankedCandidate, ...]
    trade_events: tuple[TradeEvent, ...]


class MoneyMintterEngine:
    """Small event-driven orchestration layer tying the first vertical slice together."""

    def __init__(
        self,
        broker: Broker,
        trade_manager: TradeManager,
        sessions: SessionEngine,
        ranker: CandidateRanker,
    ) -> None:
        self.broker = broker
        self.trade_manager = trade_manager
        self.sessions = sessions
        self.ranker = ranker

    def cycle(
        self,
        now_utc: datetime,
        candidates: list[Candidate],
        quotes: dict[str, Quote],
        signals: list[TradeSignal],
    ) -> CycleResult:
        open_markets = tuple(self.sessions.open_markets(now_utc))
        events: list[TradeEvent] = []

        # Manage existing risk first. There is deliberately no artificial re-entry cooldown.
        # A trade that closes during this cycle is allowed to re-enter later in the same cycle
        # if the fresh candidate/signal checks still pass.
        active_symbols_before_management = set(self.trade_manager.open_trades)
        for symbol in sorted(active_symbols_before_management):
            quote = quotes.get(symbol)
            if quote is not None:
                event = self.trade_manager.process_quote(quote)
                if event is not None:
                    events.append(event)

        # Rank only symbols that are not currently active after management. This preserves
        # open positions while allowing immediate, confirmation-driven re-entry.
        active_symbols = set(self.trade_manager.open_trades)
        eligible_candidates = [
            c for c in candidates
            if c.market in open_markets and c.symbol not in active_symbols
        ]
        deep = tuple(self.ranker.rank(eligible_candidates))
        deep_symbols = {item.candidate.symbol for item in deep}

        # Only act on signals for currently selected deep candidates.
        for signal in sorted(signals, key=lambda item: (-item.confidence, item.symbol)):
            if signal.symbol not in deep_symbols:
                continue
            quote = quotes.get(signal.symbol)
            if quote is None:
                events.append(TradeEvent("REJECT", signal.symbol, "missing quote"))
                continue
            if signal.confidence < self.ranker.min_score:
                continue
            events.append(self.trade_manager.open_trade(signal, quote))

        return CycleResult(open_markets, deep, tuple(events))
