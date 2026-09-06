import hashlib
import json
import os
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .activation import activation_portfolio
from .close_auction_live import (
    live_scan_status,
    live_strategy_spec,
    run_close_auction_pilot_tick,
)
from .closing_auction import (
    MISSION_ID as CLOSE_AUCTION_MISSION_ID,
)
from .closing_auction import (
    run_close_auction_cycle,
    run_close_auction_validation,
)
from .closing_auction import (
    strategy_spec as closing_auction_spec,
)
from .committee import get_committee_program_report, run_committee_replay
from .daily_market_refresh import DailyMarketRefreshService
from .data_sources import (
    DataSourceError,
    OpenDartClient,
    load_market_bundle,
    market_import_directory,
)
from .desktop_live import DesktopLiveError, DesktopLiveProcessLease, recover_desktop_live
from .execution import (
    ExecutionError,
    ExecutionGuardian,
    approve_live_intent,
    build_live_intent,
    cancel_live_intent,
    reconcile_broker_orders,
    reconcile_managed_account,
    run_l0_automation_tick,
    run_l2_automation_tick,
    stage_gate_report,
    submit_live_intent,
    sync_official_quote,
    transition_stage,
)
from .kiwoom import KiwoomError, KiwoomReadOnlyClient
from .l0_exits import l0_exit_status, run_l0_exit_tick
from .l0_modes import daily_modes_status, mode_specs, run_daily_mode_pilot_tick
from .news import news_status, poll_official_news
from .official_research import build_official_kr_snapshot, configured_universe
from .opening_range import (
    MISSION_ID as OPENING_RANGE_MISSION_ID,
)
from .opening_range import (
    aggregate_kiwoom_realtime_events,
    build_fixture_session,
    build_opening_range_signal_plan,
    load_opening_range_archive,
    run_opening_range_oos_archive,
    run_opening_range_session,
)
from .opening_range import (
    strategy_spec as opening_range_spec,
)
from .opening_range import (
    validation_report as opening_range_validation,
)
from .opening_range_live import (
    live_opening_spec,
    opening_range_live_status,
    run_opening_range_pilot_tick,
)
from .operations import (
    OperationsError,
    create_database_backup,
    readiness_report,
    run_daily_shadow_operations,
    run_failure_drills,
    run_recovery_drill,
    verify_accounting_and_audit,
)
from .performance import (
    PerformanceError,
    autonomy_readiness,
    close_shadow_trade,
    deployment_readiness,
    performance_report,
    runtime_monitor,
    strategy_comparison,
)
from .pilot import (
    PILOT_MISSION_ID,
    generate_due_pilot_reviews,
    pilot_status,
    record_pilot_operating_day,
)
from .qualification import (
    load_qualified_market_bundle,
    qualification_spec,
    qualification_status,
)
from .research import build_fixture_snapshot, finalize_snapshot, run_committee_cycle
from .security import AuthenticationError, OidcBearerVerifier
from .shadow_orders import (
    prepare_close_auction_shadow_order,
    prepare_official_quote_shadow_order,
    prepare_shadow_order,
    settle_close_auction_shadow_order,
    settle_shadow_order_from_official_quote,
    simulate_shadow_order,
)
from .storage import Database
from .validation import run_walk_forward_validation


class AgentState(StrEnum):
    DONE = "DONE"
    WORKING = "WORKING"
    WAITING = "WAITING"
    CONFLICT = "CONFLICT"
    READY = "READY"


class AgentSummary(BaseModel):
    model_config = ConfigDict(frozen=True)
    code: str
    name: str
    role: str
    state: AgentState
    summary: str


