from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from money_mintter.domain import Side
from money_mintter.strategy import Candle, LiquiditySweepReversalStrategy, MultiTimeframeSeries


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    initial_capital: float = 10_000.0
    risk_per_trade_pct: float = 0.005
    max_position_pct: float = 0.20
    spread_bps: float = 5.0
    slippage_bps: float = 5.0
    commission_bps: float = 2.0
    gap_slippage_bps: float = 10.0
    max_holding_minutes: int = 120
    session_start: time = time(9, 30)
    session_end: time = time(16, 0)
    exclude_opening_minutes: int = 0
    evaluation_start: date | None = None
    evaluation_end: date | None = None
    timezone_name: str = "America/New_York"

    def __post_init__(self) -> None:
        if self.initial_capital <= 0:
            raise ValueError("initial capital must be positive")
        if not 0 < self.risk_per_trade_pct <= 1:
            raise ValueError("risk per trade must be in (0, 1]")
        if not 0 < self.max_position_pct <= 1:
            raise ValueError("max position must be in (0, 1]")
        if min(self.spread_bps, self.slippage_bps, self.commission_bps, self.gap_slippage_bps) < 0:
            raise ValueError("execution costs cannot be negative")
        if self.max_holding_minutes <= 0:
            raise ValueError("max holding time must be positive")
        if self.session_start >= self.session_end:
            raise ValueError("session start must precede session end")
        if self.exclude_opening_minutes < 0:
            raise ValueError("exclude opening minutes cannot be negative")
        if self.evaluation_start and self.evaluation_end and self.evaluation_start > self.evaluation_end:
            raise ValueError("evaluation start must precede evaluation end")


class StrategyLike(Protocol):
    config: object

    def signal(self, snapshot): ...


@dataclass(frozen=True, slots=True)
class BacktestTrade:
    symbol: str
    side: Side
    entry_index: int
    exit_index: int
    entry_time: object
    exit_time: object
    quantity: float
    entry_price: float
    exit_price: float
    stop_price: float
    target_price: float
    pnl: float
    r_multiple: float
    exit_reason: str
    entry_fee: float
    exit_fee: float


@dataclass(frozen=True, slots=True)
class BacktestReport:
    initial_capital: float
    ending_capital: float
    net_pnl: float
    return_pct: float
    trade_count: int
    wins: int
    losses: int
    win_rate: float
    gross_profit: float
    gross_loss: float
    profit_factor: float
    expectancy_r: float
    average_win_r: float
    average_loss_r: float
    max_drawdown_pct: float
    max_consecutive_losses: int
    trades: tuple[BacktestTrade, ...]


@dataclass(slots=True)
class _OpenTrade:
    symbol: str
    side: Side
    entry_index: int
    entry_time: object
    quantity: float
    entry_price: float
    stop_price: float
    target_price: float
    entry_fee: float
    risk_dollars: float


