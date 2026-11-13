from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
import pytest

from money_mintter.domain import Side
from money_mintter.config import Settings
from money_mintter.core.live_bot import LivePortfolioManager, LiveMarketBot, normalize_symbol
from money_mintter.strategy.ict.integration import ICTExecutionInstruction, ICTDeduplicationTracker



class FakeQuoteClient:
    def __init__(self, prices=None, *, age_seconds=0.0, quote_overrides=None):
        self.prices = prices or {}
        self.age_seconds = age_seconds
        self.quote_overrides = quote_overrides or {}

    def get_stock_latest_quote(self, request):
        timestamp = datetime.now(timezone.utc) - timedelta(seconds=self.age_seconds)
        quotes = {}
        for symbol in request.symbol_or_symbols:
            if symbol in self.quote_overrides:
                bid, ask = self.quote_overrides[symbol]
            else:
                mid = self.prices.get(symbol, 100.0)
                bid, ask = mid - 0.01, mid + 0.01
            quotes[symbol] = SimpleNamespace(
                timestamp=timestamp,
                bid_price=bid,
                ask_price=ask,
            )
        return quotes



def test_open_order_reconciliation_fails_closed_on_missing_symbol() -> None:
    logger = logging.getLogger("missing_open_order_symbol")
    client = SimpleNamespace(
        get_orders=lambda request: [
            SimpleNamespace(id="unknown-order", symbol=None)
        ]
    )
    pm = LivePortfolioManager(client, logger)

    with pytest.raises(RuntimeError, match="missing or invalid symbol"):
        pm.fetch_open_order_symbols()


