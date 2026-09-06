from datetime import datetime, timedelta, timezone

from moneygun_api.auto_execution_scheduler import _active

KST = timezone(timedelta(hours=9))


def test_auto_execution_scheduler_runs_only_during_weekday_guard_window() -> None:
    assert _active(datetime(2026, 9, 3, 8, 45, tzinfo=KST)) is True
    assert _active(datetime(2026, 9, 3, 18, 5, tzinfo=KST)) is True
    assert _active(datetime(2026, 9, 3, 8, 44, tzinfo=KST)) is False
    assert _active(datetime(2026, 9, 5, 10, 0, tzinfo=KST)) is False
