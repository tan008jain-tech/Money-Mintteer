from __future__ import annotations

import os
import time


class SessionLock:
    """Exclusive process session lock ensuring only one live trading session runs per environment."""
    def __init__(self, path: str, stale_hours: float = 12.0) -> None:
        self.path = path
        self.stale_hours = stale_hours
        self._acquired = False

    def acquire(self) -> bool:
        if os.path.exists(self.path):
            try:
                mtime = os.path.getmtime(self.path)
                age_hours = (time.time() - mtime) / 3600.0
                if age_hours < self.stale_hours:
                    return False
                else:
                    os.remove(self.path)
            except Exception:
                try:
                    os.remove(self.path)
                except Exception:
                    pass

        parent = os.path.dirname(self.path)
        if parent and not os.path.exists(parent):
            try:
                os.makedirs(parent, exist_ok=True)
            except Exception:
                pass

        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            with os.fdopen(fd, "w") as f:
                f.write(f"{os.getpid()}\n{time.time()}")
            self._acquired = True
            return True
        except OSError:
            return False

    def release(self) -> None:
        if self._acquired:
            try:
                if os.path.exists(self.path):
                    os.remove(self.path)
            except Exception:
                pass
            self._acquired = False
