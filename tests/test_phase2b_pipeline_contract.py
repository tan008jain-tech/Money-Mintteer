from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest
from alpaca.data.enums import DataFeed
from alpaca.trading.enums import AssetClass, AssetStatus

from money_mintter.config import Settings
from money_mintter.core.eligibility import StructuralEligibilityEngine
from money_mintter.core.live_bot import LiveMarketBot, MarketScanner, ConnectivityError, discover_universe
from money_mintter.core.suitability import DailySuitabilityEngine, SuitabilityResult
from money_mintter.strategy.ict.acquisition import fetch_ict_market_data


class FakeAsset:
    def __init__(self, symbol: str, status: AssetStatus = AssetStatus.ACTIVE, tradable: bool = True):
        self.symbol = symbol
        self.status = status
        self.tradable = tradable
        self.asset_class = AssetClass.US_EQUITY
        self.name = f"{symbol} Common Stock"
        self.exchange = "NASDAQ"
        self.shortable = True
        self.fractionable = True
        self.marginable = True


class MockTradingClient:
    def __init__(self, assets: list[Any] | None = None, should_raise: bool = False, return_none: bool = False):
        self.assets = assets or []
        self.should_raise = should_raise
        self.return_none = return_none

    def get_all_assets(self, request=None):
        if self.should_raise:
            raise RuntimeError("API failure")
        if self.return_none:
            return None
        return self.assets


@pytest.fixture(autouse=True)
def _setup_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MM_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("MONEY_MINTTER_TEMP_DIR", str(tmp_path))
    monkeypatch.setenv("MIN_UNIVERSE_HEALTH", "1")
    monkeypatch.setenv("MAX_UNIVERSE_CACHE_AGE_HOURS", "24")
    monkeypatch.setenv("MAX_DAILY_POOL_SIZE", "50")
    monkeypatch.setenv("ALPACA_HISTORICAL_FEED", "sip")
    monkeypatch.setenv("ALPACA_REALTIME_FEED", "iex")
    monkeypatch.setenv("ALPACA_ALLOW_HISTORICAL_FEED_FALLBACK", "false")
    monkeypatch.delenv("ALPACA_DATA_FEED", raising=False)


def _metadata(symbol: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "name": f"{symbol} Common Stock",
        "exchange": "NASDAQ",
        "shortable": True,
        "fractionable": True,
        "marginable": True,
        "asset_class": "us_equity",
        "status": "active",
        "tradable": True,
    }


def test_feed_split_defaults_and_layer2_pool_target(monkeypatch):
    for key in (
        "ALPACA_HISTORICAL_FEED",
        "ALPACA_REALTIME_FEED",
        "ALPACA_DATA_FEED",
        "MAX_DAILY_POOL_SIZE",
        "MIN_UNIVERSE_HEALTH",
    ):
        monkeypatch.delenv(key, raising=False)
    settings = Settings()
    assert settings.alpaca_historical_feed == "sip"
    assert settings.alpaca_realtime_feed == "iex"
    assert settings.max_daily_pool_size == 50
    assert settings.min_universe_health == 50
    assert settings.max_universe_cache_age_hours == 24.0
    assert settings.alpaca_allow_historical_feed_fallback is False


