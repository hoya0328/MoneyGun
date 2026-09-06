from datetime import date, timedelta

from fastapi.testclient import TestClient

from moneygun_api.execution import MODE_EXECUTION_POLICIES, stage_gate_report
from moneygun_api.main import create_app
from moneygun_api.storage import Database


def _eligible_validation(database: Database, mission_id: str, strategy_id: str) -> None:
    snapshot_id = database.save_snapshot(
        {
            "id": f"snap_{mission_id[-8:]}",
            "as_of": "2026-08-31T11:00:00+09:00",
            "source": "LICENSED_INTRADAY_VENDOR",
            "quality": {"state": "PASS", "issues": []},
            "checksum": "test-checksum",
        }
    )["id"]
    database.save_validation(
        mission_id,
        {
            "id": f"validation_{mission_id[-8:]}",
            "snapshot_id": snapshot_id,
            "strategy_id": strategy_id,
            "protocol_version": "TEST_POINT_IN_TIME",
            "status": "ELIGIBLE_FOR_R1_REVIEW",
            "promotion_eligible": True,
            "metrics": {},
            "failed_gates": [],
        },
    )


def test_activation_readiness_explains_every_mission_without_submitting(tmp_path) -> None:
    with TestClient(create_app(tmp_path / "activation.sqlite3")) as client:
        response = client.get("/v1/execution/activation-readiness")

    assert response.status_code == 200
    payload = response.json()
    assert payload["state"] == "BLOCKED"
    assert payload["live_orders_submitted_by_check"] == 0
    assert {item["mode_code"] for item in payload["missions"]} == {
        "L0_PILOT",
        "FOCUS",
        "CLOSE_AUCTION",
            "OPENING_RANGE",
            "BALANCED",
            "LONG_TERM",
        }
    opening = next(item for item in payload["missions"] if item["mode_code"] == "OPENING_RANGE")
    assert opening["strategy_id"] == "OPEN-RANGE-KR-v2"
    assert opening["gates"]["R1_TO_L1"]["requirements"]["decisions"] == 300
    assert opening["can_submit_live_order"] is False
    pilot = next(item for item in payload["missions"] if item["mode_code"] == "L0_PILOT")
    assert pilot["state"] == "L0_SETUP_REQUIRED"
    assert pilot["pilot"]["limits"]["capital_krw"] == 50_000
    assert pilot["pilot"]["strategy_performance_qualified"] is False


def test_opening_range_requires_300_real_shadow_decisions(tmp_path) -> None:
    database = Database(tmp_path / "opening-gate.sqlite3")
    database.initialize()
    mission_id = "mission_opening_range_001"
    _eligible_validation(
        database,
        mission_id,
        MODE_EXECUTION_POLICIES["OPENING_RANGE"]["strategy_id"],
    )
    first_day = date(2026, 1, 2)
    for index in range(60):
        database.record_operating_day(
            mission_id=mission_id,
            trade_date=(first_day + timedelta(days=index)).isoformat(),
            mode="R1",
            reconciliation_status="PASS",
            risk_violations=0,
            duplicate_orders=0,
            decision_count=4,
            fill_count=0,
            net_pnl_krw=0,
        )

    report = stage_gate_report(database, mission_id)

    assert report["R1_TO_L1"]["metrics"]["decisions"] == 240
    assert report["R1_TO_L1"]["gates"]["shadow_decisions_300"] is False
    assert report["R1_TO_L1"]["eligible"] is False


def test_every_live_mode_has_an_explicit_strategy_source_and_window() -> None:
    for policy in MODE_EXECUTION_POLICIES.values():
        assert policy["strategy_id"]
        assert policy["official_shadow_sources"]
        assert len(policy["order_window_kst"]) == 2
