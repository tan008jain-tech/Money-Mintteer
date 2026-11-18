"""Portable, bounded file logging for Money Mintter.

The handlers are designed for a single application process writing to one log
directory. The live bot's SessionLock supplies the single-session contract.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import Thread
from typing import Callable

MIB = 1024 * 1024
MASTER_LOG_MAX_BYTES = 50 * MIB
MASTER_LOG_BACKUP_COUNT = 10
DAILY_LOG_MAX_BYTES = 128 * MIB
DAILY_LOG_BACKUP_COUNT = 3
LOG_RETENTION_DAYS = 30


def _utc_now(now_fn: Callable[[], datetime] | None) -> datetime:
    value = now_fn() if now_fn is not None else datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _retention_cutoff(today: date, retention_days: int) -> date:
    if retention_days < 1:
        raise ValueError("retention_days must be at least 1")
    return today - timedelta(days=retention_days - 1)


def _warn_cleanup(path: Path, exc: OSError) -> None:
    try:
        sys.stderr.write(
            f"[MoneyMintter logging] Could not remove expired log {path}: {exc}\n"
        )
        sys.stderr.flush()
    except Exception:
        pass


def _warn_daily_overflow(path: Path) -> None:
    try:
        sys.stderr.write(
            "[MoneyMintter logging] Daily log segment limit reached for "
            f"{path}; the oldest segment for this UTC date is being replaced. "
            "Earlier records for this date may no longer be available.\n"
        )
        sys.stderr.flush()
    except Exception:
        pass


def _start_cleanup_thread(target: Callable[[], None], label: str) -> Thread:
    thread = Thread(target=target, name=label, daemon=True)
    thread.start()
    return thread


class _Utf8SizeRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler that measures encoded bytes, including UTF-8 output."""

    def shouldRollover(self, record: logging.LogRecord) -> bool:
        if self.maxBytes <= 0:
            return False
        if self.stream is None:
            self.stream = self._open()

        self.stream.seek(0, os.SEEK_END)
        message = self.format(record) + self.terminator
        encoding = getattr(self.stream, "encoding", None) or "utf-8"
        encoded_size = len(message.encode(encoding))
        return self.stream.tell() + encoded_size >= self.maxBytes


