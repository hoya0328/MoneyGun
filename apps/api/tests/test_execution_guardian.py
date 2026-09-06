from datetime import date, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from moneygun_api.execution import (
    BrokerSubmissionUnknown,
    ExecutionError,
    ExecutionGuardian,
    ExecutionRuntimeConfig,
    KiwoomExecutionClient,
    approve_live_intent,
    build_live_intent,
    evaluate_auto_demotion,
    stage_gate_report,
    submit_live_intent,
    sync_official_quote,
    transition_stage,
)
from moneygun_api.kiwoom import KiwoomConfig
from moneygun_api.main import create_app
from moneygun_api.research import build_fixture_snapshot, run_committee_cycle
from moneygun_api.storage import Database, utc_now


class ReadOnlyFake:
    def fetch_domestic_quote(self, symbol: str) -> dict[str, Any]:
        return {
            "return_code": 0,
            "stk_cd": symbol,
            "stk_nm": "테스트 주식",
            "cur_prc": "+30100",
            "open_pric": "+30000",
            "high_pric": "+30500",
            "low_pric": "+29800",
        }


class SuccessfulOrderTransport:
    def post(
        self, url: str, headers: dict[str, str], payload: dict[str, Any]
    ) -> dict[str, Any]:
        if url.endswith("/oauth2/token"):
            return {"return_code": 0, "token": "execution-test-token"}
        assert headers["api-id"] == "kt10000"
        assert payload["trde_tp"] == "0"
        return {"return_code": 0, "ord_no": "1234567"}


class UnknownOrderTransport(SuccessfulOrderTransport):
    def post(
        self, url: str, headers: dict[str, str], payload: dict[str, Any]
    ) -> dict[str, Any]:
        if url.endswith("/oauth2/token"):
            return {"return_code": 0, "token": "execution-test-token"}
        raise BrokerSubmissionUnknown("테스트 타임아웃")


def _database_with_cycle(tmp_path) -> tuple[Database, dict[str, Any]]:
    database = Database(tmp_path / "execution.sqlite3")
    database.initialize()
    mission = database.get_mission("mission_focus_001")
    snapshot = database.save_snapshot(build_fixture_snapshot())
    cycle = database.save_cycle(run_committee_cycle(mission, snapshot))
    return database, cycle


def _prepare_l1_context(tmp_path) -> tuple[Database, dict[str, Any]]:
    database, cycle = _database_with_cycle(tmp_path)
    database.save_validation(
        "mission_focus_001",
        {
            "id": "validation_execution_eligible",
            "snapshot_id": cycle["snapshot_id"],
            "strategy_id": "FOCUS-MOMENTUM-KR-v1",
            "protocol_version": "TEST_POINT_IN_TIME",
            "status": "ELIGIBLE_FOR_R1_REVIEW",
            "promotion_eligible": True,
            "metrics": {},
            "failed_gates": [],
        },
    )
    database.update_execution_control(
        "mission_focus_001",
        actor="TEST",
        reason="L1 통합 테스트",
        stage="L1",
        kill_switch_active=False,
    )
    quote = database.save_broker_quote_snapshot(
        broker="KIWOOM",
        symbol="005930",
        observed_at=utc_now(),
        payload={
            "symbol": "005930",
            "name": "테스트 주식",
            "current_price_krw": 30_100,
            "open_price_krw": 30_000,
            "high_price_krw": 30_500,
            "low_price_krw": 29_800,
            "source": "KIWOOM_OFFICIAL_REST",
            "observed_at": utc_now(),
        },
    )
    account = database.save_broker_account_snapshot(
        broker="KIWOOM",
        environment="mock",
        account_alias="테스트",
        payload={"prsm_dpst_aset_amt": "100000", "acnt_evlt_remn_indv_tot": []},
    )
    database.save_reconciliation(
        mission_id="mission_focus_001",
        account_snapshot_id=account["id"],
        status="PASS",
        details={"capital_covered": True, "position_differences": []},
    )
    shadow = database.create_shadow_order(
        {
            "mission_id": "mission_focus_001",
            "cycle_id": cycle["id"],
            "trade_date": "2026-08-31",
            "next_session_date": "2026-09-01",
            "symbol": "005930",
            "name": "테스트 주식",
            "side": "BUY",
            "quantity": 1,
            "signal_close_krw": 30_000,
            "limit_price_krw": 30_300,
            "invalidation_price_krw": 27_000,
            "simulation_source": "KIWOOM_OFFICIAL_QUOTE",
            "quote_snapshot_id": quote["id"],
            "broker_submitted": False,
            "trading_enabled": False,
        },
        idempotency_key="official-shadow-test-001",
    )
    return database, shadow


