from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class WorkflowAnalysisReport:
    summary: dict[str, Any]
    audit_status: str


def analyze_workflow_run(
    candidates_analyzed: list[dict[str, Any]],
    closed_trades: list[dict[str, Any]],
    starting_capital: float,
    ending_capital: float,
) -> WorkflowAnalysisReport:
    """Perform deep analytical audit of a live or backtested workflow session.

    Evaluates universe screening quality, candidate scoring distributions,
    trade win rates, profit factor, risk-reward ratios, and capital allocation.
    """
    total_candidates = len(candidates_analyzed)
    avg_score = (
        sum(float(c.get("score", 0.0)) for c in candidates_analyzed) / total_candidates
        if total_candidates > 0
        else 0.0
    )

    total_trades = len(closed_trades)
    winning_trades = [t for t in closed_trades if float(t.get("realized_pnl", 0.0)) > 0]
    losing_trades = [t for t in closed_trades if float(t.get("realized_pnl", 0.0)) <= 0]

    win_rate = (len(winning_trades) / total_trades) if total_trades > 0 else 0.0
    total_profit = sum(float(t.get("realized_pnl", 0.0)) for t in winning_trades)
    total_loss = abs(sum(float(t.get("realized_pnl", 0.0)) for t in losing_trades))
    profit_factor = (
        (total_profit / total_loss)
        if total_loss > 0
        else (float("inf") if total_profit > 0 else 0.0)
    )

    net_pnl = ending_capital - starting_capital
    pf_display = round(profit_factor, 2) if profit_factor != float("inf") else "INF"

    summary = {
        "total_candidates_screened": total_candidates,
        "average_candidate_score": round(avg_score, 4),
        "total_trades": total_trades,
        "winning_trades": len(winning_trades),
        "losing_trades": len(losing_trades),
        "win_rate": round(win_rate * 100, 2),
        "total_profit": round(total_profit, 2),
        "total_loss": round(total_loss, 2),
        "profit_factor": pf_display,
        "net_pnl": round(net_pnl, 2),
        "return_pct": round((net_pnl / starting_capital) * 100, 2) if starting_capital > 0 else 0.0,
    }

    audit_status = "PASSED" if avg_score >= 0.80 else "WARNING_SCORE_BELOW_GATE"
    return WorkflowAnalysisReport(summary=summary, audit_status=audit_status)