class UtcDailySizeRotatingFileHandler(_Utf8SizeRotatingFileHandler):
    """Write dated logs, switch at UTC midnight, and bound each date's files."""

    _DAILY_NAME = re.compile(r"^live_market_(\d{8})\.log(?:\.\d+)?$")

    def __init__(
        self,
        directory: str | Path,
        *,
        max_bytes: int = DAILY_LOG_MAX_BYTES,
        backup_count: int = DAILY_LOG_BACKUP_COUNT,
        retention_days: int = LOG_RETENTION_DAYS,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if backup_count < 0:
            raise ValueError("backup_count must be non-negative")
        if retention_days < 1:
            raise ValueError("retention_days must be at least 1")

        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._now_fn = now_fn
        self._retention_days = retention_days
        self._active_date = _utc_now(now_fn).date()

        super().__init__(
            str(self._path_for_date(self._active_date)),
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
            delay=False,
        )
        self._prune_expired(self._active_date)

    def _today(self) -> date:
        return _utc_now(self._now_fn).date()

    def _path_for_date(self, value: date) -> Path:
        return self.directory / f"live_market_{value:%Y%m%d}.log"

    def _prune_expired(self, today: date) -> None:
        cutoff = _retention_cutoff(today, self._retention_days)
        try:
            entries = tuple(self.directory.iterdir())
        except OSError as exc:
            _warn_cleanup(self.directory, exc)
            return

        for path in entries:
            match = self._DAILY_NAME.fullmatch(path.name)
            if match is None:
                continue
            try:
                file_date = datetime.strptime(match.group(1), "%Y%m%d").date()
            except ValueError:
                continue
            if file_date >= cutoff:
                continue
            try:
                if path.is_file():
                    path.unlink()
            except OSError as exc:
                # A Windows viewer may temporarily hold an old file open.
                # Do not interrupt logging; retry on the next startup/day.
                _warn_cleanup(path, exc)

    def _schedule_prune(self, today: date) -> None:
        try:
            _start_cleanup_thread(
                lambda: self._prune_expired(today),
                "MoneyMintter-DailyLogRetention",
            )
        except RuntimeError as exc:
            _warn_cleanup(self.directory, OSError(str(exc)))

    def _switch_date(self, today: date) -> None:
        if today == self._active_date:
            return

        old_base = self.baseFilename
        old_stream = self.stream
        self.baseFilename = os.path.abspath(str(self._path_for_date(today)))
        try:
            new_stream = self._open()
        except Exception:
            self.baseFilename = old_base
            raise

        self.stream = new_stream
        self._active_date = today

        if old_stream is not None:
            try:
                old_stream.flush()
                old_stream.close()
            except OSError as exc:
                _warn_cleanup(Path(old_base), exc)

        self._schedule_prune(today)

    def doRollover(self) -> None:
        oldest_segment = (
            Path(f"{self.baseFilename}.{self.backupCount}")
            if self.backupCount > 0
            else None
        )
        will_replace_oldest = bool(oldest_segment and oldest_segment.exists())
        super().doRollover()
        if will_replace_oldest and oldest_segment is not None:
            _warn_daily_overflow(Path(self.baseFilename))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            today = self._today()
            if today != self._active_date:
                self._switch_date(today)
            super().emit(record)
        except Exception:
            self.handleError(record)


class UtcSizeAndDailyRotatingFileHandler(_Utf8SizeRotatingFileHandler):
    """Keep live_market.log active, rotating at UTC midnight or by size."""

    _MASTER_BACKUP = re.compile(r"^live_market\.log\.\d+$")

    def __init__(
        self,
        filename: str | Path,
        *,
        max_bytes: int = MASTER_LOG_MAX_BYTES,
        backup_count: int = MASTER_LOG_BACKUP_COUNT,
        retention_days: int = LOG_RETENTION_DAYS,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if backup_count < 0:
            raise ValueError("backup_count must be non-negative")
        if retention_days < 1:
            raise ValueError("retention_days must be at least 1")

        self._now_fn = now_fn
        self._retention_days = retention_days
        self._active_date = _utc_now(now_fn).date()

        path = Path(filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            try:
                self._active_date = datetime.fromtimestamp(
                    path.stat().st_mtime, timezone.utc
                ).date()
            except OSError:
                pass

        super().__init__(
            str(path),
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
            delay=False,
        )
        self._prune_expired_backups(_utc_now(now_fn).date())

    def _today(self) -> date:
        return _utc_now(self._now_fn).date()

    def shouldRollover(self, record: logging.LogRecord) -> bool:
        if self._today() != self._active_date:
            return True
        return super().shouldRollover(record)

    def _prune_expired_backups(self, today: date) -> None:
        cutoff = _retention_cutoff(today, self._retention_days)
        directory = Path(self.baseFilename).parent
        try:
            entries = tuple(directory.iterdir())
        except OSError as exc:
            _warn_cleanup(directory, exc)
            return

        for path in entries:
            if self._MASTER_BACKUP.fullmatch(path.name) is None:
                continue
            try:
                modified_date = datetime.fromtimestamp(
                    path.stat().st_mtime, timezone.utc
                ).date()
                if modified_date < cutoff and path.is_file():
                    path.unlink()
            except OSError as exc:
                _warn_cleanup(path, exc)

    def _schedule_prune(self, today: date) -> None:
        try:
            _start_cleanup_thread(
                lambda: self._prune_expired_backups(today),
                "MoneyMintter-MasterLogRetention",
            )
        except RuntimeError as exc:
            _warn_cleanup(Path(self.baseFilename), OSError(str(exc)))

    def doRollover(self) -> None:
        previous_date = self._active_date
        super().doRollover()
        today = self._today()
        self._active_date = today
        if today != previous_date:
            self._schedule_prune(today)


def create_logging_file_handlers(
    log_directory: str | Path,
) -> tuple[UtcDailySizeRotatingFileHandler, UtcSizeAndDailyRotatingFileHandler]:
    """Create both backward-compatible, bounded file logging handlers."""
    directory = Path(log_directory)
    directory.mkdir(parents=True, exist_ok=True)
    daily = UtcDailySizeRotatingFileHandler(directory)
    master = UtcSizeAndDailyRotatingFileHandler(directory / "live_market.log")
    return daily, master
