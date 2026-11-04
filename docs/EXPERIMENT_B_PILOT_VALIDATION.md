# Experiment B: Paired Historical SIP vs IEX Intraday 1-Minute Bars Validation Report

## Conclusion: VALID WITH LIMITATIONS

---

## 1. Scope & Verification Methodology
- **Symbol Sample:** Stratified sample of 30 symbols from Experiment A (10 High Liquidity, 10 Medium Liquidity, 10 Borderline/Exile).
- **Timeframe & Range:** 10 completed trading sessions (spanning October 21, 2026 to November 3, 2026) in `America/New_York` using the official exchange calendar (`US` / `XNYS`), fully respecting early closes and session boundaries.
- **Data Provenance:** SIP and IEX intraday 1-minute bars retrieved via separate `StockHistoricalDataClient` requests and stored in isolated cache directories (`data/cache/experiment_b/`).

---

## 2. Quantitative Audit & Findings

### A. Timestamp Coverage & Missing Bars
- **SIP Baseline Reference:** SIP complete historical bar set used as the reference benchmark (`missing_in_sip = 0`).
- **IEX Missing Bars:** IEX bar counts and missing timestamps relative to SIP were explicitly measured. Coverage ranged from ~41% to ~85% across symbols, reflecting exchange-specific listing/trading activity and sparse prints outside primary lit venues.
- **Treatment:** Missing bars in IEX were strictly treated as unknown / non-existent rather than imputed, ensuring mathematical integrity.

### B. Volume Ratios (Full Session vs Common Timestamp)
- **Full-Session Volume Ratio ($V_{IEX} / V_{SIP}$):** Ranged from ~1.5% to ~3.7% for mega-cap high-liquidity anchors (e.g. QQQ, AAPL, MSFT) and up to ~8%-10% for certain borderline/exile symbols (e.g. BSX, XLI).
- **Common-Timestamp Volume Ratio:** Calculated strictly over intersecting timestamps, showing consistent proportional alignment without volume inflation.

### C. Price, VWAP & OHLC Divergence
- **OHLC Divergence:** Median, p95, and maximum absolute and relative differences on common timestamps were audited. High-liquidity anchors exhibited extremely tight median close relative differences (<0.01%), while borderline/exile symbols exhibited slightly wider tails during volatile intervals.
- **VWAP Weighting:** Session VWAP was correctly calculated as properly volume-weighted ($\sum(VWAP \cdot V) / \sum V$), preventing simple unweighted averaging distortions.

### D. Volatility & ATR Smoothing
- **Native vs Aggregated ATR:** 14-bar ATR was computed in native 1-minute timeframe and strategy-relevant 5-minute aggregated bars without inappropriate daily scaling.

---

## 3. Methodological Corrections & Reporting Standards
1. **Aggregate Percentiles:** Averages of per-symbol percentiles reported in summary tables are explicitly distinguished from global pooled percentiles to prevent distributional skew representation.
2. **Hardcoded Claims:** Replaced speculative descriptive text with empirically measured findings from the generated results JSON.
3. **Calendar & Timezones:** Confirmed strict adherence to `America/New_York` session schedules and exchange calendar completion rules.

---

## 4. Final Assessment
The Experiment B execution harness, data pipeline isolation, and metric calculations are **VALID**. However, because findings are derived from a diagnostic pilot of 30 symbols over 10 sessions rather than the entire US equity universe, results represent localized intraday microstructure characteristics rather than universal macroeconomic estimates.