def test_normal_layer1_layer2_layer4_pipeline(monkeypatch):
    assets = [FakeAsset(f"SYM{i}") for i in range(10)]
    logger = logging.getLogger("test_phase2b_flow")

    def mock_evaluate(self, symbols, now_utc=None, force_refresh=False):
        return SuitabilityResult(
            pool=symbols[:5],
            quarantined=[],
            metrics={},
            exclusion_counts={},
            requested_feed=self.historical_feed.upper(),
            actual_feed=self.historical_feed.upper(),
            data_source=self.historical_feed.upper(),
            generated_at=datetime.now(timezone.utc).isoformat(),
            stale=False,
            cached=False,
            config_signature=self._compute_config_signature(),
            universe_signature=self._compute_universe_signature(symbols),
        )

    monkeypatch.setattr(
        "money_mintter.core.suitability.DailySuitabilityEngine.evaluate_universe",
        mock_evaluate,
    )
    layer2_pool = discover_universe(MockTradingClient(assets), logger, api_key="test")
    assert layer2_pool == [f"SYM{i}" for i in range(5)]
    assert len(layer2_pool) <= 50

    selector_inputs: list[list[str]] = []

    def selector_spy(symbols, bars_dict, max_candidates=8):
        selector_inputs.append(list(symbols))
        return []

    monkeypatch.setattr("money_mintter.core.live_bot.select_layer4_candidates", selector_spy)

    class EmptyStockClient:
        def get_stock_bars(self, request):
            return MagicMock(data={}, next_page_token=None)

    scanner = MarketScanner(layer2_pool, EmptyStockClient(), logger, data_feed=DataFeed.IEX)
    monkeypatch.setattr(
        MarketScanner,
        "is_session_open",
        staticmethod(lambda symbol, now: (True, "Open")),
    )
    scanner.scan_universe(datetime(2026, 11, 13, 15, 0, tzinfo=timezone.utc), set())
    assert selector_inputs == [layer2_pool]
    assert scanner.last_l2_eligible == len(layer2_pool)


def test_undersized_layer1_discovery_fails_closed(monkeypatch):
    monkeypatch.setenv("MIN_UNIVERSE_HEALTH", "50")
    monkeypatch.setattr(
        "money_mintter.core.suitability.DailySuitabilityEngine.evaluate_universe",
        lambda *args, **kwargs: pytest.fail("Layer 2 must not receive an unhealthy Layer 1 universe"),
    )
    pool = discover_universe(
        MockTradingClient([FakeAsset("AAPL"), FakeAsset("MSFT")]),
        logging.getLogger("test_undersized"),
    )
    assert pool == []


@pytest.mark.parametrize(
    "failure_mode",
    ["api_exception", "none_response", "empty_response", "undersized_live"],
)
def test_recent_validated_layer1_cache_reenters_layer2(monkeypatch, tmp_path, failure_mode):
    monkeypatch.setenv("MIN_UNIVERSE_HEALTH", "50")
    logger = logging.getLogger("test_l1_cache_fallback")
    engine = StructuralEligibilityEngine(cache_dir=str(tmp_path))
    symbols, metadata, _ = engine.filter_universe(
        [FakeAsset(f"SYM{i:02d}") for i in range(55)],
        logger,
    )
    engine.save_persisted_cache(
        symbols,
        metadata,
        datetime.now(timezone.utc) - timedelta(hours=2),
    )
    evaluated_inputs: list[list[str]] = []

    def mock_evaluate(self, eligible_symbols, now_utc=None, force_refresh=False):
        evaluated_inputs.append(list(eligible_symbols))
        return SuitabilityResult(
            pool=eligible_symbols[:7],
            quarantined=[],
            metrics={},
            exclusion_counts={},
            requested_feed="SIP",
            actual_feed="SIP",
            data_source="SIP",
            generated_at=datetime.now(timezone.utc).isoformat(),
            stale=False,
            cached=False,
            config_signature=self._compute_config_signature(),
            universe_signature=self._compute_universe_signature(eligible_symbols),
        )

    monkeypatch.setattr(
        "money_mintter.core.suitability.DailySuitabilityEngine.evaluate_universe",
        mock_evaluate,
    )

    if failure_mode == "api_exception":
        client = MockTradingClient(should_raise=True)
    elif failure_mode == "none_response":
        client = MockTradingClient(return_none=True)
    elif failure_mode == "empty_response":
        client = MockTradingClient(assets=[])
    else:
        # A live response below the configured health floor must not bypass
        # Layer 1 cache validation or skip the subsequent Layer 2 evaluation.
        client = MockTradingClient([FakeAsset("LIVE1"), FakeAsset("LIVE2")])

    pool = discover_universe(client, logger)
    assert evaluated_inputs == [symbols]
    assert pool == symbols[:7]


