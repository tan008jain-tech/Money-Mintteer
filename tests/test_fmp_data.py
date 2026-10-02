from datetime import date, datetime, timezone

import pytest

from money_mintter.data.fmp import FmpApiError, FmpClient, FmpMinuteData
from money_mintter.screeners.fmp import FmpUniverseScreener


class FakeFmpClient:
    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses

    def get_json(self, endpoint: str, params=None):
        return self.responses[endpoint]


def test_fmp_universe_uses_active_rank_and_movement() -> None:
    client = FakeFmpClient(
        {
            "most-actives": [
                {
                    "symbol": "AAA",
                    "price": 20,
                    "changesPercentage": 4.0,
                    "exchange": "NASDAQ",
                    "volume": 1000000,
                },
                {
                    "symbol": "BBB",
                    "price": 30,
                    "changesPercentage": 1.0,
                    "exchange": "NYSE",
                    "volume": 500000,
                },
            ],
            "biggest-gainers": [{"symbol": "CCC", "price": 3, "changesPercentage": 10.0, "exchange": "NASDAQ"}],
            "biggest-losers": [],
        }
    )

    result = FmpUniverseScreener(client, max_rows=2).candidates(
        datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
    )

    assert [candidate.symbol for candidate in result[:2]] == ["CCC", "AAA"]
    assert result[-1].symbol == "BBB"
    assert result[0].liquidity_score < result[1].liquidity_score or result[0].symbol == "CCC"


def test_fmp_minute_data_sorts_and_attaches_timezone() -> None:
    client = FakeFmpClient(
        {
            "historical-chart/1min": [
                {
                    "date": "2026-10-02 10:02:00",
                    "open": 101,
                    "high": 102,
                    "low": 100,
                    "close": 101.5,
                    "volume": 20,
                },
                {
                    "date": "2026-10-02 10:01:00",
                    "open": 100,
                    "high": 101,
                    "low": 99,
                    "close": 100.5,
                    "volume": 15,
                },
            ]
        }
    )

    candles = FmpMinuteData(client).candles(
        "AAA",
        start=date(2026, 10, 2),
        end=date(2026, 10, 2),
    )

    assert [candle.timestamp.minute for candle in candles] == [1, 2]
    assert str(candles[0].timestamp.tzinfo) == "America/New_York"


def test_fmp_client_uses_api_key_header(monkeypatch) -> None:
    import json

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return json.dumps([{"symbol": "AAA"}]).encode("utf-8")

    captured = {}

    def fake_urlopen(request, timeout):
        captured["headers"] = dict(request.header_items())
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("money_mintter.data.fmp.urlopen", fake_urlopen)

    result = FmpClient("secret").get_json("most-actives")
    assert result == [{"symbol": "AAA"}]
    assert captured["headers"]["Apikey"] == "secret"
    assert captured["timeout"] == 10.0


def test_fmp_minute_data_rejects_duplicate_timestamps() -> None:
    client = FakeFmpClient(
        {
            "historical-chart/1min": [
                {
                    "date": "2026-10-02 10:01:00",
                    "open": 100,
                    "high": 101,
                    "low": 99,
                    "close": 100.5,
                    "volume": 1,
                },
                {
                    "date": "2026-10-02 10:01:00",
                    "open": 100.5,
                    "high": 102,
                    "low": 100,
                    "close": 101,
                    "volume": 2,
                },
            ]
        }
    )

    with pytest.raises(FmpApiError, match="duplicate"):
        FmpMinuteData(client).candles("AAA")


def test_fmp_client_surfaces_http_failures(monkeypatch) -> None:
    from urllib.error import HTTPError

    def fake_urlopen(_request, timeout):
        raise HTTPError("https://example", 401, "unauthorized", {}, None)

    monkeypatch.setattr("money_mintter.data.fmp.urlopen", fake_urlopen)

    with pytest.raises(FmpApiError, match="HTTP 401"):
        FmpClient("secret").get_json("most-actives")
