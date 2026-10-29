# Implementation Plan: ICT V106 Strategy Integration into Money Mintter

## Objective
Integrate the verified ICT V106 algorithm into Money Mintter as the explicit Layer 4 strategy (`ICTStrategy`), replacing `LiquiditySweepReversalStrategy`.

---

## Architecture & Components

### 1. Data Adapter (`money_mintter/strategy/ict_strategy.py`)
- Convert Money Mintter `MultiTimeframeSnapshot` candle objects (1m, 5m, 15m, etc.) into standard dict structures expected by ICT V106 functions:
  ```python
  {
      "time_ns": int(candle.timestamp.timestamp() * 1e9),
      "open": candle.open,
      "high": candle.high,
      "low": candle.low,
      "close": candle.close,
      "volume": candle.volume,
      "hour": candle.timestamp.hour,
      "minute": candle.timestamp.minute,
  }
  ```
- Map daily candles and higher timeframe structures (`dr5`, `dr15`, etc.) required by ICT helper functions (`get_liquidity_levels`, `detect_sweep_at`, `cisd_5m`, `structure_15m`, `sweep_15m`).

### 2. `ICTStrategy` Class (`money_mintter/strategy/ict_strategy.py`)
- Implements `analyze(snapshot: MultiTimeframeSnapshot) -> SetupAnalysis` and `signal(snapshot: MultiTimeframeSnapshot) -> TradeSignal | None`.
- Version identifier: `ict.v106.mtf`.
- Scoring (1 to 8) and flat 1.1 risk-reward (`rr = 1.1`).
- Zone deduplication (`_used_zones`) persisted across checks / sessions.

### 3. Layer 4 Dispatch Wiring
- Update `money_mintter/strategy/__init__.py` to export `ICTStrategy`.
- Update `money_mintter/core/pipeline.py` and `money_mintter/core/live_bot.py` (`MarketScanner.scan_universe`) to default to `ICTStrategy`.

### 4. Testing (`tests/test_ict_strategy.py`)
- Test data adapter correctness.
- Test raw signal generation and scoring parity.
- Test Layer 4 pipeline integration (`DeepAnalysisPipeline`).
- Test state deduplication and persistence/recovery.
- Test mock paper-broker order execution.

---

## Execution Steps
1. Create `money_mintter/strategy/ict_strategy.py`.
2. Update `money_mintter/strategy/__init__.py`.
3. Update `money_mintter/core/pipeline.py` and `money_mintter/core/live_bot.py`.
4. Create `tests/test_ict_strategy.py`.
5. Run `python -m pytest` to validate all tests pass successfully.
