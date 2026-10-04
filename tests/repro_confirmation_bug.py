
from datetime import datetime, timezone
from money_mintter.domain import Side
from money_mintter.strategy.candles import Candle
from money_mintter.strategy.timeframes import MultiTimeframeSnapshot, Timeframe
from money_mintter.strategy.liquidity_reversal import LiquiditySweepReversalStrategy, StrategyConfig, LiquidityZone

def test_confirmation_bug():
    config = StrategyConfig(
        entry_timeframe=Timeframe.FIVE_MIN,
        max_bars_after_sweep=8,
        max_bars_after_mss=5,
        min_displacement_body_fraction=0.1 # Make it easy
    )
    strategy = LiquiditySweepReversalStrategy(config)
    
    now = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)
    
    # Create a scenario for a LONG trade
    # 1. We need a liquidity zone below.
    # 2. We need a sweep.
    # 3. We need an MSS.
    # 4. We need a trigger candle that breaches but doesn't close above.
    # 5. We need a second candle that closes above.
    
    # Mocking some candles for context (needed for ATR etc)
    context = [Candle("TEST", now, 100, 101, 99, 100, 1000) for _ in range(50)]
    
    # Zone at 95.0
    # Sweep at index 40
    # MSS at index 42
    # Breach at index 43 (fails close)
    # Confirmation at index 44 (passes close)
    
    entry_candles = list(context)
    
    # Liquidity level: 95.0
    # Before sweep: price is around 100.
    # Sweep candle: low 94.0, close 96.0 (reclaimed)
    entry_candles[40] = Candle("TEST", now, 96.5, 96.5, 94.0, 96.0, 2000) # Rejection wick: 2.0/2.5 = 0.8
    
    # Structure before sweep (indices 32-39): high is around 100. Let's make it 98.0 for easier MSS.
    for i in range(32, 40):
        entry_candles[i] = Candle("TEST", now, 97.0, 98.0, 97.0, 97.5, 1000)
    
    # MSS candle: close > 98.0
    entry_candles[41] = Candle("TEST", now, 96.0, 98.5, 96.0, 98.5, 1500) # MSS index 41
    
    # Trigger window starts at 42.
    # Candle 42: High 99.0 (breaches MSS high 98.5), but Close 98.0 (below MSS high)
    entry_candles[42] = Candle("TEST", now, 98.5, 99.0, 97.5, 98.0, 1000)
    
    # Candle 43: High 99.5, Close 99.5 (should confirm)
    entry_candles[43] = Candle("TEST", now, 98.0, 99.5, 98.0, 99.5, 1200)
    
    snapshot = MultiTimeframeSnapshot(
        as_of=now,
        daily=(),
        four_hour=(),
        one_hour=tuple([Candle("TEST", now, 100, 100, 95, 100, 1000)] * 10), # Enough context
        fifteen_minute=tuple([Candle("TEST", now, 100, 100, 95, 100, 1000)] * 10), # Enough context
        entry=tuple(entry_candles[:44]),
        entry_timeframe=Timeframe.FIVE_MIN,
        spread_bps=1.0
    )
    
    # We need to monkeypatch _liquidity_zones to return our 95.0 zone easily
    import money_mintter.strategy.liquidity_reversal as lr
    original_zones = lr._liquidity_zones
    lr._liquidity_zones = lambda *args, **kwargs: (LiquidityZone(95.0, ("1H",), 2.0),)
    # Also need an opposing zone for target
    lr._nearest_opposing_zone = lambda *args, **kwargs: LiquidityZone(105.0, ("1H",), 2.0)

    try:
        result = strategy.analyze(snapshot)
        print(f"Result Passed: {result.passed}")
        print(f"Reason: {result.reason}")
        print(f"Vetoes: {result.vetoes}")
        
        if not result.passed and "weak_confirmation" in result.vetoes:
            print("CONFIRMED: The bug exists. First breach failed confirmation and blocked subsequent candles.")
        elif result.passed:
            print("The strategy passed (no bug or different behavior).")
    finally:
        lr._liquidity_zones = original_zones

if __name__ == "__main__":
    test_confirmation_bug()
