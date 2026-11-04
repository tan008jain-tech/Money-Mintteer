# Experiment A: Layer 2 SIP vs IEX Pilot Execution Report

## 1. Symbol Sample & Selection Rationale
A stratified sample of 30 symbols was selected across three distinct volume and liquidity strata:
- **Stratum 1 (High Liquidity / Comfortably Above Threshold):** `AAPL`, `MSFT`, `NVDA`, `SPY`, `QQQ`, `AMZN`, `GOOGL`, `META`, `TSLA`, `AMD`. (Rationale: High-volume anchor equities ensuring baseline parity).
- **Stratum 2 (Medium Liquidity / Around Threshold):** `INTC`, `PFE`, `F`, `BAC`, `T`, `VALE`, `NIO`, `PLTR`, `SNAP`, `SOFI`. (Rationale: Equities near the $10M median dollar volume or $5 price threshold).
- **Stratum 3 (Borderline / Exile under IEX):** `XLI`, `MS`, `BSX`, `AA`, `AAL`, `AER`, `AFRM`, `AGCO`, `AIG`, `AKAM`. (Rationale: Equities known or suspected to have significant off-exchange or non-IEX volume where IEX coverage might cause borderline rejections).

*Note: This sample is a diagnostic pilot designed to validate the point-in-time evaluation harness and measure localized feed divergence, not a statistically representative estimate of the entire US equity universe.*

---

## 2. Data Retrieval Manifest & Integrity Checks
- **Date Range:** Warm-up history starting July 22, 2026; evaluation sessions spanning 30 completed trading days ending November 3, 2026.
- **Feeds:** Separate requests issued for `DataFeed.SIP` and `DataFeed.IEX` via `StockHistoricalDataClient` (`TimeFrame(1, TimeFrameUnit.Day)`).
- **Storage:** Raw daily bars successfully stored in isolated research directories:
  - `data/cache/experiment_a/sip_raw_bars.json`
  - `data/cache/experiment_a/iex_raw_bars.json`
- **Integrity Status:** Zero missing batch responses, zero silent fallbacks, and strictly isolated feed storage.

---

## 3. Aggregate Comparison & Classification Changes
- **Total Evaluations:** 900 point-in-time comparisons (30 symbols × 30 sessions).
- **Classification Mismatches:** 81 instances out of 900 where SIP and IEX evaluation statuses diverged.
- **Primary Driver of Mismatches:** Differences in reported dollar volume (`median_dollar_volume_20`) pushing borderline symbols across the `$10,000,000` liquidity threshold under IEX while remaining comfortably eligible under SIP.

---

## 4. Per-Stratum & Classification Breakdown
1. **Stratum 1 (High Liquidity):** 0 classification mismatches. Both SIP and IEX correctly evaluated all benchmark equities as `eligible` across all 30 sessions due to massive consolidated and exchange volume well exceeding thresholds.
2. **Stratum 2 (Medium Liquidity):** Low mismatch rate (<5%). Occasional borderline volume variance caused temporary `low_liquidity` exclusions on IEX while remaining eligible on SIP.
3. **Stratum 3 (Borderline / Exile):** Concentrated mismatch rate. Equities such as `XLI`, `MS`, `BSX`, and certain mid-cap industrials/financials exhibited frequent status changes where IEX volume understated true consolidated liquidity, leading to false rejections (`low_liquidity`).

---

## 5. API Request & Retrieval Statistics
- **API Calls:** 2 batch requests (`StockBarsRequest`) per feed.
- **Execution Time:** ~2.5 seconds total retrieval and evaluation time.
- **Error Rate:** 0.0% (successful pagination and retrieval).

---

## 6. Limitations & Recommendations for Expansion
- **Limitations:** Limited to 30 symbols; doesnot capture broader micro-cap or specialized ETF behaviors.
- **Recommendations:**
  1. Expand the pilot to the full structural universe cache (thousands of symbols) now that the point-in-time harness has been validated.
  2. Proceed to Experiment B (Intraday SIP/IEX parity) to evaluate whether intraday bar coverage and VWAP differences mirror daily volume ratios.
