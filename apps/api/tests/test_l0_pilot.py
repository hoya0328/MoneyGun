from datetime import date, timedelta
from typing import Any

from fastapi.testclient import TestClient

from moneygun_api.execution import (
    ExecutionGuardian,
    ExecutionRuntimeConfig,
    KiwoomExecutionClient,
    approve_live_intent,
    build_live_intent,
    run_l0_automation_tick,
    submit_live_intent,
    transition_stage,
)
from moneygun_api.kiwoom import KiwoomConfig
from moneygun_api.main import create_app
from moneygun_api.performance import sync_live_trade_outcomes
from moneygun_api.pilot import (
    PILOT_MISSION_ID,
    generate_due_pilot_reviews,
    pilot_risk_snapshot,
    pilot_status,
)
from moneygun_api.research import build_fixture_snapshot, run_committee_cycle
from moneygun_api.storage import Database, utc_now


class SuccessfulOrderTransport:
    def post(
        self, url: str, headers: dict[str, str], payload: dict[str, Any]
    ) -> dict[str, Any]:
        if url.endswith("/oauth2/token"):
            return {"return_code": 0, "token": "l0-test-token"}
        return {"return_code": 0, "ord_no": "7654321"}


def _guardian() -> ExecutionGuardian:
    return ExecutionGuardian(
        KiwoomExecutionClient(
            KiwoomConfig("order-app", "order-secret", "mock", "테스트"),
            SuccessfulOrderTransport(),
        ),
        ExecutionRuntimeConfig(
            trading_enabled=True,
            release_approved=True,
            owner_api_token="owner-token-at-least-24-characters",
            capital_limit_krw=100_000,
            l0_capital_limit_krw=50_000,
        ),
    )


def _pilot_context(tmp_path) -> tuple[Database, dict[str, Any]]:
    database = Database(tmp_path / "l0.sqlite3")
    database.initialize()
    transition_stage(database, PILOT_MISSION_ID, "L0", reason="5만 원 손실 가능성 승인")
    focus = database.get_mission("mission_focus_001")
    snapshot = database.save_snapshot(build_fixture_snapshot())
    cycle = database.save_cycle(run_committee_cycle(focus, snapshot))
    quote = database.save_broker_quote_snapshot(
        broker="KIWOOM",
        symbol="005930",
        observed_at=utc_now(),
        payload={
            "symbol": "005930",
            "name": "테스트 주식",
            "current_price_krw": 30_000,
            "open_price_krw": 30_000,
            "high_price_krw": 30_000,
            "low_price_krw": 30_000,
            "source": "KIWOOM_OFFICIAL_REST",
            "observed_at": utc_now(),
        },
    )
    account = database.save_broker_account_snapshot(
        broker="KIWOOM",
        environment="mock",
        account_alias="테스트",
        payload={"prsm_dpst_aset_amt": "50000", "acnt_evlt_remn_indv_tot": []},
    )
    database.save_reconciliation(
        mission_id=PILOT_MISSION_ID,
        account_snapshot_id=account["id"],
        status="PASS",
        details={"capital_covered": True, "position_differences": []},
    )
    shadow = database.create_shadow_order(
        {
            "mission_id": "mission_focus_001",
            "cycle_id": cycle["id"],
            "trade_date": "2026-09-01",
            "next_session_date": "2026-09-02",
            "symbol": "005930",
            "name": "테스트 주식",
            "side": "BUY",
            "quantity": 1,
            "signal_close_krw": 30_000,
            "limit_price_krw": 30_000,
            "invalidation_price_krw": 28_500,
            "simulation_source": "KIWOOM_OFFICIAL_QUOTE",
            "quote_snapshot_id": quote["id"],
            "broker_submitted": False,
            "trading_enabled": False,
        },
        idempotency_key="l0-source-shadow-001",
    )
    return database, shadow