def _guardian(transport: Any) -> ExecutionGuardian:
    broker = KiwoomExecutionClient(
        KiwoomConfig("app-key", "secret-key", "mock", "테스트"), transport
    )
    runtime = ExecutionRuntimeConfig(
        trading_enabled=True,
        release_approved=True,
        owner_api_token="owner-token-at-least-24-characters",
        capital_limit_krw=100_000,
    )
    return ExecutionGuardian(broker, runtime)


def test_official_quote_snapshot_is_normalized(tmp_path) -> None:
    database = Database(tmp_path / "quote.sqlite3")
    database.initialize()

    result = sync_official_quote(database, ReadOnlyFake(), "005930")  # type: ignore[arg-type]

    assert result["symbol"] == "005930"
    assert result["payload"]["current_price_krw"] == 30_100
    assert result["payload"]["source"] == "KIWOOM_OFFICIAL_REST"
    assert result["trading_enabled"] is False


def test_order_adapter_never_reuses_read_only_credentials(monkeypatch) -> None:
    monkeypatch.setenv("KIWOOM_APP_KEY", "read-only-app")
    monkeypatch.setenv("KIWOOM_SECRET_KEY", "read-only-secret")
    monkeypatch.delenv("KIWOOM_ORDER_APP_KEY", raising=False)
    monkeypatch.delenv("KIWOOM_ORDER_SECRET_KEY", raising=False)

    assert KiwoomExecutionClient().config.configured is False

    monkeypatch.setenv("KIWOOM_ORDER_APP_KEY", "order-app")
    monkeypatch.setenv("KIWOOM_ORDER_SECRET_KEY", "order-secret")
    assert KiwoomExecutionClient().config.configured is True


def test_production_guardian_requires_the_full_deployment_gate(monkeypatch) -> None:
    monkeypatch.setenv("MONEYGUN_DATABASE_BACKEND", "sqlite")
    broker = KiwoomExecutionClient(
        KiwoomConfig("order-app", "order-secret", "production", "운영계좌"),
        SuccessfulOrderTransport(),
    )
    guardian = ExecutionGuardian(
        broker,
        ExecutionRuntimeConfig(
            trading_enabled=True,
            release_approved=True,
            owner_api_token="owner-token-at-least-24-characters",
            capital_limit_krw=100_000,
        ),
    )

    status = guardian.status()

    assert status["configured"] is False
    assert any("배포 관문" in blocker for blocker in status["blockers"])


def test_l1_intent_requires_immutable_owner_approval_then_submits_once(tmp_path) -> None:
    database, shadow = _prepare_l1_context(tmp_path)
    guardian = _guardian(SuccessfulOrderTransport())

    intent = build_live_intent(
        database,
        guardian,
        shadow_order_id=shadow["id"],
        idempotency_key="live-intent-test-001",
    )
    assert intent["state"] == "AWAITING_APPROVAL"
    assert intent["precheck_blockers"] == []

    approved = approve_live_intent(
        database,
        intent["id"],
        decision="APPROVE",
        scope_hash=intent["scope_hash"],
    )
    assert approved["state"] == "READY"

    submitted = submit_live_intent(database, guardian, intent["id"])
    assert submitted["state"] == "ACKNOWLEDGED"
    assert submitted["events"][-1]["payload"]["broker_order_no"] == "1234567"
    with pytest.raises(ExecutionError):
        submit_live_intent(database, guardian, intent["id"])


