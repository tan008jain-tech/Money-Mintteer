from datetime import date
from pathlib import Path

import pytest

from money_mintter.data.hf import HfCandleLoader, HfDataClient, HfDataError


class FakeResponse:
    status = 200

    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, _size=-1):
        return self.payload


def test_hf_loader_parses_naive_eastern_timestamps(tmp_path: Path) -> None:
    path = tmp_path / "AAPL.csv"
    path.write_text(
        "datetime,open,high,low,close,volume,source\n"
        "2024-01-02 09:31:00,100,101,99,100.5,10,pitrading\n"
        "2024-01-02 09:30:00,99,100,98,99.5,20,pitrading\n",
        encoding="utf-8",
    )

    candles = HfCandleLoader().load(
        path,
        "AAPL",
        start=date(2024, 1, 2),
        end=date(2024, 1, 2),
    )

    assert [candle.timestamp.minute for candle in candles] == [30, 31]
    assert str(candles[0].timestamp.tzinfo) == "America/New_York"


def test_hf_loader_rejects_empty_range(tmp_path: Path) -> None:
    path = tmp_path / "AAPL.csv"
    path.write_text(
        "datetime,open,high,low,close,volume,source\n"
        "2024-01-02 09:31:00,100,101,99,100.5,10,pitrading\n",
        encoding="utf-8",
    )

    with pytest.raises(HfDataError, match="no HF candles"):
        HfCandleLoader().load(
            path,
            "AAPL",
            start=date(2024, 1, 3),
            end=date(2024, 1, 3),
        )


def test_hf_client_adds_api_key_for_authenticated_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def fake_urlopen(request, timeout):
        captured["headers"] = dict(request.header_items())
        captured["timeout"] = timeout
        return FakeResponse(b'{"url":"https://download.example/token"}')

    monkeypatch.setattr("money_mintter.data.hf.urlopen", fake_urlopen)

    client = HfDataClient("secret")
    url = client.request_download_url("AAPL")

    assert url == "https://download.example/token"
    assert captured["headers"]["X-api-key"] == "secret"


def test_hf_symbols_are_public(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(request, timeout):
        assert "X-api-key" not in dict(request.header_items())
        return FakeResponse(b'{"count":2,"symbols":[{"ticker":"AAPL"},{"ticker":"MSFT"}]}')

    monkeypatch.setattr("money_mintter.data.hf.urlopen", fake_urlopen)

    client = HfDataClient("secret")
    assert [row["ticker"] for row in client.symbols()] == ["AAPL", "MSFT"]


def test_hf_client_surfaces_auth_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    from urllib.error import HTTPError

    def fake_urlopen(_request, timeout):
        raise HTTPError("https://example", 403, "forbidden", {}, None)

    monkeypatch.setattr("money_mintter.data.hf.urlopen", fake_urlopen)

    with pytest.raises(HfDataError, match="HTTP 403"):
        HfDataClient("secret").request_download_url("AAPL")