class ModeProfileSummary(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str
    code: str
    version: int
    display_name: str
    max_positions: int
    max_single_position_bps: int
    max_trade_risk_bps: int
    max_open_risk_bps: int
    halt_drawdown_bps: int
    created_at: str


class MissionCreate(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    mode_code: str = Field(default="FOCUS", pattern=r"^[A-Z_]+$")
    seed_capital_krw: int = Field(ge=10_000, le=10_000_000)
    goal_capital_krw: int = Field(ge=10_000, le=1_000_000_000)

    @model_validator(mode="after")
    def goal_must_cover_seed(self) -> "MissionCreate":
        if self.goal_capital_krw < self.seed_capital_krw:
            raise ValueError("goal_capital_krw must be greater than or equal to seed_capital_krw")
        return self


class MissionSummary(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str
    name: str
    mode_code: str
    mode_version: int
    mode_display_name: str
    stage: str
    state: str
    seed_capital_krw: int
    goal_capital_krw: int
    available_krw: int
    exposed_krw: int
    reserved_profit_krw: int
    equity_krw: int
    halt_equity_krw: int
    trading_enabled: bool = False
    created_at: str


class LedgerPosting(BaseModel):
    model_config = ConfigDict(frozen=True)
    transaction_id: str
    kind: str
    correlation_id: str
    note: str
    occurred_at: str
    account_code: str
    amount_krw: int


class AuditEventSummary(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str
    actor: str
    action: str
    aggregate_type: str
    aggregate_id: str
    payload: dict[str, Any]
    previous_hash: str
    event_hash: str
    occurred_at: str


class CycleRunRequest(BaseModel):
    mission_id: str = "mission_focus_001"
    data_source: str = Field(default="FIXTURE_KR_REPRODUCIBLE", pattern=r"^[A-Z_]+$")
    snapshot_id: str | None = Field(default=None, pattern=r"^snap_[a-f0-9]{16}$")


class SnapshotImportRequest(BaseModel):
    file_name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\.json$")


class QualifiedSnapshotImportRequest(BaseModel):
    file_name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\.qualified\.json(?:\.gz)?$")


class ValidationRunRequest(BaseModel):
    mission_id: str = "mission_focus_001"
    snapshot_id: str | None = Field(default=None, pattern=r"^snap_[a-f0-9]{16}$")


class CloseAuctionRunRequest(BaseModel):
    mission_id: str = CLOSE_AUCTION_MISSION_ID
    snapshot_id: str | None = Field(default=None, pattern=r"^snap_[a-f0-9]{16}$")


class OpeningRangeRunRequest(BaseModel):
    mission_id: str = OPENING_RANGE_MISSION_ID
    snapshot_id: str | None = Field(default=None, pattern=r"^snap_[a-f0-9]{16}$")


class OpeningRangeArchiveImportRequest(BaseModel):
    file_name: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\.intraday\.json(?:\.gz)?$"
    )


class OpeningRangeSignalRequest(BaseModel):
    mission_id: str = OPENING_RANGE_MISSION_ID
    snapshot_id: str = Field(pattern=r"^snap_[a-f0-9]{16}$")


class OpeningRangeCaptureRequest(BaseModel):
    symbols: list[str] = Field(
        default_factory=lambda: ["005930", "000660", "069500"], min_length=1, max_length=20
    )
    max_messages: int = Field(default=100, ge=1, le=500)
    timeout_seconds: int = Field(default=10, ge=1, le=30)

    @model_validator(mode="after")
    def symbols_must_be_unique_domestic_codes(self) -> "OpeningRangeCaptureRequest":
        if len(set(self.symbols)) != len(self.symbols):
            raise ValueError("symbols must be unique")
        if any(not symbol.isdigit() or len(symbol) != 6 for symbol in self.symbols):
            raise ValueError("symbols must be six digit domestic stock codes")
        return self


class L0ModeTickRequest(BaseModel):
    mode_code: str = Field(pattern=r"^(FOCUS|BALANCED|LONG_TERM)$")
    force_scan: bool = False


class OfficialSnapshotSyncRequest(BaseModel):
    as_of_date: date = Field(default_factory=date.today)
    max_chart_pages: int = Field(default=4, ge=1, le=8)
    fundamental_start_year: int = Field(default=2016, ge=2010, le=2025)


class DailyOperationsRequest(BaseModel):
    mission_id: str = "mission_focus_001"
    force_time_gate: bool = False


class DailyMarketRefreshRequest(BaseModel):
    reference_date: date | None = None


class DartSearchRequest(BaseModel):
    corp_code: str = Field(pattern=r"^\d{8}$")
    begin_date: str = Field(pattern=r"^\d{8}$")
    end_date: str = Field(pattern=r"^\d{8}$")

    @model_validator(mode="after")
    def date_range_must_be_ordered(self) -> "DartSearchRequest":
        if self.end_date < self.begin_date:
            raise ValueError("end_date must be greater than or equal to begin_date")
        return self


class DartFinancialRequest(BaseModel):
    corp_code: str = Field(pattern=r"^\d{8}$")
    business_year: str = Field(pattern=r"^\d{4}$")
    report_code: str = Field(pattern=r"^(11011|11012|11013|11014)$")
    fs_div: str = Field(default="CFS", pattern=r"^(CFS|OFS)$")


class CommitteeReplayRequest(BaseModel):
    mission_id: str = "mission_focus_001"
    end_date: date = date(2026, 8, 31)
    target_days: int = Field(default=20, ge=20, le=20)


class ShadowOrderPrepareRequest(BaseModel):
    mission_id: str = "mission_focus_001"


class ShadowOrderSimulationRequest(BaseModel):
    scenario: str = Field(default="AUTO", pattern=r"^(AUTO|GAP_REDUCE|GAP_UP_CANCEL)$")


class StageTransitionRequest(BaseModel):
    mission_id: str = "mission_focus_001"
    target_stage: str = Field(pattern=r"^(L0|R1|L1|L2)$")
    reason: str = Field(min_length=4, max_length=200)


class ControlReasonRequest(BaseModel):
    mission_id: str = "mission_focus_001"
    reason: str = Field(min_length=4, max_length=200)


class AutomationRequest(BaseModel):
    mission_id: str = "mission_focus_001"
    enabled: bool
    reason: str = Field(min_length=4, max_length=200)
    mandate_version: str = Field(default="L0-AUTO-v1", pattern=r"^L0-AUTO-v1$")
    confirm_l0_full_loss: bool = False


class LiveIntentCreateRequest(BaseModel):
    shadow_order_id: str = Field(pattern=r"^shadow_[a-f0-9]{20}$")
    execution_mission_id: str | None = Field(
        default=None, pattern=r"^mission_[a-z0-9_]+$"
    )


class LiveIntentApprovalRequest(BaseModel):
    decision: str = Field(pattern=r"^(APPROVE|REJECT)$")
    scope_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class ShadowTradeCloseRequest(BaseModel):
    exit_reason: str = Field(
        default="HOLDING_COMPLETE",
        pattern=r"^(HOLDING_COMPLETE|INVALIDATION|KILL_SWITCH|MANUAL_RISK_EXIT)$",
    )


class ChangeRequestCreate(BaseModel):
    mission_id: str = "mission_focus_001"
    change_type: str = Field(pattern=r"^(STRATEGY|MANDATE|MODE|RISK_LIMIT)$")
    target_id: str = Field(min_length=2, max_length=120)
    proposal: dict[str, Any]
    reason: str = Field(min_length=8, max_length=500)


class ChangeRequestDecision(BaseModel):
    decision: str = Field(pattern=r"^(APPROVE|REJECT)$")
    scope_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


def get_database(request: Request) -> Database:
    return request.app.state.database


DatabaseDependency = Annotated[Database, Depends(get_database)]


def mission_response(mission: dict[str, Any]) -> MissionSummary:
    return MissionSummary(**mission, trading_enabled=False)


def create_app(database_path: str | Path | None = None) -> FastAPI:
    environment = os.getenv("MONEYGUN_ENV", "local").strip().lower()
    configured_origin = os.getenv("MONEYGUN_PUBLIC_ORIGIN", "").strip().rstrip("/")
    if environment == "desktop-live" and configured_origin not in {
        "",
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    }:
        raise RuntimeError("DESKTOP_LIVE의 공개 주소는 루프백만 허용합니다.")
    desktop_live_lease = (
        DesktopLiveProcessLease.acquire() if environment == "desktop-live" else None
    )
    application = FastAPI(
        title="MoneyGun Foundation API",
        version="0.10.0",
        description="Research, shadow, guarded L0 execution, and DESKTOP_LIVE operations.",
    )
    try:
        application.state.database = Database(database_path)
        application.state.database.initialize()
    except Exception:
        if desktop_live_lease is not None:
            desktop_live_lease.close()
        raise
    if desktop_live_lease is not None:
        application.state.desktop_live_lease = desktop_live_lease
    application.state.kiwoom = KiwoomReadOnlyClient()
    application.state.execution_guardian = ExecutionGuardian()
    application.state.daily_market_refresh = DailyMarketRefreshService(
        application.state.database
    )
    application.state.oidc = OidcBearerVerifier()
    market_import_directory().mkdir(parents=True, exist_ok=True)
    allowed_origins = (
        [configured_origin]
        if environment == "production" and configured_origin
        else ["http://127.0.0.1:5173", "http://localhost:5173"]
    )
    if environment not in {"production", "desktop-live"} and configured_origin:
        allowed_origins.append(configured_origin)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "Idempotency-Key",
            "X-Owner-Approval-Token",
        ],
    )

    if environment == "production":

        @application.middleware("http")
        async def require_production_identity(request: Request, call_next: Any) -> Any:
            if request.url.path == "/health":
                return await call_next(request)
            authorization = request.headers.get("Authorization", "")
            if not authorization.startswith("Bearer "):
                return JSONResponse(status_code=401, content={"detail": "로그인이 필요합니다."})
            try:
                request.state.identity = application.state.oidc.verify(authorization[7:])
            except AuthenticationError as error:
                return JSONResponse(status_code=401, content={"detail": str(error)})
            return await call_next(request)

    @application.get("/health")
    def health() -> dict[str, str | bool]:
        return {
            "status": "ok",
            "environment": environment,
            "database_backend": application.state.database.backend,
            "authentication": (
                "OIDC"
                if environment == "production"
                else "WINDOWS_LOCAL_OWNER"
                if environment == "desktop-live"
                else "LOCAL_ONLY"
            ),
            "trading_enabled": False,
        }

    @application.get("/v1/operations/readiness")
    def operations_readiness(request: Request, database: DatabaseDependency) -> dict[str, Any]:
        return readiness_report(database, request.app.state.kiwoom)

    @application.post("/v1/operations/daily-run")
    def daily_operations_run(
        payload: DailyOperationsRequest,
        request: Request,
        database: DatabaseDependency,
    ) -> dict[str, Any]:
        try:
            return run_daily_shadow_operations(
                database,
                request.app.state.kiwoom,
                mission_id=payload.mission_id,
                force=payload.force_time_gate,
            )
        except OperationsError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @application.get("/v1/operations/daily-run/latest")
    def latest_daily_operations_run(
        database: DatabaseDependency, mission_id: str = "mission_focus_001"
    ) -> dict[str, Any]:
        result = database.latest_operational_run(mission_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Operational run not found")
        return result

    @application.get("/v1/operations/notifications")
    def operations_notifications(
        database: DatabaseDependency,
        mission_id: str = "mission_focus_001",
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> list[dict[str, Any]]:
        return database.list_notifications(mission_id, limit)

    @application.get("/v1/news/status")
    def get_news_status(database: DatabaseDependency) -> dict[str, Any]:
        return news_status(database)

    @application.post("/v1/news/poll")
    def poll_news(database: DatabaseDependency) -> dict[str, Any]:
        return poll_official_news(database)

    @application.post("/v1/news/events/{event_id}/acknowledge")
    def acknowledge_news_event(
        event_id: str,
        request: Request,
        database: DatabaseDependency,
        owner_token: Annotated[str | None, Header(alias="X-Owner-Approval-Token")] = None,
    ) -> dict[str, Any]:
        require_owner(request, owner_token)
        try:
            return database.acknowledge_news_event(event_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="News event not found") from error

    @application.post("/v1/operations/notifications/{notification_id}/acknowledge")
    def acknowledge_operations_notification(
        notification_id: str, database: DatabaseDependency
    ) -> dict[str, Any]:
        try:
            return database.acknowledge_notification(notification_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Notification not found") from error

    @application.get("/v1/operations/integrity")
    def operations_integrity(database: DatabaseDependency) -> dict[str, Any]:
        return verify_accounting_and_audit(database)

    @application.post("/v1/operations/backups")
    def create_operations_backup(database: DatabaseDependency) -> dict[str, Any]:
        try:
            return create_database_backup(database)
        except OperationsError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @application.post("/v1/operations/recovery-drill")
    def execute_recovery_drill(database: DatabaseDependency) -> dict[str, Any]:
        try:
            return run_recovery_drill(database)
        except OperationsError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @application.post("/v1/operations/failure-drills")
    def execute_failure_drills(database: DatabaseDependency) -> list[dict[str, Any]]:
        return run_failure_drills(database)

    @application.get("/v1/operations/failure-drills")
    def list_failure_drills(database: DatabaseDependency) -> list[dict[str, Any]]:
        return database.list_drill_runs()

    @application.get("/v1/operations/strategy-evaluations")
    def strategy_evaluations(
        database: DatabaseDependency, mission_id: str = "mission_focus_001"
    ) -> list[dict[str, Any]]:
        return database.list_strategy_evaluations(mission_id)

    @application.get("/v1/performance/report")
    def get_performance_report(
        database: DatabaseDependency, mission_id: str = "mission_focus_001"
    ) -> dict[str, Any]:
        return performance_report(database, mission_id)

    @application.get("/v1/performance/runtime-monitor")
    def get_runtime_monitor(
        database: DatabaseDependency, mission_id: str = "mission_focus_001"
    ) -> dict[str, Any]:
        return runtime_monitor(database, mission_id)

    @application.post("/v1/performance/shadow/{order_id}/close")
    def close_shadow_performance(
        order_id: str,
        payload: ShadowTradeCloseRequest,
        database: DatabaseDependency,
    ) -> dict[str, Any]:
        try:
            return close_shadow_trade(database, order_id, exit_reason=payload.exit_reason)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Shadow order not found") from error
        except PerformanceError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @application.get("/v1/strategy-lab/comparison")
    def get_strategy_comparison(database: DatabaseDependency) -> dict[str, Any]:
        return strategy_comparison(database)

    @application.get("/v1/operations/deployment-readiness")
    def get_deployment_readiness(database: DatabaseDependency) -> dict[str, Any]:
        return deployment_readiness(database)

    @application.post("/v1/operations/desktop-live/recover")
    def recover_desktop_live_runtime(
        request: Request,
        database: DatabaseDependency,
        owner_token: Annotated[str | None, Header(alias="X-Owner-Approval-Token")] = None,
    ) -> dict[str, Any]:
        require_owner(request, owner_token)
        try:
            return recover_desktop_live(
                database,
                request.app.state.execution_guardian,
                request.app.state.kiwoom,
            )
        except (DesktopLiveError, KiwoomError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @application.get("/v1/operations/autonomy-readiness")
    def get_autonomy_readiness(
        database: DatabaseDependency, mission_id: str = "mission_focus_001"
    ) -> dict[str, Any]:
        return autonomy_readiness(database, mission_id)

    @application.get("/v1/change-requests")
    def get_change_requests(
        database: DatabaseDependency, mission_id: str = "mission_focus_001"
    ) -> list[dict[str, Any]]:
        return database.list_change_requests(mission_id)

    @application.post("/v1/change-requests", status_code=201)
    def propose_change_request(
        payload: ChangeRequestCreate, database: DatabaseDependency
    ) -> dict[str, Any]:
        return database.create_change_request(
            payload.mission_id,
            change_type=payload.change_type,
            target_id=payload.target_id,
            proposal=payload.proposal,
            reason=payload.reason,
        )

    @application.post("/v1/change-requests/{request_id}/decision")
    def decide_requested_change(
        request_id: str,
        payload: ChangeRequestDecision,
        request: Request,
        database: DatabaseDependency,
        owner_token: Annotated[str | None, Header(alias="X-Owner-Approval-Token")] = None,
    ) -> dict[str, Any]:
        require_owner(request, owner_token)
        try:
            return database.decide_change_request(
                request_id,
                decision=payload.decision,
                scope_hash=payload.scope_hash,
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Change request not found") from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @application.get("/v1/data-sources/status")
    def data_source_status() -> dict[str, Any]:
        dart = OpenDartClient()
        import_dir = market_import_directory()
        bundle_count = len(list(import_dir.glob("*.json"))) if import_dir.is_dir() else 0
        return {
            "fixture": {"configured": True, "purpose": "재현 가능한 P0 통합 검증"},
            "open_dart": {
                "configured": dart.configured,
                "purpose": "실제 공시 수집",
                "required_environment": "OPENDART_API_KEY",
            },
            "market_bundle_inbox": {
                "configured": import_dir.is_dir(),
                "purpose": "사용자가 적법하게 확보한 시점 고정 시장 번들 가져오기",
                "available_bundle_count": bundle_count,
            },
            "kiwoom_official_daily": {
                "configured": application.state.kiwoom.config.configured,
                "api_id": "ka10081",
                "purpose": "사용자 승인 내부 연구용 수정주가 일봉",
                "universe": configured_universe(),
            },
        }

    @application.get("/v1/data-qualification/spec")
    def data_qualification_spec() -> dict[str, Any]:
        return qualification_spec()

    @application.get("/v1/data-qualification/status")
    def data_qualification_status(
        request: Request, database: DatabaseDependency
    ) -> dict[str, Any]:
        latest = database.get_latest_snapshot_for_sources(
            (
                "KRX_AUTHORIZED_EXPORT",
                "LICENSED_VENDOR",
                "OFFICIAL_PUBLIC_DATA_API_AND_KIND_UI_EXPORT",
                "OFFICIAL_PUBLIC_DATA_API_KIND_AND_KIWOOM",
            )
        )
        if latest is None:
            latest = database.get_latest_snapshot("KIWOOM_OFFICIAL_REST")
        return {
            **qualification_status(None, latest),
            "daily_refresh": request.app.state.daily_market_refresh.status(),
        }

    @application.post("/v1/data-qualification/daily-refresh/run")
    def run_daily_market_refresh(
        payload: DailyMarketRefreshRequest, request: Request
    ) -> dict[str, Any]:
        return request.app.state.daily_market_refresh.trigger(
            reference_date=payload.reference_date,
            background=True,
        )

    @application.get("/v1/data-qualification/daily-refresh/status")
    def daily_market_refresh_status(request: Request) -> dict[str, Any]:
        return request.app.state.daily_market_refresh.status()

    @application.get("/v1/brokers/kiwoom/status")
    def kiwoom_status(request: Request) -> dict[str, Any]:
        return request.app.state.kiwoom.status()

    @application.post("/v1/brokers/kiwoom/account-snapshots/sync")
    def sync_kiwoom_account_snapshot(
        request: Request, database: DatabaseDependency
    ) -> dict[str, Any]:
        client: KiwoomReadOnlyClient = request.app.state.kiwoom
        if not client.config.configured:
            raise HTTPException(
                status_code=503,
                detail="키움 조회 전용 연결 키가 없습니다. 로컬 환경변수를 설정하세요.",
            )
        try:
            payload = client.fetch_domestic_account_evaluation()
        except KiwoomError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        return database.save_broker_account_snapshot(
            broker="KIWOOM",
            environment=client.config.environment,
            account_alias=client.config.account_alias,
            payload=payload,
        )

    @application.post("/v1/brokers/kiwoom/quotes/{symbol}/sync")
    def sync_kiwoom_quote(
        symbol: str, request: Request, database: DatabaseDependency
    ) -> dict[str, Any]:
        client: KiwoomReadOnlyClient = request.app.state.kiwoom
        try:
            return sync_official_quote(database, client, symbol)
        except KiwoomError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @application.post("/v1/reconciliations/run")
    def run_reconciliation(
        request: Request,
        database: DatabaseDependency,
        mission_id: str = "mission_focus_001",
    ) -> dict[str, Any]:
        try:
            result = reconcile_managed_account(
                database, request.app.state.kiwoom, mission_id=mission_id
            )
            if mission_id == PILOT_MISSION_ID:
                risk = record_pilot_operating_day(database)
                generate_due_pilot_reviews(database)
                return {**result, "pilot_risk": risk}
            return result
        except KiwoomError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @application.get("/v1/reconciliations/latest")
    def latest_reconciliation(
        database: DatabaseDependency, mission_id: str = "mission_focus_001"
    ) -> dict[str, Any]:
        result = database.get_latest_reconciliation(mission_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Reconciliation not found")
        return result

    @application.get("/v1/shadow/orders")
    def list_shadow_orders(
        database: DatabaseDependency,
        mission_id: str = "mission_focus_001",
    ) -> list[dict[str, Any]]:
        return database.list_shadow_orders(mission_id)

    @application.post("/v1/shadow/orders", status_code=status.HTTP_201_CREATED)
    def create_shadow_order(
        payload: ShadowOrderPrepareRequest,
        database: DatabaseDependency,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=120)
        ],
    ) -> dict[str, Any]:
        try:
            database.get_mission(payload.mission_id)
            return prepare_shadow_order(
                database,
                mission_id=payload.mission_id,
                idempotency_key=idempotency_key,
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Mission not found") from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @application.post("/v1/shadow/orders/{order_id}/simulate")
    def simulate_order(
        order_id: str,
        payload: ShadowOrderSimulationRequest,
        database: DatabaseDependency,
    ) -> dict[str, Any]:
        try:
            return simulate_shadow_order(database, order_id, scenario=payload.scenario)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Shadow order not found") from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @application.post("/v1/shadow/orders/from-official-quote", status_code=201)
    def create_official_shadow_order(
        payload: ShadowOrderPrepareRequest,
        database: DatabaseDependency,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=120)
        ],
    ) -> dict[str, Any]:
        try:
            return prepare_official_quote_shadow_order(
                database,
                mission_id=payload.mission_id,
                idempotency_key=idempotency_key,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @application.post("/v1/shadow/orders/close-auction", status_code=201)
    def create_close_auction_shadow_order(
        payload: ShadowOrderPrepareRequest,
        database: DatabaseDependency,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=120)
        ],
    ) -> dict[str, Any]:
        try:
            return prepare_close_auction_shadow_order(
                database,
                mission_id=payload.mission_id,
                idempotency_key=idempotency_key,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @application.post("/v1/shadow/orders/{order_id}/settle-close-auction")
    def settle_close_auction_order(order_id: str, database: DatabaseDependency) -> dict[str, Any]:
        try:
            return settle_close_auction_shadow_order(database, order_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Shadow order not found") from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @application.post("/v1/shadow/orders/{order_id}/settle-official")
    def settle_official_shadow_order(order_id: str, database: DatabaseDependency) -> dict[str, Any]:
        try:
            return settle_shadow_order_from_official_quote(database, order_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Shadow order not found") from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    def require_owner(request: Request, supplied: str | None) -> None:
        guardian: ExecutionGuardian = request.app.state.execution_guardian
        if not guardian.runtime.verify_owner(supplied):
            raise HTTPException(status_code=401, detail="소유자 승인 토큰이 올바르지 않습니다.")

    @application.get("/v1/l0-pilot/status")
    def get_l0_pilot_status(database: DatabaseDependency) -> dict[str, Any]:
        return pilot_status(database)

    @application.get("/v1/l0-pilot/exits/status")
    def get_l0_exit_status(database: DatabaseDependency) -> dict[str, Any]:
        return l0_exit_status(database)

    @application.post("/v1/l0-pilot/exits/tick")
    def l0_exit_tick(request: Request, database: DatabaseDependency) -> dict[str, Any]:
        try:
            return run_l0_exit_tick(
                database,
                request.app.state.kiwoom,
                request.app.state.execution_guardian,
            )
        except (ExecutionError, KiwoomError, ValueError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @application.get("/v1/l0-pilot/candidates")
    def get_l0_pilot_candidates(database: DatabaseDependency) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for source_mission_id in (
            "mission_focus_001",
            "mission_close_auction_001",
            "mission_opening_range_001",
            "mission_balanced_001",
            "mission_long_term_001",
        ):
            for order in database.list_shadow_orders(source_mission_id):
                if order["state"] != "READY" or not order["simulation_source"].startswith(
                    "KIWOOM_"
                ):
                    continue
                cycle = database.get_cycle(order["cycle_id"])
                candidates.append(
                    {
                        **order,
                        "source_mission_id": source_mission_id,
                        "strategy_id": cycle["strategy_id"],
                    }
                )
        return sorted(candidates, key=lambda item: item["created_at"], reverse=True)

    @application.post("/v1/l0-pilot/reviews/generate")
    def create_due_l0_pilot_reviews(
        request: Request,
        database: DatabaseDependency,
        owner_token: Annotated[str | None, Header(alias="X-Owner-Approval-Token")] = None,
    ) -> dict[str, Any]:
        require_owner(request, owner_token)
        reviews = generate_due_pilot_reviews(database)
        return {"reviews": reviews, "status": pilot_status(database)}

    @application.get("/v1/execution/status")
    def execution_status(
        request: Request,
        database: DatabaseDependency,
        mission_id: str = "mission_focus_001",
    ) -> dict[str, Any]:
        return {
            "guardian": request.app.state.execution_guardian.status(database),
            "control": database.get_execution_control(mission_id),
            "gates": stage_gate_report(database, mission_id),
            "latest_reconciliation": database.get_latest_reconciliation(mission_id),
            "incidents": database.list_incidents(mission_id),
        }

    @application.get("/v1/execution/activation-readiness")
    def execution_activation_readiness(
        request: Request, database: DatabaseDependency
    ) -> dict[str, Any]:
        return activation_portfolio(
            database,
            request.app.state.execution_guardian,
            request.app.state.kiwoom,
        )

    @application.post("/v1/execution/stage/transition")
    def request_stage_transition(
        payload: StageTransitionRequest,
        request: Request,
        database: DatabaseDependency,
        owner_token: Annotated[str | None, Header(alias="X-Owner-Approval-Token")] = None,
    ) -> dict[str, Any]:
        require_owner(request, owner_token)
        try:
            return transition_stage(
                database, payload.mission_id, payload.target_stage, reason=payload.reason
            )
        except ExecutionError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @application.post("/v1/execution/kill-switch/activate")
    def activate_kill_switch(
        payload: ControlReasonRequest, database: DatabaseDependency
    ) -> dict[str, Any]:
        return database.update_execution_control(
            payload.mission_id,
            actor="OWNER_UI",
            reason=payload.reason,
            kill_switch_active=True,
            automation_enabled=False,
        )

    @application.post("/v1/execution/kill-switch/resume")
    def resume_after_kill_switch(
        payload: ControlReasonRequest,
        request: Request,
        database: DatabaseDependency,
        owner_token: Annotated[str | None, Header(alias="X-Owner-Approval-Token")] = None,
    ) -> dict[str, Any]:
        require_owner(request, owner_token)
        latest = database.get_latest_reconciliation(payload.mission_id)
        if latest is None or latest["status"] != "PASS":
            raise HTTPException(status_code=409, detail="PASS 잔고 대사 후에만 재개할 수 있습니다.")
        return database.update_execution_control(
            payload.mission_id,
            actor="OWNER",
            reason=payload.reason,
            kill_switch_active=False,
        )

    @application.post("/v1/execution/automation")
    def set_execution_automation(
        payload: AutomationRequest,
        request: Request,
        database: DatabaseDependency,
        owner_token: Annotated[str | None, Header(alias="X-Owner-Approval-Token")] = None,
    ) -> dict[str, Any]:
        require_owner(request, owner_token)
        control = database.get_execution_control(payload.mission_id)
        if payload.enabled and control["stage"] == "L0":
            release_capable = os.getenv(
                "MONEYGUN_L0_AUTO_EXECUTION_RELEASE_ENABLED", ""
            ).strip().lower() in {"1", "true", "yes", "enabled"}
            if payload.mission_id != PILOT_MISSION_ID:
                raise HTTPException(
                    status_code=409, detail="L0 자동 주문은 5만 원 파일럿 원장에만 허용합니다."
                )
            if not release_capable:
                raise HTTPException(
                    status_code=409, detail="L0 자동 실행 릴리스 자격이 활성화되지 않았습니다."
                )
            if not payload.confirm_l0_full_loss:
                raise HTTPException(
                    status_code=409, detail="5만 원 전액 손실 가능성 확인이 필요합니다."
                )
            guardian_status = request.app.state.execution_guardian.status(database)
            if not guardian_status["configured"]:
                raise HTTPException(
                    status_code=409,
                    detail="Execution Guardian 실주문 관문이 준비되지 않았습니다.",
                )
            if control["kill_switch_active"]:
                raise HTTPException(status_code=409, detail="킬 스위치 해제 후 활성화하세요.")
            current_news = news_status(database)
            if current_news["state"] != "READY":
                raise HTTPException(
                    status_code=409, detail="김뉴스 공식 공시 감시가 READY가 아닙니다."
                )
        elif payload.enabled and control["stage"] != "L2":
            raise HTTPException(
                status_code=409,
                detail="자동 주문은 승인된 L0 또는 L2에서만 켭니다.",
            )
        return database.update_execution_control(
            payload.mission_id,
            actor="OWNER",
            reason=(
                f"{payload.mandate_version}: {payload.reason}"
                if payload.enabled and control["stage"] == "L0"
                else payload.reason
            ),
            automation_enabled=payload.enabled,
        )

    @application.get("/v1/execution/intents")
    def list_execution_intents(
        database: DatabaseDependency, mission_id: str = "mission_focus_001"
    ) -> list[dict[str, Any]]:
        return database.list_live_order_intents(mission_id)

    @application.post("/v1/execution/intents", status_code=201)
    def create_execution_intent(
        payload: LiveIntentCreateRequest,
        request: Request,
        database: DatabaseDependency,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=120)
        ],
    ) -> dict[str, Any]:
        try:
            return build_live_intent(
                database,
                request.app.state.execution_guardian,
                shadow_order_id=payload.shadow_order_id,
                idempotency_key=idempotency_key,
                execution_mission_id=payload.execution_mission_id,
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Shadow order not found") from error
        except ExecutionError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @application.post("/v1/execution/intents/{intent_id}/approval")
    def approve_execution_intent(
        intent_id: str,
        payload: LiveIntentApprovalRequest,
        request: Request,
        database: DatabaseDependency,
        owner_token: Annotated[str | None, Header(alias="X-Owner-Approval-Token")] = None,
    ) -> dict[str, Any]:
        require_owner(request, owner_token)
        try:
            return approve_live_intent(
                database,
                intent_id,
                decision=payload.decision,
                scope_hash=payload.scope_hash,
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Live intent not found") from error
        except (ExecutionError, ValueError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @application.post("/v1/execution/intents/{intent_id}/submit")
    def submit_execution_intent(
        intent_id: str,
        request: Request,
        database: DatabaseDependency,
        owner_token: Annotated[str | None, Header(alias="X-Owner-Approval-Token")] = None,
    ) -> dict[str, Any]:
        require_owner(request, owner_token)
        try:
            return submit_live_intent(database, request.app.state.execution_guardian, intent_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Live intent not found") from error
        except ExecutionError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @application.post("/v1/execution/intents/{intent_id}/cancel")
    def cancel_execution_intent(
        intent_id: str,
        request: Request,
        database: DatabaseDependency,
        owner_token: Annotated[str | None, Header(alias="X-Owner-Approval-Token")] = None,
    ) -> dict[str, Any]:
        require_owner(request, owner_token)
        try:
            return cancel_live_intent(database, request.app.state.execution_guardian, intent_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Live intent not found") from error
        except ExecutionError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @application.post("/v1/execution/reconcile-orders")
    def reconcile_execution_orders(
        request: Request,
        database: DatabaseDependency,
        mission_id: str = "mission_focus_001",
    ) -> dict[str, Any]:
        try:
            return reconcile_broker_orders(
                database, request.app.state.kiwoom, mission_id=mission_id
            )
        except KiwoomError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @application.post("/v1/execution/automation/tick")
    def run_automation_tick(
        request: Request,
        database: DatabaseDependency,
        mission_id: str = "mission_focus_001",
    ) -> dict[str, Any]:
        try:
            control = database.get_execution_control(mission_id)
            if control["stage"] == "L0":
                return run_l0_automation_tick(
                    database,
                    request.app.state.execution_guardian,
                    request.app.state.kiwoom,
                    mission_id=mission_id,
                    worker_id="api-l0-auto-execution",
                )
            return run_l2_automation_tick(
                database,
                request.app.state.execution_guardian,
                mission_id=mission_id,
                worker_id=f"api-tick-{uuid4().hex}",
            )
        except (ExecutionError, KiwoomError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @application.get("/v1/modes", response_model=list[ModeProfileSummary])
    def list_modes(database: DatabaseDependency) -> list[ModeProfileSummary]:
        return [ModeProfileSummary(**mode) for mode in database.list_modes()]

    @application.post(
        "/v1/missions", response_model=MissionSummary, status_code=status.HTTP_201_CREATED
    )
    def create_mission(
        payload: MissionCreate,
        database: DatabaseDependency,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=120)
        ],
    ) -> MissionSummary:
        try:
            mission = database.create_mission(
                name=payload.name,
                mode_code=payload.mode_code,
                seed_capital_krw=payload.seed_capital_krw,
                goal_capital_krw=payload.goal_capital_krw,
                idempotency_key=idempotency_key,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return mission_response(mission)

    @application.get("/v1/missions/{mission_id}", response_model=MissionSummary)
    def get_mission(mission_id: str, database: DatabaseDependency) -> MissionSummary:
        try:
            return mission_response(database.get_mission(mission_id))
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Mission not found") from error

    @application.get("/v1/missions/{mission_id}/ledger", response_model=list[LedgerPosting])
    def get_ledger(mission_id: str, database: DatabaseDependency) -> list[LedgerPosting]:
        try:
            database.get_mission(mission_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Mission not found") from error
        return [LedgerPosting(**posting) for posting in database.list_ledger(mission_id)]

    @application.get("/v1/audit-events", response_model=list[AuditEventSummary])
    def get_audit_events(
        database: DatabaseDependency,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> list[AuditEventSummary]:
        return [AuditEventSummary(**event) for event in database.list_audit_events(limit)]

    @application.post("/v1/research/cycles/run")
    def run_research_cycle(
        payload: CycleRunRequest,
        database: DatabaseDependency,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=120)
        ],
    ) -> dict[str, Any]:
        del idempotency_key  # the cycle ID is content-addressed and therefore idempotent
        try:
            mission = database.get_mission(payload.mission_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Mission not found") from error
        if payload.data_source == "FIXTURE_KR_REPRODUCIBLE":
            snapshot = database.save_snapshot(build_fixture_snapshot())
        elif payload.data_source == "IMPORTED_SNAPSHOT" and payload.snapshot_id:
            try:
                snapshot = database.get_snapshot(payload.snapshot_id)
            except KeyError as error:
                raise HTTPException(status_code=404, detail="Snapshot not found") from error
            if snapshot["source"] not in {
                "KRX_AUTHORIZED_EXPORT",
                "LICENSED_VENDOR",
                "OFFICIAL_PUBLIC_DATA_API_AND_KIND_UI_EXPORT",
                "OFFICIAL_PUBLIC_DATA_API_KIND_AND_KIWOOM",
                "KIWOOM_OFFICIAL_REST",
            }:
                raise HTTPException(status_code=422, detail="승인된 실데이터 스냅샷이 아닙니다.")
        else:
            raise HTTPException(
                status_code=422,
                detail="fixture 또는 snapshot_id가 있는 IMPORTED_SNAPSHOT만 실행할 수 있습니다.",
            )
        package = run_committee_cycle(mission, snapshot)
        return database.save_cycle(package)

    @application.post("/v1/research/snapshots/import")
    def import_research_snapshot(
        payload: SnapshotImportRequest,
        database: DatabaseDependency,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=120)
        ],
    ) -> dict[str, Any]:
        del idempotency_key  # the snapshot checksum is the idempotency boundary
        try:
            snapshot = finalize_snapshot(load_market_bundle(payload.file_name))
        except (DataSourceError, KeyError, TypeError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        if snapshot["quality"]["state"] != "PASS":
            raise HTTPException(status_code=422, detail={"quality": snapshot["quality"]})
        return database.save_snapshot(snapshot)

    @application.post("/v1/research/snapshots/qualified-import")
    def import_qualified_research_snapshot(
        payload: QualifiedSnapshotImportRequest,
        database: DatabaseDependency,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=120)
        ],
    ) -> dict[str, Any]:
        del idempotency_key  # bundle and snapshot checksums are the immutable boundary
        try:
            snapshot = finalize_snapshot(load_qualified_market_bundle(payload.file_name))
        except (DataSourceError, KeyError, TypeError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        if snapshot["quality"]["state"] != "PASS":
            raise HTTPException(status_code=422, detail={"quality": snapshot["quality"]})
        return database.save_snapshot(snapshot)

    @application.post("/v1/research/snapshots/kiwoom-sync")
    def sync_official_research_snapshot(
        payload: OfficialSnapshotSyncRequest,
        request: Request,
        database: DatabaseDependency,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=120)
        ],
    ) -> dict[str, Any]:
        del idempotency_key  # official contents are checksum-addressed
        if payload.as_of_date > date.today():
            raise HTTPException(status_code=422, detail="미래 기준일 스냅샷은 만들 수 없습니다.")
        try:
            snapshot = build_official_kr_snapshot(
                request.app.state.kiwoom,
                OpenDartClient(),
                as_of_date=payload.as_of_date,
                max_chart_pages=payload.max_chart_pages,
                fundamental_start_year=payload.fundamental_start_year,
            )
        except (DataSourceError, KiwoomError, KeyError, TypeError, ValueError) as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        if snapshot["quality"]["state"] != "PASS":
            raise HTTPException(status_code=422, detail={"quality": snapshot["quality"]})
        return database.save_snapshot(snapshot)

    @application.post("/v1/research/evidence/open-dart/search")
    def search_open_dart(payload: DartSearchRequest) -> dict[str, Any]:
        try:
            disclosures = OpenDartClient().search_disclosures(
                corp_code=payload.corp_code,
                begin_date=payload.begin_date,
                end_date=payload.end_date,
            )
        except DataSourceError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        return {
            "source": "OPENDART_OFFICIAL",
            "corp_code": payload.corp_code,
            "items": disclosures,
            "trading_enabled": False,
        }

    @application.post("/v1/research/evidence/open-dart/financial-statements")
    def fetch_open_dart_financial_statements(payload: DartFinancialRequest) -> dict[str, Any]:
        try:
            statements = OpenDartClient().financial_statements(
                corp_code=payload.corp_code,
                business_year=payload.business_year,
                report_code=payload.report_code,
                fs_div=payload.fs_div,
            )
        except DataSourceError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        return {
            "source": "OPENDART_OFFICIAL",
            "corp_code": payload.corp_code,
            "business_year": payload.business_year,
            "report_code": payload.report_code,
            "fs_div": payload.fs_div,
            "items": statements,
            "trading_enabled": False,
        }

    @application.post("/v1/research/validations/run")
    def run_research_validation(
        payload: ValidationRunRequest,
        database: DatabaseDependency,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=120)
        ],
    ) -> dict[str, Any]:
        del idempotency_key
        try:
            database.get_mission(payload.mission_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Mission not found") from error
        snapshot_id = payload.snapshot_id
        if snapshot_id is None:
            cycle = database.get_latest_cycle(payload.mission_id)
            if cycle is None:
                raise HTTPException(status_code=404, detail="Research cycle not found")
            snapshot_id = cycle["snapshot_id"]
        try:
            snapshot = database.get_snapshot(snapshot_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Snapshot not found") from error
        report = run_walk_forward_validation(snapshot)
        return database.save_validation(payload.mission_id, report)

    @application.get("/v1/strategies/close-auction/spec")
    def get_close_auction_spec() -> dict[str, Any]:
        return closing_auction_spec()

    @application.get("/v1/strategies/close-auction/live/status")
    def get_close_auction_live_status(database: DatabaseDependency) -> dict[str, Any]:
        return live_scan_status(database)

    @application.get("/v1/strategies/close-auction/live/spec")
    def get_close_auction_live_spec() -> dict[str, Any]:
        return live_strategy_spec()

    @application.get("/v1/strategies/l0-modes/specs")
    def get_l0_mode_specs() -> list[dict[str, Any]]:
        return mode_specs()

    @application.get("/v1/strategies/l0-modes/status")
    def get_l0_modes_status(database: DatabaseDependency) -> dict[str, Any]:
        return daily_modes_status(database)

    @application.post("/v1/strategies/l0-modes/pilot/tick")
    def l0_mode_pilot_tick(
        payload: L0ModeTickRequest,
        request: Request,
        database: DatabaseDependency,
    ) -> dict[str, Any]:
        try:
            return run_daily_mode_pilot_tick(
                database,
                request.app.state.kiwoom,
                request.app.state.execution_guardian,
                payload.mode_code,
                force_scan=payload.force_scan,
            )
        except (ExecutionError, KiwoomError, ValueError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @application.post("/v1/strategies/close-auction/pilot/tick")
    def close_auction_pilot_tick(request: Request, database: DatabaseDependency) -> dict[str, Any]:
        try:
            return run_close_auction_pilot_tick(
                database,
                request.app.state.kiwoom,
                request.app.state.execution_guardian,
            )
        except (ExecutionError, KiwoomError, ValueError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @application.post("/v1/strategies/close-auction/run")
    def run_close_auction_research(
        payload: CloseAuctionRunRequest,
        database: DatabaseDependency,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=120)
        ],
    ) -> dict[str, Any]:
        del idempotency_key
        try:
            mission = database.get_mission(payload.mission_id)
        except KeyError as error:
            raise HTTPException(
                status_code=404, detail="Closing-auction mission not found"
            ) from error
        if mission["mode_code"] != "CLOSE_AUCTION":
            raise HTTPException(status_code=422, detail="종가매매 모드 미션만 실행할 수 있습니다.")
        try:
            snapshot = (
                database.get_snapshot(payload.snapshot_id)
                if payload.snapshot_id
                else database.get_latest_snapshot("KIWOOM_OFFICIAL_REST")
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Snapshot not found") from error
        if snapshot is None:
            raise HTTPException(status_code=404, detail="키움 공식 시장 스냅샷이 없습니다.")
        if snapshot["source"] not in {
            "KRX_AUTHORIZED_EXPORT",
            "LICENSED_VENDOR",
            "OFFICIAL_PUBLIC_DATA_API_AND_KIND_UI_EXPORT",
            "OFFICIAL_PUBLIC_DATA_API_KIND_AND_KIWOOM",
            "KIWOOM_OFFICIAL_REST",
        }:
            raise HTTPException(status_code=422, detail="승인된 실데이터 스냅샷이 아닙니다.")
        validation = database.save_validation(
            payload.mission_id, run_close_auction_validation(snapshot)
        )
        cycle = database.save_cycle(run_close_auction_cycle(mission, snapshot, validation))
        return {
            "spec": closing_auction_spec(),
            "validation": validation,
            "cycle": cycle,
            "trading_enabled": False,
        }

    @application.get("/v1/strategies/close-auction/latest")
    def latest_close_auction_research(database: DatabaseDependency) -> dict[str, Any]:
        validation = database.get_latest_validation(CLOSE_AUCTION_MISSION_ID)
        cycle = database.get_latest_cycle(CLOSE_AUCTION_MISSION_ID)
        if validation is None or cycle is None:
            raise HTTPException(status_code=404, detail="Closing-auction research not found")
        return {
            "spec": closing_auction_spec(),
            "validation": validation,
            "cycle": cycle,
            "trading_enabled": False,
        }

    @application.get("/v1/strategies/opening-range/spec")
    def get_opening_range_spec() -> dict[str, Any]:
        return opening_range_spec()

    @application.get("/v1/strategies/opening-range/live/spec")
    def get_opening_range_live_spec() -> dict[str, Any]:
        return live_opening_spec()

    @application.get("/v1/strategies/opening-range/live/status")
    def get_opening_range_live_status(database: DatabaseDependency) -> dict[str, Any]:
        return opening_range_live_status(database)

    @application.post("/v1/strategies/opening-range/pilot/tick")
    async def opening_range_pilot_tick(
        request: Request, database: DatabaseDependency
    ) -> dict[str, Any]:
        try:
            return await run_opening_range_pilot_tick(
                database,
                request.app.state.kiwoom,
                request.app.state.execution_guardian,
            )
        except (ExecutionError, KiwoomError, ValueError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @application.post("/v1/strategies/opening-range/import-archive", status_code=201)
    def import_opening_range_archive(
        payload: OpeningRangeArchiveImportRequest,
        database: DatabaseDependency,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=120)
        ],
    ) -> dict[str, Any]:
        del idempotency_key  # archive checksum is the immutable idempotency boundary
        try:
            snapshot = load_opening_range_archive(
                payload.file_name, market_import_directory()
            )
        except (KeyError, TypeError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        saved = database.save_snapshot(snapshot)
        return {
            "snapshot_id": saved["id"],
            "source": saved["source"],
            "session_count": len(saved["sessions"]),
            "history": saved.get("history", {}),
            "quality": saved["quality"],
            "checksum": saved["checksum"],
            "trading_enabled": False,
        }

    @application.post("/v1/strategies/opening-range/run")
    def run_opening_range_research(
        payload: OpeningRangeRunRequest,
        database: DatabaseDependency,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=120)
        ],
    ) -> dict[str, Any]:
        del idempotency_key  # snapshot/cycle/order bodies are content-addressed
        try:
            mission = database.get_mission(payload.mission_id)
        except KeyError as error:
            raise HTTPException(
                status_code=404, detail="Opening-range mission not found"
            ) from error
        if mission["mode_code"] != "OPENING_RANGE":
            raise HTTPException(status_code=422, detail="장초 단타 모드 미션만 실행할 수 있습니다.")
        if payload.snapshot_id:
            try:
                snapshot = database.get_snapshot(payload.snapshot_id)
            except KeyError as error:
                raise HTTPException(
                    status_code=404, detail="Intraday snapshot not found"
                ) from error
            if snapshot["source"] not in {"LICENSED_INTRADAY_VENDOR", "KIWOOM_REALTIME_ARCHIVE"}:
                raise HTTPException(status_code=422, detail="승인된 정규화 장중 스냅샷이 아닙니다.")
        else:
            snapshot = database.save_snapshot(build_fixture_session())
        try:
            if snapshot.get("sessions"):
                cycle_package, validation_package = run_opening_range_oos_archive(
                    mission, snapshot
                )
            else:
                cycle_package = run_opening_range_session(mission, snapshot)
                validation_package = opening_range_validation(snapshot, cycle_package)
            cycle = database.save_cycle(cycle_package)
        except (KeyError, TypeError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        validation = database.save_validation(
            payload.mission_id, validation_package
        )
        shadow_orders: list[dict[str, Any]] = []
        if snapshot.get("sessions"):
            return {
                "spec": opening_range_spec(),
                "validation": validation,
                "cycle": cycle,
                "shadow_orders": [],
                "broker_submitted": False,
                "trading_enabled": False,
            }
        for trade in cycle["shadow_trades"]:
            if trade["state"] != "SHADOW_FILLED_AND_EXITED":
                continue
            order = database.create_shadow_order(
                {
                    "mission_id": payload.mission_id,
                    "cycle_id": cycle["id"],
                    "trade_date": snapshot["trade_date"],
                    "next_session_date": snapshot["trade_date"],
                    "symbol": trade["symbol"],
                    "name": trade["name"],
                    "side": "BUY",
                    "quantity": trade["quantity"],
                    "signal_close_krw": trade["limit_price_krw"],
                    "limit_price_krw": trade["limit_price_krw"],
                    "invalidation_price_krw": trade["stop_price_krw"],
                    "exit_price_krw": trade["exit_price_krw"],
                    "exit_reason": trade["exit_reason"],
                    "net_pnl_krw": trade["net_pnl_krw"],
                    "simulation_source": snapshot["source"],
                    "entry_style": "OPENING_RANGE_BREAKOUT_LIMIT",
                    "broker_submitted": False,
                    "trading_enabled": False,
                },
                idempotency_key=f"opening-range:{cycle['id']}:{trade['symbol']}:{trade['entry_at']}",
            )
            database.complete_shadow_fill(
                order["id"],
                quantity=int(trade["quantity"]),
                price_krw=int(trade["entry_price_krw"]),
            )
            shadow_orders.append(database.get_shadow_order(order["id"]))
        return {
            "spec": opening_range_spec(),
            "validation": validation,
            "cycle": cycle,
            "shadow_orders": shadow_orders,
            "broker_submitted": False,
            "trading_enabled": False,
        }

    @application.post("/v1/strategies/opening-range/signal", status_code=201)
    def prepare_opening_range_realtime_signal(
        payload: OpeningRangeSignalRequest,
        database: DatabaseDependency,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=120)
        ],
    ) -> dict[str, Any]:
        try:
            mission = database.get_mission(payload.mission_id)
            snapshot = database.get_snapshot(payload.snapshot_id)
        except KeyError as error:
            raise HTTPException(
                status_code=404, detail="미션 또는 장중 스냅샷이 없습니다."
            ) from error
        if mission["mode_code"] != "OPENING_RANGE":
            raise HTTPException(status_code=422, detail="장초 단타 모드 미션만 허용합니다.")
        control = database.get_execution_control(payload.mission_id)
        if control["stage"] not in {"R1", "L1", "L2"}:
            raise HTTPException(
                status_code=409, detail="R1 이상에서만 당일 장초 신호를 기록합니다."
            )
        try:
            cycle = database.save_cycle(build_opening_range_signal_plan(mission, snapshot))
        except (KeyError, TypeError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        decision = cycle["decision"]
        order = database.create_shadow_order(
            {
                "mission_id": payload.mission_id,
                "cycle_id": cycle["id"],
                "trade_date": snapshot["trade_date"],
                "next_session_date": snapshot["trade_date"],
                "symbol": decision["symbol"],
                "name": decision["name"],
                "side": "BUY",
                "quantity": decision["quantity"],
                "signal_close_krw": decision["limit_price_krw"],
                "limit_price_krw": decision["limit_price_krw"],
                "invalidation_price_krw": decision["invalidation_price_krw"],
                "simulation_source": "KIWOOM_OPENING_RANGE_REALTIME",
                "entry_style": decision["entry_style"],
                "broker_submitted": False,
                "trading_enabled": False,
            },
            idempotency_key=idempotency_key,
        )
        if control["stage"] == "R1":
            current_orders = [
                item
                for item in database.list_shadow_orders(payload.mission_id)
                if item["trade_date"] == snapshot["trade_date"]
                and item["simulation_source"] == "KIWOOM_OPENING_RANGE_REALTIME"
            ]
            reconciliation = database.get_latest_reconciliation(payload.mission_id)
            database.record_operating_day(
                mission_id=payload.mission_id,
                trade_date=snapshot["trade_date"],
                mode="R1",
                reconciliation_status=(
                    "PASS" if reconciliation and reconciliation["status"] == "PASS" else "FAIL"
                ),
                risk_violations=0,
                duplicate_orders=0,
                decision_count=len(current_orders),
                fill_count=0,
                net_pnl_krw=0,
            )
        return {
            "cycle": cycle,
            "shadow_order": order,
            "next": (
                "R1 그림자 체결·청산을 계속 기록합니다."
                if control["stage"] == "R1"
                else "15초 호가·30초 대사 후 Execution Guardian 사전점검으로 넘깁니다."
            ),
            "broker_submitted": False,
            "trading_enabled": False,
        }

    @application.get("/v1/strategies/opening-range/latest")
    def latest_opening_range_research(database: DatabaseDependency) -> dict[str, Any]:
        validation = database.get_latest_validation(OPENING_RANGE_MISSION_ID)
        cycle = database.get_latest_cycle(OPENING_RANGE_MISSION_ID)
        if validation is None or cycle is None:
            raise HTTPException(status_code=404, detail="Opening-range research not found")
        return {
            "spec": opening_range_spec(),
            "validation": validation,
            "cycle": cycle,
            "shadow_orders": database.list_shadow_orders(OPENING_RANGE_MISSION_ID),
            "broker_submitted": False,
            "trading_enabled": False,
        }

    @application.post("/v1/strategies/opening-range/realtime-capture")
    async def capture_opening_range_realtime(
        payload: OpeningRangeCaptureRequest,
        request: Request,
        database: DatabaseDependency,
    ) -> dict[str, Any]:
        try:
            events = await request.app.state.kiwoom.collect_realtime_market_events(
                payload.symbols,
                max_messages=payload.max_messages,
                timeout_seconds=payload.timeout_seconds,
            )
        except KiwoomError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        captured_at = datetime.now(UTC).isoformat(timespec="seconds")
        trade_date = datetime.now().astimezone().date().isoformat()
        minute_bars = aggregate_kiwoom_realtime_events(events, trade_date)
        archive: dict[str, Any] = {
            "as_of": captured_at,
            "trade_date": trade_date,
            "source": "KIWOOM_REALTIME_RAW_ARCHIVE",
            "quality": {
                "state": "HOLD",
                "issues": ["원시 REAL 프레임이며 1분봉·일별 유동성·지정상태 결합 전입니다."],
            },
            "symbols": payload.symbols,
            "events": events,
            "minute_bars": minute_bars,
            "history": {"trading_days": 0, "final_126_untouched": False},
            "benchmark": {"bars": []},
            "instruments": [],
            "broker_submitted": False,
            "trading_enabled": False,
        }
        material = json.dumps(archive, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        archive["checksum"] = hashlib.sha256(material.encode()).hexdigest()
        archive["id"] = f"snap_{archive['checksum'][:16]}"
        database.save_snapshot(archive)
        return {
            "archive_id": archive["id"],
            "event_count": len(events),
            "event_types": sorted({event["type"] for event in events}),
            "quality_state": "HOLD",
            "next": "분봉 집계와 시점고정 메타데이터 결합 전까지 전략 입력·주문은 차단됩니다.",
            "broker_submitted": False,
            "trading_enabled": False,
        }

    @application.get("/v1/research/validations/latest")
    def latest_research_validation(
        database: DatabaseDependency,
        mission_id: str = "mission_focus_001",
    ) -> dict[str, Any]:
        report = database.get_latest_validation(mission_id)
        if report is None:
            raise HTTPException(status_code=404, detail="Research validation not found")
        return report

    @application.get("/v1/research/validations/{validation_id}")
    def get_research_validation(validation_id: str, database: DatabaseDependency) -> dict[str, Any]:
        try:
            return database.get_validation(validation_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Research validation not found") from error

    @application.post("/v1/committee/programs/replay")
    def replay_committee_program(
        payload: CommitteeReplayRequest,
        database: DatabaseDependency,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=120)
        ],
    ) -> dict[str, Any]:
        del idempotency_key  # mission, mode, source and date range content-address the program
        try:
            return run_committee_replay(
                database,
                mission_id=payload.mission_id,
                end_date=payload.end_date,
                target_days=payload.target_days,
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Mission not found") from error

    @application.get("/v1/committee/programs/latest")
    def latest_committee_program(
        database: DatabaseDependency,
        mission_id: str = "mission_focus_001",
    ) -> dict[str, Any]:
        program = database.get_latest_committee_program(mission_id)
        if program is None:
            raise HTTPException(status_code=404, detail="Committee program not found")
        return get_committee_program_report(database, program["id"])

    @application.get("/v1/committee/programs/{program_id}")
    def get_committee_program(program_id: str, database: DatabaseDependency) -> dict[str, Any]:
        try:
            return get_committee_program_report(database, program_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Committee program not found") from error

    @application.get("/v1/research/cycles/latest")
    def latest_research_cycle(
        database: DatabaseDependency,
        mission_id: str = "mission_focus_001",
    ) -> dict[str, Any]:
        package = database.get_latest_cycle(mission_id)
        if package is None:
            raise HTTPException(status_code=404, detail="Research cycle not found")
        return package

    @application.get("/v1/research/cycles/{cycle_id}")
    def get_research_cycle(cycle_id: str, database: DatabaseDependency) -> dict[str, Any]:
        try:
            return database.get_cycle(cycle_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Research cycle not found") from error

    @application.get("/v1/research/snapshots/{snapshot_id}")
    def get_research_snapshot(snapshot_id: str, database: DatabaseDependency) -> dict[str, Any]:
        try:
            return database.get_snapshot(snapshot_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Snapshot not found") from error

    @application.get("/v1/prototype/cockpit", response_model=MissionSummary)
    def prototype_cockpit(database: DatabaseDependency) -> MissionSummary:
        return mission_response(database.get_mission("mission_focus_001"))

    @application.get("/v1/prototype/agents", response_model=list[AgentSummary])
    def prototype_agents() -> list[AgentSummary]:
        return [
            AgentSummary(
                code="DATA",
                name="김데이터",
                role="데이터 검증",
                state="DONE",
                summary="가격·거래량·공시 스냅샷을 점검합니다.",
            ),
            AgentSummary(
                code="NOVA",
                name="김뉴스",
                role="뉴스·거시 분석",
                state="DONE",
                summary="공식 출처의 촉매와 위험을 분류합니다.",
            ),
            AgentSummary(
                code="SERENITY",
                name="김산업",
                role="산업·공급망 분석",
                state="WORKING",
                summary="공급사부터 고객사까지 근거를 교차검증합니다.",
            ),
            AgentSummary(
                code="PULSE",
                name="김차트",
                role="차트·모멘텀 분석",
                state="DONE",
                summary="추세·거래량·과열 여부를 계산합니다.",
            ),
            AgentSummary(
                code="BULL",
                name="김찬성",
                role="상승 논리",
                state="DONE",
                summary="매수해야 할 근거와 상승 시나리오를 냅니다.",
            ),
            AgentSummary(
                code="BEAR",
                name="김반대",
                role="반대 논리",
                state="CONFLICT",
                summary="사지 말아야 할 이유와 실패 조건을 찾습니다.",
            ),
            AgentSummary(
                code="RISK",
                name="김안전",
                role="위험 판정",
                state="WAITING",
                summary="한도를 계산하고 위험한 주문을 거부합니다.",
            ),
            AgentSummary(
                code="ACE",
                name="김투자",
                role="포트폴리오 결정",
                state="WAITING",
                summary="찬반과 위험 판정을 종합해 최종 결정을 냅니다.",
            ),
            AgentSummary(
                code="OPS",
                name="김주문",
                role="주문·체결·대사",
                state="READY",
                summary="승인된 주문만 처리하며 현재 실제 주문은 차단됩니다.",
            ),
        ]

    return application


app = create_app()
