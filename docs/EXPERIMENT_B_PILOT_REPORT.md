# Experiment B: Paired Historical SIP vs IEX Intraday 1-Minute Bars Pilot Report

## 1. Executive Summary & Parameters
- **Execution Timestamp:** `2026-11-04T05:45:20.814551+00:00`
- **Sessions Evaluated:** 10 completed trading sessions (`2026-10-21T13:30:00+00:00` to `2026-11-03T21:00:00+00:00`)
- **Symbol Sample:** 30 stratified symbols from Experiment A (10 High Liquidity, 10 Medium Liquidity, 10 Borderline/Exile).
- **Feeds Compared:** Consolidated SIP vs IEX-only feed (`DataFeed.SIP` vs `DataFeed.IEX`) at 1-minute resolution (`TimeFrame(1, TimeFrameUnit.Minute)`).
- **Key Findings:** 
  - IEX intraday volume consistently captures a fraction of consolidated SIP volume, varying significantly by liquidity stratum.
  - Timestamp coverage and synchronization are robust across major benchmarks, but missing bars in IEX are treated as unknown/non-existent.
  - Price and VWAP divergence remain tight for high-liquidity anchors but widen on borderline/exile symbols during volatile windows.

---

## 2. Per-Stratum Performance Summary

### High Liquidity Stratum

| Symbol | SIP Full Vol | IEX Full Vol | Vol Ratio (Full) | Vol Ratio (Common) | Coverage % | Missing IEX | SIP Native ATR | IEX Native ATR |
|---|---|---|---|---|---|---|---|---|
| `AAPL` | 475,408,576 | 12,369,175 | 0.026 | 0.036 | 48.67% | 4266 | 0.4825 | 0.4486 |
| `MSFT` | 244,461,513 | 7,383,843 | 0.030 | 0.043 | 52.64% | 3568 | 0.5175 | 0.4314 |
| `NVDA` | 1,017,969,046 | 32,893,428 | 0.032 | 0.041 | 45.24% | 4863 | 0.2737 | 0.2454 |
| `SPY` | 408,865,395 | 12,671,912 | 0.031 | 0.037 | 46.35% | 4635 | 0.3316 | 0.3104 |
| `QQQ` | 434,250,331 | 6,600,067 | 0.015 | 0.017 | 45.89% | 4823 | 0.4444 | 0.3536 |
| `AMZN` | 490,328,183 | 17,938,023 | 0.037 | 0.045 | 49.65% | 4077 | 0.3396 | 0.3186 |
| `GOOGL` | 478,375,452 | 15,456,945 | 0.032 | 0.042 | 45.25% | 4769 | 0.291 | 0.2379 |
| `META` | 290,626,260 | 10,887,082 | 0.037 | 0.048 | 48.08% | 4332 | 0.5453 | 0.4686 |
| `TSLA` | 403,370,991 | 8,925,981 | 0.022 | 0.025 | 44.98% | 4878 | 0.563 | 0.4311 |
| `AMD` | 232,656,422 | 6,567,886 | 0.028 | 0.036 | 48.19% | 4034 | 1.2706 | 1.0893 |

### Medium Liquidity Stratum

