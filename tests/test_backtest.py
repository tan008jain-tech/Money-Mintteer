from datetime import datetime, timedelta, timezone

from money_mintter.domain import Side, TradeSignal
from money_mintter.research.backtest import BacktestConfig, StrategyBacktester
from money_mintter.strategy import MultiTimeframeSnapshot, StrategyConfig, Timeframe


class DummyStrategy:
    config = StrategyConfig(entry_timeframe=Timeframe.FIVE_MIN)

    def signal(self, snapshot: MultiTimeframeSnapshot):
        if snapshot.as_of.minute == 35:
            return TradeSignal(
                "ABC",
                Side.BUY,
                100.0,
                99.0,
                101.0,
                0.9,
                snapshot.as_of,
                "timing test",
            )
        return None


def minute_bar(index: int) -> "Candle":
    from money_mintter.strategy import Candle

    start = datetime(2026, 10, 1, 9, 30, tzinfo=timezone.utc)
    timestamp = start + timedelta(minutes=index)
    if index == 5:
        return Candle("ABC", timestamp, 100.2, 100.7, 100.1, 100.4, 1000)
    if index == 6:
        return Candle("ABC", timestamp, 100.5, 101.2, 100.4, 101.0, 1000)
    return Candle("ABC", timestamp, 100.0, 100.4, 99.6, 100.0, 1000)


def test_mtf_backtester_enters_on_next_one_minute_bar():
    candles = [minute_bar(i) for i in range(12)]
    report = StrategyBacktester(
        DummyStrategy(),
        BacktestConfig(
            spread_bps=0,
            slippage_bps=0,
            commission_bps=0,
            timezone_name="UTC",
        ),
    ).run(candles)

    assert report.trade_count == 1
    trade = report.trades[0]
    assert trade.side is Side.BUY
    assert trade.entry_index == 5
    assert trade.exit_index == 6
    assert trade.exit_reason == "target"


def test_backtester_supports_short_signal():
    class ShortStrategy:
        config = StrategyConfig(entry_timeframe=Timeframe.FIVE_MIN)

        def signal(self, snapshot):
            if snapshot.as_of.minute == 35:
                return TradeSignal(
                    "ABC",
                    Side.SELL,
                    100.0,
                    101.0,
                    99.0,
                    0.9,
                    snapshot.as_of,
                    "short timing test",
                )
            return None

    bars = [minute_bar(i) for i in range(12)]
    bars[6] = bars[6].__class__("ABC", bars[6].timestamp, 99.5, 99.8, 98.7, 99.0, 1000)
    report = StrategyBacktester(
        ShortStrategy(),
        BacktestConfig(
            spread_bps=0,
            slippage_bps=0,
            commission_bps=0,
            timezone_name="UTC",
        ),
    ).run(bars)

    assert report.trade_count == 1
    assert report.trades[0].side is Side.SELL
    assert report.trades[0].exit_reason == "target"