def test_l0_mission_is_exactly_fifty_thousand_and_does_not_require_oos(tmp_path) -> None:
    database, shadow = _pilot_context(tmp_path)

    mission = database.get_mission(PILOT_MISSION_ID)
    intent = build_live_intent(
        database,
        _guardian(),
        shadow_order_id=shadow["id"],
        idempotency_key="l0-live-intent-001",
        execution_mission_id=PILOT_MISSION_ID,
    )

    assert mission["seed_capital_krw"] == 50_000
    assert mission["available_krw"] == 50_000
    assert intent["mission_id"] == PILOT_MISSION_ID
    assert intent["source_mission_id"] == "mission_focus_001"
    assert intent["stage"] == "L0"
    assert intent["state"] == "AWAITING_APPROVAL"
    assert intent["performance_qualified"] is False
    assert intent["precheck_blockers"] == []


def test_l0_order_requires_owner_approval_and_submits_only_once(tmp_path) -> None:
    database, shadow = _pilot_context(tmp_path)
    guardian = _guardian()
    intent = build_live_intent(
        database,
        guardian,
        shadow_order_id=shadow["id"],
        idempotency_key="l0-live-intent-submit-001",
        execution_mission_id=PILOT_MISSION_ID,
    )
    approved = approve_live_intent(
        database,
        intent["id"],
        decision="APPROVE",
        scope_hash=intent["scope_hash"],
    )

    submitted = submit_live_intent(database, guardian, approved["id"])

    assert submitted["state"] == "ACKNOWLEDGED"
    assert submitted["events"][-1]["payload"]["broker_order_no"] == "7654321"


def test_l0_automation_approves_and_submits_only_post_mandate_intent(tmp_path) -> None:
    database, shadow = _pilot_context(tmp_path)
    guardian = _guardian()
    database.update_execution_control(
        PILOT_MISSION_ID,
        actor="OWNER",
        reason="L0-AUTO-v1 테스트 승인",
        automation_enabled=True,
    )
    with database.connect() as connection:
        connection.execute(
            "UPDATE execution_controls SET updated_at = ? WHERE mission_id = ?",
            ("2000-01-01T00:00:00+00:00", PILOT_MISSION_ID),
        )
        connection.commit()
    intent = build_live_intent(
        database,
        guardian,
        shadow_order_id=shadow["id"],
        idempotency_key="l0-auto-post-mandate",
        execution_mission_id=PILOT_MISSION_ID,
    )

    result = run_l0_automation_tick(database, guardian)
    updated = database.get_live_order_intent(intent["id"])
    approval_audit = next(
        event
        for event in database.list_audit_events(limit=30)
        if event["action"] == "order.approval.approve"
    )

    assert result["submitted"] is True
    assert result["automatic_broker_submission"] is True
    assert updated["state"] == "ACKNOWLEDGED"
    assert approval_audit["actor"] == "OWNER_DELEGATED_L0_AUTO"
    assert approval_audit["payload"]["approval_actor"] == "OWNER_DELEGATED_L0_AUTO"


def test_l0_automation_does_not_retroactively_submit_old_intent(tmp_path) -> None:
    database, shadow = _pilot_context(tmp_path)
    guardian = _guardian()
    intent = build_live_intent(
        database,
        guardian,
        shadow_order_id=shadow["id"],
        idempotency_key="l0-auto-before-mandate",
        execution_mission_id=PILOT_MISSION_ID,
    )
    database.update_execution_control(
        PILOT_MISSION_ID,
        actor="OWNER",
        reason="L0-AUTO-v1 테스트 승인",
        automation_enabled=True,
    )

    result = run_l0_automation_tick(database, guardian)

    assert result["action"] == "NO_ELIGIBLE_INTENT"
    assert database.get_live_order_intent(intent["id"])["state"] == "AWAITING_APPROVAL"


def test_l0_automation_halts_on_unknown_without_retry(tmp_path) -> None:
    database, shadow = _pilot_context(tmp_path)
    guardian = _guardian()
    intent = build_live_intent(
        database,
        guardian,
        shadow_order_id=shadow["id"],
        idempotency_key="l0-auto-unknown",
        execution_mission_id=PILOT_MISSION_ID,
    )
    database.update_execution_control(
        PILOT_MISSION_ID,
        actor="OWNER",
        reason="L0-AUTO-v1 테스트 승인",
        automation_enabled=True,
    )
    database.append_live_order_event(
        intent["id"],
        "UNKNOWN",
        {"reason": "테스트 응답 불명", "retry_forbidden": True},
    )

    result = run_l0_automation_tick(database, guardian)

    assert result["action"] == "AMBIGUOUS_ORDER_HALTED"
    assert result["control"]["kill_switch_active"] is True
    assert result["control"]["automation_enabled"] is False
    assert database.get_live_order_intent(intent["id"])["state"] == "UNKNOWN"


