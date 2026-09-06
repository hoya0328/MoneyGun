from __future__ import annotations

import atexit
import ctypes
import os
import platform
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

if TYPE_CHECKING:
    from .execution import ExecutionGuardian
    from .kiwoom import KiwoomReadOnlyClient
    from .storage import Database

DESKTOP_LIVE_ENVIRONMENT = "desktop-live"
DESKTOP_LIVE_READY = "DESKTOP_LIVE_ELIGIBLE"
MANAGED_LIVE_READY = "DEPLOYMENT_READY"
ELIGIBLE_DEPLOYMENT_STATES = {DESKTOP_LIVE_READY, MANAGED_LIVE_READY}
RECOVERY_INTENT_STATES = {"SUBMITTING", "UNKNOWN", "ACKNOWLEDGED", "PARTIALLY_FILLED"}


class DesktopLiveError(RuntimeError):
    pass


def _enabled(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "enabled"}


def _is_loopback_origin(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}


def _fresh_recovery(
    database: Database | None,
    kind: str,
    *,
    max_age: timedelta,
) -> bool:
    if database is None:
        return False
    record = database.latest_recovery_run(kind)
    if not record or record["state"] != "PASS":
        return False
    created_at = datetime.fromisoformat(str(record["created_at"]))
    return datetime.now(UTC) - created_at <= max_age


def desktop_live_readiness(database: Database | None = None) -> dict[str, Any]:
    database_url = os.getenv("MONEYGUN_DATABASE_URL", "").strip()
    database_backend = os.getenv("MONEYGUN_DATABASE_BACKEND", "sqlite").strip().lower()
    database_path = os.getenv("MONEYGUN_DATABASE_PATH", "data/moneygun.sqlite3").strip()
    registered_ip = os.getenv("MONEYGUN_KIWOOM_REGISTERED_IP", "").strip()
    observed_ip = os.getenv("MONEYGUN_OBSERVED_PUBLIC_IP", "").strip()
    public_origin = os.getenv("MONEYGUN_PUBLIC_ORIGIN", "http://127.0.0.1:5173").strip()
    checks = {
        "windows_host": platform.system() == "Windows",
        "loopback_only_origin": _is_loopback_origin(public_origin),
        "local_database_configured": (
            database_backend == "sqlite" and bool(database_path)
        )
        or (
            database_backend == "postgresql"
            and database_url.startswith(("postgresql://", "postgres://"))
            and "change-me" not in database_url
        ),
        "windows_dpapi_secrets_loaded": (
            os.getenv("MONEYGUN_SECRET_BACKEND", "").strip().upper() == "WINDOWS_DPAPI"
            and _enabled("MONEYGUN_WINDOWS_SECRETS_LOADED")
        ),
        "scheduled_autostart_configured": _enabled("MONEYGUN_DESKTOP_AUTOSTART_CONFIGURED"),
        "single_process_lock_acquired": _enabled("MONEYGUN_SINGLE_PROCESS_LOCKED"),
        "sleep_disabled": _enabled("MONEYGUN_SLEEP_DISABLED"),
        "windows_clock_synchronized": _enabled("MONEYGUN_CLOCK_SYNCHRONIZED"),
        "registered_public_ip_matches": bool(
            registered_ip and observed_ip and registered_ip == observed_ip
        ),
        "local_logging_configured": _enabled("MONEYGUN_DESKTOP_LOGGING_CONFIGURED"),
        "backup_encryption_confirmed": _enabled("MONEYGUN_BACKUP_ENCRYPTION_CONFIRMED"),
        "database_backup_fresh_24h": _fresh_recovery(
            database,
            (
                "POSTGRES_CUSTOM_BACKUP"
                if database_backend == "postgresql"
                else "SQLITE_ONLINE_BACKUP"
            ),
            max_age=timedelta(hours=24),
        ),
        "database_restore_drill_fresh_31d": _fresh_recovery(
            database,
            "POSTGRES_RESTORE_DRILL" if database_backend == "postgresql" else "RESTORE_DRILL",
            max_age=timedelta(days=31),
        ),
    }
    labels = {
        "windows_host": "Windows 전용 호스트",
        "loopback_only_origin": "루프백 전용 Web/API",
        "local_database_configured": "로컬 단일 원장 DB",
        "windows_dpapi_secrets_loaded": "Windows DPAPI 비밀 저장소",
        "scheduled_autostart_configured": "Windows 자동 시작",
        "single_process_lock_acquired": "단일 Signal Guild 프로세스 잠금",
        "sleep_disabled": "절전·최대절전 차단",
        "windows_clock_synchronized": "Windows 시각 동기화",
        "registered_public_ip_matches": "키움 등록 공인 IP 일치",
        "local_logging_configured": "로컬 장애 로그",
        "backup_encryption_confirmed": "백업 저장소 암호화",
        "database_backup_fresh_24h": "24시간 이내 원장 백업",
        "database_restore_drill_fresh_31d": "31일 이내 격리 복원 훈련",
    }
    failed = [key for key, passed in checks.items() if not passed]
    return {
        "profile": "DESKTOP_LIVE",
        "state": DESKTOP_LIVE_READY if not failed else "DESKTOP_LIVE_SETUP_REQUIRED",
        "checks": checks,
        "check_labels": labels,
        "software_boundaries": {
            "loopback_only": True,
            "single_execution_guardian": True,
            "recovery_before_orders": True,
            "unknown_orders_never_retried": True,
            "windows_dpapi_secret_boundary": True,
            "database_backup_and_restore": True,
        },
        "next_action": (
            "모든 DESKTOP_LIVE 환경 관문이 준비됐습니다. 소유자 복구 대사를 실행하세요."
            if not failed
            else f"{labels[failed[0]]} 관문을 먼저 완료하세요."
        ),
        "trading_enabled": False,
    }


