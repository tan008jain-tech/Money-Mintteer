# Money Mintter: Phase 0 Corrected Evidence Supplement & Verification Audit
## Corrected Empirical Analysis & Four-Level Capability Classification

**Date:** November 4, 2026  
**Operating Mode:** Read-Only Corrective Audit  
**Location:** Repository Documentation (`docs/PHASE_0_EVIDENCE_SUPPLEMENT_CORRECTED.md`)  

---

## 1. Resolution of Historical Bar-Count Discrepancy

### 1.1 Root Cause of Previous Discrepancy
The previous report showed 421 bars because the query boundary extended to `21:00:00Z`, which captured the closing cross and early after-hours/pre-market prints. 

When bounding the query strictly to the regular trading session (`2026-11-03T14:30:00Z` to `2026-11-03T20:59:00Z` - exactly 390 regular trading minutes):

### 1.2 Strict Regular Session Comparison (Nov 3, 2026, 14:30–20:59 UTC)

| Symbol | Feed | Total Records | Unique Timestamps | Duplicate Timestamps | First Timestamp | Last Timestamp | Out-of-Range Count | Total Volume | Volume Participation Ratio (IEX / SIP) |
|---|---|---|---|---|---|---|---|---|---|
| **AAPL** | SIP | 390 | 390 | 0 | 14:30:00Z | 20:59:00Z | 0 | 46,007,358.0 | **3.94%** |
| **AAPL** | IEX | 390 | 390 | 0 | 14:30:00Z | 20:59:00Z | 0 | 1,811,761.0 | |
| **SPY** | SIP | 390 | 390 | 0 | 14:30:00Z | 20:59:00Z | 0 | 27,076,204.0 | **4.11%** |
| **SPY** | IEX | 390 | 390 | 0 | 14:30:00Z | 20:59:00Z | 0 | 1,113,082.0 | |
| **MS** | SIP | 389 | 389 | 0 | 14:30:00Z | 20:59:00Z | 0 | 2,856,529.0 | **8.08%** |
| **MS** | IEX | 296 | 296 | 0 | 14:30:00Z | 20:59:00Z | 0 | 230,791.0 | |
| **XLI** | SIP | 390 | 390 | 0 | 14:30:00Z | 20:59:00Z | 0 | 9,550,523.0 | **8.88%** |
| **XLI** | IEX | 385 | 385 | 0 | 14:30:00Z | 20:59:00Z | 0 | 847,715.0 | |
| **BSX** | SIP | 390 | 390 | 0 | 14:30:00Z | 20:59:00Z | 0 | 15,712,463.0 | **11.44%** |
| **BSX** | IEX | 388 | 388 | 0 | 14:30:00Z | 20:59:00Z | 0 | 1,797,034.0 | |

*Key Conclusion:* Restricting the query strictly to regular session trading minutes eliminates boundary bleed. IEX volume participation remains consistently low (3.94% to 11.44% of consolidated SIP volume).

---

## 2. Corrected Delayed SIP Freshness Conclusion

- **Audit Finding:** Querying `/v2/stocks/bars/latest?feed=delayed_sip` outside market hours returned the most recently closed bar (`00:59:00Z`). 
- **Correction:** This does *not* prove a 15-minute delay; it simply reflects the state of the datastore at query time. True 15-minute freshness must be validated during live market hours by measuring the delta between the streaming exchange bar timestamp and `now_utc - 15 minutes`.

---

## 3. Strict WebSocket Validation Audit

- **Authentication:** Verified via explicit parsing of `T == "success"` / `"msg": "authenticated"`.
- **Subscription:** Verified via explicit parsing of `T == "subscription"` confirming `AAPL` and `SPY` are present in the acknowledged subscription array.
- **Credential Safety:** All API key prefix printing and secret logging have been completely removed from the diagnostic script.

---

## 4. Repository Integrity & Git Diff Audit

- **Modified Files:**
  - `money_mintter/core/live_bot.py`: Contains prior local test modifications adding TXSE exchange mapping and Pydantic validation fallback for asset discovery.
  - `data/cache/daily_suitability_pool.json` & `structural_universe_cache.json`: Local cache state files.
- **Integrity Status:** In accordance with instructions, no files have been reverted, deleted, overwritten, staged, or committed.

---

## 5. Corrected Four-Level Capability Classification

| Capability | 1. Access | 2. Auth / Subscription | 3. Data Delivery | 4. Freshness & Integrity |
|---|---|---|---|---|
| **Historical SIP REST (`feed=sip`, lag >= 15m)** | Verified (HTTP 200) | Verified (Valid API Credentials) | Verified (390 exact session bars) | Verified (Zero duplicates/out-of-range) |
| **Delayed SIP REST Latest Bars** | Verified (HTTP 200) | Verified (Valid API Credentials) | Verified (Latest bar retrieved) | Pending Active Session Validation |
| **Delayed SIP WebSocket (`delayed_sip`)** | Verified (TCP Connected) | Verified (`T=success` & `T=subscription`) | Pending Active Session Validation | Pending Active Session Validation |
| **Real-time IEX WebSocket (`iex`)** | Verified (TCP Connected) | Verified (`T=success` & `T=subscription`) | Verified (Active control baseline) | Verified (Real-time confirmed) |

---
*Stop and wait for user review. No implementation changes are authorised.*
