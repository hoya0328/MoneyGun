from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from moneygun_api import desktop_live
from moneygun_api.desktop_live import (
    DesktopLiveError,
    DesktopLiveProcessLease,
    DesktopLiveRuntime,
    recover_desktop_live,
)
from moneygun_api.execution import (
    ExecutionGuardian,
    ExecutionRuntimeConfig,
    KiwoomExecutionClient,
)
from moneygun_api.kiwoom import KiwoomConfig, KiwoomReadOnlyClient
from moneygun_api.main import create_app
from moneygun_api.operations import create_database_backup
from moneygun_api.storage import Database


def _desktop_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    values = {
        "MONEYGUN_ENV": "desktop-live",
        "MONEYGUN_DATABASE_BACKEND": "sqlite",
        "MONEYGUN_DATABASE_PATH": "data/moneygun.sqlite3",
        "MONEYGUN_PUBLIC_ORIGIN": "http://127.0.0.1:5173",
        "MONEYGUN_SECRET_BACKEND": "WINDOWS_DPAPI",
        "MONEYGUN_WINDOWS_SECRETS_LOADED": "true",
        "MONEYGUN_DESKTOP_AUTOSTART_CONFIGURED": "true",
        "MONEYGUN_SINGLE_PROCESS_LOCKED": "true",
        "MONEYGUN_SLEEP_DISABLED": "true",
        "MONEYGUN_CLOCK_SYNCHRONIZED": "true",
        "MONEYGUN_KIWOOM_REGISTERED_IP": "203.0.113.10",
        "MONEYGUN_OBSERVED_PUBLIC_IP": "203.0.113.10",
        "MONEYGUN_DESKTOP_LOGGING_CONFIGURED": "true",
        "MONEYGUN_BACKUP_ENCRYPTION_CONFIRMED": "true",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(desktop_live.platform, "system", lambda: "Windows")


def _recovery_evidence(database: Database) -> None:
    database.save_recovery_run(
        kind="SQLITE_ONLINE_BACKUP",
        state="PASS",
        artifact_path="data/backups/moneygun-test.dump",
        checksum="a" * 64,
        details={"test": True},
    )
    database.save_recovery_run(
        kind="RESTORE_DRILL",
        state="PASS",
        artifact_path="data/backups/moneygun-test.dump",
        checksum="a" * 64,
        details={"test": True},
    )


def test_desktop_live_readiness_requires_fresh_backup_and_restore(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _desktop_environment(monkeypatch)
    database = Database(tmp_path / "readiness.sqlite3")
    database.initialize()
    blocked = desktop_live.desktop_live_readiness(database)
    assert blocked["state"] == "DESKTOP_LIVE_SETUP_REQUIRED"
    assert blocked["checks"]["database_backup_fresh_24h"] is False
    assert blocked["checks"]["database_restore_drill_fresh_31d"] is False

    _recovery_evidence(database)
    ready = desktop_live.desktop_live_readiness(database)
    assert ready["state"] == "DESKTOP_LIVE_ELIGIBLE"
    assert all(ready["checks"].values())


def test_guardian_starts_recovery_required_and_only_arms_after_checks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _desktop_environment(monkeypatch)
    database = Database(tmp_path / "guardian.sqlite3")
    database.initialize()
    _recovery_evidence(database)
    runtime = DesktopLiveRuntime(enabled=True)
    guardian = ExecutionGuardian(
        KiwoomExecutionClient(
            KiwoomConfig("order-app", "order-secret", "production", "계좌")
        ),
        ExecutionRuntimeConfig(
            trading_enabled=True,
            release_approved=True,
            owner_api_token="owner-token-with-at-least-24-characters",
            capital_limit_krw=100_000,
        ),
        runtime,
    )
    blocked = guardian.status(database)
    assert blocked["configured"] is False
    assert blocked["desktop_live"]["state"] == "RECOVERY_REQUIRED"
    with pytest.raises(DesktopLiveError):
        runtime.require_armed()

    runtime.arm({"deployment": True, "reconciliation": True})
    ready = guardian.status(database)
    assert ready["configured"] is True
    assert ready["desktop_live"]["state"] == "ARMED"

    runtime.disarm("네트워크 변경")
    assert guardian.status(database)["configured"] is False


def test_desktop_live_rejects_non_loopback_origin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MONEYGUN_ENV", "desktop-live")
    monkeypatch.setenv("MONEYGUN_PUBLIC_ORIGIN", "http://0.0.0.0:5173")
    with pytest.raises(RuntimeError, match="루프백"):
        create_app(tmp_path / "unsafe-origin.sqlite3")


def test_desktop_live_process_mutex_rejects_a_second_api() -> None:
    first = DesktopLiveProcessLease.acquire()
    try:
        with pytest.raises(DesktopLiveError, match="already running"):
            DesktopLiveProcessLease.acquire()
    finally:
        first.close()


class _ReadTransport:
    def post(
        self, url: str, headers: dict[str, str], payload: dict[str, Any]
    ) -> dict[str, Any]:
        if url.endswith("/oauth2/token"):
            return {"return_code": 0, "token": "read-token"}
        if headers["api-id"] == "ka10075":
            return {"return_code": 0, "oso": []}
        if headers["api-id"] == "ka10076":
            return {"return_code": 0, "cntr": []}
        return {
            "return_code": 0,
            "prsm_dpst_aset_amt": "50000",
            "acnt_evlt_remn_indv_tot": [],
        }


def test_recovery_reconciles_before_arming_without_submitting_orders(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _desktop_environment(monkeypatch)
    database = Database(tmp_path / "recover.sqlite3")
    database.initialize()
    _recovery_evidence(database)
    runtime = DesktopLiveRuntime(enabled=True)
    guardian = ExecutionGuardian(
        KiwoomExecutionClient(
            KiwoomConfig("order-app", "order-secret", "production", "계좌")
        ),
        ExecutionRuntimeConfig(
            trading_enabled=False,
            release_approved=False,
            owner_api_token="owner-token-with-at-least-24-characters",
            capital_limit_krw=100_000,
        ),
        runtime,
    )
    read_client = KiwoomReadOnlyClient(
        KiwoomConfig("read-app", "read-secret", "production", "계좌"),
        _ReadTransport(),
    )

    result = recover_desktop_live(database, guardian, read_client)

    assert result["state"] == "ARMED"
    assert all(result["checks"].values())
    assert result["broker_orders_submitted"] == 0
    assert result["order_reconciliation"]["retry_submitted"] == 0


class _BackupDatabase:
    backend = "postgresql"
    path = "postgresql://moneygun:secret@localhost:5432/moneygun"

    def save_recovery_run(self, **payload: Any) -> dict[str, Any]:
        return {"id": "backup-test", **payload}


def test_postgres_backup_is_hashed_and_rotated_without_exposing_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from moneygun_api import operations

    def fake_tool(
        executable: str,
        arguments: list[str],
        *,
        environment: dict[str, str],
        timeout_seconds: int = 180,
    ) -> Any:
        assert executable == "pg_dump"
        assert environment["PGPASSWORD"] == "secret"
        target = Path(arguments[arguments.index("--file") + 1])
        target.write_bytes(b"postgres-custom-backup")
        return object()

    monkeypatch.setattr(operations, "_run_postgres_tool", fake_tool)
    monkeypatch.setenv("MONEYGUN_BACKUP_ENCRYPTION_CONFIRMED", "true")
    result = create_database_backup(_BackupDatabase(), tmp_path / "backups")  # type: ignore[arg-type]
    assert result["state"] == "PASS"
    assert len(result["checksum"]) == 64
    assert result["details"]["encryption_confirmed"] is True
    assert "secret" not in str(result)