def test_ict_instruction_capacity_block_recoverability(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_PORTFOLIO_RISK_PCT", "3")
    logger = logging.getLogger("test")
    client = type("FakeClient", (), {"submit_order": lambda self, req: type("Ord", (), {"order_class": "bracket", "legs": [1, 2], "id": "123"})()})()
    pm = LivePortfolioManager(client, logger, stock_client=FakeQuoteClient({"AAPL": 100.0}))
    settings = Settings()

    inst = ICTExecutionInstruction(
        symbol="AAPL",
        side=Side.BUY,
        entry_price=100.0,
        stop_price=98.0,
        target_price=105.0,
        risk_pts=2.0,
        timestamp=datetime.now(timezone.utc),
        timestamp_ns=1000000,
        zone_type="disp_fvg",
        score=1.0,
        zone_top=101.0,
        zone_bot=99.0,
        reason="ict_test",
        is_diagnostic=False,
    )

    tracker = ICTDeduplicationTracker()

    # Case 1: 5 positions locked -> capacity full (0 untraded slots) -> instruction blocked, not marked executed
    locked_full = {"S1", "S2", "S3", "S4", "S5"}
    sent_full = pm.submit_ict_instructions([inst], locked_full, 100000.0, settings, dedup_tracker=tracker)
    assert sent_full == 0
    assert not tracker.is_executed("AAPL", "bull", "disp_fvg", 101.0, 99.0)

    # Case 2: Slot opens up (4 positions locked) -> instruction is recoverable and successfully submitted
    locked_open = {"S1", "S2", "S3", "S4"}
    sent_open = pm.submit_ict_instructions([inst], locked_open, 100000.0, settings, dedup_tracker=tracker)
    assert sent_open == 1
    assert tracker.is_executed("AAPL", "bull", "disp_fvg", 101.0, 99.0)

    # Case 3: Subsequent submission attempt is deduplicated (0 orders sent)
    sent_dup = pm.submit_ict_instructions([inst], locked_open, 100000.0, settings, dedup_tracker=tracker)
    assert sent_dup == 0


def test_run_cycle_uses_ict_exclusively_despite_legacy_strategy(monkeypatch: pytest.MonkeyPatch) -> None:
    from money_mintter.core.live_bot import LiveMarketBot, LivePortfolioManager, MarketScanner

    bot = LiveMarketBot.__new__(LiveMarketBot)
    bot.logger = logging.getLogger("test")
    bot.settings = Settings()
    bot.trading_client = type("FakeClient", (), {"fetch_account": lambda self: (100000.0, 100000.0), "fetch_positions": lambda self: [], "submit_order": lambda self, req: type("Ord", (), {"order_class": "bracket", "legs": [1, 2], "id": "123"})()})()
    bot.stock_client = type("StockClient", (), {})()
    bot.kill_switch = type("KillSwitch", (), {"tripped": False, "check_daily_loss": lambda *args: None})()
    bot.portfolio_manager = LivePortfolioManager(bot.trading_client, bot.logger)
    bot.global_universe = ["AAPL"]
    bot.latest_state = {}
    bot.processed_signals = set()
    bot.ict_dedup_tracker = ICTDeduplicationTracker()

    # Assign a legacy strategy attribute without use_legacy=True
    bot.strategy = object()

    ict_called = False
    legacy_called = False

    class MockScanner:
        last_scan_attempts = 1
        last_scan_errors = 0
        last_scan_connectivity_errors = 0
        def scan_universe(self, now_utc, locked_symbols, strategy=None, daily_pool=None, dedup_tracker=None, **kwargs):
            nonlocal ict_called, legacy_called
            if dedup_tracker is not None or strategy is None:
                ict_called = True
                return (), []
            else:
                legacy_called = True
                return [], []

    bot.scanner = MockScanner()
    monkeypatch.setattr(MarketScanner, "is_session_open", lambda sym, now: (True, "Open"))
    monkeypatch.setattr(bot.portfolio_manager, "fetch_account_and_positions", lambda: (100000.0, 100000.0, 0.0, [], [], []))
    monkeypatch.setattr(bot.portfolio_manager, "fetch_open_order_symbols", lambda: set())
    monkeypatch.setattr(bot.portfolio_manager, "submit_ict_instructions", lambda *args, **kwargs: 0)

    now_utc = datetime(2026, 10, 18, 10, 10, 0, tzinfo=timezone.utc)
    bot.run_cycle(now_utc)

    assert ict_called, "Production run_cycle() must invoke ICT scanning path"
    assert not legacy_called, "Production run_cycle() must not invoke legacy strategy path even if self.strategy attribute exists"


def test_ict_ambiguous_submission_accepted() -> None:
    logger = logging.getLogger("test")
    client = type("FakeClient", (), {"submit_order": lambda self, req: type("Ord", (), {"order_class": "bracket", "legs": [1, 2], "id": "123"})()})()
    pm = LivePortfolioManager(client, logger, stock_client=FakeQuoteClient({"AAPL": 100.0}))
    settings = Settings()
    tracker = ICTDeduplicationTracker()

    inst = ICTExecutionInstruction(
        symbol="AAPL", side=Side.BUY, entry_price=100.0, stop_price=98.0, target_price=105.0,
        risk_pts=2.0, timestamp=datetime.now(timezone.utc), timestamp_ns=1000, zone_type="disp_fvg",
        score=1.0, zone_top=101.0, zone_bot=99.0, reason="test"
    )

    sent = pm.submit_ict_instructions([inst], set(), 100000.0, settings, dedup_tracker=tracker)
    assert sent == 1
    assert tracker.is_executed("AAPL", "bull", "disp_fvg", 101.0, 99.0)
    assert not tracker.is_pending("AAPL", "bull", "disp_fvg", 101.0, 99.0)


def test_ict_ambiguous_submission_explicit_rejection() -> None:
    logger = logging.getLogger("test")
    class RejectClient:
        def submit_order(self, req):
            raise ValueError("Order rejected by broker: insufficient funds")
    pm = LivePortfolioManager(RejectClient(), logger, stock_client=FakeQuoteClient({"AAPL": 100.0}))
    settings = Settings()
    tracker = ICTDeduplicationTracker()

    inst = ICTExecutionInstruction(
        symbol="AAPL", side=Side.BUY, entry_price=100.0, stop_price=98.0, target_price=105.0,
        risk_pts=2.0, timestamp=datetime.now(timezone.utc), timestamp_ns=1000, zone_type="disp_fvg",
        score=1.0, zone_top=101.0, zone_bot=99.0, reason="test"
    )

    sent = pm.submit_ict_instructions([inst], set(), 100000.0, settings, dedup_tracker=tracker)
    assert sent == 0
    assert not tracker.is_executed("AAPL", "bull", "disp_fvg", 101.0, 99.0)
    assert not tracker.is_pending("AAPL", "bull", "disp_fvg", 101.0, 99.0)


def test_ict_ambiguous_submission_timeout_retains_pending() -> None:
    logger = logging.getLogger("test")
    class TimeoutClient:
        def submit_order(self, req):
            raise TimeoutError("Connection timed out")
    pm = LivePortfolioManager(TimeoutClient(), logger, stock_client=FakeQuoteClient({"AAPL": 100.0}))
    settings = Settings()
    tracker = ICTDeduplicationTracker()

    inst = ICTExecutionInstruction(
        symbol="AAPL", side=Side.BUY, entry_price=100.0, stop_price=98.0, target_price=105.0,
        risk_pts=2.0, timestamp=datetime.now(timezone.utc), timestamp_ns=1000, zone_type="disp_fvg",
        score=1.0, zone_top=101.0, zone_bot=99.0, reason="test"
    )

    # Cycle 1: Timeout occurs -> retains pending and blocks further orders
    sent1 = pm.submit_ict_instructions([inst], set(), 100000.0, settings, dedup_tracker=tracker)
    assert sent1 == 0
    assert tracker.is_pending("AAPL", "bull", "disp_fvg", 101.0, 99.0)
    assert not tracker.is_executed("AAPL", "bull", "disp_fvg", 101.0, 99.0)

    # Cycle 2: Even with no broker order visible yet, pending state is retained and blocks resubmission
    sent2 = pm.submit_ict_instructions([inst], set(), 100000.0, settings, dedup_tracker=tracker, locked_normalized=set())
    assert sent2 == 0
    assert tracker.is_pending("AAPL", "bull", "disp_fvg", 101.0, 99.0)


def test_ict_ambiguous_submission_reconciliation() -> None:
    logger = logging.getLogger("test")
    client = type("FakeClient", (), {})()
    pm = LivePortfolioManager(client, logger, stock_client=FakeQuoteClient({"AAPL": 100.0}))
    settings = Settings()
    tracker = ICTDeduplicationTracker()

    # Simulate timeout placing symbol in pending
    tracker.mark_pending("AAPL", "bull", "disp_fvg", 101.0, 99.0)
    assert tracker.is_pending("AAPL", "bull", "disp_fvg", 101.0, 99.0)

    bot = LiveMarketBot.__new__(LiveMarketBot)
    bot.logger = logger
    bot.settings = settings
    bot.trading_client = client
    bot.portfolio_manager = pm
    bot.ict_dedup_tracker = tracker

    open_order_symbols = {"AAPL"}
    locked_normalized = {normalize_symbol("AAPL")} | open_order_symbols
    if bot.ict_dedup_tracker is not None:
        for p_sym in list(bot.ict_dedup_tracker.pending_symbols):
            if p_sym in locked_normalized:
                bot.ict_dedup_tracker.resolve_pending_to_executed_for_symbol(p_sym)

    assert not tracker.is_pending("AAPL", "bull", "disp_fvg", 101.0, 99.0)
    assert tracker.is_executed("AAPL", "bull", "disp_fvg", 101.0, 99.0)


def test_ict_ambiguous_submission_generic_exception_retains_pending() -> None:
    logger = logging.getLogger("test")
    class GenericErrorClient:
        def submit_order(self, req):
            raise RuntimeError("Unexpected internal system error")
    pm = LivePortfolioManager(GenericErrorClient(), logger, stock_client=FakeQuoteClient({"AAPL": 100.0}))
    settings = Settings()
    tracker = ICTDeduplicationTracker()

    inst = ICTExecutionInstruction(
        symbol="AAPL", side=Side.BUY, entry_price=100.0, stop_price=98.0, target_price=105.0,
        risk_pts=2.0, timestamp=datetime.now(timezone.utc), timestamp_ns=1000, zone_type="disp_fvg",
        score=1.0, zone_top=101.0, zone_bot=99.0, reason="test"
    )

    # Cycle 1: Generic unexpected exception occurs -> retains pending and blocks further submissions
    sent1 = pm.submit_ict_instructions([inst], set(), 100000.0, settings, dedup_tracker=tracker)
    assert sent1 == 0
    assert tracker.is_pending("AAPL", "bull", "disp_fvg", 101.0, 99.0)
    assert not tracker.is_executed("AAPL", "bull", "disp_fvg", 101.0, 99.0)

    # Cycle 2: Pending state is retained, preventing resubmission
    sent2 = pm.submit_ict_instructions([inst], set(), 100000.0, settings, dedup_tracker=tracker, locked_normalized=set())
    assert sent2 == 0
    assert tracker.is_pending("AAPL", "bull", "disp_fvg", 101.0, 99.0)


def test_ict_ambiguous_submission_http_408_retains_pending() -> None:
    logger = logging.getLogger("test")
    from alpaca.common.exceptions import APIError
    class HTTP408Client:
        def submit_order(self, req):
            err = APIError("Request Timeout")
            err.status_code = 408
            raise err
    pm = LivePortfolioManager(HTTP408Client(), logger, stock_client=FakeQuoteClient({"AAPL": 100.0}))
    settings = Settings()
    tracker = ICTDeduplicationTracker()

    inst = ICTExecutionInstruction(
        symbol="AAPL", side=Side.BUY, entry_price=100.0, stop_price=98.0, target_price=105.0,
        risk_pts=2.0, timestamp=datetime.now(timezone.utc), timestamp_ns=1000, zone_type="disp_fvg",
        score=1.0, zone_top=101.0, zone_bot=99.0, reason="test"
    )

    sent1 = pm.submit_ict_instructions([inst], set(), 100000.0, settings, dedup_tracker=tracker)
    assert sent1 == 0
    assert tracker.is_pending("AAPL", "bull", "disp_fvg", 101.0, 99.0)
    assert not tracker.is_executed("AAPL", "bull", "disp_fvg", 101.0, 99.0)


def test_market_scanner_ict_historical_lookback_window(monkeypatch: pytest.MonkeyPatch) -> None:
    from money_mintter.core.live_bot import MarketScanner, HISTORY_DAYS
    from money_mintter.strategy import Candle

    logger = logging.getLogger("test")

    class FakeStockClient:
        def get_stock_bars(self, req):
            return type("Resp", (), {"data": {}, "next_page_token": None})()

    stock_client = FakeStockClient()
    scanner = MarketScanner(["AAPL"], stock_client, logger)

    now_utc = datetime(2026, 10, 18, 12, 0, 0, tzinfo=timezone.utc)

    recent_ts = now_utc - timedelta(minutes=4)
    cached_candles = [
        Candle(timestamp=recent_ts - timedelta(minutes=i), open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0, symbol="AAPL")
        for i in range(5)
    ]
    scanner._candle_cache["AAPL"] = cached_candles

    mock_candidate = type("Candidate", (), {"symbol": "AAPL"})()
    monkeypatch.setattr("money_mintter.core.live_bot.select_layer4_candidates", lambda pool, bars, max_candidates: [mock_candidate])

    class CaptureStop(BaseException):
        pass

    captured_start = None
    captured_end = None
    def mock_fetch_ict(client, symbol, start_dt, end_dt, feed="iex"):
        nonlocal captured_start, captured_end
        captured_start = start_dt
        captured_end = end_dt
        raise CaptureStop("Captured arguments successfully")

    monkeypatch.setattr("money_mintter.core.live_bot.fetch_ict_market_data", mock_fetch_ict)
    monkeypatch.setattr(MarketScanner, "is_session_open", lambda sym, now: (True, "Open"))

    with pytest.raises(CaptureStop):
        scanner.scan_universe(now_utc=now_utc, locked_symbols=set(), strategy=None)

    expected_start = now_utc - timedelta(days=HISTORY_DAYS)
    assert captured_start == expected_start, f"Expected start_dt {expected_start}, got {captured_start}"
    assert captured_start != recent_ts - timedelta(minutes=15)
    assert captured_end == now_utc


@pytest.mark.parametrize(
    ("bar_age_minutes", "expected_symbols"),
    [(1, ["AAPL"]), (2, [])],
)
def test_scanner_filters_stale_bars_before_layer4(
    monkeypatch: pytest.MonkeyPatch,
    bar_age_minutes: int,
    expected_symbols: list[str],
) -> None:
    from money_mintter.core.live_bot import MarketScanner

    now_utc = datetime(2026, 10, 18, 12, 0, 20, tzinfo=timezone.utc)
    raw_bar = SimpleNamespace(
        timestamp=now_utc - timedelta(minutes=bar_age_minutes),
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=10_000.0,
    )

    class StockClient:
        def get_stock_bars(self, request):
            return SimpleNamespace(data={"AAPL": [raw_bar]}, next_page_token=None)

    scanner = MarketScanner(["AAPL"], StockClient(), logging.getLogger("layer4_freshness"))
    selector_inputs: list[list[str]] = []

    def selector_spy(pool, bars, max_candidates=8):
        selector_inputs.append(list(bars))
        return []

    monkeypatch.setattr("money_mintter.core.live_bot.select_layer4_candidates", selector_spy)
    monkeypatch.setattr(
        MarketScanner,
        "is_session_open",
        staticmethod(lambda symbol, now: (True, "Open")),
    )

    instructions, _ = scanner.scan_universe(now_utc, set(), strategy=None)

    assert instructions == ()
    assert selector_inputs == [expected_symbols]
    assert scanner.last_l4_selected == 0






def test_scanner_excludes_malformed_symbol_bars_without_aborting_other_symbols(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from money_mintter.core.live_bot import MarketScanner

    now_utc = datetime(2026, 10, 18, 12, 0, 20, tzinfo=timezone.utc)
    valid_bar = SimpleNamespace(
        timestamp=now_utc - timedelta(minutes=1),
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=10_000.0,
    )
    malformed_bar = SimpleNamespace(
        timestamp=now_utc - timedelta(minutes=1),
        open=float("nan"),
        high=101.0,
        low=99.0,
        close=100.5,
        volume=10_000.0,
    )

    class StockClient:
        def get_stock_bars(self, request):
            return SimpleNamespace(
                data={"AAPL": [malformed_bar], "MSFT": [valid_bar]},
                next_page_token=None,
            )

    scanner = MarketScanner(
        ["AAPL", "MSFT"], StockClient(), logging.getLogger("malformed_bar_isolation")
    )
    selector_inputs: list[list[str]] = []

    def selector_spy(pool, bars, max_candidates=8):
        selector_inputs.append(list(bars))
        return []

    monkeypatch.setattr("money_mintter.core.live_bot.select_layer4_candidates", selector_spy)
    monkeypatch.setattr(
        MarketScanner,
        "is_session_open",
        staticmethod(lambda symbol, now: (True, "Open")),
    )

    instructions, _ = scanner.scan_universe(now_utc, set(), strategy=None)

    assert instructions == ()
    assert selector_inputs == [["MSFT"]]
    assert scanner.last_scan_attempts == 2
    assert scanner.last_scan_errors == 1


def _execution_instruction(
    symbol: str,
    *,
    entry: float = 100.0,
    stop: float = 98.0,
    target: float = 105.0,
    timestamp_ns: int = 1000,
    zone_top: float = 101.0,
    zone_bot: float = 99.0,
) -> ICTExecutionInstruction:
    return ICTExecutionInstruction(
        symbol=symbol,
        side=Side.BUY,
        entry_price=entry,
        stop_price=stop,
        target_price=target,
        risk_pts=abs(entry - stop),
        timestamp=datetime.now(timezone.utc),
        timestamp_ns=timestamp_ns,
        zone_type="disp_fvg",
        score=1.0,
        zone_top=zone_top,
        zone_bot=zone_bot,
        reason="phase2c_test",
    )



@pytest.mark.parametrize(
    ("b1_age_minutes", "b5_age_minutes", "expected_instruction_count"),
    [
        (1, 5, 1),   # latest completed 1m and 5m bars
        (2, 5, 0),   # 1m stream is stale even though 5m is fresh
        (1, 10, 0),  # 5m stream is stale even though 1m is fresh
    ],
)
def test_scanner_uses_timeframe_specific_ict_freshness_gates(
    monkeypatch: pytest.MonkeyPatch,
    b1_age_minutes: int,
    b5_age_minutes: int,
    expected_instruction_count: int,
) -> None:
    from money_mintter.core.live_bot import MarketScanner

    now_utc = datetime(2026, 10, 18, 12, 5, 20, tzinfo=timezone.utc)
    fresh_l1_bar = SimpleNamespace(
        timestamp=now_utc - timedelta(minutes=1),
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=10_000.0,
    )

    class StockClient:
        def get_stock_bars(self, request):
            return SimpleNamespace(data={"AAPL": [fresh_l1_bar]}, next_page_token=None)

    scanner = MarketScanner(
        ["AAPL"], StockClient(), logging.getLogger("timeframe_specific_ict_freshness")
    )
    candidate = SimpleNamespace(symbol="AAPL")
    monkeypatch.setattr(
        "money_mintter.core.live_bot.select_layer4_candidates",
        lambda pool, bars, max_candidates=8: [candidate],
    )
    monkeypatch.setattr(
        MarketScanner,
        "is_session_open",
        staticmethod(lambda symbol, now: (True, "Open")),
    )
    monkeypatch.setattr(
        "money_mintter.core.live_bot.fetch_ict_market_data",
        lambda *args, **kwargs: SimpleNamespace(
            b1_success=True, b5_success=True, error_message=None
        ),
    )

    b1_ns = int(
        (now_utc - timedelta(minutes=b1_age_minutes)).timestamp() * 1_000_000_000
    )
    b5_ns = int(
        (now_utc - timedelta(minutes=b5_age_minutes)).timestamp() * 1_000_000_000
    )
    adapter_result = SimpleNamespace(
        b1=({"time_ns": b1_ns},),
        b5=({"time_ns": b5_ns},),
        b15=(),
        b15_coverage=(),
    )
    monkeypatch.setattr(
        "money_mintter.core.live_bot.generate_production_ict_signals",
        lambda *args, **kwargs: (
            adapter_result,
            (_execution_instruction("AAPL"),),
        ),
    )

    instructions, _ = scanner.scan_universe(now_utc, set(), strategy=None)

    assert len(instructions) == expected_instruction_count
    assert scanner.ict_attempts == 1
    assert scanner.ict_acquisition_successes == 1
    assert scanner.ict_evaluation_successes == expected_instruction_count
    assert scanner.ict_instructions == expected_instruction_count

def test_pending_orders_consume_capacity_and_duplicate_symbols_are_filtered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_PORTFOLIO_RISK_PCT", "3")
    logger = logging.getLogger("phase2c_capacity")
    submitted: list[str] = []

    class RecordingBroker:
        def submit_order(self, req):
            submitted.append(req.symbol)
            return type("Order", (), {"order_class": "bracket", "legs": [1, 2], "id": f"id_{req.symbol}"})()

    pm = LivePortfolioManager(
        RecordingBroker(),
        logger,
        stock_client=FakeQuoteClient({
            "AAPL": 100.0, "MSFT": 100.0, "NVDA": 100.0,
            "EXPENSIVE": 1_000.0, "VALID": 100.0,
        }),
    )
    settings = Settings()
    tracker = ICTDeduplicationTracker()

    first_aapl = _execution_instruction("AAPL", timestamp_ns=1, zone_top=101.0, zone_bot=99.0)
    duplicate_aapl = _execution_instruction("AAPL", timestamp_ns=2, zone_top=102.0, zone_bot=100.0)
    msft = _execution_instruction("MSFT", timestamp_ns=3)
    nvda = _execution_instruction("NVDA", timestamp_ns=4)

    sent = pm.submit_ict_instructions(
        [first_aapl, duplicate_aapl, msft, nvda],
        locked_symbols={"HELD_POSITION"},
        account_equity=100_000.0,
        settings=settings,
        dedup_tracker=tracker,
        locked_normalized={"HELD_POSITION", "OPEN_ORDER_1", "OPEN_ORDER_2"},
        buying_power=100_000.0,
    )

    # One position + two different-symbol open orders occupy three slots.
    # Only two slots remain, and the second AAPL instruction cannot use another slot.
    assert sent == 2
    assert submitted == ["AAPL", "MSFT"]
    assert tracker.is_executed("AAPL", "bull", "disp_fvg", 101.0, 99.0)
    assert not tracker.is_executed("AAPL", "bull", "disp_fvg", 102.0, 100.0)
    assert tracker.is_executed("MSFT", "bull", "disp_fvg", 101.0, 99.0)


def test_risk_or_capital_rejection_backfills_next_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_OPEN_POSITIONS", "1")
    monkeypatch.setenv("MAX_PORTFOLIO_RISK_PCT", "3")
    logger = logging.getLogger("phase2c_backfill")
    submitted: list[str] = []

    class RecordingBroker:
        def submit_order(self, req):
            submitted.append(req.symbol)
            return type("Order", (), {"order_class": "bracket", "legs": [1, 2], "id": "valid_order"})()

    pm = LivePortfolioManager(
        RecordingBroker(),
        logger,
        stock_client=FakeQuoteClient({
            "AAPL": 100.0, "MSFT": 100.0, "NVDA": 100.0,
            "EXPENSIVE": 1_000.0, "VALID": 100.0,
        }),
    )
    settings = Settings()
    assert settings.max_open_positions == 1

    too_expensive = _execution_instruction("EXPENSIVE", entry=1_000.0, stop=980.0, target=1_050.0)
    valid = _execution_instruction("VALID", entry=100.0, stop=98.0, target=105.0, timestamp_ns=2)

    sent = pm.submit_ict_instructions(
        [too_expensive, valid],
        locked_symbols=set(),
        account_equity=100_000.0,
        settings=settings,
        open_exposure=99_500.0,
        buying_power=500.0,
    )

    # EXPENSIVE cannot fit the available $500 allocation. It must not consume the
    # only slot; VALID is evaluated and submitted instead.
    assert sent == 1
    assert submitted == ["VALID"]


def test_max_open_positions_setting_is_enforced_against_positions_and_orders(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_OPEN_POSITIONS", "2")
    logger = logging.getLogger("phase2c_capacity_config")
    submitted: list[str] = []

    class RecordingBroker:
        def submit_order(self, req):
            submitted.append(req.symbol)
            return type("Order", (), {"order_class": "bracket", "legs": [1, 2], "id": "unexpected"})()

    pm = LivePortfolioManager(
        RecordingBroker(),
        logger,
        stock_client=FakeQuoteClient({
            "AAPL": 100.0, "MSFT": 100.0, "NVDA": 100.0,
            "EXPENSIVE": 1_000.0, "VALID": 100.0,
        }),
    )
    settings = Settings()
    assert settings.max_open_positions == 2

    sent = pm.submit_ict_instructions(
        [_execution_instruction("AAPL")],
        locked_symbols={"POSITION_1"},
        account_equity=100_000.0,
        settings=settings,
        locked_normalized={"POSITION_1", "OPEN_ORDER_1"},
    )

    assert sent == 0
    assert submitted == []


def test_execution_preflight_rejects_stale_quote() -> None:
    logger = logging.getLogger("phase2c_stale_quote")
    submitted: list[str] = []

    class Broker:
        def submit_order(self, req):
            submitted.append(req.symbol)
            return type("Order", (), {"order_class": "bracket", "legs": [1, 2], "id": "stale"})()

    tracker = ICTDeduplicationTracker()
    pm = LivePortfolioManager(
        Broker(),
        logger,
        stock_client=FakeQuoteClient({"AAPL": 100.0}, age_seconds=30.0),
    )
    inst = _execution_instruction("AAPL")

    sent = pm.submit_ict_instructions([inst], set(), 100_000.0, Settings(), dedup_tracker=tracker)

    assert sent == 0
    assert submitted == []
    assert not tracker.is_pending("AAPL", "bull", "disp_fvg", 101.0, 99.0)


def test_execution_preflight_rechecks_reward_risk_at_executable_ask() -> None:
    logger = logging.getLogger("phase2c_quote_rr")
    submitted: list[str] = []

    class Broker:
        def submit_order(self, req):
            submitted.append(req.symbol)
            return type("Order", (), {"order_class": "bracket", "legs": [1, 2], "id": "rr"})()

    # Signal R/R is 1.10 at 100.00. A valid but higher ask at 100.50
    # reduces executable R/R below 1.10 and must be rejected.
    pm = LivePortfolioManager(
        Broker(),
        logger,
        stock_client=FakeQuoteClient(
            {"AAPL": 100.0},
            quote_overrides={"AAPL": (100.49, 100.50)},
        ),
    )
    inst = _execution_instruction("AAPL", entry=100.0, stop=99.0, target=101.1)

    sent = pm.submit_ict_instructions([inst], set(), 100_000.0, Settings())

    assert sent == 0
    assert submitted == []



def test_ict_execution_rejects_target_profit_below_commission_gate() -> None:
    logger = logging.getLogger("ict_commission_cost_gate")
    submitted: list[str] = []

    class Broker:
        def submit_order(self, request):
            submitted.append(request.symbol)
            return type(
                "Order",
                (),
                {"order_class": "bracket", "legs": [1, 2], "id": "cost-gate"},
            )()

    pm = LivePortfolioManager(
        Broker(),
        logger,
        stock_client=FakeQuoteClient({"AAPL": 10.0}),
    )
    settings = Settings()
    instruction = _execution_instruction(
        "AAPL",
        entry=10.0,
        stop=9.51,
        target=10.57,
    )

    sent = pm.submit_ict_instructions([instruction], set(), 100.0, settings)

    assert sent == 0
    assert submitted == []
    assert pm.last_submit_failures == 0


def test_execution_preflight_fails_closed_without_quote_client() -> None:
    logger = logging.getLogger("phase2c_missing_quote")
    submitted: list[str] = []

    class Broker:
        def submit_order(self, req):
            submitted.append(req.symbol)
            return type("Order", (), {"order_class": "bracket", "legs": [1, 2], "id": "missing"})()

    pm = LivePortfolioManager(Broker(), logger)
    sent = pm.submit_ict_instructions([_execution_instruction("AAPL")], set(), 100_000.0, Settings())

    assert sent == 0
    assert submitted == []



def test_executable_quote_is_side_aware_and_uses_configured_feed() -> None:
    logger = logging.getLogger("phase2c_side_aware_quote")
    pm = LivePortfolioManager(
        object(),
        logger,
        stock_client=FakeQuoteClient({"AAPL": 100.0}),
    )
    settings = Settings()

    buy_price = pm._get_executable_price("AAPL", Side.BUY, 100.0, settings)
    sell_price = pm._get_executable_price("AAPL", Side.SELL, 100.0, settings)

    assert buy_price == pytest.approx(100.01)
    assert sell_price == pytest.approx(99.99)


def test_execution_preflight_rejects_excessive_quote_deviation() -> None:
    logger = logging.getLogger("phase2c_quote_deviation")
    submitted: list[str] = []

    class Broker:
        def submit_order(self, req):
            submitted.append(req.symbol)
            return type("Order", (), {"order_class": "bracket", "legs": [1, 2], "id": "deviation"})()

    pm = LivePortfolioManager(
        Broker(),
        logger,
        stock_client=FakeQuoteClient(
            {"AAPL": 100.0},
            quote_overrides={"AAPL": (101.00, 101.02)},
        ),
    )
    sent = pm.submit_ict_instructions([_execution_instruction("AAPL")], set(), 100_000.0, Settings())

    assert sent == 0
    assert submitted == []
