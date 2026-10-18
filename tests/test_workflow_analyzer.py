from __future__ import annotations

import pytest

from money_mintter.research.workflow_analyzer import analyze_workflow_run


def test_workflow_analyzer_audit() -> None:
    candidates = [
        {"symbol": "NVDA", "score": 0.95},
        {"symbol": "AAPL", "score": 0.92},
        {"symbol": "TSLA", "score": 0.89},
        {"symbol": "MSFT", "score": 0.91},
        {"symbol": "AMD", "score": 0.88},
    ]
    trades = [
        {"symbol": "NVDA", "realized_pnl": 150.0},
        {"symbol": "AAPL", "realized_pnl": -45.0},
        {"symbol": "TSLA", "realized_pnl": 120.0},
        {"symbol": "MSFT", "realized_pnl": 85.0},
    ]
    starting_capital = 10000.0
    ending_capital = 10310.0

    report = analyze_workflow_run(candidates, trades, starting_capital, ending_capital)

    assert report.audit_status == "PASSED"
    assert report.summary["total_candidates_screened"] == 5
    assert report.summary["total_trades"] == 4
    assert report.summary["winning_trades"] == 3
    assert report.summary["losing_trades"] == 1
    assert report.summary["win_rate"] == 75.0
    assert report.summary["net_pnl"] == 310.0
    assert report.summary["return_pct"] == 3.1
