from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from money_mintter.domain import Side
from money_mintter.strategy import Candle, LiquiditySweepReversalStrategy, StrategyInput


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    initial_capital: float = 10_000.0
    risk_per_trade_pct: float = 0.005
    max_position_pct: float = 0.20
    spread_bps: float = 5.0
    slippage_bps: float = 5.0
    commission_bps: float = 2.0
    max_holding_bars: int = 60

    def __post_init__(self) -> None:
        if self.initial_capital <= 0:
            raise ValueError("initial capital must be positive")
        if not 0 < self.risk_per_trade_pct <= 1:
            raise ValueError("risk per trade must be in (0, 1]")
        if not 0 < self.max_position_pct <= 1:
            raise ValueError("max position must be in (0, 1]")
        if min(self.spread_bps, self.slippage_bps, self.commission_bps) < 0:
            raise ValueError("execution costs cannot be negative")
        if self.max_holding_bars <= 0:
            raise ValueError("max holding bars must be positive")


@dataclass(frozen=True, slots=True)
class BacktestTrade:
    symbol: str
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
    entry_index: int
    entry_time: object
    quantity: float
    entry_price: float
    stop_price: float
    target_price: float
    entry_fee: float
    risk_dollars: float


ContextProvider = Callable[[int], StrategyInput | None]


