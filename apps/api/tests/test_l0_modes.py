from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from fastapi.testclient import TestClient

from moneygun_api.execution import ExecutionGuardian, build_live_intent
from moneygun_api.l0_modes import MODE_DEFINITIONS, run_daily_mode_scan
from moneygun_api.main import create_app
from moneygun_api.opening_range import build_opening_range_signal_plan
from moneygun_api.opening_range_live import _archive
from moneygun_api.pilot import PILOT_MISSION_ID
from moneygun_api.storage import Database, utc_now

KST = timezone(timedelta(hours=9))


def _business_days(end: date, count: int) -> list[str]:
    days: list[str] = []
    current = end
    while len(days) < count:
        if current.weekday() < 5:
            days.append(current.isoformat())
        current -= timedelta(days=1)
    return list(reversed(days))


def _bars(end: date, base: int, step: int, volume: int) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index, day in enumerate(_business_days(end, 230)):
        close = base + step * index
        result.append(
            {
                "date": day,
                "open": close - 10,
                "high": close + 35,
                "low": close - 35,
                "close": close,
                "volume": volume,
            }
        )
    return result


class DailyModeClient:
    def fetch_today_volume_top(
        self, *, market: str, price_filter: str = "0", max_pages: int
    ) -> list[dict[str, Any]]:
        del price_filter, max_pages
        if market != "KOSPI":
            return []
        return [
            {
                "symbol": "123456",
                "name": "튼튼회사",
                "market": "KOSPI",
                "current_price_krw": 12_500,
                "volume": 3_000_000,
                "estimated_turnover_krw": 37_500_000_000,
            }
        ]

    def fetch_domestic_quote(self, symbol: str) -> dict[str, Any]:
        assert symbol == "123456"
        return {
            "stk_nm": "튼튼회사",
            "cur_prc": "12500",
            "open_pric": "12400",
            "high_pric": "12600",
            "low_pric": "12350",
            "trde_qty": "3000000",
            "mac": "25000",
        }

    def fetch_daily_bars(
        self, symbol: str, *, base_date: str, max_pages: int
    ) -> list[dict[str, Any]]:
        del base_date, max_pages
        if symbol == "069500":
            return _bars(date(2026, 9, 2), 30_000, 1, 5_000_000)
        return _bars(date(2026, 9, 2), 10_000, 11, 2_000_000)


def _qualified() -> dict[str, Any]:
    return {
        "id": "snap_l0_modes_qualified",
        "as_of": "2026-09-01T18:00:00+09:00",
        "source": "KRX_AUTHORIZED_EXPORT",
        "quality": {"state": "PASS", "issues": []},
        "checksum": "b" * 64,
        "universe_history": [
            {
                "symbol": "123456",
                "name": "튼튼회사",
                "market": "KOSPI",
                "security_type": "COMMON_STOCK",
                "listed_on": "2010-01-01",
                "delisted_on": None,
            }
        ],
        "designation_history": [],
    }


def test_balanced_and_long_term_missions_are_seeded(tmp_path) -> None:
    database = Database(tmp_path / "seed.sqlite3")
    database.initialize()

    assert database.get_mission("mission_balanced_001")["mode_code"] == "BALANCED"
    assert database.get_mission("mission_long_term_001")["mode_code"] == "LONG_TERM"


def test_all_daily_l0_modes_build_candidate_without_submitting(tmp_path) -> None:
    database = Database(tmp_path / "modes.sqlite3")
    database.initialize()
    database.save_snapshot(_qualified())
    now = datetime(2026, 9, 2, 15, 40, tzinfo=KST)

    for mode_code, definition in MODE_DEFINITIONS.items():
        result = run_daily_mode_scan(
            database,
            DailyModeClient(),  # type: ignore[arg-type]
            mode_code,
            now_kst=now,
        )
        cycle = result["cycle"]
        assert cycle["strategy_id"] == definition.strategy_id
        assert cycle["decision"]["action"] == "BUY_CANDIDATE", (
            mode_code,
            cycle["candidates"],
        )
        assert cycle["decision"]["quantity"] >= 1
        assert cycle["risk"]["result"] == "PASS_L0_EXPERIMENTAL"
        assert cycle["trading_enabled"] is False