@pytest.mark.parametrize("response_mode", ["none", "empty"])
def test_none_and_empty_layer1_responses_fail_closed_without_cache(
    monkeypatch, response_mode
):
    monkeypatch.setattr(
        "money_mintter.core.suitability.DailySuitabilityEngine.evaluate_universe",
        lambda *args, **kwargs: pytest.fail(
            "Layer 2 must not receive an empty or unavailable Layer 1 universe"
        ),
    )
    client = (
        MockTradingClient(return_none=True)
        if response_mode == "none"
        else MockTradingClient(assets=[])
    )

    if response_mode == "none":
        with pytest.raises(RuntimeError, match="returned None"):
            discover_universe(client, logging.getLogger("test_l1_none"))
    else:
        assert discover_universe(
            client, logging.getLogger("test_l1_empty")
        ) == []


def test_expired_layer1_cache_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("MIN_UNIVERSE_HEALTH", "50")
    logger = logging.getLogger("test_expired_l1")
    engine = StructuralEligibilityEngine(cache_dir=str(tmp_path))
    symbols, metadata, _ = engine.filter_universe(
        [FakeAsset(f"SYM{i:02d}") for i in range(55)],
        logger,
    )
    engine.save_persisted_cache(
        symbols,
        metadata,
        datetime.now(timezone.utc) - timedelta(hours=25),
    )
    cached, _, fetched = engine.load_validated_cache(datetime.now(timezone.utc), 24, 50)
    assert cached == []
    assert fetched is None
    with pytest.raises(ConnectivityError):
        discover_universe(MockTradingClient(should_raise=True), logger)


def test_corrupt_layer1_cache_metadata_is_rejected(tmp_path):
    engine = StructuralEligibilityEngine(cache_dir=str(tmp_path))
    engine.save_persisted_cache(
        ["AAPL", "MSFT"],
        {"AAPL": {}, "MSFT": {}},
        datetime.now(timezone.utc),
    )
    cached, metadata, fetched = engine.load_validated_cache(datetime.now(timezone.utc), 24, 1)
    assert cached == []
    assert metadata == {}
    assert fetched is None


def test_startup_uses_same_discovery_and_layer2_cache_path(monkeypatch, tmp_path):
    monkeypatch.setenv("MIN_UNIVERSE_HEALTH", "1")
    cache_dir = str(tmp_path)
    monkeypatch.setenv("MM_CACHE_DIR", cache_dir)
    monkeypatch.setenv("MONEY_MINTTER_TEMP_DIR", cache_dir)
    now = datetime.now(timezone.utc)
    symbols = ["AAPL", "MSFT"]
    l1_engine = StructuralEligibilityEngine(cache_dir=cache_dir)
    l1_engine.save_persisted_cache(
        symbols,
        {symbol: _metadata(symbol) for symbol in symbols},
        now - timedelta(hours=2),
    )
    l2_engine = DailySuitabilityEngine(
        settings=Settings(),
        cache_dir=cache_dir,
        historical_feed="sip",
    )
    l2_engine.save_persisted_pool(
        SuitabilityResult(
            pool=symbols,
            quarantined=[],
            metrics={},
            exclusion_counts={},
            requested_feed="SIP",
            actual_feed="SIP",
            data_source="SIP",
            generated_at=(now - timedelta(hours=2)).isoformat(),
            stale=False,
            cached=True,
            config_signature=l2_engine._compute_config_signature(),
            universe_signature=l2_engine._compute_universe_signature(symbols),
        )
    )

    trading_client = MagicMock()
    trading_client.get_all_assets.side_effect = RuntimeError("API unavailable")
    monkeypatch.setattr(
        "money_mintter.core.live_bot.TradingClient",
        lambda *args, **kwargs: trading_client,
    )
    discovery_calls = []

    def mock_discover(client, logger, api_key=""):
        assert client is trading_client
        discovery_calls.append(True)
        return symbols

    monkeypatch.setattr("money_mintter.core.live_bot.discover_universe", mock_discover)
    monkeypatch.setattr(
        "money_mintter.core.live_bot.StockHistoricalDataClient",
        lambda *args, **kwargs: MagicMock(),
    )
    monkeypatch.setattr("money_mintter.core.live_bot.apply_default_timeout", lambda *args, **kwargs: None)

    bot = LiveMarketBot()
    assert discovery_calls == [True]
    assert bot.global_universe == symbols
    assert bot.scanner.global_universe == symbols
    assert bot.scanner.data_feed == DataFeed.IEX
    assert bot._universe_last_fetched is not None
    assert abs((bot._universe_last_fetched - (now - timedelta(hours=2))).total_seconds()) < 5


