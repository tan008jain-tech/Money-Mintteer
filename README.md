# Money Mintter

Money Mintter is an event-driven US-equities market-scanning and **Alpaca paper-trading** system. The current production path uses the ICT V106 strategy integration, a layered universe-selection pipeline, guarded bracket-order execution, and session-level reconciliation.

The default Git branch is **`ict-rebuild`**. A normal clone therefore checks out the current working line:

~~~powershell
git clone https://github.com/tan008jain-tech/Money-Mintteer.git
cd Money-Mintteer
git branch --show-current
~~~

The last command should print `ict-rebuild`.

> **Trading and account-safety notice:** the production entry point currently constructs Alpaca's trading client with `paper=True`. Use Alpaca **paper-trading** credentials only. This software is experimental; passing tests or producing a profitable paper session does not establish that a strategy is profitable or suitable for live capital.

---

## 1. Current production architecture

The production session follows this path:

~~~text
Alpaca US-equity asset discovery
        |
        v
Layer 1: structural eligibility and universe-health checks
        |
        v
Layer 2: daily suitability using historical bars
        |
        v
Validated daily pool (up to 50 symbols by default)
        |
        v
Layer 4: intraday movement/activity ranking (up to 8 candidates)
        |
        v
ICT V106 acquisition, adapter and signal generation
        |
        v
Signal validation and execution-safety checks
        |
        v
Position sizing and Alpaca paper bracket orders
        |
        v
Order journal, broker reconciliation and session summary
~~~

### Universe selection

- **Layer 1** discovers active US-equity assets and applies structural eligibility rules. The live universe must meet `MIN_UNIVERSE_HEALTH` (default 50). A persisted Layer 1 cache is accepted only within `MAX_UNIVERSE_CACHE_AGE_HOURS` (default 24 hours).
- **Layer 2** evaluates daily-bar suitability and returns a qualified pool up to `MAX_DAILY_POOL_SIZE` (default 50). It does not pad the pool to reach the limit. Cache compatibility includes the current universe, screening configuration and feed policy.
- **Layer 3 market-cap filtering is not currently part of the active production path.**
- **Layer 4** ranks the Layer 2 pool using intraday movement and activity information. It selects at most eight candidates and does not pad the result. The selector runs during regular US-equity trading hours.
- Existing positions and broker-open orders are accounted for separately from new-entry candidates.

The eight-symbol Layer 4 selection cap is **not** the portfolio position limit. `MAX_OPEN_POSITIONS` defaults to five. Existing positions, open broker orders and uncertain pending submissions consume execution capacity.

### ICT V106 integration

The production scanner acquires 1-minute and 5-minute market bars for selected symbols through Alpaca. The ICT adapter validates and orders candle data, enforces the input contract, and converts candles into the format expected by the ICT integration. The production signal path is implemented under:

- `money_mintter/strategy/ict/v106_dynamic_rr_zone_entry.py`
- `money_mintter/strategy/ict/adapter.py`
- `money_mintter/strategy/ict/acquisition.py`
- `money_mintter/strategy/ict/integration.py`

The production scanner requests approximately 30 calendar days of 1-minute and 5-minute data for ICT evaluation. Data availability and history limits depend on the Alpaca account and selected feed.

The ICT strategy is the signal-generation component. The surrounding Money Mintter code handles data acquisition, adaptation, signal lifecycle/deduplication, portfolio constraints, broker interaction and operational safety. Do not treat the older `run_paper_demo.py` or legacy research strategy files as the production ICT session.

### Market-data policy

The active configuration deliberately separates historical suitability data from intraday data:

| Setting | Current default | Purpose |
|---|---|---|
| `ALPACA_HISTORICAL_FEED` | `sip` | Layer 2 historical daily-bar suitability |
| `ALPACA_REALTIME_FEED` | `iex` | Layer 4 intraday scanning, ICT acquisition and execution reference quotes |
| `ALPACA_DATA_FEED` | `iex` | Legacy compatibility setting |
| `ALPACA_ALLOW_HISTORICAL_FEED_FALLBACK` | `false` | Prevents silent cross-feed substitution |