def test_unknown_submission_is_never_retried(tmp_path) -> None:
    database, shadow = _prepare_l1_context(tmp_path)
    guardian = _guardian(UnknownOrderTransport())
    intent = build_live_intent(
        database,
        guardian,
        shadow_order_id=shadow["id"],
        idempotency_key="live-intent-unknown-001",
    )
    approve_live_intent(
        database,
        intent["id"],
        decision="APPROVE",
        scope_hash=intent["scope_hash"],
    )

    unknown = submit_live_intent(database, guardian, intent["id"])

    assert unknown["state"] == "UNKNOWN"
    assert unknown["events"][-1]["payload"]["retry_allowed"] is False
    with pytest.raises(ExecutionError, match="재전송"):
        submit_live_intent(database, guardian, intent["id"])


def test_l2_planned_loss_over_five_percent_is_rejected(tmp_path) -> None:
    database, original = _prepare_l1_context(tmp_path)
    database.update_execution_control(
        "mission_focus_001", actor="TEST", reason="L2 위험 검사", stage="L2"
    )
    risky = database.create_shadow_order(
        {
            **{
                key: original[key]
                for key in (
                    "mission_id",
                    "cycle_id",
                    "trade_date",
                    "next_session_date",
                    "symbol",
                    "name",
                    "side",
                    "quantity",
                    "signal_close_krw",
                    "limit_price_krw",
                    "simulation_source",
                )
            },
            "invalidation_price_krw": 20_000,
            "broker_submitted": False,
            "trading_enabled": False,
        },
        idempotency_key="official-shadow-risky-test-001",
    )

    intent = build_live_intent(
        database,
        _guardian(SuccessfulOrderTransport()),
        shadow_order_id=risky["id"],
        idempotency_key="live-intent-risky-test-001",
    )

    assert intent["state"] == "REJECTED"
    assert "거래당 계획 손실이 집중투자 미션 한도를 넘습니다." in intent[
        "precheck_blockers"
    ]


def test_l2_gate_and_critical_incident_auto_demotion(tmp_path) -> None:
    database = Database(tmp_path / "l2-gate.sqlite3")
    database.initialize()
    database.update_execution_control(
        "mission_focus_001", actor="TEST", reason="L1 증거 테스트", stage="L1"
    )
    first_day = date(2026, 1, 2)
    for index in range(60):
        database.record_operating_day(
            mission_id="mission_focus_001",
            trade_date=(first_day + timedelta(days=index)).isoformat(),
            mode="L1",
            reconciliation_status="PASS",
            risk_violations=0,
            duplicate_orders=0,
            decision_count=2,
            fill_count=2,
            net_pnl_krw=100,
        )

    report = stage_gate_report(database, "mission_focus_001")
    assert report["L1_TO_L2"]["eligible"] is True
    transition_stage(database, "mission_focus_001", "L2", reason="검증 증거 통과")
    database.update_execution_control(
        "mission_focus_001",
        actor="TEST",
        reason="자동화 테스트",
        automation_enabled=True,
    )
    database.create_incident(
        "mission_focus_001",
        severity="CRITICAL",
        code="BROKER_BALANCE_MISMATCH",
        detail="테스트 사고",
    )

    result = evaluate_auto_demotion(database, "mission_focus_001")

    assert result["demoted"] is True
    assert result["control"]["stage"] == "R1"
    assert result["control"]["kill_switch_active"] is True
    assert result["control"]["automation_enabled"] is False


def test_execution_api_is_fail_closed_without_owner_token(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "owner-auth.sqlite3"))

    status = client.get("/v1/execution/status")
    transition = client.post(
        "/v1/execution/stage/transition",
        json={"target_stage": "R1", "reason": "사용자 승인 테스트"},
    )
    kill = client.post(
        "/v1/execution/kill-switch/activate",
        json={"reason": "사용자 긴급 중지"},
    )

    assert status.status_code == 200
    assert status.json()["guardian"]["configured"] is False
    assert status.json()["control"]["stage"] == "R0"
    assert transition.status_code == 401
    assert kill.status_code == 200
    assert kill.json()["kill_switch_active"] is True