def test_refresh_failure_never_reintroduces_raw_layer1_cache(monkeypatch, tmp_path):
    cache_dir = str(tmp_path)
    monkeypatch.setenv("MM_CACHE_DIR", cache_dir)
    monkeypatch.setenv("MONEY_MINTTER_TEMP_DIR", cache_dir)
    l1_engine = StructuralEligibilityEngine(cache_dir=cache_dir)
    raw_symbols = [f"RAW{i:02d}" for i in range(55)]
    l1_engine.save_persisted_cache(
        raw_symbols,
        {symbol: _metadata(symbol) for symbol in raw_symbols},
        datetime.now(timezone.utc),
    )

    trading_client = MagicMock()
    monkeypatch.setattr(
        "money_mintter.core.live_bot.TradingClient",
        lambda *args, **kwargs: trading_client,
    )
    monkeypatch.setattr(
        "money_mintter.core.live_bot.StockHistoricalDataClient",
        lambda *args, **kwargs: MagicMock(),
    )
    monkeypatch.setattr("money_mintter.core.live_bot.apply_default_timeout", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "money_mintter.core.live_bot.discover_universe",
        lambda *args, **kwargs: ["VALIDATED_L2"],
    )
    bot = LiveMarketBot()
    bot._cached_universe = ["VALIDATED_L2"]
    bot._universe_last_fetched = datetime.now(timezone.utc) - timedelta(hours=25)
    monkeypatch.setattr(
        "money_mintter.core.live_bot.discover_universe",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("refresh failure")),
    )
    universe = bot._get_or_refresh_universe(datetime.now(timezone.utc))
    assert universe == []
    assert not set(universe).intersection(raw_symbols)
    assert bot.global_universe == []


def test_empty_layer2_pool_enters_retry_cooldown(monkeypatch):
    calls = []

    trading_client = MagicMock()
    monkeypatch.setattr(
        "money_mintter.core.live_bot.TradingClient",
        lambda *args, **kwargs: trading_client,
    )
    monkeypatch.setattr(
        "money_mintter.core.live_bot.StockHistoricalDataClient",
        lambda *args, **kwargs: MagicMock(),
    )
    monkeypatch.setattr(
        "money_mintter.core.live_bot.apply_default_timeout",
        lambda *args, **kwargs: None,
    )

    def empty_discovery(*args, **kwargs):
        calls.append(True)
        return []

    monkeypatch.setattr("money_mintter.core.live_bot.discover_universe", empty_discovery)
    bot = LiveMarketBot()
    assert bot.global_universe == []
    assert len(calls) == 1

    failed_at = bot._universe_last_failed
    assert failed_at is not None
    assert bot._get_or_refresh_universe(failed_at + timedelta(minutes=5)) == []
    assert len(calls) == 1

    assert bot._get_or_refresh_universe(failed_at + timedelta(minutes=65)) == []
    assert len(calls) == 2


@pytest.mark.parametrize(
    ("feed", "expected_feed"),
    [("iex", DataFeed.IEX), ("sip", DataFeed.SIP)],
)
def test_ict_acquisition_uses_one_explicit_feed_for_both_timeframes(feed, expected_feed):
    stock_client = MagicMock()
    stock_client.get_stock_bars.return_value = MagicMock(data={}, next_page_token=None)
    start = datetime(2026, 11, 13, 13, 0, tzinfo=timezone.utc)
    end = datetime(2026, 11, 13, 15, 0, tzinfo=timezone.utc)
    result = fetch_ict_market_data(stock_client, "AAPL", start, end, feed=feed)
    requested_feeds = [call.args[0].feed for call in stock_client.get_stock_bars.call_args_list]
    assert requested_feeds == [expected_feed, expected_feed]
    assert result.feed == feed
