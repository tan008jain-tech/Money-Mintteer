from __future__ import annotations


class KillSwitch:
    def __init__(self, max_consecutive_errors: int = 5, max_daily_loss_pct: float = 0.02) -> None:
        self.max_consecutive_errors = max_consecutive_errors
        self.max_daily_loss_pct = max_daily_loss_pct
        self._consecutive_errors = 0
        self._tripped = False
        self._reason = ""

    def trip(self, reason: str) -> None:
        self._tripped = True
        self._reason = reason

    def record_success(self) -> None:
        self._consecutive_errors = 0

    def record_error(self) -> None:
        self._consecutive_errors += 1
        if self._consecutive_errors >= self.max_consecutive_errors:
            self._tripped = True
            self._reason = f"consecutive errors limit reached ({self._consecutive_errors})"

    def check_daily_loss(self, start_equity: float, current_equity: float) -> None:
        if self._tripped:
            return
        if start_equity > 0:
            drawdown_pct = (start_equity - current_equity) / start_equity
            if drawdown_pct >= self.max_daily_loss_pct:
                self._tripped = True
                self._reason = f"max daily loss exceeded ({drawdown_pct:.4f})"

    @property
    def tripped(self) -> bool:
        return self._tripped

    @property
    def reason(self) -> str:
        return self._reason
