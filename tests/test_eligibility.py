from __future__ import annotations

import logging
import tempfile
import os
import json
from datetime import datetime, timezone, timedelta
import pytest

from alpaca.trading.enums import AssetStatus, AssetClass
from money_mintter.core.eligibility import StructuralEligibilityEngine
from money_mintter.core.live_bot import discover_universe, LiveMarketBot, ConnectivityError


class MockAsset:
    def __init__(
        self,
        symbol: str,
        name: str = "Common Stock",
        exchange: str = "NASDAQ",
        status: AssetStatus = AssetStatus.ACTIVE,
        tradable: bool = True,
        asset_class: AssetClass = AssetClass.US_EQUITY,
        shortable: bool = True,
        fractionable: bool = True,
        marginable: bool = True,
    ):
        self.symbol = symbol
        self.name = name
        self.exchange = exchange
        self.status = status
        self.tradable = tradable
        self.asset_class = asset_class
        self.shortable = shortable
        self.fractionable = fractionable
        self.marginable = marginable


def test_structural_eligibility_rules():
    logger = logging.getLogger("test_elig")
    engine = StructuralEligibilityEngine()

    # Valid ordinary equity
    asset_valid = MockAsset("AAPL", name="Apple Inc Common Stock", exchange="NASDAQ", shortable=False)
    is_elig, reason, meta = engine.evaluate_asset(asset_valid)
    assert is_elig is True
    assert reason == "eligible"
    assert meta["symbol"] == "AAPL"
    assert meta["shortable"] is False


def test_false_positive_instrument_classification():
    logger = logging.getLogger("test_elig")
    engine = StructuralEligibilityEngine()

    legitimate_assets = [
        MockAsset("UPS", name="United Parcel Service Inc"),
        MockAsset("SBNY", name="Short-Wave Technologies Inc"),
        MockAsset("ARE", name="Alexandria Real Estate Equities Inc"),
        MockAsset("PRGO", name="Perrigo Company plc"),
        MockAsset("BRK.A", name="Berkshire Hathaway Inc Class A"),
    ]

    for asset in legitimate_assets:
        is_elig, reason, meta = engine.evaluate_asset(asset)
        assert is_elig is True, f"Legitimate company {asset.symbol} ({asset.name}) was falsely excluded with reason: {reason}"


def test_unsupported_classifications_precise():
    logger = logging.getLogger("test_elig")
    engine = StructuralEligibilityEngine()

    warrant = MockAsset("WMT.WT", name="Walmart Warrant")
    assert engine.evaluate_asset(warrant)[0] is False

    right = MockAsset("XYZ.RT", name="XYZ Corp Rights")
    assert engine.evaluate_asset(right)[0] is False

    unit = MockAsset("ABC.U", name="ABC Acquisition Unit")
    assert engine.evaluate_asset(unit)[0] is False

    pref = MockAsset("BAC.PR", name="Bank of America Preferred Stock")
    assert engine.evaluate_asset(pref)[0] is False

    lev = MockAsset("TQQQ", name="ProShares UltraPro QQQ 3X")
    assert engine.evaluate_asset(lev)[0] is False


def test_malformed_cache_data_handled_safely(tmp_path):
    cache_dir = str(tmp_path)
    engine = StructuralEligibilityEngine(cache_dir=cache_dir)

    with open(engine.cache_file, "w", encoding="utf-8") as f:
        f.write("{malformed json...}")

    symbols, metadata, fetched_at = engine.load_persisted_cache()
    assert symbols == []
    assert metadata == {}
    assert fetched_at is None


def test_atomic_cache_replacement(tmp_path):
    cache_dir = str(tmp_path)
    engine = StructuralEligibilityEngine(cache_dir=cache_dir)

    symbols = ["AAPL", "MSFT"]
    metadata = {"AAPL": {"symbol": "AAPL"}, "MSFT": {"symbol": "MSFT"}}
    now = datetime.now(timezone.utc)

    engine.save_persisted_cache(symbols, metadata, now)
    assert os.path.exists(engine.cache_file)

    loaded_syms, loaded_meta, loaded_time = engine.load_persisted_cache()
    assert loaded_syms == symbols
    assert loaded_time is not None


def test_no_cache_first_startup(monkeypatch, tmp_path):
    logger = logging.getLogger("test_elig")
    cache_dir = str(tmp_path)
    engine = StructuralEligibilityEngine(cache_dir=cache_dir)
    monkeypatch.setenv("MM_CACHE_DIR", cache_dir)
    monkeypatch.setenv("MONEY_MINTTER_TEMP_DIR", cache_dir)

    fresh_time = datetime.now(timezone.utc) - timedelta(hours=2)
    engine.save_persisted_cache(["AAPL", "MSFT"], {"AAPL": {}, "MSFT": {}}, fresh_time)

    discovered_called = False
    def mock_discover(client, logger, api_key=""):
        nonlocal discovered_called
        discovered_called = True
        return ["TSLA", "AMZN"]

    monkeypatch.setattr("money_mintter.core.live_bot.discover_universe", mock_discover)
    from money_mintter.core.suitability import SuitabilityResult
    monkeypatch.setattr("money_mintter.core.suitability.DailySuitabilityEngine.evaluate_universe", lambda self, syms, now: SuitabilityResult(
        pool=["TSLA", "AMZN"], quarantined=[], metrics={}, exclusion_counts={},
        requested_feed="SIP", actual_feed="SIP", data_source="SIP",
        generated_at=now.isoformat(), stale=False, cached=False, config_signature="sig", universe_signature="sig"
    ))

    bot = LiveMarketBot()
    assert discovered_called is True
    assert bot.global_universe == ["TSLA", "AMZN"]

    monkeypatch.setattr("money_mintter.core.live_bot.discover_universe", lambda client, logger, api_key: (_ for _ in ()).throw(RuntimeError("API down")))
    with pytest.raises(RuntimeError, match="UNIVERSE_DISCOVERY_FAILED"):
        LiveMarketBot()


def test_refresh_failure_fails_closed(monkeypatch, tmp_path):
    logger = logging.getLogger("test_elig")
    cache_dir = str(tmp_path)
    engine = StructuralEligibilityEngine(cache_dir=cache_dir)
    monkeypatch.setenv("MM_CACHE_DIR", cache_dir)

    stale_time = datetime.now(timezone.utc) - timedelta(hours=30)
    engine.save_persisted_cache(["AAPL", "MSFT"], {"AAPL": {}, "MSFT": {}}, stale_time)

    call_count = 0
    def mock_discover(client, logger, api_key=""):
        nonlocal call_count
        call_count += 1
        raise RuntimeError("API failure")

    monkeypatch.setattr("money_mintter.core.live_bot.discover_universe", mock_discover)

    with pytest.raises(RuntimeError, match="UNIVERSE_DISCOVERY_FAILED"):
        LiveMarketBot()
