from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from moneygun_api.close_auction_live import (
    MISSION_ID,
    STRATEGY_ID,
    live_scan_status,
    run_live_close_auction_scan,
)
from moneygun_api.storage import Database

KST = timezone(timedelta(hours=9))


def _business_days(end: date, count: int) -> list[str]:
    result: list[str] = []
    current = end
    while len(result) < count:
        if current.weekday() < 5:
            result.append(current.isoformat())
        current -= timedelta(days=1)
    return list(reversed(result))


def _bars(end: date, *, base: int, daily_step: int, volume: int) -> list[dict[str, Any]]:
    result = []
    for index, day in enumerate(_business_days(end, 220)):
        close = base + daily_step * index
        result.append(
            {
                "date": day,
                "open": close - 20,
                "high": close + 80,
                "low": close - 100,
                "close": close,
                "volume": volume,
            }
        )
    return result


class FakeLiveClient:
    def fetch_today_volume_top(
        self, *, market: str, price_filter: str, max_pages: int
    ) -> list[dict[str, Any]]:
        del max_pages
        if market != "KOSDAQ" or price_filter != "10":
            return []
        return [
            {
                "symbol": "123456",
                "name": "작은회사A",
                "market": "KOSDAQ",
                "current_price_krw": 12_500,
                "volume": 1_000_000,
                "change_rate_pct": 2.5,
                "estimated_turnover_krw": 12_500_000_000,
                "source": "KIWOOM_KA10030",
            }
        ]

    def fetch_domestic_quote(self, symbol: str) -> dict[str, Any]:
        assert symbol == "123456"
        return {
            "stk_nm": "작은회사A",
            "cur_prc": "12500",
            "open_pric": "12000",
            "high_pric": "12600",
            "low_pric": "11800",
            "trde_qty": "1000000",
            "mac": "8000",
            "flu_rt": "2.5",
        }

    def fetch_daily_bars(
        self, symbol: str, *, base_date: str, max_pages: int
    ) -> list[dict[str, Any]]:
        del base_date, max_pages
        if symbol == "069500":
            return _bars(date(2026, 9, 1), base=30_000, daily_step=2, volume=5_000_000)
        return _bars(date(2026, 9, 1), base=9_000, daily_step=14, volume=500_000)


def _qualified_snapshot() -> dict[str, Any]:
    return {
        "id": "snap_qualified_for_live_test",
        "as_of": "2026-09-01T18:00:00+09:00",
        "source": "KRX_AUTHORIZED_EXPORT",
        "market": "KR",
        "benchmark": {"symbol": "069500", "bars": []},
        "instruments": [],
        "evidence": [],
        "quality": {"state": "PASS", "issues": []},
        "checksum": "a" * 64,
        "collection_manifest": {
            "survivorship_bias_controlled": True,
            "historical_designation_states_complete": True,
        },
        "universe_history": [
            {
                "symbol": "123456",
                "name": "작은회사A",
                "market": "KOSDAQ",
                "security_type": "COMMON_STOCK",
                "listed_on": "2010-01-04",
                "delisted_on": None,
            }
        ],
        "designation_history": [],
    }


def test_l0_live_scan_allows_liquid_affordable_small_cap_without_order(tmp_path) -> None:
    database = Database(tmp_path / "close-live.sqlite3")
    database.initialize()
    database.save_snapshot(_qualified_snapshot())

    result = run_live_close_auction_scan(
        database,
        FakeLiveClient(),  # type: ignore[arg-type]
        now_kst=datetime(2026, 9, 2, 15, 7, tzinfo=KST),
    )

    cycle = result["cycle"]
    assert cycle["mission_id"] == MISSION_ID
    assert cycle["strategy_id"] == STRATEGY_ID
    assert cycle["decision"]["action"] == "BUY_CANDIDATE"
    assert cycle["decision"]["symbol"] == "123456"
    assert cycle["decision"]["quantity"] >= 1
    assert cycle["decision"]["order_allowed"] is False
    assert cycle["candidates"][0]["size_band"] == "SMALL_CAP"
    assert cycle["coverage"]["coverage"]["affordable_rows"] == 1
    assert cycle["coverage"]["small_cap_evaluated_count"] == 1
    assert cycle["trading_enabled"] is False


def test_l0_live_scan_is_same_day_idempotent(tmp_path) -> None:
    database = Database(tmp_path / "close-live-reuse.sqlite3")
    database.initialize()
    database.save_snapshot(_qualified_snapshot())
    now = datetime(2026, 9, 2, 15, 7, tzinfo=KST)

    first = run_live_close_auction_scan(
        database, FakeLiveClient(), now_kst=now  # type: ignore[arg-type]
    )
    second = run_live_close_auction_scan(
        database, FakeLiveClient(), now_kst=now  # type: ignore[arg-type]
    )

    assert first["cycle"]["id"] == second["cycle"]["id"]
    assert second["reused"] is True


def test_live_status_distinguishes_missed_window_from_hold(tmp_path) -> None:
    database = Database(tmp_path / "close-live-status.sqlite3")
    database.initialize()

    status = live_scan_status(
        database, now_kst=datetime(2026, 9, 2, 16, 0, tzinfo=KST)
    )

    assert status["state"] == "WINDOW_ENDED"
    assert status["cycle"] is None
    assert status["automatic_broker_submission"] is False