def _real_event(symbol: str, hour_minute: str, price: int, volume: int) -> dict[str, Any]:
    return {
        "received_at": f"2026-09-02T{hour_minute[:2]}:{hour_minute[2:]}:20+09:00",
        "type": "0B",
        "item": symbol,
        "values": {
            "20": f"{hour_minute}20",
            "10": str(price),
            "15": str(volume),
            "27": str(price),
            "28": str(price - 10),
        },
    }


def test_opening_live_archive_can_create_current_l0_signal(tmp_path) -> None:
    database = Database(tmp_path / "opening.sqlite3")
    database.initialize()
    quote_events = [
        {
            "received_at": "2026-09-02T09:00:00+09:00",
            "type": "0D",
            "item": symbol,
            "values": {"51": "10000", "41": "10010", "125": "500000", "121": "500000"},
        }
        for symbol in ("123456", "069500")
    ]
    stock_prices = [10000, 10010, 10020, 10030, 10040, 10050, 10060, 10070, 10080, 10100, 10120]
    benchmark_prices = [30000 + index * 10 for index in range(11)]
    events = quote_events + [
        _real_event("123456", f"09{index:02d}", price, 100_000)
        for index, price in enumerate(stock_prices)
    ] + [
        _real_event("069500", f"09{index:02d}", price, 100_000)
        for index, price in enumerate(benchmark_prices)
    ]
    state = {
        "trade_date": "2026-09-02",
        "qualified_snapshot_id": "snap_qualified",
        "events": events,
        "watchlist": [
            {
                "symbol": "123456",
                "name": "장초회사",
                "market": "KOSDAQ",
                "previous_close": 9_950,
                "median_daily_turnover_krw": 50_000_000_000,
                "median_first10_turnover_krw": 500_000_000,
                "designation": {},
            }
        ],
    }
    now = datetime(2026, 9, 2, 9, 10, 30, tzinfo=KST)
    snapshot = _archive(state, now)
    database.save_snapshot(snapshot)

    package = build_opening_range_signal_plan(
        database.get_mission(PILOT_MISSION_ID), snapshot, now=now
    )

    assert snapshot["quality"]["state"] == "PASS"
    assert package["decision"]["symbol"] == "123456"
    assert package["decision"]["quantity"] >= 1
    assert package["broker_submitted"] is False


def test_l0_mode_api_reports_five_operable_sources(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "api.sqlite3"))

    daily = client.get("/v1/strategies/l0-modes/status")
    opening = client.get("/v1/strategies/opening-range/live/status")
    exits = client.get("/v1/l0-pilot/exits/status")

    assert daily.status_code == 200
    assert {item["mode_code"] for item in daily.json()["modes"]} == {
        "FOCUS",
        "BALANCED",
        "LONG_TERM",
    }
    assert opening.status_code == 200
    assert opening.json()["spec"]["strategy_id"] == "OPEN-RANGE-KR-v3-L0"
    assert exits.status_code == 200
    assert exits.json()["existing_broker_holdings_ignored"] is True