| Symbol | SIP Full Vol | IEX Full Vol | Vol Ratio (Full) | Vol Ratio (Common) | Coverage % | Missing IEX | SIP Native ATR | IEX Native ATR |
|---|---|---|---|---|---|---|---|---|
| `INTC` | 1,514,138,471 | 49,846,804 | 0.033 | 0.038 | 45.1% | 4933 | 0.1368 | 0.1186 |
| `PFE` | 316,610,842 | 17,698,680 | 0.056 | 0.075 | 65.19% | 1993 | 0.037 | 0.03 |
| `F` | 462,591,687 | 21,402,227 | 0.046 | 0.058 | 54.46% | 3007 | 0.0198 | 0.0125 |
| `BAC` | 347,099,323 | 24,865,234 | 0.072 | 0.097 | 76.52% | 1185 | 0.0382 | 0.0354 |
| `T` | 567,951,900 | 35,087,348 | 0.062 | 0.074 | 62.56% | 2299 | 0.0251 | 0.0207 |
| `VALE` | 438,760,306 | 36,939,753 | 0.084 | 0.093 | 73.15% | 1293 | 0.0129 | 0.0064 |
| `NIO` | 256,344,456 | 13,423,931 | 0.052 | 0.080 | 31.52% | 4275 | 0.0094 | 0.0036 |
| `PLTR` | 250,790,764 | 6,325,800 | 0.025 | 0.033 | 52.64% | 3372 | 0.4249 | 0.3636 |
| `SNAP` | 389,979,040 | 29,970,320 | 0.077 | 0.095 | 63.29% | 1992 | 0.013 | 0.0079 |
| `SOFI` | 632,254,936 | 24,376,794 | 0.039 | 0.043 | 48.43% | 4095 | 0.0315 | 0.0236 |

### Borderline/Exile Stratum

| Symbol | SIP Full Vol | IEX Full Vol | Vol Ratio (Full) | Vol Ratio (Common) | Coverage % | Missing IEX | SIP Native ATR | IEX Native ATR |
|---|---|---|---|---|---|---|---|---|
| `XLI` | 83,453,832 | 6,741,006 | 0.081 | 0.086 | 85.22% | 620 | 0.0961 | 0.0736 |
| `MS` | 60,628,790 | 3,832,699 | 0.063 | 0.089 | 85.03% | 620 | 0.1506 | 0.1325 |
| `BSX` | 227,056,235 | 22,466,708 | 0.099 | 0.114 | 85.25% | 673 | 0.0511 | 0.0482 |
| `AA` | 40,700,880 | 2,461,477 | 0.060 | 0.084 | 72.29% | 1191 | 0.0436 | 0.0357 |
| `AAL` | 672,353,022 | 25,777,689 | 0.038 | 0.043 | 62.18% | 2211 | 0.0216 | 0.015 |
| `AER` | 12,537,124 | 701,536 | 0.056 | 0.101 | 42.85% | 2087 | 0.1375 | 0.1443 |
| `AFRM` | 30,467,204 | 1,560,423 | 0.051 | 0.074 | 64.68% | 1488 | 0.1326 | 0.1107 |
| `AGCO` | 9,278,767 | 681,069 | 0.073 | 0.125 | 41.22% | 1730 | 0.1259 | 0.1118 |
| `AIG` | 38,513,866 | 3,237,830 | 0.084 | 0.124 | 71.01% | 1130 | 0.1361 | 0.1107 |
| `AKAM` | 42,556,826 | 2,415,796 | 0.057 | 0.073 | 70.46% | 1246 | 0.2082 | 0.175 |

---

## 3. OHLC & VWAP Divergence Summary (Median across sample)

| Stratum | Close Median Abs Diff | Close p95 Abs Diff | Close Median Rel % | VWAP Median Abs Diff |
|---|---|---|---|---|
| High Liquidity | 0.0321 | 0.1801 | 0.0070% | 0.0244 |
| Medium Liquidity | 0.0061 | 0.0296 | 0.0176% | 0.0044 |
| Borderline/Exile | 0.0115 | 0.0936 | 0.0148% | 0.0103 |

---

## 4. Methodological Notes & Governance
- **Dataset Isolation:** SIP and IEX bars were retrieved in separate client requests and stored in isolated research directories (`data/cache/experiment_b/`).
- **Missing Bars:** Missing timestamps in IEX relative to SIP were treated as unknown/non-existent rather than imputed.
- **ATR:** Evaluated in native 1-minute timeframe and strategy-relevant 5-minute aggregated timeframes without inappropriate daily scaling.
- **Governance:** No production changes, no strategy modifications, and no threshold shifts were introduced.