Layer 2 historical requests end at least 15 minutes before the current time. The requested and actual feed provenance is recorded, and caches from incompatible feed policies are not silently reused. SIP availability depends on the Alpaca account's data entitlements. If the configured feed cannot be accessed, investigate the API response and cache compatibility rather than silently changing feeds.

The live runtime currently requires `ALPACA_REALTIME_FEED=iex`; configuring another real-time feed causes startup to fail. IEX represents only part of consolidated US-market activity, so IEX volume should not be interpreted as total-market volume.

---

## 2. Execution and risk safeguards

The production execution path validates the broker/account state and the executable market quote before submitting an order. Important current safeguards include:

- Maximum five open position slots by default; positions, open orders and uncertain submissions reserve capacity.
- Per-trade risk default of 0.50% of account equity and maximum portfolio risk default of 2.0%.
- Maximum position allocation default of 20% of account equity.
- Latest quote age limited to 10 seconds, with at most 2 seconds of future timestamp skew.
- Side-aware execution-price deviation limited to 1% from the ICT signal entry (ask for buys, bid for sells).
- Bid/ask spread limited to 1% of the quote midpoint.
- Invalid, missing, stale, crossed or out-of-tolerance quotes fail closed.
- The executable entry, stop and target must have valid directional bracket geometry and at least 1.10 reward/risk before sizing.
- Commission/cost estimates are included in the pre-trade checks.
- The kill switch monitors daily loss and repeated errors.
- Order lifecycle events, broker fills and available fee activities are reconciled through the order journal.

These are execution and operational gates. They should not be loosened merely to increase the number of trades. The absence of a trade can be a valid result when the market, data or risk conditions do not qualify.

**Use a dedicated Alpaca paper account for Money Mintter.** The session cleanup queries the broker account, attempts to cancel/close outstanding broker activity, flattens positions and verifies the final broker snapshot. Do not leave unrelated positions or orders in the same paper account.

---

## 3. Requirements

- Windows 10/11, macOS or Linux.
- **Python 3.12** (required by `pyproject.toml`).
- Git.
- An Alpaca account with paper-trading API credentials.
- Market-data access for the configured feeds. Historical SIP access is subject to Alpaca account entitlements.

The commands below use Windows PowerShell. On macOS/Linux, use the equivalent `python3.12 -m venv .venv`, activate `.venv/bin/activate`, and run the same Python module commands through the active environment.

---

## 4. Local setup (Windows PowerShell)

### 4.1 Clone the repository

Because `ict-rebuild` is now the default branch, no branch flag is needed:

~~~powershell
git clone https://github.com/tan008jain-tech/Money-Mintteer.git
Set-Location Money-Mintteer
git branch --show-current
~~~

Confirm the branch is `ict-rebuild`.

For an existing clone, check local changes before pulling. Preserve any local cache, log or code changes you need; do not use `git reset --hard` as a routine update command.

~~~powershell
git status --short
git switch ict-rebuild
git pull --ff-only origin ict-rebuild
~~~

### 4.2 Create and activate a virtual environment

Check that the Python 3.12 launcher is available:

~~~powershell
py -3.12 --version
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
~~~

If PowerShell blocks activation, do not change the machine-wide execution policy just for this project. Use the virtual environment's interpreter directly instead:

~~~powershell
.\.venv\Scripts\python.exe --version
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
~~~

When activation succeeds, install the project and development/test dependencies:

~~~powershell
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
~~~

The editable install reads dependencies from `pyproject.toml`. The `dev` extra installs pytest. Python 3.12 is required; installing packages into a different global Python interpreter can lead to commands such as `pytest` not being found in the environment used to run the project.

### 4.3 Configure Alpaca paper credentials

Create your local environment file:

~~~powershell
Copy-Item .env.example .env
notepad .env
~~~

In `.env`, enter the **paper-trading** API key and secret issued by Alpaca. The preferred variable names are:

~~~dotenv
APCA_API_KEY_ID=your_paper_api_key
APCA_API_SECRET_KEY=your_paper_secret_key
APCA_API_BASE_URL=https://paper-api.alpaca.markets
~~~

