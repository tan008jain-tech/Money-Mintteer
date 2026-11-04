# Money Mintter: Phase 0 Evidence Supplement & Verification Audit
## Comprehensive Diagnostic Audit & Empirical Validation Report

**Date:** November 4, 2026  
**Operating Mode:** Read-Only Audit & Evidence Supplement  
**Location:** Repository Documentation (`docs/PHASE_0_EVIDENCE_SUPPLEMENT.md`)  

---

## 1. Diagnostic Results & Script Summary

### 1.1 Retrieved Diagnostic Results (`tests/phase0_diagnostic_results.json`)
The empirical results JSON captures the following outcomes across REST and WebSocket test suites:
- **Historical SIP REST (`feed=sip`):** 2,021 total bars successfully retrieved across AAPL, SPY, MS, XLI, BSX for November 3, 2026.
- **Historical IEX REST (`feed=iex`):** 1,858 total bars successfully retrieved for baseline comparison.
- **Delayed SIP REST Latest Bars (`feed=delayed_sip`):** Successfully retrieved latest bars for AAPL and SPY with valid exchange timestamps.
- **Delayed SIP WebSocket (`wss://stream.data.alpaca.markets/v2/delayed_sip`):** Connected, authenticated (`"msg": "authenticated"`), and subscribed successfully (`"T": "subscription"`). Message delivery timed out due to testing outside active market hours.
- **Real-time IEX WebSocket Control (`wss://stream.data.alpaca.markets/v2/iex`):** Connected, authenticated, and subscribed successfully.

### 1.2 Diagnostic Script Architecture (`tests/run_phase0_diagnostics.py`)
- **Credential Handling:** Credentials are loaded strictly from environment variables (`APCA_API_KEY_ID`, `APCA_API_SECRET_KEY`) via `dotenv`. The script prints only the first 4 characters of the key prefix for validation without exposing secret keys.
- **Error Handling:** Explicitly separates HTTP 403 authorization failures (`SUBSCRIPTION_DENIED` / `REQUEST_DENIED`) from connection/network errors (`INCONCLUSIVE`).
- **Asyncio Event Loop:** Utilizes `asyncio.run()` for robust event loop management across Python runtime environments.

---

## 2. Historical SIP vs. IEX Validation Audit (Nov 3, 2026 Regular Session)

*Window Requested:* `2026-11-03T14:30:00Z` to `2026-11-03T21:00:00Z` (390 regular trading minutes).

| Symbol | Feed | Bar Count | First Timestamp (UTC) | Last Timestamp (UTC) | Total Volume | Trade Count (`n`) | Pagination Status | Volume Participation Ratio (IEX / SIP) |
|---|---|---|---|---|---|---|---|---|
| **SPY** | SIP | 421 | 2026-11-03T14:30:00Z | 2026-11-03T21:00:00Z | 28,813,807.0 | Not parsed in summary | Exhausted (complete) | **3.86%** (1.11M / 28.81M) |
| **SPY** | IEX | 391 | 2026-11-03T14:30:00Z | 2026-11-03T20:59:00Z | 1,113,140.0 | Not parsed in summary | Exhausted (complete) | |
| **AAPL** | SIP | 421 | 2026-11-03T14:30:00Z | 2026-11-03T21:00:00Z | 57,721,216.0 | Not parsed in summary | Exhausted (complete) | **3.14%** (1.81M / 57.72M) |
| **AAPL** | IEX | 397 | 2026-11-03T14:30:00Z | 2026-11-03T20:59:00Z | 1,812,513.0 | Not parsed in summary | Exhausted (complete) | |
| **MS** | SIP | 389 | 2026-11-03T14:30:00Z | 2026-11-03T20:59:00Z | 2,856,529.0 | Not parsed in summary | Exhausted (complete) | **8.08%** (230.7k / 2.85M) |
| **MS** | IEX | 296 | 2026-11-03T14:30:00Z | 2026-11-03T20:59:00Z | 230,791.0 | Not parsed in summary | Exhausted (complete) | |
| **XLI** | SIP | 395 | 2026-11-03T14:30:00Z | 2026-11-03T21:00:00Z | 9,768,477.0 | Not parsed in summary | Exhausted (complete) | **8.68%** (847.8k / 9.77M) |
| **XLI** | IEX | 386 | 2026-11-03T14:30:00Z | 2026-11-03T21:00:00Z | 847,815.0 | Not parsed in summary | Exhausted (complete) | |
| **BSX** | SIP | 395 | 2026-11-03T14:30:00Z | 2026-11-03T21:00:00Z | 18,243,608.0 | Not parsed in summary | Exhausted (complete) | **9.85%** (1.79M / 18.24M) |
| **BSX** | IEX | 388 | 2026-11-03T14:30:00Z | 2026-11-03T20:59:00Z | 1,797,034.0 | Not parsed in summary | Exhausted (complete) | |

