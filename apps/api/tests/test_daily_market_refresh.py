from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from moneygun_api.daily_market_refresh import (
    DailyMarketRefreshService,
    latest_public_trading_date,
)
from moneygun_api.daily_market_refresh_scheduler import schedule_period
from moneygun_api.public_market_data import PageResult
from moneygun_api.storage import Database

KST = timezone(timedelta(hours=9))


class _PriceClient:
    def price_page(
        self, *, start: date, end: date, page: int, page_size: int
    ) -> PageResult:
        del start, end, page, page_size
        return PageResult(
            page=1,
            total_count=2,
            items=[{"basDt": "20260903"}, {"basDt": "20260902"}],
        )


def test_latest_public_trading_date_uses_published_completed_day() -> None:
    assert latest_public_trading_date(_PriceClient(), date(2026, 9, 4)) == date(2026, 9, 3)


def test_scheduler_has_post_close_and_morning_fallback_periods() -> None:
    assert schedule_period(datetime(2026, 9, 4, 18, 30, tzinfo=KST)) == (
        "2026-09-04:POST_CLOSE"
    )
    assert schedule_period(datetime(2026, 9, 5, 7, 0, tzinfo=KST)) == (
        "2026-09-05:MORNING_FALLBACK"
    )
    assert schedule_period(datetime(2026, 9, 5, 19, 0, tzinfo=KST)) is None


def test_service_records_success_without_order_capability(tmp_path: Path) -> None:
    database = Database(tmp_path / "moneygun.sqlite3")
    database.initialize()

    def runner(*args: object, **kwargs: object) -> dict[str, object]:
        del args, kwargs
        return {
            "state": "SUCCEEDED",
            "as_of": "2026-09-04",
            "snapshot_id": "snap_0123456789abcdef",
            "bundle_file": "moneygun-official-20260904.qualified.json.gz",
            "instrument_count": 2500,
            "bar_count": 2_500_000,
            "designation_interval_count": 1200,
            "message": "ok",
        }

    service = DailyMarketRefreshService(
        database,
        import_root=tmp_path / "imports",
        status_path=tmp_path / "status.json",
        runner=runner,
    )
    result = service.trigger(background=False, reference_date=date(2026, 9, 4))
    assert result["state"] == "SUCCEEDED"
    assert result["last_success_as_of"] == "2026-09-04"
    assert result["automatic_order_submission"] is False
    actions = [item["action"] for item in database.list_audit_events(10)]
    assert "data.refresh.started" in actions
    assert "data.refresh.completed" in actions


def test_latest_qualification_metadata_does_not_require_payload_decode(tmp_path: Path) -> None:
    database = Database(tmp_path / "moneygun.sqlite3")
    database.initialize()
    for day, suffix in (("2026-09-02", "a"), ("2026-09-03", "b")):
        database.save_snapshot(
            {
                "id": f"snap_{suffix * 16}",
                "as_of": f"{day}T15:40:00+09:00",
                "source": "OFFICIAL_PUBLIC_DATA_API_KIND_AND_KIWOOM",
                "quality": {"state": "PASS"},
                "checksum": suffix * 64,
            }
        )
    metadata = database.get_latest_snapshot_metadata_for_sources(
        ("OFFICIAL_PUBLIC_DATA_API_KIND_AND_KIWOOM",)
    )
    assert metadata is not None
    assert metadata["id"] == "snap_bbbbbbbbbbbbbbbb"
    assert metadata["quality_state"] == "PASS"


def test_service_keeps_failure_visible_and_retryable(tmp_path: Path) -> None:
    database = Database(tmp_path / "moneygun.sqlite3")
    database.initialize()

    def runner(*args: object, **kwargs: object) -> dict[str, object]:
        del args, kwargs
        raise RuntimeError("official source unavailable")

    service = DailyMarketRefreshService(
        database,
        status_path=tmp_path / "status.json",
        runner=runner,
    )
    result = service.trigger(background=False)
    assert result["state"] == "FAILED"
    assert "official source unavailable" in result["error"]
    assert result["automatic_order_submission"] is False


def test_status_file_contains_no_secret_material(tmp_path: Path) -> None:
    path = tmp_path / "status.json"
    path.write_text(
        json.dumps({"state": "SUCCEEDED", "last_success_as_of": "2026-09-04"}),
        encoding="utf-8",
    )
    raw = path.read_bytes()
    assert hashlib.sha256(raw).hexdigest()
    assert b"APP_KEY" not in raw
