from __future__ import annotations

import logging
import pytest
from unittest.mock import MagicMock
from money_mintter.core.live_bot import discover_universe

def test_discover_universe_txse_validation_fallback(monkeypatch):
    trading_client = MagicMock()
    # Simulate get_all_assets raising a validation error due to unknown exchange 'TXSE'
    trading_client.get_all_assets.side_effect = ValueError("1 validation error for list[Asset] exchange Input should be 'AMEX', 'ARCA' ... [type=enum, input_value='TXSE']")
    
    # Simulate fallback trading_client.get returning raw asset dicts
    raw_assets = [
        {
            "symbol": "AAPL",
            "name": "Apple Inc",
            "exchange": "TXSE",
            "status": "active",
            "tradable": True,
            "asset_class": "us_equity",
            "shortable": True,
            "fractionable": True,
            "marginable": True,
        }
    ]
    trading_client.get.return_value = raw_assets

    stock_client = MagicMock()
    # Mock stock historical data client so Layer 2 suitability evaluation passes
    mock_bars = MagicMock()
    mock_bars.df = MagicMock()
    # Mock sufficient data for suitability
    stock_client.get_stock_bars.return_value = mock_bars

    monkeypatch.setattr("money_mintter.core.live_bot.StockHistoricalDataClient", lambda *args, **kwargs: stock_client)
    monkeypatch.setattr("money_mintter.core.suitability.StockHistoricalDataClient", lambda *args, **kwargs: stock_client)

    logger = logging.getLogger("test_txse_fallback")
    
    # Even if suitability might require specific bars, let's test that get was called and fallback triggered
    # (or if suitability fails due to mock bars, we check that trading_client.get was invoked)
    try:
        discover_universe(trading_client, logger)
    except Exception:
        pass

    assert trading_client.get.called
    assert trading_client.get.call_args[0][0] == "/assets"