def test_l0_rejects_a_single_order_above_forty_five_thousand(tmp_path) -> None:
    database, shadow = _pilot_context(tmp_path)
    oversized = database.create_shadow_order(
        {
            **{key: shadow[key] for key in (
                "mission_id", "cycle_id", "trade_date", "next_session_date", "symbol",
                "name", "side", "signal_close_krw", "invalidation_price_krw",
                "simulation_source",
            )},
            "quantity": 2,
            "limit_price_krw": 30_000,
            "broker_submitted": False,
            "trading_enabled": False,
        },
        idempotency_key="l0-source-shadow-oversized",
    )

    intent = build_live_intent(
        database,
        _guardian(),
        shadow_order_id=oversized["id"],
        idempotency_key="l0-live-intent-oversized",
        execution_mission_id=PILOT_MISSION_ID,
    )

    assert intent["state"] == "REJECTED"
    assert any("45,000원" in blocker for blocker in intent["precheck_blockers"])


def test_l0_daily_loss_blocks_a_new_buy(tmp_path) -> None:
    database, shadow = _pilot_context(tmp_path)
    base_payload = {
        "mission_id": PILOT_MISSION_ID,
        "source_mission_id": "mission_focus_001",
        "shadow_order_id": shadow["id"],
        "stage": "L0",
        "strategy_id": "FOCUS-MOMENTUM-KR-v1",
        "entry_style": "NEXT_OPEN_LIMIT",
        "symbol": "005930",
        "name": "테스트 주식",
        "quantity": 1,
        "limit_price_krw": 30_000,
        "invalidation_price_krw": 28_500,
        "order_value_krw": 30_000,
        "precheck_blockers": [],
        "real_submission_attempted": False,
        "pilot_version": "v1.0",
        "performance_qualified": False,
    }
    buy = database.create_live_order_intent(
        {**base_payload, "side": "BUY"},
        idempotency_key="l0-loss-buy",
        scope_hash="a" * 64,
    )
    database.record_live_fill(
        buy["id"],
        broker_order_no="1111111",
        quantity=1,
        price_krw=30_000,
        fee_krw=0,
        tax_krw=0,
        occurred_at=utc_now(),
        broker_fill_key="l0-loss-buy-fill",
    )
    sell_shadow = database.create_shadow_order(
        {
            **{key: shadow[key] for key in (
                "mission_id", "cycle_id", "trade_date", "next_session_date", "symbol",
                "name", "signal_close_krw", "invalidation_price_krw", "simulation_source",
            )},
            "side": "SELL",
            "quantity": 1,
            "limit_price_krw": 28_000,
            "broker_submitted": False,
            "trading_enabled": False,
        },
        idempotency_key="l0-loss-sell-shadow",
    )
    sell = database.create_live_order_intent(
        {
            **base_payload,
            "shadow_order_id": sell_shadow["id"],
            "side": "SELL",
            "limit_price_krw": 28_000,
            "order_value_krw": 28_000,
        },
        idempotency_key="l0-loss-sell",
        scope_hash="b" * 64,
    )
    database.record_live_fill(
        sell["id"],
        broker_order_no="2222222",
        quantity=1,
        price_krw=28_000,
        fee_krw=0,
        tax_krw=0,
        occurred_at=utc_now(),
        broker_fill_key="l0-loss-sell-fill",
    )
    sync_live_trade_outcomes(database, PILOT_MISSION_ID)
    next_shadow = database.create_shadow_order(
        {**shadow, "id": "ignored"},
        idempotency_key="l0-source-shadow-after-loss",
    )

    risk = pilot_risk_snapshot(database)
    intent = build_live_intent(
        database,
        _guardian(),
        shadow_order_id=next_shadow["id"],
        idempotency_key="l0-live-intent-after-loss",
        execution_mission_id=PILOT_MISSION_ID,
    )

    assert risk["daily_loss_reached"] is True
    assert risk["daily_defense_pnl_krw"] == -2_000
    assert any("당일 손실" in blocker for blocker in intent["precheck_blockers"])