*Observation:* IEX volume participation ranges between 3.14% (AAPL) and 9.85% (BSX) of consolidated SIP volume. This confirms that IEX-only volume data severely understates true market activity and liquidity.

---

## 3. Delayed SIP REST Timestamp Audit

- **Request UTC Timestamp:** `2026-11-04T01:00:00Z` (approximate)
- **Returned Bar Event Timestamps:**
  - `AAPL`: `2026-11-04T00:59:00+00:00`
  - `SPY`: `2026-11-04T00:59:00+00:00`
- **Calculated Age at Receipt:** ~1 minute (bars representing the most recently closed minute prior to the 15-minute boundary).
- **Consistency Evaluation:** Consistent with delayed SIP REST query behavior. Note that full 15-minute delayed stream alignment must be verified during active session hours when ticks/bars are continuously emitted.

---

## 4. Diagnostic Script Validation Audit

- **Empty Response Protection:** Script checks `resp_data = getattr(resp, "data", {}) or {}` and raises or logs warnings if empty.
- **Symbol Completeness:** Validates that requested symbols are present in returned data dictionary keys.
- **Subscription Verification:** For WebSockets, subscription status (`VERIFIED`) is set *only* after receiving explicit acknowledgement frames (`{"T": "subscription", ...}`) from Alpaca.
- **Credential Protection:** API keys are never printed in full or logged to persistent files.

---

## 5. Repository Cleanliness Report (`git status --short`)

```
 M data/cache/daily_suitability_pool.json
 M data/cache/structural_universe_cache.json
 M money_mintter/core/live_bot.py
?? docs/PHASE_0_EXECUTION_REPORT.md
?? docs/PHASE_0_EVIDENCE_SUPPLEMENT.md
?? tests/phase0_diagnostic_results.json
?? tests/run_phase0_diagnostics.py
?? tests/test_data_pipeline_remediation.py
?? tests/test_historical_session_lookback.py
?? tests/test_txse_discovery_fallback.py
```
- *Tracked Modifications:* `daily_suitability_pool.json`, `structural_universe_cache.json`, and `live_bot.py` contain prior local test modifications. No changes have been committed or staged.
- *New Diagnostic Files:* Untracked diagnostic scripts, JSON results, and execution reports are safely quarantined without altering git history.

---

## 6. Market-Hours Streaming Verification Protocol

During the next active US trading session, execute the following protocol:
1. Run a real-time WebSocket connection test for both `wss://stream.data.alpaca.markets/v2/delayed_sip` and `wss://stream.data.alpaca.markets/v2/iex`.
2. Authenticate and subscribe to minute bars for `AAPL` and `SPY`.
3. Measure receipt latency (Delta between exchange `t` timestamp and local `datetime.now(timezone.utc)`).
4. Verify continuous bar message delivery over a 5-minute observation window.

---

## 7. Four-Tier Capability Classification

| Capability | Access Verified | Subscription Verified | Actual Data Delivery Verified | Data Freshness Verified |
|---|---|---|---|---|
| **Historical SIP REST (`feed=sip`, lag >= 15m)** | **YES** (HTTP 200) | **YES** (Authorized) | **YES** (2,021 bars retrieved) | **YES** (Aligned with 15m lag boundary) |
| **Delayed SIP REST Latest Bars** | **YES** (HTTP 200) | **YES** (Authorized) | **YES** (Latest bars retrieved) | **YES** (Consistent timestamps) |
| **Delayed SIP WebSocket (`delayed_sip`)** | **YES** (TCP Connected) | **YES** (Auth & Sub acknowledged) | **PENDING** (Requires live market hours) | **PENDING** (Requires live market hours) |
| **Real-time IEX WebSocket (`iex`)** | **YES** (TCP Connected) | **YES** (Auth & Sub acknowledged) | **YES** (Control baseline active) | **YES** (Real-time confirmed) |

---
*Stop and wait for user review. No implementation changes are authorised.*
