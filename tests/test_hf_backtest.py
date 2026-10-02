from datetime import date
from pathlib import Path

from money_mintter.data.hf_backtest import run_hf_backtest


def test_hf_backtest_smoke(tmp_path: Path) -> None:
    path = tmp_path / "AAPL.csv"
    rows = [
        "datetime,open,high,low,close,volume,source",
        "2024-01-02 09:30:00,100,100.2,99.8,100,1000,pitrading",
        "2024-01-02 09:31:00,100,100.2,99.9,100.1,1100,pitrading",
        "2024-01-03 09:30:00,99,99.2,98.8,99,1200,pitrading",
        "2024-01-03 09:31:00,99,100,98.7,99.9,5000,pitrading",
    ]
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")

    result = run_hf_backtest(
        path,
        "AAPL",
        start=date(2024, 1, 2),
        end=date(2024, 1, 3),
        timezone_name="America/New_York",
    )

    assert result.candle_count == 4
    assert result.report.initial_capital == 10_000.0