The application also accepts the legacy `APCA_API_KEY` and `APCA_API_SECRET` names. Use one complete key/secret pair; do not mix a key from one account with a secret from another.

Keep the data-feed settings in `.env` at their documented defaults unless you have verified a deliberate reason to change them:

~~~dotenv
ALPACA_DATA_FEED=iex
ALPACA_HISTORICAL_FEED=sip
ALPACA_REALTIME_FEED=iex
ALPACA_ALLOW_HISTORICAL_FEED_FALLBACK=false
~~~

Do not commit `.env`, paste credentials into issues or chat, or place secrets in source files. The production `TradingClient` is constructed with `paper=True`. This is a paper-trading runtime, not a live-trading switch; changing `APCA_API_BASE_URL` alone does not enable live trading.

### 4.4 Run the tests

Run the focused ICT signal-freshness test:

~~~powershell
python -m pytest -q tests/test_ict_signal_freshness.py
~~~

Then run the full suite:

~~~powershell
python -m pytest -q
~~~

Use `python -m pytest`, not only `pytest`, so the test runner is resolved from the active Python environment. If Python reports `No module named pytest`, repeat the editable install command above using that same interpreter. A passing test suite verifies the covered software contracts; it does not prove trading profitability or guarantee a successful broker session.

---

## 5. Running the first paper-trading session

### 5.1 Pre-session checklist

Before launching:

1. Confirm `.env` contains valid Alpaca **paper** credentials.
2. Confirm the paper account is dedicated to Money Mintter and has no unrelated positions or open orders.
3. Confirm `ALPACA_REALTIME_FEED=iex` and review whether the account can access the configured historical SIP feed.
4. Confirm the machine has a reliable internet connection and will remain awake for the session.
5. Run the test suite and review any failures before starting the bot.
6. Check that no other Money Mintter session is already running.

### 5.2 Start the production session

From the repository root, with the virtual environment active:

~~~powershell
python run_live_market.py
~~~

This is the production paper-session entry point. It initializes the Alpaca paper trading client, discovers and validates the universe, scans candidates, evaluates ICT signals and submits eligible bracket orders to the paper account.

**Start timing matters.** The session uses Alpaca's market clock. If the market is closed and the next open is more than `MAX_WAIT_FOR_OPEN_MINUTES` away (45 minutes by default), the run is skipped. Start during regular US-equity hours or no more than 45 minutes before the next open. The scheduler uses US market time and the broker clock; do not hard-code a UTC or India-local opening time because daylight-saving changes shift the local conversion.

### 5.3 What happens during a session

- The bot runs its scan/execution cycle around completed 5-minute bar boundaries.
- New entries stop at `NO_NEW_ENTRIES_MINUTES_BEFORE_CLOSE` (30 minutes before the scheduled close by default).
- The session attempts to flatten positions `FLATTEN_MINUTES_BEFORE_CLOSE` (5 minutes before close by default).
- After flattening, the bot takes a fresh broker account, positions and open-orders snapshot to verify the account is flat.
- The process then exits and releases its session lock.

Keep the terminal open. A normal `Ctrl+C` while trading is active invokes an emergency-cleanup attempt, but do not use interruption as the normal end-of-day procedure. A forced process termination, machine shutdown or network failure can prevent cleanup from completing. Always verify positions and orders in Alpaca after an abnormal shutdown.

### 5.4 Understand the process result

The current runner uses these exit codes:

| Exit code | Meaning |
|---:|---|
| `0` | Session completed, or market was closed and the run was skipped |
| `2` | Session halted by a safety condition |
| `3` | Session error or end-of-session flatten could not be verified as complete |
| `4` | A fresh session lock already exists; another session may be running |
| `130` | Session interrupted by the user |

A skipped session is not a completed trading session. A halted, interrupted or error result requires log review. If the terminal reports remaining positions, `UNKNOWN`, or an incomplete flatten, open the Alpaca paper dashboard immediately, reconcile open orders and positions, and close anything that remains before restarting Money Mintter.

