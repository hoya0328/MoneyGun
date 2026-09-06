from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from fastapi.testclient import TestClient

from moneygun_api.execution import _news_feed_blocker
from moneygun_api.main import create_app
from moneygun_api.news import (
    classify_disclosure,
    news_status,
    poll_official_news,
    reset_corporation_cache,
)
from moneygun_api.storage import Database

KST = timezone(timedelta(hours=9))


class FakeDartClient:
    configured = True

    def corporate_codes(self) -> list[dict[str, str]]:
        return [
            {
                "corp_code": "00126380",
                "corp_name": "삼성전자",
                "stock_code": "005930",
                "modify_date": "20260903",
            }
        ]

    def search_disclosures(
        self,
        *,
        corp_code: str,
        begin_date: str,
        end_date: str,
        last_report_only: bool = True,
    ) -> list[dict[str, Any]]:
        assert corp_code == "00126380"
        assert begin_date <= end_date
        assert last_report_only is False
        return [
            {
                "corp_name": "삼성전자",
                "rcept_no": "20260903000001",
                "rcept_dt": "20260903",
                "report_nm": "유상증자결정",
            },
            {
                "corp_name": "삼성전자",
                "rcept_no": "20260903000002",
                "rcept_dt": "20260903",
                "report_nm": "현금ㆍ현물배당결정",
            },
        ]


def test_disclosure_classifier_is_deterministic_and_conservative() -> None:
    critical = classify_disclosure("횡령ㆍ배임 혐의 발생")
    warning = classify_disclosure("주주배정 유상증자 결정")
    information = classify_disclosure("단일판매ㆍ공급계약 체결")

    assert critical["severity"] == "CRITICAL"
    assert critical["blocks_new_buy"] is True
    assert warning["severity"] == "WARNING"
    assert warning["review_required"] is True
    assert information["severity"] == "INFO"
    assert information["sentiment"] == "NEUTRAL"
    assert information["analysis"]["llm_used"] is False


def test_official_news_poll_is_idempotent_and_owner_review_unblocks_warning(tmp_path) -> None:
    reset_corporation_cache()
    database = Database(tmp_path / "news.sqlite3")
    database.initialize()
    now = datetime(2026, 9, 3, 8, 30, tzinfo=KST)

    first = poll_official_news(database, FakeDartClient(), now_kst=now)  # type: ignore[arg-type]
    second = poll_official_news(database, FakeDartClient(), now_kst=now)  # type: ignore[arg-type]

    assert first["state"] == "PASS"
    assert first["inserted_count"] == 2
    assert first["blocking_count"] == 1
    assert second["inserted_count"] == 0
    events = database.list_news_events()
    warning = next(event for event in events if event["severity"] == "WARNING")
    assert database.active_news_blocks(
        "005930", now_utc=now.astimezone(UTC)
    )[0]["id"] == warning["id"]

    database.acknowledge_news_event(warning["id"])

    assert database.active_news_blocks("005930", now_utc=now.astimezone(UTC)) == []
    status = news_status(database, now_kst=now)
    assert status["counts"]["total"] == 2
    assert status["counts"]["unresolved"] == 0


def test_critical_disclosure_remains_blocking_after_acknowledgement(tmp_path) -> None:
    database = Database(tmp_path / "critical-news.sqlite3")
    database.initialize()
    classification = classify_disclosure("상장폐지 결정")
    event = database.save_news_event(
        {
            "id": "news_critical_test",
            "source": "OPENDART_OFFICIAL",
            "source_event_id": "20260903009999",
            "symbol": "005930",
            "corp_code": "00126380",
            "company_name": "삼성전자",
            "title": "상장폐지 결정",
            **classification,
            "published_at": "2026-09-03T00:00:00+09:00",
            "collected_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "url": "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260903009999",
        }
    )

    database.acknowledge_news_event(event["id"])

    assert database.active_news_blocks("005930")[0]["severity"] == "CRITICAL"


def test_news_acknowledgement_requires_owner_token(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MONEYGUN_OWNER_API_TOKEN", "owner-token-with-at-least-24-characters")
    app = create_app(tmp_path / "news-api.sqlite3")
    database = app.state.database
    classification = classify_disclosure("전환사채권발행결정")
    event = database.save_news_event(
        {
            "id": "news_api_test",
            "source": "OPENDART_OFFICIAL",
            "source_event_id": "20260903008888",
            "symbol": "005930",
            "corp_code": "00126380",
            "company_name": "삼성전자",
            "title": "전환사채권발행결정",
            **classification,
            "published_at": "2026-09-03T00:00:00+09:00",
            "collected_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "url": "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260903008888",
        }
    )
    client = TestClient(app)

    unauthorized = client.post(f"/v1/news/events/{event['id']}/acknowledge")
    authorized = client.post(
        f"/v1/news/events/{event['id']}/acknowledge",
        headers={"X-Owner-Approval-Token": "owner-token-with-at-least-24-characters"},
    )

    assert unauthorized.status_code == 401
    assert authorized.status_code == 200
    assert authorized.json()["acknowledged_at"]


def test_enabled_news_feed_fails_closed_when_stale(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MONEYGUN_NEWS_SCHEDULER_ENABLED", "true")
    database = Database(tmp_path / "stale-news.sqlite3")
    database.initialize()
    stale = (datetime.now(UTC) - timedelta(minutes=16)).isoformat(timespec="seconds")
    database.save_news_poll_run(
        {
            "id": "newspoll_stale_test",
            "source": "OPENDART_OFFICIAL",
            "state": "PASS",
            "watched_symbols": 8,
            "fetched_count": 0,
            "inserted_count": 0,
            "blocking_count": 0,
            "error": None,
            "started_at": stale,
            "completed_at": stale,
        }
    )

    assert "15분보다 오래" in (_news_feed_blocker(database) or "")

    fresh = datetime.now(UTC).isoformat(timespec="seconds")
    database.save_news_poll_run(
        {
            "id": "newspoll_fresh_test",
            "source": "OPENDART_OFFICIAL",
            "state": "PASS",
            "watched_symbols": 8,
            "fetched_count": 0,
            "inserted_count": 0,
            "blocking_count": 0,
            "error": None,
            "started_at": fresh,
            "completed_at": fresh,
        }
    )
    assert _news_feed_blocker(database) is None