def test_l0_freezes_15_day_reviews_and_v2_candidate_without_applying(tmp_path) -> None:
    database = Database(tmp_path / "l0-review.sqlite3")
    database.initialize()
    start = date(2026, 1, 2)
    for index in range(60):
        database.record_operating_day(
            mission_id=PILOT_MISSION_ID,
            trade_date=(start + timedelta(days=index)).isoformat(),
            mode="L0",
            reconciliation_status="PASS",
            risk_violations=0,
            duplicate_orders=0,
            decision_count=1,
            fill_count=1,
            net_pnl_krw=10,
        )

    first = generate_due_pilot_reviews(database)
    second = generate_due_pilot_reviews(database)
    status = pilot_status(database)

    assert [item["checkpoint_day"] for item in first] == [15, 30, 45, 60]
    assert len(second) == 4
    assert first[-1]["candidate_version"] == "v2.0"
    assert first[-1]["status"] == "PENDING_OWNER"
    assert status["v2_candidate_ready"] is True
    assert status["automatic_version_apply"] is False


def test_l0_api_requires_owner_token_and_exposes_pilot_status(
    tmp_path, monkeypatch
) -> None:
    owner_token = "owner-token-at-least-24-characters"
    monkeypatch.setenv("MONEYGUN_OWNER_API_TOKEN", owner_token)
    client = TestClient(create_app(tmp_path / "l0-api.sqlite3"))

    status = client.get("/v1/l0-pilot/status")
    unauthorized = client.post("/v1/l0-pilot/reviews/generate")
    transitioned = client.post(
        "/v1/execution/stage/transition",
        headers={"X-Owner-Approval-Token": owner_token},
        json={
            "mission_id": PILOT_MISSION_ID,
            "target_stage": "L0",
            "reason": "5만 원 손실 가능성 확인",
        },
    )

    assert status.status_code == 200
    assert status.json()["limits"]["capital_krw"] == 50_000
    assert unauthorized.status_code == 401
    assert transitioned.status_code == 200
    assert transitioned.json()["stage"] == "L0"


def test_l0_api_automation_requires_release_and_full_loss_confirmation(
    tmp_path, monkeypatch
) -> None:
    owner_token = "owner-token-at-least-24-characters"
    monkeypatch.setenv("MONEYGUN_OWNER_API_TOKEN", owner_token)
    monkeypatch.delenv("MONEYGUN_L0_AUTO_EXECUTION_RELEASE_ENABLED", raising=False)
    client = TestClient(create_app(tmp_path / "l0-auto-api.sqlite3"))
    headers = {"X-Owner-Approval-Token": owner_token}
    transitioned = client.post(
        "/v1/execution/stage/transition",
        headers=headers,
        json={
            "mission_id": PILOT_MISSION_ID,
            "target_stage": "L0",
            "reason": "5만 원 손실 가능성 확인",
        },
    )
    payload = {
        "mission_id": PILOT_MISSION_ID,
        "enabled": True,
        "reason": "L0-AUTO-v1 테스트 승인",
        "mandate_version": "L0-AUTO-v1",
        "confirm_l0_full_loss": True,
    }

    no_release = client.post("/v1/execution/automation", headers=headers, json=payload)
    monkeypatch.setenv("MONEYGUN_L0_AUTO_EXECUTION_RELEASE_ENABLED", "true")
    no_loss_confirmation = client.post(
        "/v1/execution/automation",
        headers=headers,
        json={**payload, "confirm_l0_full_loss": False},
    )

    assert transitioned.status_code == 200
    assert no_release.status_code == 409
    assert "릴리스 자격" in no_release.json()["detail"]
    assert no_loss_confirmation.status_code == 409
    assert "전액 손실 가능성" in no_loss_confirmation.json()["detail"]
