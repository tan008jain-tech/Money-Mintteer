from __future__ import annotations

import logging
from datetime import datetime, timezone
import os

from money_mintter.core.logging_storage import (
    DAILY_LOG_BACKUP_COUNT,
    DAILY_LOG_MAX_BYTES,
    LOG_RETENTION_DAYS,
    MASTER_LOG_BACKUP_COUNT,
    MASTER_LOG_MAX_BYTES,
    UtcDailySizeRotatingFileHandler,
    UtcSizeAndDailyRotatingFileHandler,
    create_logging_file_handlers,
)


def _record(message: str) -> logging.LogRecord:
    return logging.LogRecord(
        name="logging-storage-test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )


def _set_plain_formatter(handler: logging.Handler) -> None:
    handler.setFormatter(logging.Formatter("%(message)s"))


def test_daily_handler_switches_to_new_utc_date(tmp_path):
    now = [datetime(2026, 11, 17, 23, 59, tzinfo=timezone.utc)]
    handler = UtcDailySizeRotatingFileHandler(
        tmp_path,
        max_bytes=1024,
        backup_count=3,
        now_fn=lambda: now[0],
    )
    _set_plain_formatter(handler)

    handler.handle(_record("previous-day"))
    now[0] = datetime(2026, 11, 18, 0, 0, tzinfo=timezone.utc)
    handler.handle(_record("new-day"))
    handler.close()

    previous = tmp_path / "live_market_20261117.log"
    current = tmp_path / "live_market_20261118.log"
    assert "previous-day" in previous.read_text(encoding="utf-8")
    assert "new-day" in current.read_text(encoding="utf-8")


def test_daily_handler_size_rotation_is_bounded_by_segment_count(tmp_path):
    now = [datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)]
    handler = UtcDailySizeRotatingFileHandler(
        tmp_path,
        max_bytes=80,
        backup_count=3,
        now_fn=lambda: now[0],
    )
    _set_plain_formatter(handler)

    for index in range(12):
        handler.handle(_record(f"{index}-" + ("x" * 35)))
    handler.close()

    files = list(tmp_path.glob("live_market_20261118.log*"))
    assert len(files) <= 4
    assert {path.name for path in files} >= {
        "live_market_20261118.log",
        "live_market_20261118.log.1",
        "live_market_20261118.log.2",
        "live_market_20261118.log.3",
    }


def test_daily_retention_removes_only_expired_dated_logs(tmp_path):
    expired = tmp_path / "live_market_20261019.log"
    expired_segment = tmp_path / "live_market_20261019.log.1"
    boundary = tmp_path / "live_market_20261020.log"
    current = tmp_path / "live_market_20261118.log"
    master_backup = tmp_path / "live_market.log.1"
    unrelated = tmp_path / "notes.txt"

    for path in (expired, expired_segment, boundary, current, master_backup, unrelated):
        path.write_text("preserve-check", encoding="utf-8")

    handler = UtcDailySizeRotatingFileHandler(
        tmp_path,
        max_bytes=1024,
        backup_count=3,
        retention_days=30,
        now_fn=lambda: datetime(2026, 11, 18, tzinfo=timezone.utc),
    )
    handler.close()

    assert not expired.exists()
    assert not expired_segment.exists()
    assert boundary.exists()
    assert current.exists()
    assert master_backup.exists()
    assert unrelated.exists()


def test_daily_size_check_counts_utf8_bytes(tmp_path):
    handler = UtcDailySizeRotatingFileHandler(
        tmp_path,
        max_bytes=32,
        backup_count=3,
        now_fn=lambda: datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc),
    )
    _set_plain_formatter(handler)

    handler.handle(_record("a" * 12))
    handler.handle(_record("💹" * 12))
    handler.close()

    assert (tmp_path / "live_market_20261118.log").exists()
    assert (tmp_path / "live_market_20261118.log.1").exists()


def test_master_handler_rotates_at_utc_midnight(tmp_path):
    now = [datetime(2026, 11, 17, 23, 59, tzinfo=timezone.utc)]
    path = tmp_path / "live_market.log"
    handler = UtcSizeAndDailyRotatingFileHandler(
        path,
        max_bytes=1024,
        backup_count=3,
        now_fn=lambda: now[0],
    )
    _set_plain_formatter(handler)

    handler.handle(_record("master-before-midnight"))
    now[0] = datetime(2026, 11, 18, 0, 0, tzinfo=timezone.utc)
    handler.handle(_record("master-after-midnight"))
    handler.close()

    assert "master-after-midnight" in path.read_text(encoding="utf-8")
    assert "master-before-midnight" in (tmp_path / "live_market.log.1").read_text(
        encoding="utf-8"
    )


def test_master_handler_size_rotation_is_bounded_by_backup_count(tmp_path):
    handler = UtcSizeAndDailyRotatingFileHandler(
        tmp_path / "live_market.log",
        max_bytes=80,
        backup_count=2,
        now_fn=lambda: datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc),
    )
    _set_plain_formatter(handler)

    for index in range(8):
        handler.handle(_record(f"{index}-" + ("y" * 35)))
    handler.close()

    files = list(tmp_path.glob("live_market.log*"))
    assert len(files) <= 3
    assert {path.name for path in files} >= {
        "live_market.log",
        "live_market.log.1",
        "live_market.log.2",
    }


def test_master_retention_uses_backup_mtime_without_touching_active_log(tmp_path):
    active = tmp_path / "live_market.log"
    expired_backup = tmp_path / "live_market.log.1"
    recent_backup = tmp_path / "live_market.log.2"
    active.write_text("active", encoding="utf-8")
    expired_backup.write_text("expired", encoding="utf-8")
    recent_backup.write_text("recent", encoding="utf-8")

    old_time = datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp()
    recent_time = datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp()
    os.utime(expired_backup, (old_time, old_time))
    os.utime(recent_backup, (recent_time, recent_time))

    handler = UtcSizeAndDailyRotatingFileHandler(
        active,
        max_bytes=1024,
        backup_count=2,
        retention_days=30,
        now_fn=lambda: datetime(2026, 11, 18, tzinfo=timezone.utc),
    )
    handler.close()

    assert active.exists()
    assert not expired_backup.exists()
    assert recent_backup.exists()


def test_factory_keeps_both_existing_log_destinations(tmp_path):
    daily, master = create_logging_file_handlers(tmp_path)
    _set_plain_formatter(daily)
    _set_plain_formatter(master)

    daily.handle(_record("same-record"))
    master.handle(_record("same-record"))
    daily.close()
    master.close()

    date_file = tmp_path / "live_market_20261118.log"
    master_file = tmp_path / "live_market.log"
    assert "same-record" in date_file.read_text(encoding="utf-8")
    assert "same-record" in master_file.read_text(encoding="utf-8")

    assert DAILY_LOG_MAX_BYTES == 128 * 1024 * 1024
    assert DAILY_LOG_BACKUP_COUNT == 3
    assert MASTER_LOG_MAX_BYTES == 50 * 1024 * 1024
    assert MASTER_LOG_BACKUP_COUNT == 10
    assert LOG_RETENTION_DAYS == 30