def test_exit_intent_cannot_sell_more_than_moneygun_filled(tmp_path) -> None:
    database = Database(tmp_path / "sell-guard.sqlite3")
    database.initialize()
    database.update_execution_control(
        PILOT_MISSION_ID, actor="TEST", reason="L0 매도 수량 검사", stage="L0"
    )
    snapshot = database.save_snapshot(
        {
            "id": "snap_sell_guard",
            "as_of": "2026-09-02T09:00:00+09:00",
            "source": "KIWOOM_FOCUS_L0_SCAN",
            "quality": {"state": "PASS", "issues": []},
            "checksum": "c" * 64,
        }
    )
    cycle = database.save_cycle(
        {
            "id": "cycle_sell_guard",
            "mission_id": "mission_focus_001",
            "snapshot_id": snapshot["id"],
            "strategy_id": "FOCUS-MOMENTUM-KR-v2-L0",
            "protocol_version": "TEST-v1",
            "status": "COMPLETE",
            "decision": {"action": "BUY_CANDIDATE", "entry_style": "NEXT_OPEN_LIMIT"},
            "risk": {"result": "PASS_L0_EXPERIMENTAL"},
        }
    )
    buy_shadow = database.create_shadow_order(
        {
            "mission_id": "mission_focus_001",
            "cycle_id": cycle["id"],
            "trade_date": "2026-09-02",
            "next_session_date": "2026-09-02",
            "symbol": "123456",
            "name": "관리종목",
            "side": "BUY",
            "quantity": 1,
            "signal_close_krw": 10_000,
            "limit_price_krw": 10_000,
            "invalidation_price_krw": 9_500,
            "simulation_source": "KIWOOM_L0_NEXT_OPEN_QUOTE",
        },
        idempotency_key="sell-guard-buy-shadow",
    )
    buy_intent = database.create_live_order_intent(
        {
            "mission_id": PILOT_MISSION_ID,
            "source_mission_id": "mission_focus_001",
            "shadow_order_id": buy_shadow["id"],
            "stage": "L0",
            "strategy_id": "FOCUS-MOMENTUM-KR-v2-L0",
            "entry_style": "NEXT_OPEN_LIMIT",
            "symbol": "123456",
            "name": "관리종목",
            "side": "BUY",
            "quantity": 1,
            "limit_price_krw": 10_000,
            "invalidation_price_krw": 9_500,
            "order_value_krw": 10_000,
            "precheck_blockers": [],
            "real_submission_attempted": False,
        },
        idempotency_key="sell-guard-buy-intent",
        scope_hash="a" * 64,
    )
    database.record_live_fill(
        buy_intent["id"],
        broker_order_no="1111111",
        quantity=1,
        price_krw=10_000,
        fee_krw=0,
        tax_krw=0,
        occurred_at=utc_now(),
        broker_fill_key="sell-guard-fill",
    )
    sell_shadow = database.create_shadow_order(
        {
            "mission_id": "mission_focus_001",
            "cycle_id": cycle["id"],
            "trade_date": "2026-09-02",
            "next_session_date": "2026-09-02",
            "symbol": "123456",
            "name": "관리종목",
            "side": "SELL",
            "quantity": 2,
            "signal_close_krw": 10_000,
            "limit_price_krw": 10_000,
            "invalidation_price_krw": 9_500,
            "simulation_source": "KIWOOM_L0_EXIT_QUOTE",
        },
        idempotency_key="sell-guard-sell-shadow",
    )
    database.save_broker_quote_snapshot(
        broker="KIWOOM",
        symbol="123456",
        observed_at=utc_now(),
        payload={"current_price_krw": 10_000, "source": "KIWOOM_OFFICIAL_REST"},
    )
    account = database.save_broker_account_snapshot(
        broker="KIWOOM",
        environment="mock",
        account_alias="test",
        payload={"prsm_dpst_aset_amt": "50000", "acnt_evlt_remn_indv_tot": []},
    )
    database.save_reconciliation(
        mission_id=PILOT_MISSION_ID,
        account_snapshot_id=account["id"],
        status="PASS",
        details={"position_differences": []},
    )

    intent = build_live_intent(
        database,
        ExecutionGuardian(),
        shadow_order_id=sell_shadow["id"],
        idempotency_key="sell-guard-over-intent",
        execution_mission_id=PILOT_MISSION_ID,
    )

    assert intent["state"] == "REJECTED"
    assert any("관리하는 보유 수량" in blocker for blocker in intent["precheck_blockers"])
