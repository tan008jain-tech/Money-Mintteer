import pytest


@pytest.fixture(autouse=True)
def _isolate_logs(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("MM_LOG_DIR", str(tmp_path))