### 5.5 Do not confuse the demo with the production session

`run_paper_demo.py` is a self-contained simulated demo. It creates a local `PaperBroker`, injects a synthetic AAPL trade and serves a demo dashboard on `http://127.0.0.1:8765`. It does not run the Alpaca live-market scanner or the production ICT V106 session.

Use `python run_live_market.py` for the actual Alpaca paper-trading session.

---

## 6. Configuration reference

`.env.example` is the starting point and should be treated as the baseline. The following settings are the most operationally important:

| Variable | Default | Meaning |
|---|---:|---|
| `MIN_UNIVERSE_HEALTH` | `50` | Minimum healthy Layer 1 symbol count |
| `MAX_UNIVERSE_CACHE_AGE_HOURS` | `24` | Maximum accepted Layer 1 cache age |
| `MAX_DAILY_POOL_SIZE` | `50` | Maximum Layer 2 qualified symbols |
| `MAX_OPEN_POSITIONS` | `5` | Maximum occupied execution slots |
| `RISK_PER_TRADE_PCT` | `0.50` | Per-trade risk as a percentage |
| `MAX_PORTFOLIO_RISK_PCT` | `2.0` | Maximum portfolio risk as a percentage |
| `MAX_POSITION_PCT` | `20.0` | Maximum position allocation as a percentage |
| `QUOTE_MAX_AGE_SECONDS` | `10` | Maximum quote age |
| `QUOTE_MAX_FUTURE_SKEW_SECONDS` | `2` | Maximum quote timestamp lead |
| `MAX_EXECUTION_PRICE_DEVIATION_PCT` | `1` | Maximum executable-price deviation, percent |
| `MAX_QUOTE_SPREAD_PCT` | `1` | Maximum quote spread, percent |
| `MIN_GROSS_TO_COST_MULTIPLE` | `3.0` | Minimum expected gross-to-cost multiple |
| `NO_NEW_ENTRIES_MINUTES_BEFORE_CLOSE` | `30` | Stop opening new trades before close |
| `FLATTEN_MINUTES_BEFORE_CLOSE` | `5` | End-of-day flatten lead time |
| `MAX_WAIT_FOR_OPEN_MINUTES` | `45` | Maximum wait for the next market open |
| `MAX_OUTAGE_MINUTES` | `15` | Network-outage kill-switch threshold |

Percentage variables are entered as percentage numbers, not decimal fractions: use `0.50` for 0.50%, not `0.005`. The executable order path also has a hard minimum reward/risk check of 1.10 after quote/price validation and bracket construction. `MIN_REWARD_RISK` is a separate optional configured gate; setting it to zero does not remove the executable 1.10 check.

Change risk, feed and execution settings only after inspecting the code path and testing the consequence. Do not loosen protective limits solely to force more orders.

---

## 7. Caches, logs and runtime state

### Cache files

By default, universe caches are stored under `data/cache/`:

- `structural_universe_cache.json` — validated Layer 1 structural universe.
- `daily_suitability_pool.json` — Layer 2 suitability pool and feed/configuration provenance.

The default location can be overridden with `MM_CACHE_DIR`. The older `MONEY_MINTTER_TEMP_DIR` variable is also recognized as a fallback. These files are stateful runtime inputs, not disposable logs. Do not delete them as a routine troubleshooting step. The application checks their age, universe/configuration signatures and feed compatibility; an invalid cache should be rejected rather than blindly trusted.

### Logs and order journal

By default, runtime records are written under `logs/`. Override the directory with `MM_LOG_DIR`.

| File | Contents |
|---|---|
| `live_market_YYYYMMDD.log` | Date-specific operational log (UTC date) |
| `live_market.log` | Rolling master operational log |
| `order_lifecycle.jsonl` | Order submissions, broker order/fill observations, reconciliation and fee-activity events |
| `mm_session.lock` | Single-session lock |

Logs rotate and old log files are pruned by the logging subsystem. Preserve the daily log and order journal when investigating a session; they are often needed to distinguish signal generation, execution rejection, broker rejection and fill outcomes.

