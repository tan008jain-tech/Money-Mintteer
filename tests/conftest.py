import pytest


@pytest.fixture(autouse=True)
def _isolate_logs(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("MM_LOG_DIR", str(tmp_path))


@pytest.fixture(autouse=True)
def _mock_default_discovery(request, monkeypatch: pytest.MonkeyPatch):
    if "test_dynamic_universe.py" not in str(request.path):
        monkeypatch.setattr("money_mintter.core.live_bot.discover_universe", lambda client, logger, api_key: ["AAPL", "MSFT", "NVDA", "TSLA", "AMZN"])