def deployment_is_eligible(report: dict[str, Any]) -> bool:
    return str(report.get("state")) in ELIGIBLE_DEPLOYMENT_STATES


class DesktopLiveProcessLease:
    def __init__(self) -> None:
        self._handle: int | None = None

    @classmethod
    def acquire(cls) -> DesktopLiveProcessLease:
        lease = cls()
        if platform.system() != "Windows":
            raise DesktopLiveError("DESKTOP_LIVE process lock is supported on Windows only.")
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        handle = kernel32.CreateMutexW(None, False, "Local\\MoneyGunDesktopLiveApi")
        if not handle:
            raise DesktopLiveError("DESKTOP_LIVE process mutex could not be created.")
        if kernel32.GetLastError() == 183:
            kernel32.CloseHandle(handle)
            raise DesktopLiveError(
                "Another Signal Guild DESKTOP_LIVE API process is already running."
            )
        lease._handle = int(handle)
        os.environ["MONEYGUN_SINGLE_PROCESS_LOCKED"] = "true"
        atexit.register(lease.close)
        return lease

    def close(self) -> None:
        if self._handle is None:
            return
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        kernel32.CloseHandle(ctypes.c_void_p(self._handle))
        self._handle = None


@dataclass
class DesktopLiveRuntime:
    enabled: bool = field(
        default_factory=lambda: os.getenv("MONEYGUN_ENV", "local").strip().lower()
        == DESKTOP_LIVE_ENVIRONMENT
    )
    state: str = field(init=False)
    recovered_at: str | None = None
    recovery_checks: dict[str, bool] = field(default_factory=dict)
    recovery_reason: str = "프로세스 기동 후 브로커 대사가 필요합니다."

    def __post_init__(self) -> None:
        self.state = "RECOVERY_REQUIRED" if self.enabled else "NOT_APPLICABLE"

    def require_armed(self) -> None:
        if self.enabled and self.state != "ARMED":
            raise DesktopLiveError("DESKTOP_LIVE 복구 대사가 완료되지 않아 주문을 차단했습니다.")

    def arm(self, checks: dict[str, bool]) -> None:
        if not self.enabled:
            raise DesktopLiveError("DESKTOP_LIVE 환경에서만 복구 상태를 활성화할 수 있습니다.")
        if not checks or not all(checks.values()):
            self.state = "RECOVERY_REQUIRED"
            self.recovery_checks = dict(checks)
            self.recovery_reason = "복구 검사 중 하나 이상이 실패했습니다."
            raise DesktopLiveError(self.recovery_reason)
        self.state = "ARMED"
        self.recovered_at = datetime.now(UTC).isoformat(timespec="seconds")
        self.recovery_checks = dict(checks)
        self.recovery_reason = "기동 후 브로커·원장 대사가 완료됐습니다."

    def disarm(self, reason: str) -> None:
        if not self.enabled:
            return
        self.state = "RECOVERY_REQUIRED"
        self.recovery_reason = reason

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "state": self.state,
            "recovered_at": self.recovered_at,
            "checks": self.recovery_checks,
            "reason": self.recovery_reason,
        }


def recover_desktop_live(
    database: Database,
    guardian: ExecutionGuardian,
    read_client: KiwoomReadOnlyClient,
) -> dict[str, Any]:
    from .execution import reconcile_broker_orders, reconcile_managed_account
    from .operations import verify_accounting_and_audit
    from .pilot import PILOT_MISSION_ID

    if not guardian.desktop_live.enabled:
        raise DesktopLiveError("MONEYGUN_ENV=desktop-live에서만 복구 대사를 실행할 수 있습니다.")
    deployment = desktop_live_readiness(database)
    integrity = verify_accounting_and_audit(database)
    order_reconciliation = reconcile_broker_orders(
        database, read_client, mission_id=PILOT_MISSION_ID
    )
    account_reconciliation = reconcile_managed_account(
        database, read_client, mission_id=PILOT_MISSION_ID
    )
    unresolved = [
        item["id"]
        for item in database.list_live_order_intents(PILOT_MISSION_ID)
        if item["state"] in RECOVERY_INTENT_STATES
    ]
    checks = {
        "deployment_eligible": deployment_is_eligible(deployment),
        "audit_and_ledger_pass": integrity["state"] == "PASS",
        "broker_order_reconciliation_complete": (
            int(order_reconciliation["unresolved_unknown"]) == 0 and not unresolved
        ),
        "account_reconciliation_pass": account_reconciliation["status"] == "PASS",
    }
    guardian.desktop_live.arm(checks)
    return {
        "state": guardian.desktop_live.state,
        "checks": checks,
        "deployment": deployment,
        "integrity": integrity,
        "order_reconciliation": order_reconciliation,
        "account_reconciliation": account_reconciliation,
        "unresolved_intent_ids": unresolved,
        "broker_orders_submitted": 0,
    }
