from __future__ import annotations

from typing import Any

from .desktop_live import deployment_is_eligible
from .execution import MODE_EXECUTION_POLICIES, ExecutionGuardian, stage_gate_report
from .kiwoom import KiwoomReadOnlyClient
from .operations import verify_accounting_and_audit
from .performance import deployment_readiness
from .pilot import PILOT_CAPITAL_KRW, PILOT_MISSION_ID, pilot_status
from .storage import Database


def _step(
    code: str,
    label: str,
    passed: bool,
    *,
    owner_action: bool,
    detail: str,
) -> dict[str, Any]:
    return {
        "code": code,
        "label": label,
        "passed": passed,
        "owner_action": owner_action,
        "detail": detail,
    }


def mission_activation_readiness(
    database: Database,
    guardian: ExecutionGuardian,
    read_client: KiwoomReadOnlyClient,
    mission_id: str,
) -> dict[str, Any]:
    mission = database.get_mission(mission_id)
    control = database.get_execution_control(mission_id)
    validation = database.get_latest_validation(mission_id)
    gates = stage_gate_report(database, mission_id)
    policy = MODE_EXECUTION_POLICIES.get(mission["mode_code"])
    reconciliation = database.get_latest_reconciliation(mission_id)
    recovery = database.latest_recovery_run()
    integrity = verify_accounting_and_audit(database)
    deployment = deployment_readiness(database)
    guardian_status = guardian.status(database)
    if mission_id == PILOT_MISSION_ID:
        status = pilot_status(database)
        steps = [
            _step(
                "l0_policy",
                "L0 5만 원 실행 정책",
                int(mission["seed_capital_krw"]) == PILOT_CAPITAL_KRW,
                owner_action=False,
                detail="최대 5만 원·주문 4만5천 원·동시 1종목·하루 3회",
            ),
            _step(
                "experimental_acknowledgement",
                "성과 미검증 표시",
                status["strategy_performance_qualified"] is False,
                owner_action=False,
                detail="OOS 미통과 전략도 L0에서만 허용하며 전액 손실 가능성을 표시합니다.",
            ),
            _step(
                "l0_stage",
                "L0 파일럿 단계",
                control["stage"] == "L0",
                owner_action=True,
                detail=f"현재 {control['stage']} · 소유자 2차 토큰으로 한 번만 전환합니다.",
            ),
            _step(
                "kill_switch_clear",
                "킬 스위치 정상",
                not control["kill_switch_active"],
                owner_action=True,
                detail="손실·대사·중복·UNKNOWN 이상 시 즉시 신규 주문을 막습니다.",
            ),
            _step(
                "account_reconciliation",
                "키움 5만 원 계좌 대사",
                bool(
                    reconciliation
                    and reconciliation["status"] == "PASS"
                    and reconciliation["details"].get("capital_covered")
                ),
                owner_action=False,
                detail=(
                    "최근 5만 원 L0 대사 PASS"
                    if reconciliation and reconciliation["status"] == "PASS"
                    else "L0 미션으로 PASS 대사가 필요합니다."
                ),
            ),
            _step(
                "audit_backup",
                "감사·백업·복구",
                integrity["state"] == "PASS"
                and bool(recovery and recovery["state"] == "PASS"),
                owner_action=False,
                detail=(
                    f"감사 {integrity['state']} · 복구 "
                    f"{recovery['state'] if recovery else '없음'}"
                ),
            ),
            _step(
                "production_deployment",
                "운영 배포 환경",
                deployment_is_eligible(deployment),
                owner_action=True,
                detail=(
                    "DESKTOP_LIVE 자동 시작·DPAPI·SQLite 단일 원장·IP·복구·백업"
                    if deployment.get("profile") == "DESKTOP_LIVE"
                    else "PostgreSQL·OIDC·비밀관리·HTTPS·오류감시·관리형 백업"
                ),
            ),
            _step(
                "desktop_recovery",
                "기동 후 키움 복구 대사",
                not guardian.desktop_live.enabled or guardian.desktop_live.state == "ARMED",
                owner_action=True,
                detail=(
                    guardian.desktop_live.recovery_reason
                    if guardian.desktop_live.enabled
                    else "관리형 배포에는 적용되지 않습니다."
                ),
            ),
            _step(
                "read_credentials",
                "키움 운영 조회 연결",
                read_client.config.configured
                and read_client.config.environment == "production",
                owner_action=True,
                detail="조회 키는 주문 키와 분리합니다.",
            ),
            _step(
                "order_credentials",
                "키움 운영 주문 연결",
                guardian.broker.config.configured
                and guardian.broker.config.environment == "production",
                owner_action=True,
                detail="키움 실전 키를 격리된 KIWOOM_ORDER_* 경로에 명시적으로 연결해야 합니다.",
            ),
            _step(
                "owner_second_factor",
                "소유자 2차 승인 토큰",
                guardian.runtime.owner_auth_configured,
                owner_action=True,
                detail="24자 이상이며 브라우저 저장을 금지합니다.",
            ),
            _step(
                "release_approval",
                "L0 릴리스 점검 승인",
                guardian.runtime.release_approved,
                owner_action=True,
                detail="현재 소스와 백업·복구·주문 훈련이 일치해야 합니다.",
            ),
            _step(
                "owner_live_activation",
                "L0 실거래 최종 활성화",
                guardian.runtime.trading_enabled,
                owner_action=True,
                detail="마지막에만 KIWOOM_TRADING_ENABLED를 활성화합니다.",
            ),
        ]
        blocking = [item for item in steps if not item["passed"]]
        return {
            "mission_id": mission_id,
            "mission_name": mission["name"],
            "mode_code": mission["mode_code"],
            "strategy_id": "FOCUS·종가·장초 공식 신호 중 1개",
            "state": "L0_LIVE_READY" if not blocking else "L0_SETUP_REQUIRED",
            "can_submit_live_order": not blocking,
            "steps": steps,
            "blocking_count": len(blocking),
            "next_step": blocking[0] if blocking else None,
            "guardian": guardian_status,
            "control": control,
            "gates": gates,
            "pilot": status,
        }
    required_decisions = int(
        policy["shadow_decisions_required"] if policy is not None else 0
    )
    steps = [
        _step(
            "execution_policy",
            "모드별 실주문 정책",
            policy is not None,
            owner_action=False,
            detail=(
                f"{policy['strategy_id']} · 주문창 "
                f"{policy['order_window_kst'][0]}~{policy['order_window_kst'][1]}"
                if policy
                else "승인된 실행 정책이 없습니다."
            ),
        ),
        _step(
            "official_signal_contract",
            "공식 신호→가디언 계약",
            bool(policy and policy["official_shadow_sources"]),
            owner_action=False,
            detail=(
                "허용 출처 " + ", ".join(sorted(policy["official_shadow_sources"]))
                if policy
                else "허용된 공식 신호 출처가 없습니다."
            ),
        ),
        _step(
            "qualified_oos",
            "전략 OOS 자격",
            bool(validation and validation.get("promotion_eligible")),
            owner_action=False,
            detail=(
                "모든 사전등록 게이트를 통과했습니다."
                if validation and validation.get("promotion_eligible")
                else f"미통과 관문 {len((validation or {}).get('failed_gates', []))}개"
            ),
        ),
        _step(
            "r1_stage",
            "R1 공식 그림자 단계",
            control["stage"] in {"R1", "L1", "L2"},
            owner_action=True,
            detail=f"현재 {control['stage']} · OOS 통과 후 주인이 R1 승격을 승인합니다.",
        ),
        _step(
            "shadow_evidence",
            "공식 그림자 운용",
            gates["R1_TO_L1"]["eligible"],
            owner_action=False,
            detail=(
                f"{gates['R1_TO_L1']['metrics']['operating_days']}/60일 · "
                f"{gates['R1_TO_L1']['metrics']['decisions']}/{required_decisions}결정"
            ),
        ),
        _step(
            "account_reconciliation",
            "키움 계좌 대사",
            bool(reconciliation and reconciliation["status"] == "PASS"),
            owner_action=False,
            detail=(
                "최근 대사 PASS"
                if reconciliation and reconciliation["status"] == "PASS"
                else "PASS 대사가 필요합니다."
            ),
        ),
        _step(
            "audit_backup",
            "감사·백업·복구",
            integrity["state"] == "PASS" and bool(recovery and recovery["state"] == "PASS"),
            owner_action=False,
            detail=(
                f"감사 {integrity['state']} · 복구 "
                f"{recovery['state'] if recovery else '없음'}"
            ),
        ),
        _step(
            "production_deployment",
            "운영 배포 환경",
            deployment_is_eligible(deployment),
            owner_action=True,
                detail=(
                "DESKTOP_LIVE 자동 시작·DPAPI·SQLite 단일 원장·IP·복구·백업"
                if deployment.get("profile") == "DESKTOP_LIVE"
                else "PostgreSQL·OIDC·비밀관리·HTTPS·오류감시·관리형 백업"
            ),
        ),
        _step(
            "desktop_recovery",
            "기동 후 키움 복구 대사",
            not guardian.desktop_live.enabled or guardian.desktop_live.state == "ARMED",
            owner_action=True,
            detail=(
                guardian.desktop_live.recovery_reason
                if guardian.desktop_live.enabled
                else "관리형 배포에는 적용되지 않습니다."
            ),
        ),
        _step(
            "read_credentials",
            "키움 운영 조회 연결",
            read_client.config.configured and read_client.config.environment == "production",
            owner_action=True,
            detail="조회 키는 주문 키와 분리합니다.",
        ),
        _step(
            "order_credentials",
            "키움 운영 주문 연결",
            guardian.broker.config.configured
            and guardian.broker.config.environment == "production",
            owner_action=True,
            detail="키움 실전 키를 격리된 KIWOOM_ORDER_* 경로에 명시적으로 연결해야 합니다.",
        ),
        _step(
            "owner_second_factor",
            "소유자 2차 승인 토큰",
            guardian.runtime.owner_auth_configured,
            owner_action=True,
            detail="24자 이상이며 브라우저에 저장하지 않습니다.",
        ),
        _step(
            "release_approval",
            "릴리스 점검 승인",
            guardian.runtime.release_approved,
            owner_action=True,
            detail="특정 릴리스 후보 점검 통과 후에만 승인합니다.",
        ),
        _step(
            "owner_live_activation",
            "10만 원 실거래 최종 활성화",
            guardian.runtime.trading_enabled,
            owner_action=True,
            detail="마지막 단계에서만 KIWOOM_TRADING_ENABLED를 활성화합니다.",
        ),
        _step(
            "l1_stage",
            "L1 승인형 단계",
            control["stage"] in {"L1", "L2"},
            owner_action=True,
            detail=f"현재 {control['stage']} · 단계는 순서대로만 승격합니다.",
        ),
    ]
    blocking = [item for item in steps if not item["passed"]]
    if not bool(validation and validation.get("promotion_eligible")):
        state = "RESEARCH_ONLY"
    elif not gates["R1_TO_L1"]["eligible"]:
        state = "SHADOW_EVIDENCE_REQUIRED"
    elif blocking:
        state = "OWNER_AND_DEPLOYMENT_ACTION_REQUIRED"
    else:
        state = "L1_LIVE_READY"
    return {
        "mission_id": mission_id,
        "mission_name": mission["name"],
        "mode_code": mission["mode_code"],
        "strategy_id": policy["strategy_id"] if policy else None,
        "state": state,
        "can_submit_live_order": not blocking,
        "steps": steps,
        "blocking_count": len(blocking),
        "next_step": blocking[0] if blocking else None,
        "guardian": guardian_status,
        "control": control,
        "gates": gates,
    }


def activation_portfolio(
    database: Database,
    guardian: ExecutionGuardian,
    read_client: KiwoomReadOnlyClient,
) -> dict[str, Any]:
    mission_ids = [
        PILOT_MISSION_ID,
        "mission_focus_001",
        "mission_close_auction_001",
        "mission_opening_range_001",
        "mission_balanced_001",
        "mission_long_term_001",
    ]
    missions: list[dict[str, Any]] = []
    for mission_id in mission_ids:
        try:
            missions.append(
                mission_activation_readiness(database, guardian, read_client, mission_id)
            )
        except KeyError:
            continue
    return {
        "state": (
            "LIVE_READY"
            if any(item["can_submit_live_order"] for item in missions)
            else "BLOCKED"
        ),
        "missions": missions,
        "live_orders_submitted_by_check": 0,
    }
