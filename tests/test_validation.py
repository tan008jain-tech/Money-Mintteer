from datetime import datetime, timedelta, timezone

from money_mintter.research.validation import WalkForwardValidator


def test_walk_forward_windows_are_chronological() -> None:
    windows = WalkForwardValidator().windows(
        100,
        train_size=40,
        test_size=20,
        step=20,
    )
    assert windows[0].train_start == 0
    assert windows[0].train_end == 40
    assert windows[0].test_start == 40
    assert windows[0].test_end == 60
    assert windows[1].train_start == 20
    assert windows[1].test_end == 80