class StrategyBacktester:
    """Event-order-aware long-only backtester for the current strategy.

    The strategy may signal only after a candle closes. The backtester therefore
    executes that signal on the following candle's open, with spread/slippage and
    commission. Stop/target collisions inside one bar are resolved pessimistically
    by assuming the stop was hit first.
    """

    def __init__(
        self,
        strategy: LiquiditySweepReversalStrategy,
        config: BacktestConfig | None = None,
    ) -> None:
        self.strategy = strategy
        self.config = config or BacktestConfig()

    def run(
        self,
        candles: list[Candle],
        context_provider: ContextProvider,
    ) -> BacktestReport:
        if not candles:
            raise ValueError("backtest requires at least one candle")

        cfg = self.config
        cash = cfg.initial_capital
        open_trade: _OpenTrade | None = None
        pending_signal = None
        trades: list[BacktestTrade] = []
        equity_curve = [cash]

        for index, bar in enumerate(candles):
            if pending_signal is not None and open_trade is None:
                signal = pending_signal
                pending_signal = None
                if signal.side is not Side.BUY:
                    continue

                raw_entry = bar.open
                execution_penalty = cfg.spread_bps / 20_000 + cfg.slippage_bps / 10_000
                entry_price = raw_entry * (1 + execution_penalty)
                risk_per_share = entry_price - signal.stop_price
                if risk_per_share <= 0:
                    continue

                risk_budget = cash * cfg.risk_per_trade_pct
                max_notional = cash * cfg.max_position_pct
                quantity = min(
                    risk_budget / risk_per_share,
                    max_notional / entry_price,
                )
                if quantity <= 0:
                    continue

                entry_fee = entry_price * quantity * cfg.commission_bps / 10_000
                total_entry_cost = entry_price * quantity + entry_fee
                if total_entry_cost > cash:
                    quantity = max(0.0, (cash - entry_fee) / entry_price)
                    if quantity <= 0:
                        continue
                    entry_fee = entry_price * quantity * cfg.commission_bps / 10_000
                    total_entry_cost = entry_price * quantity + entry_fee

                cash -= total_entry_cost
                open_trade = _OpenTrade(
                    symbol=signal.symbol,
                    entry_index=index,
                    entry_time=bar.timestamp,
                    quantity=quantity,
                    entry_price=entry_price,
                    stop_price=signal.stop_price,
                    target_price=signal.target_price,
                    entry_fee=entry_fee,
                    risk_dollars=risk_per_share * quantity,
                )

            if open_trade is not None:
                reason: str | None = None
                raw_exit: float | None = None

                # Gap checks use the bar open first.
                if bar.open <= open_trade.stop_price:
                    reason, raw_exit = "stop_gap", bar.open
                elif bar.open >= open_trade.target_price:
                    reason, raw_exit = "target_gap", bar.open
                # Inside the bar, stop is deliberately evaluated before target.
                elif bar.low <= open_trade.stop_price and bar.high >= open_trade.target_price:
                    reason, raw_exit = "stop_and_target_same_bar", open_trade.stop_price
                elif bar.low <= open_trade.stop_price:
                    reason, raw_exit = "stop", open_trade.stop_price
                elif bar.high >= open_trade.target_price:
                    reason, raw_exit = "target", open_trade.target_price
                elif index - open_trade.entry_index >= cfg.max_holding_bars:
                    reason, raw_exit = "time_exit", bar.close

                if raw_exit is not None:
                    exit_price = raw_exit * (1 - execution_penalty)
                    exit_fee = exit_price * open_trade.quantity * cfg.commission_bps / 10_000
                    proceeds = exit_price * open_trade.quantity - exit_fee
                    cash += proceeds

                    pnl = (
                        (exit_price - open_trade.entry_price) * open_trade.quantity
                        - open_trade.entry_fee
                        - exit_fee
                    )
                    r_multiple = (
                        pnl / open_trade.risk_dollars
                        if open_trade.risk_dollars > 0
                        else 0.0
                    )
                    trades.append(
                        BacktestTrade(
                            symbol=open_trade.symbol,
                            entry_index=open_trade.entry_index,
                            exit_index=index,
                            entry_time=open_trade.entry_time,
                            exit_time=bar.timestamp,
                            quantity=open_trade.quantity,
                            entry_price=open_trade.entry_price,
                            exit_price=exit_price,
                            stop_price=open_trade.stop_price,
                            target_price=open_trade.target_price,
                            pnl=pnl,
                            r_multiple=r_multiple,
                            exit_reason=reason,
                            entry_fee=open_trade.entry_fee,
                            exit_fee=exit_fee,
                        )
                    )
                    open_trade = None

            mark_value = cash
            if open_trade is not None:
                mark_value += open_trade.quantity * bar.close
            equity_curve.append(mark_value)

            if open_trade is None and index + 1 < len(candles):
                context = context_provider(index)
                if context is not None:
                    pending_signal = self.strategy.signal(candles[: index + 1], context)

        if open_trade is not None:
            final_bar = candles[-1]
            execution_penalty = cfg.spread_bps / 20_000 + cfg.slippage_bps / 10_000
            exit_price = final_bar.close * (1 - execution_penalty)
            exit_fee = exit_price * open_trade.quantity * cfg.commission_bps / 10_000
            cash += exit_price * open_trade.quantity - exit_fee
            pnl = (
                (exit_price - open_trade.entry_price) * open_trade.quantity
                - open_trade.entry_fee
                - exit_fee
            )
            r_multiple = pnl / open_trade.risk_dollars if open_trade.risk_dollars > 0 else 0.0
            trades.append(
                BacktestTrade(
                    symbol=open_trade.symbol,
                    entry_index=open_trade.entry_index,
                    exit_index=len(candles) - 1,
                    entry_time=open_trade.entry_time,
                    exit_time=final_bar.timestamp,
                    quantity=open_trade.quantity,
                    entry_price=open_trade.entry_price,
                    exit_price=exit_price,
                    stop_price=open_trade.stop_price,
                    target_price=open_trade.target_price,
                    pnl=pnl,
                    r_multiple=r_multiple,
                    exit_reason="end_of_data",
                    entry_fee=open_trade.entry_fee,
                    exit_fee=exit_fee,
                )
            )

        ending_capital = cash
        wins = sum(1 for trade in trades if trade.pnl > 0)
        losses = sum(1 for trade in trades if trade.pnl < 0)
        gross_profit = sum(trade.pnl for trade in trades if trade.pnl > 0)
        gross_loss = -sum(trade.pnl for trade in trades if trade.pnl < 0)
        r_values = [trade.r_multiple for trade in trades]

        peak = equity_curve[0]
        max_drawdown = 0.0
        for equity in equity_curve:
            peak = max(peak, equity)
            if peak > 0:
                max_drawdown = max(max_drawdown, (peak - equity) / peak)

        max_consecutive_losses = 0
        current_losses = 0
        for trade in trades:
            if trade.pnl < 0:
                current_losses += 1
                max_consecutive_losses = max(max_consecutive_losses, current_losses)
            else:
                current_losses = 0

        win_rs = [trade.r_multiple for trade in trades if trade.pnl > 0]
        loss_rs = [trade.r_multiple for trade in trades if trade.pnl < 0]

        return BacktestReport(
            initial_capital=cfg.initial_capital,
            ending_capital=ending_capital,
            net_pnl=ending_capital - cfg.initial_capital,
            return_pct=(ending_capital / cfg.initial_capital - 1) * 100,
            trade_count=len(trades),
            wins=wins,
            losses=losses,
            win_rate=wins / len(trades) if trades else 0.0,
            gross_profit=gross_profit,
            gross_loss=gross_loss,
            profit_factor=(
                gross_profit / gross_loss if gross_loss > 0 else float("inf")
            ),
            expectancy_r=sum(r_values) / len(r_values) if r_values else 0.0,
            average_win_r=sum(win_rs) / len(win_rs) if win_rs else 0.0,
            average_loss_r=sum(loss_rs) / len(loss_rs) if loss_rs else 0.0,
            max_drawdown_pct=max_drawdown * 100,
            max_consecutive_losses=max_consecutive_losses,
            trades=tuple(trades),
        )