class StrategyBacktester:
    """Run MTF analysis on closed HTF/entry bars with 1-minute execution."""

    def __init__(
        self,
        strategy: StrategyLike | LiquiditySweepReversalStrategy,
        config: BacktestConfig | None = None,
    ) -> None:
        self.strategy = strategy
        self.config = config or BacktestConfig()

    def run(self, candles: list[Candle]) -> BacktestReport:
        if not candles:
            raise ValueError("backtest requires 5-minute candles (1m forbidden)")

        ordered = sorted(candles, key=lambda candle: candle.timestamp)
        entry_timeframe = getattr(self.strategy.config, "entry_timeframe", None)
        if entry_timeframe is None:
            raise ValueError("strategy must expose an entry_timeframe")

        series = MultiTimeframeSeries.from_5m(
            ordered,
            entry_timeframe=entry_timeframe,
            timezone_name=self.config.timezone_name,
            spread_bps=self.config.spread_bps,
        )
        timezone = ZoneInfo(self.config.timezone_name)
        penalty = self.config.spread_bps / 20_000 + self.config.slippage_bps / 10_000

        signals_by_execution_time = {}
        for entry_bar in series.entry.bars:
            as_of = entry_bar.timestamp + timedelta(minutes=series.entry.timeframe.minutes)
            if as_of > series.source_end:
                continue
            local = as_of.astimezone(timezone)
            if not self._within_session(local.time()) or not self._within_evaluation_window(local.date()):
                continue
            signal = self.strategy.signal(series.snapshot_at(as_of, lookback_bars=96))
            if signal is not None:
                signals_by_execution_time[as_of] = signal

        cash = self.config.initial_capital
        open_trade: _OpenTrade | None = None
        trades: list[BacktestTrade] = []
        equity_curve = [cash]

        for index, bar in enumerate(ordered):
            local = bar.timestamp.astimezone(timezone)
            local_time = local.time()

            if (
                open_trade is None
                and self._within_session(local_time)
                and self._within_evaluation_window(local.date())
            ):
                signal = signals_by_execution_time.get(bar.timestamp)
                if signal is not None:
                    opened = self._open_trade(signal, bar, cash, penalty, index)
                    if opened is not None:
                        open_trade = opened
                        if signal.side is Side.BUY:
                            cash -= open_trade.entry_price * open_trade.quantity + open_trade.entry_fee
                        else:
                            cash -= open_trade.entry_fee

            if open_trade is not None:
                reason: str | None = None
                raw_exit: float | None = None

                session_close_bar = (
                    local_time >= self.config.session_end
                    or (
                        local_time < self.config.session_end
                        and bar.timestamp + timedelta(minutes=1)
                        >= local.replace(
                            hour=self.config.session_end.hour,
                            minute=self.config.session_end.minute,
                            second=0,
                            microsecond=0,
                        )
                    )
                )

                if open_trade.side is Side.BUY:
                    if bar.open <= open_trade.stop_price:
                        reason, raw_exit = "stop_gap", bar.open
                    elif bar.open >= open_trade.target_price:
                        reason, raw_exit = "target_gap", bar.open
                    elif bar.low <= open_trade.stop_price and bar.high >= open_trade.target_price:
                        reason, raw_exit = "stop_and_target_same_bar", open_trade.stop_price
                    elif bar.low <= open_trade.stop_price:
                        reason, raw_exit = "stop", open_trade.stop_price
                    elif bar.high >= open_trade.target_price:
                        reason, raw_exit = "target", open_trade.target_price
                else:
                    if bar.open >= open_trade.stop_price:
                        reason, raw_exit = "stop_gap", bar.open
                    elif bar.open <= open_trade.target_price:
                        reason, raw_exit = "target_gap", bar.open
                    elif bar.high >= open_trade.stop_price and bar.low <= open_trade.target_price:
                        reason, raw_exit = "stop_and_target_same_bar", open_trade.stop_price
                    elif bar.high >= open_trade.stop_price:
                        reason, raw_exit = "stop", open_trade.stop_price
                    elif bar.low <= open_trade.target_price:
                        reason, raw_exit = "target", open_trade.target_price

                holding_minutes = (
                    bar.timestamp - ordered[open_trade.entry_index].timestamp
                ).total_seconds() / 60
                if raw_exit is None and holding_minutes >= self.config.max_holding_minutes:
                    reason, raw_exit = "time_exit", bar.close
                if raw_exit is None and session_close_bar:
                    reason, raw_exit = "session_exit", bar.close

                mark_value = (
                    cash + open_trade.quantity * bar.close
                    if open_trade.side is Side.BUY
                    else cash + (open_trade.entry_price - bar.close) * open_trade.quantity
                )

                if raw_exit is not None:
                    effective_raw_exit = raw_exit
                    if reason == "stop_gap":
                        gap_penalty = self.config.gap_slippage_bps / 10_000
                        effective_raw_exit = (
                            raw_exit * (1 - gap_penalty)
                            if open_trade.side is Side.BUY
                            else raw_exit * (1 + gap_penalty)
                        )
                    exit_price = (
                        effective_raw_exit * (1 - penalty)
                        if open_trade.side is Side.BUY
                        else effective_raw_exit * (1 + penalty)
                    )
                    exit_fee = exit_price * open_trade.quantity * self.config.commission_bps / 10_000
                    pnl_before_fees = (
                        (exit_price - open_trade.entry_price) * open_trade.quantity
                        if open_trade.side is Side.BUY
                        else (open_trade.entry_price - exit_price) * open_trade.quantity
                    )
                    if open_trade.side is Side.BUY:
                        cash += exit_price * open_trade.quantity - exit_fee
                    else:
                        cash += pnl_before_fees - exit_fee

                    pnl = pnl_before_fees - open_trade.entry_fee - exit_fee
                    r_multiple = pnl / open_trade.risk_dollars if open_trade.risk_dollars else 0.0
                    trades.append(
                        BacktestTrade(
                            open_trade.symbol,
                            open_trade.side,
                            open_trade.entry_index,
                            index,
                            open_trade.entry_time,
                            bar.timestamp,
                            open_trade.quantity,
                            open_trade.entry_price,
                            exit_price,
                            open_trade.stop_price,
                            open_trade.target_price,
                            pnl,
                            r_multiple,
                            reason or "unknown",
                            open_trade.entry_fee,
                            exit_fee,
                        )
                    )
                    open_trade = None
                    equity_curve.append(cash)
                else:
                    equity_curve.append(mark_value)
            else:
                equity_curve.append(cash)

        if open_trade is not None:
            final_bar = ordered[-1]
            exit_price = (
                final_bar.close * (1 - penalty)
                if open_trade.side is Side.BUY
                else final_bar.close * (1 + penalty)
            )
            pnl_before_fees = (
                (exit_price - open_trade.entry_price) * open_trade.quantity
                if open_trade.side is Side.BUY
                else (open_trade.entry_price - exit_price) * open_trade.quantity
            )
            exit_fee = exit_price * open_trade.quantity * self.config.commission_bps / 10_000
            if open_trade.side is Side.BUY:
                cash += exit_price * open_trade.quantity - exit_fee
            else:
                cash += pnl_before_fees - exit_fee
            pnl = pnl_before_fees - open_trade.entry_fee - exit_fee
            trades.append(
                BacktestTrade(
                    open_trade.symbol,
                    open_trade.side,
                    open_trade.entry_index,
                    len(ordered) - 1,
                    open_trade.entry_time,
                    final_bar.timestamp,
                    open_trade.quantity,
                    open_trade.entry_price,
                    exit_price,
                    open_trade.stop_price,
                    open_trade.target_price,
                    pnl,
                    pnl / open_trade.risk_dollars if open_trade.risk_dollars else 0.0,
                    "end_of_data",
                    open_trade.entry_fee,
                    exit_fee,
                )
            )

        wins = sum(trade.pnl > 0 for trade in trades)
        losses = sum(trade.pnl < 0 for trade in trades)
        gross_profit = sum(trade.pnl for trade in trades if trade.pnl > 0)
        gross_loss = -sum(trade.pnl for trade in trades if trade.pnl < 0)
        r_values = [trade.r_multiple for trade in trades]

        peak = equity_curve[0]
        max_drawdown = 0.0
        for equity in equity_curve:
            peak = max(peak, equity)
            if peak > 0:
                max_drawdown = max(max_drawdown, (peak - equity) / peak)

        streak = 0
        max_streak = 0
        for trade in trades:
            if trade.pnl < 0:
                streak += 1
                max_streak = max(max_streak, streak)
            else:
                streak = 0

        win_rs = [trade.r_multiple for trade in trades if trade.pnl > 0]
        loss_rs = [trade.r_multiple for trade in trades if trade.pnl < 0]

        return BacktestReport(
            self.config.initial_capital,
            cash,
            cash - self.config.initial_capital,
            (cash / self.config.initial_capital - 1) * 100,
            len(trades),
            wins,
            losses,
            wins / len(trades) if trades else 0.0,
            gross_profit,
            gross_loss,
            gross_profit / gross_loss if gross_loss > 0 else float("inf"),
            sum(r_values) / len(r_values) if r_values else 0.0,
            sum(win_rs) / len(win_rs) if win_rs else 0.0,
            sum(loss_rs) / len(loss_rs) if loss_rs else 0.0,
            max_drawdown * 100,
            max_streak,
            tuple(trades),
        )

    def _open_trade(self, signal, bar: Candle, cash: float, penalty: float, entry_index: int) -> _OpenTrade | None:
        if signal.side is Side.BUY:
            entry_price = bar.open * (1 + penalty)
            risk_per_share = entry_price - signal.stop_price
        else:
            entry_price = bar.open * (1 - penalty)
            risk_per_share = signal.stop_price - entry_price
        if risk_per_share <= 0:
            return None

        risk_budget = cash * self.config.risk_per_trade_pct
        max_notional = cash * self.config.max_position_pct
        quantity = min(risk_budget / risk_per_share, max_notional / entry_price)
        if quantity <= 0:
            return None

        entry_fee = entry_price * quantity * self.config.commission_bps / 10_000
        if signal.side is Side.BUY and entry_price * quantity + entry_fee > cash:
            quantity = max(0.0, (cash - entry_fee) / entry_price)
            entry_fee = entry_price * quantity * self.config.commission_bps / 10_000
        if signal.side is Side.SELL and entry_fee > cash:
            return None
        if quantity <= 0:
            return None

        return _OpenTrade(
            signal.symbol,
            signal.side,
            entry_index,
            bar.timestamp,
            quantity,
            entry_price,
            signal.stop_price,
            signal.target_price,
            entry_fee,
            risk_per_share * quantity,
        )

    def _within_session(self, local_time: time) -> bool:
        if not (self.config.session_start <= local_time < self.config.session_end):
            return False
        if self.config.exclude_opening_minutes > 0:
            open_dt = datetime.combine(date.today(), self.config.session_start) + timedelta(minutes=self.config.exclude_opening_minutes)
            if local_time < open_dt.time():
                return False
        return True

    def _within_evaluation_window(self, local_date: date) -> bool:
        return (
            (self.config.evaluation_start is None or local_date >= self.config.evaluation_start)
            and (self.config.evaluation_end is None or local_date <= self.config.evaluation_end)
        )
