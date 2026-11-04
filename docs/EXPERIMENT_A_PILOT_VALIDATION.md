# Experiment A: Layer 2 SIP vs IEX Pilot Validation Report

## 1. Evaluation Harness Audit
The evaluation harness `evaluate_symbol_point_in_time()` in `tests/run_experiment_a_pilot.py` was compared line-by-line against `DailySuitabilityEngine` in `money_mintter/core/suitability.py`.
- **Equivalence Confirmed:** Dollar volume formula (`price_to_use * volume` using VWAP if available else Close), 5-session median close, 14-period Wilder ATR smoothing, valid bar counting (`min_valid_daily_bars = 15`), completed session cutoff (`b_dt.date() < eval_date`), and exclusion/quarantine reasons (`low_price`, `low_median_price`, `insufficient_history`, `low_liquidity`, `low_volatility`, `high_volatility_quarantine`) match production logic exactly.
- **Differences:** None in core formula logic. The harness operates statelessly per symbol and evaluation date for research isolation.

---

## 2. Sample Selection & Stratum Validation
The 30 selected symbols across 3 strata were verified against empirical metrics:
- **Stratum 1 (High Liquidity):** `AAPL`, `MSFT`, `NVDA`, `SPY`, `QQQ`, `AMZN`, `GOOGL`, `META`, `TSLA`, `AMD`. (Verified: Median dollar volumes exceed $1B-$26B on SIP; 0 classification mismatches).
- **Stratum 2 (Medium Liquidity):** `INTC`, `PFE`, `F`, `BAC`, `T`, `VALE`, `NIO`, `PLTR`, `SNAP`, `SOFI`. (Verified: Median dollar volumes around $50M-$500M; very low mismatch rate).
- **Stratum 3 (Borderline / Exile):** `XLI`, `MS`, `BSX`, `AA`, `AAL`, `AER`, `AFRM`, `AGCO`, `AIG`, `AKAM`. (Verified: Mismatches concentrated here due to IEX consolidated volume proportion differences pushing borderline symbols below the $10M threshold).

---

## 3. JSON Results Reconciliation (`tests/experiment_a_pilot_results.json`)
- **Total Records:** Exactly 900 records (30 symbols × 30 evaluation dates).
- **Unique Symbols & Dates:** 30 unique symbols, 30 unique evaluation dates.
- **Missing / Duplicate Pairs:** Zero missing or duplicate pairs.
- **Classification Mismatches:** Exactly 81 mismatches out of 900 (9.0%).
- **Primary Driver:** All 81 mismatches were driven by `low_liquidity` rejections under IEX where consolidated SIP volume maintained eligibility above the $10M threshold.
- **Status-Transition Matrix:** SIP `eligible` -> IEX `excluded` (81 instances). SIP `excluded` -> IEX `eligible` (0 instances).

---

## 4. Raw Data Integrity (`data/cache/experiment_a/`)
- **Record Counts & Coverage:** Both SIP and IEX raw datasets contain complete daily bar series for all 30 symbols spanning the requested window (July 22, 2026 to November 3, 2026).
- **Warm-Up Sessions:** 20+ completed sessions prior to the first evaluation date (Sept 23, 2026) are fully present in both datasets.
- **Missing Data / Empty Responses:** Zero missing symbols or empty responses.

---

## 5. Report Audit (`docs/EXPERIMENT_A_PILOT_REPORT.md`)
- **Terminology Correction:** Replaced loose references to "true market liquidity" with precise terminology: **consolidated trading activity** across lit exchanges and TRFs, distinguishing aggregate trade volume from executable liquidity or market depth.
- **Strata & Metrics:** Numerical totals (900 records, 81 mismatches) verified against JSON results.

---

## 6. Repository Status (`git status --short`)
Working tree preserves all Phase 0 and Experiment A artifacts while leaving production source code untouched except for expected runtime caches.

---

## Conclusion

**VALID WITH LIMITATIONS**

### Rationale
The pilot evaluation harness, raw data retrieval, point-in-time calculation logic, and JSON result metrics are fully verified and trustworthy. However, because the evaluation is based on a stratified pilot sample of 30 symbols rather than the full ~5,000+ US equity universe, the specific quantitative mismatch rate (9.0%) and Stratum 3 behavior represent localized diagnostic findings rather than universal macro-market statistics. Expansion to the full universe is recommended for final production threshold calibration.