The session journal currently reports realized gross P&L and fee-activity information separately and marks `net_pnl=NOT_CALCULATED`. Do not treat a gross trade-P&L line as verified net account return. Use the final Alpaca equity snapshot as the account-level reference and reconcile fills, fees and deposits/withdrawals before calculating performance.

Generated logs and local runtime state should not be committed as source code. Inspect `git status --short` before committing.

---

## 8. Troubleshooting

### `pytest` is not recognized

Use:

~~~powershell
python -m pytest -q
~~~

If the module is missing, install the development extra into the same interpreter:

~~~powershell
python -m pip install -e ".[dev]"
~~~

### Alpaca authentication or permission errors

Verify that the key and secret belong to the same Alpaca **paper** account, that the credentials are current, and that the account is enabled. Never substitute live credentials as a troubleshooting shortcut.

### Historical data/feed errors

Check the requested feed, account data entitlements, API response and cache provenance. SIP historical suitability and IEX intraday scanning are intentionally separate. Historical fallback is disabled by default; do not enable it just to silence an error without understanding the data-quality consequences.

### No candidates or no orders

A zero-trade session is possible and is not automatically a malfunction. Inspect the logs in order: Layer 1 health, Layer 2 pool size and staleness, Layer 4 selection count, ICT acquisition/evaluation counters, raw entries, entry-mode passes, execution instructions, quote rejections, risk/slot rejections and broker submissions. Do not relax the filters before identifying the stage that removed the candidates.

### `new_entries_disabled=true` or position-management-only mode

Review Layer 1 health, Layer 2 evaluation errors, historical-feed access and cache compatibility. The system is designed to avoid opening new positions when it cannot establish a valid universe.

### Market closed / skipped session

The session runner skips when the next market open is more than the configured wait window away. Check the Alpaca market clock, US exchange holiday schedule and the time at which the process was started.

### Session lock already exists

Do not start a second instance. Verify that no Money Mintter process is still active before taking any action on the lock file. Never remove a lock file to bypass a running session.

### Positions remain after shutdown

Check the Alpaca paper account directly. Reconcile and close remaining positions/orders manually before restarting. A log message or process exit is not a substitute for broker-side verification.

---

## 9. Repository map

~~~text
.
├── money_mintter/
│   ├── brokers/              # Broker interfaces and paper broker support
│   ├── core/                 # Universe, scanning, risk, execution and session orchestration
│   ├── data/                 # Data helpers and historical-data utilities
│   ├── domain/               # Shared domain models
│   ├── research/             # Research, diagnostics and backtesting utilities
│   └── strategy/
│       └── ict/              # ICT V106 strategy, acquisition, adapter and integration
├── docs/                     # Architecture, strategy and research notes
├── tests/                    # Unit and integration tests
├── data/cache/               # Persisted universe/suitability caches
├── logs/                     # Local runtime logs and order journal
├── run_live_market.py        # Production Alpaca paper-session entry point
├── run_paper_demo.py         # Synthetic local demo (not production market scanning)
├── pyproject.toml            # Python package and dependency configuration
└── .env.example              # Local configuration template
~~~

---

## 10. Development rules

- Keep `ict-rebuild` as the default working branch unless there is a deliberate repository-level decision to change it.
- Prefer small, focused commits on the existing branch. Create another branch only for a substantial change that genuinely needs isolation.
- Keep strategy changes separate from data-adapter, execution, risk and operational changes.
- Do not silently alter ICT V106 strategy logic while fixing surrounding data or execution infrastructure.
- Add or update tests for behavioural changes and run the focused tests before the full suite.
- Preserve cache, log and order-journal evidence when investigating a session.
- Treat paper results as observations that require repeated, out-of-sample validation, realistic costs and drawdown analysis.

## Disclaimer

Money Mintter is experimental software for research and paper-trading evaluation. Market data may be delayed, incomplete or unavailable; paper fills may differ from live execution; fees, slippage and liquidity can materially affect results. No return is guaranteed, and no paper-trading result should be treated as a promise of future performance.
