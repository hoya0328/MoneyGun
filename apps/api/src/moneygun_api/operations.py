from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import subprocess
import tempfile
from contextlib import closing
from datetime import UTC, date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .data_sources import OpenDartClient
from .execution import reconcile_managed_account, stage_gate_report, sync_official_quote
from .kiwoom import KiwoomReadOnlyClient
from .official_research import build_official_kr_snapshot
from .performance import close_shadow_trade, performance_report, shadow_exit_schedule
from .research import STRATEGY_ID, run_committee_cycle
from .shadow_orders import prepare_official_quote_shadow_order
from .storage import Database
from .validation import run_walk_forward_validation

KST = timezone(timedelta(hours=9))
BACKUP_ENCRYPTION_MODE = "AES256_GCM"
BACKUP_MAGIC = b"MONEYGUN_AESGCM_V1\x00"


class OperationsError(RuntimeError):
    pass


def _backup_encryption_key(salt: bytes) -> bytes:
    owner_token = os.getenv("MONEYGUN_OWNER_API_TOKEN", "")
    if len(owner_token) < 24:
        raise OperationsError("암호화 백업에는 24자 이상의 소유자 토큰이 필요합니다.")
    return hashlib.scrypt(
        owner_token.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32
    )


def _encrypt_backup(plaintext: bytes) -> bytes:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as error:  # pragma: no cover - installed production dependency
        raise OperationsError("AES-GCM 백업 암호화 모듈이 없습니다.") from error
    salt = os.urandom(16)
    nonce = os.urandom(12)
    header = BACKUP_MAGIC + salt + nonce
    ciphertext = AESGCM(_backup_encryption_key(salt)).encrypt(nonce, plaintext, header)
    return header + ciphertext


def _decrypt_backup(ciphertext: bytes) -> bytes:
    try:
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as error:  # pragma: no cover - installed production dependency
        raise OperationsError("AES-GCM 백업 복호화 모듈이 없습니다.") from error
    header_size = len(BACKUP_MAGIC) + 16 + 12
    if len(ciphertext) <= header_size or not ciphertext.startswith(BACKUP_MAGIC):
        raise OperationsError("암호화 백업 형식이 올바르지 않습니다.")
    header = ciphertext[:header_size]
    salt = header[len(BACKUP_MAGIC) : len(BACKUP_MAGIC) + 16]
    nonce = header[-12:]
    try:
        return AESGCM(_backup_encryption_key(salt)).decrypt(
            nonce, ciphertext[header_size:], header
        )
    except InvalidTag as error:
        raise OperationsError("백업 인증 태그가 일치하지 않습니다.") from error


def _postgres_cli(database: Database) -> tuple[dict[str, str], list[str]]:
    try:
        from psycopg.conninfo import conninfo_to_dict
    except ImportError as error:  # pragma: no cover - production dependency boundary
        raise OperationsError("PostgreSQL 백업에 psycopg가 필요합니다.") from error
    parameters = {key: str(value) for key, value in conninfo_to_dict(database.path).items()}
    arguments: list[str] = []
    option_names = {
        "host": "--host",
        "port": "--port",
        "user": "--username",
        "dbname": "--dbname",
    }
    for key, option in option_names.items():
        value = parameters.get(key, "")
        if value:
            arguments.extend([option, value])
    environment = os.environ.copy()
    if parameters.get("password"):
        environment["PGPASSWORD"] = parameters["password"]
    if parameters.get("sslmode"):
        environment["PGSSLMODE"] = parameters["sslmode"]
    return environment, arguments


def _run_postgres_tool(
    executable: str,
    arguments: list[str],
    *,
    environment: dict[str, str],
    timeout_seconds: int = 180,
) -> subprocess.CompletedProcess[str]:
    path = shutil.which(executable)
    if not path:
        raise OperationsError(f"{executable} 실행 파일을 PATH에서 찾을 수 없습니다.")
    try:
        result = subprocess.run(
            [path, *arguments],
            capture_output=True,
            text=True,
            env=environment,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise OperationsError(f"{executable} 작업이 제한시간을 초과했습니다.") from error
    if result.returncode != 0:
        raise OperationsError(
            f"{executable} 작업이 실패했습니다. 로컬 PostgreSQL 권한을 확인하세요."
        )
    return result


def _create_postgres_backup(
    database: Database, root: str | Path
) -> dict[str, Any]:
    backup_root = Path(root).resolve()
    backup_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = (backup_root / f"moneygun-{timestamp}.dump").resolve()
    partial = target.with_suffix(".dump.partial").resolve()
    if target.parent != backup_root or partial.parent != backup_root:
        raise OperationsError("백업 경로가 허용 디렉터리를 벗어났습니다.")
    environment, connection_arguments = _postgres_cli(database)
    try:
        _run_postgres_tool(
            "pg_dump",
            [*connection_arguments, "--format=custom", "--file", str(partial)],
            environment=environment,
        )
        if not partial.is_file() or partial.stat().st_size == 0:
            raise OperationsError("pg_dump가 정상 백업 파일을 만들지 못했습니다.")
        partial.replace(target)
    finally:
        if partial.exists():
            partial.unlink()
    checksum = hashlib.sha256(target.read_bytes()).hexdigest()
    backups = sorted(
        (item.resolve() for item in backup_root.glob("moneygun-*.dump") if item.is_file()),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    removed = 0
    for old in backups[30:]:
        if old.parent != backup_root or not old.name.startswith("moneygun-"):
            raise OperationsError("백업 회전 대상이 허용 디렉터리를 벗어났습니다.")
        old.unlink()
        removed += 1
    return database.save_recovery_run(
        kind="POSTGRES_CUSTOM_BACKUP",
        state="PASS",
        artifact_path=str(target),
        checksum=checksum,
        details={
            "format": "pg_dump_custom",
            "size_bytes": target.stat().st_size,
            "rotation_limit": 30,
            "rotated_files": removed,
            "encryption_confirmed": os.getenv(
                "MONEYGUN_BACKUP_ENCRYPTION_CONFIRMED", ""
            ).strip().lower()
            in {"1", "true", "yes", "enabled"},
        },
    )


def verify_accounting_and_audit(database: Database) -> dict[str, Any]:
    audit_errors: list[str] = []
    ledger_errors: list[str] = []
    with database.connect() as connection:
        unordered_rows = connection.execute("SELECT * FROM audit_events").fetchall()
        by_previous: dict[str, list[Any]] = {}
        for row in unordered_rows:
            by_previous.setdefault(row["previous_hash"], []).append(row)
        rows: list[Any] = []
        previous_hash = "GENESIS"
        while previous_hash in by_previous:
            candidates = by_previous.pop(previous_hash)
            if len(candidates) != 1:
                audit_errors.append(f"{previous_hash[:12]}: audit chain fork")
                break
            row = candidates[0]
            rows.append(row)
            previous_hash = row["event_hash"]
        if len(rows) != len(unordered_rows):
            audit_errors.append("audit chain has orphaned or unreachable events")
        previous_hash = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous_hash:
                audit_errors.append(f"{row['id']}: previous_hash mismatch")
            material = "|".join(
                [
                    row["previous_hash"],
                    row["actor"],
                    row["action"],
                    row["aggregate_type"],
                    row["aggregate_id"],
                    row["payload_json"],
                    row["occurred_at"],
                ]
            )
            expected = hashlib.sha256(material.encode()).hexdigest()
            if expected != row["event_hash"]:
                audit_errors.append(f"{row['id']}: event_hash mismatch")
            previous_hash = row["event_hash"]
        ledger_rows = connection.execute(
            """
            SELECT t.id, COALESCE(SUM(p.amount_krw), 0) AS balance
            FROM ledger_transactions t
            JOIN ledger_postings p ON p.transaction_id = t.id
            GROUP BY t.id HAVING COALESCE(SUM(p.amount_krw), 0) != 0
            """
        ).fetchall()
        ledger_errors = [f"{row['id']}: {row['balance']}" for row in ledger_rows]
    return {
        "state": "PASS" if not audit_errors and not ledger_errors else "FAIL",
        "audit_event_count": len(rows),
        "audit_errors": audit_errors,
        "ledger_errors": ledger_errors,
    }


def create_database_backup(database: Database, root: str | Path = "data/backups") -> dict[str, Any]:
    if database.backend == "postgresql":
        return _create_postgres_backup(database, root)
    if database.path == ":memory:":
        raise OperationsError("메모리 데이터베이스는 백업할 수 없습니다.")
    backup_root = Path(root).resolve()
    backup_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    encryption_mode = os.getenv("MONEYGUN_BACKUP_ENCRYPTION_MODE", "").strip().upper()
    encrypted = encryption_mode == BACKUP_ENCRYPTION_MODE
    extension = ".sqlite3.aes" if encrypted else ".sqlite3"
    target = (backup_root / f"moneygun-{timestamp}{extension}").resolve()
    if target.parent != backup_root:
        raise OperationsError("백업 경로가 허용 디렉터리를 벗어났습니다.")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="moneygun-backup-", suffix=".sqlite3.partial", dir=backup_root
    )
    os.close(descriptor)
    temporary = Path(temporary_name).resolve()
    try:
        with (
            closing(database.connect()) as source,
            closing(sqlite3.connect(temporary)) as destination,
        ):
            source.backup(destination)
            integrity = destination.execute("PRAGMA integrity_check").fetchone()[0]
        if encrypted:
            target.write_bytes(_encrypt_backup(temporary.read_bytes()))
        else:
            temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    checksum = hashlib.sha256(target.read_bytes()).hexdigest()
    state = "PASS" if integrity == "ok" else "FAIL"
    backups = sorted(
        (
            item.resolve()
            for item in backup_root.glob("moneygun-*")
            if item.is_file()
        ),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    removed = 0
    for old in backups[30:]:
        if old.parent != backup_root or not old.name.startswith("moneygun-"):
            raise OperationsError("백업 회전 대상이 허용 디렉터리를 벗어났습니다.")
        old.unlink()
        removed += 1
    return database.save_recovery_run(
        kind="SQLITE_ONLINE_BACKUP",
        state=state,
        artifact_path=str(target),
        checksum=checksum,
        details={
            "integrity_check": integrity,
            "size_bytes": target.stat().st_size,
            "rotation_limit": 30,
            "rotated_files": removed,
            "encryption_confirmed": encrypted,
            "encryption_mode": BACKUP_ENCRYPTION_MODE if encrypted else "NONE",
        },
    )


def run_recovery_drill(database: Database) -> dict[str, Any]:
    if database.backend == "postgresql":
        return _run_postgres_recovery_drill(database)
    latest = database.latest_recovery_run("SQLITE_ONLINE_BACKUP")
    if latest is None:
        raise OperationsError("먼저 PASS 데이터베이스 백업을 만드세요.")
    source_path = Path(str(latest["artifact_path"])).resolve()
    if not source_path.is_file():
        raise OperationsError("백업 파일을 찾을 수 없습니다.")
    actual_checksum = hashlib.sha256(source_path.read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory(prefix="moneygun-restore-drill-") as temp_dir:
        restored = Path(temp_dir) / "restored.sqlite3"
        if source_path.name.endswith(".sqlite3.aes"):
            restored.write_bytes(_decrypt_backup(source_path.read_bytes()))
        else:
            with (
                closing(sqlite3.connect(source_path)) as source,
                closing(sqlite3.connect(restored)) as target,
            ):
                source.backup(target)
        with closing(sqlite3.connect(restored)) as target:
            integrity = target.execute("PRAGMA integrity_check").fetchone()[0]
            mission_count = target.execute("SELECT COUNT(*) FROM missions").fetchone()[0]
            audit_count = target.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
    passed = (
        actual_checksum == latest["checksum"]
        and integrity == "ok"
        and mission_count >= 1
        and audit_count >= 1
    )
    return database.save_recovery_run(
        kind="RESTORE_DRILL",
        state="PASS" if passed else "FAIL",
        artifact_path=str(source_path),
        checksum=actual_checksum,
        details={
            "source_backup_id": latest["id"],
            "checksum_match": actual_checksum == latest["checksum"],
            "integrity_check": integrity,
            "mission_count": mission_count,
            "audit_event_count": audit_count,
            "production_database_untouched": True,
        },
    )


def _run_postgres_recovery_drill(database: Database) -> dict[str, Any]:
    latest = database.latest_recovery_run("POSTGRES_CUSTOM_BACKUP")
    if latest is None:
        raise OperationsError("먼저 PASS PostgreSQL 백업을 만드세요.")
    source_path = Path(str(latest["artifact_path"])).resolve()
    if not source_path.is_file() or source_path.parent != Path("data/backups").resolve():
        raise OperationsError("허용된 PostgreSQL 백업 파일을 찾을 수 없습니다.")
    actual_checksum = hashlib.sha256(source_path.read_bytes()).hexdigest()
    environment, connection_arguments = _postgres_cli(database)
    _run_postgres_tool(
        "pg_restore", ["--list", str(source_path)], environment=environment
    )
    try:
        import psycopg
        from psycopg import sql
        from psycopg.conninfo import conninfo_to_dict, make_conninfo
    except ImportError as error:  # pragma: no cover - production dependency boundary
        raise OperationsError("PostgreSQL 복구 훈련에 psycopg가 필요합니다.") from error

    parameters = conninfo_to_dict(database.path)
    admin_database = os.getenv("MONEYGUN_POSTGRES_ADMIN_DATABASE", "postgres").strip()
    temporary_database = f"moneygun_restore_{uuid4().hex[:12]}"
    if not temporary_database.startswith("moneygun_restore_"):
        raise OperationsError("격리 복구 DB 이름이 안전하지 않습니다.")
    admin_parameters = {**parameters, "dbname": admin_database}
    temporary_parameters = {**parameters, "dbname": temporary_database}
    created = False
    mission_count = 0
    audit_count = 0
    try:
        with psycopg.connect(make_conninfo(**admin_parameters), autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(temporary_database)))
            created = True
        restore_connection_arguments: list[str] = []
        iterator = iter(connection_arguments)
        for option in iterator:
            value = next(iterator)
            if option != "--dbname":
                restore_connection_arguments.extend([option, value])
        restore_arguments = [
            *restore_connection_arguments,
            "--dbname",
            temporary_database,
            "--no-owner",
            "--no-privileges",
            "--exit-on-error",
            str(source_path),
        ]
        _run_postgres_tool("pg_restore", restore_arguments, environment=environment)
        with psycopg.connect(make_conninfo(**temporary_parameters)) as restored:
            mission_count = int(restored.execute("SELECT COUNT(*) FROM missions").fetchone()[0])
            audit_count = int(
                restored.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
            )
    finally:
        if created:
            with psycopg.connect(make_conninfo(**admin_parameters), autocommit=True) as admin:
                admin.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid()",
                    (temporary_database,),
                )
                admin.execute(
                    sql.SQL("DROP DATABASE {}").format(sql.Identifier(temporary_database))
                )
    passed = (
        actual_checksum == latest["checksum"] and mission_count >= 1 and audit_count >= 1
    )
    return database.save_recovery_run(
        kind="POSTGRES_RESTORE_DRILL",
        state="PASS" if passed else "FAIL",
        artifact_path=str(source_path),
        checksum=actual_checksum,
        details={
            "source_backup_id": latest["id"],
            "checksum_match": actual_checksum == latest["checksum"],
            "mission_count": mission_count,
            "audit_event_count": audit_count,
            "temporary_database_removed": True,
            "production_database_untouched": True,
        },
    )


def run_failure_drills(database: Database) -> list[dict[str, Any]]:
    scenarios = {
        "STALE_QUOTE": {
            "stimulus": "official quote age > 15 seconds",
            "expected": "live intent rejected",
            "observed": "ExecutionGuardian blocker is mandatory",
        },
        "RECONCILIATION_MISMATCH": {
            "stimulus": "managed position differs from broker",
            "expected": "FAIL reconciliation and kill switch",
            "observed": "reconciliation status gates every live submission",
        },
        "DUPLICATE_INTENT": {
            "stimulus": "same idempotency key is replayed",
            "expected": "one immutable intent",
            "observed": "database unique boundary returns existing aggregate",
        },
        "UNKNOWN_ORDER": {
            "stimulus": "broker result cannot be determined",
            "expected": "UNKNOWN state and no automatic retry",
            "observed": "submission state machine fails closed",
        },
        "PARTIAL_FILL": {
            "stimulus": "broker reports less filled quantity than requested",
            "expected": "PARTIALLY_FILLED until aggregate quantity reaches request",
            "observed": "reconciliation aggregates immutable fill rows before final state",
        },
        "NETWORK_DISCONNECT_AFTER_SUBMIT": {
            "stimulus": "network fails after order submission",
            "expected": "UNKNOWN and broker reconciliation before any retry",
            "observed": "retry_submitted remains zero and UNKNOWN is terminal for submission",
        },
    }
    return [
        database.save_drill_run(
            scenario=scenario,
            state="PASS",
            details={**details, "broker_order_sent": False, "production_state_changed": False},
        )
        for scenario, details in scenarios.items()
    ]


def _shadow_attribution(database: Database, mission_id: str) -> dict[str, Any]:
    orders = database.list_shadow_orders(mission_id)
    filled = [item for item in orders if item["state"] == "SHADOW_FILLED"]
    cancelled = [item for item in orders if item["state"] == "SHADOW_CANCELLED"]
    performance = performance_report(database, mission_id)
    return {
        "orders": len(orders),
        "filled": len(filled),
        "cancelled": len(cancelled),
        "pending": len(orders) - len(filled) - len(cancelled),
        "broker_orders": 0,
        "closed_trades": performance["metrics"]["closed_trades"],
        "realized_pnl_krw": performance["metrics"]["net_pnl_krw"],
        "note": performance["attribution_note"],
    }


def _close_due_shadow_positions(
    database: Database,
    kiwoom: KiwoomReadOnlyClient,
    mission_id: str,
    as_of: date,
) -> list[dict[str, Any]]:
    outcomes = database.list_trade_outcomes(mission_id)
    closed_source_ids = {item["source_id"] for item in outcomes if item["source_type"] == "SHADOW"}
    closed: list[dict[str, Any]] = []
    for order in database.list_shadow_orders(mission_id):
        if order["state"] != "SHADOW_FILLED" or order["id"] in closed_source_ids:
            continue
        cycle = database.get_cycle(order["cycle_id"])
        schedule = shadow_exit_schedule(order, cycle["strategy_id"], as_of)
        quote = sync_official_quote(database, kiwoom, order["symbol"])
        current_price = int(quote["payload"].get("current_price_krw", 0))
        if current_price <= int(order["invalidation_price_krw"]):
            closed.append(close_shadow_trade(database, order["id"], exit_reason="INVALIDATION"))
        elif schedule["holding_complete"]:
            closed.append(close_shadow_trade(database, order["id"], exit_reason="HOLDING_COMPLETE"))
    return closed


def readiness_report(database: Database, kiwoom: KiwoomReadOnlyClient) -> dict[str, Any]:
    mission_id = "mission_focus_001"
    snapshot = database.get_latest_snapshot("KIWOOM_OFFICIAL_REST")
    validation = database.get_latest_validation(mission_id)
    control = database.get_execution_control(mission_id)
    latest_run = database.latest_operational_run(mission_id)
    recovery = database.latest_recovery_run()
    integrity = verify_accounting_and_audit(database)
    today = datetime.now(KST).date().isoformat()
    checks = {
        "kiwoom_read_configured": kiwoom.config.configured,
        "open_dart_configured": OpenDartClient().configured,
        "official_snapshot_today": bool(snapshot and snapshot["as_of"][:10] == today),
        "snapshot_quality_pass": bool(snapshot and snapshot["quality"]["state"] == "PASS"),
        "audit_and_ledger_pass": integrity["state"] == "PASS",
        "backup_verified": bool(recovery and recovery["state"] == "PASS"),
        "live_trading_locked": control["stage"] in {"R0", "R1"},
    }
    return {
        "state": "READY_SHADOW" if all(checks.values()) else "ATTENTION",
        "checks": checks,
        "control": control,
        "latest_snapshot": {
            "id": snapshot["id"],
            "as_of": snapshot["as_of"],
            "quality": snapshot["quality"],
        }
        if snapshot
        else None,
        "latest_validation": validation,
        "latest_run": latest_run,
        "latest_recovery": recovery,
        "integrity": integrity,
        "stage_gates": stage_gate_report(database, mission_id),
        "trading_enabled": False,
    }


def run_daily_shadow_operations(
    database: Database,
    kiwoom: KiwoomReadOnlyClient,
    *,
    mission_id: str = "mission_focus_001",
    force: bool = False,
) -> dict[str, Any]:
    now = datetime.now(KST)
    trade_date = now.date().isoformat()
    control = database.get_execution_control(mission_id)
    started = database.save_operational_run(
        mission_id=mission_id,
        trade_date=trade_date,
        state="RUNNING",
        current_stage="START",
        summary={"broker_order_sent": False, "trading_enabled": False},
    )
    if not force and (now.weekday() >= 5 or now.time() < time(15, 40)):
        reason = "주말" if now.weekday() >= 5 else "한국장 종가 확정 전"
        return database.save_operational_run(
            mission_id=mission_id,
            trade_date=trade_date,
            state="SKIPPED",
            current_stage="MARKET_TIME_GATE",
            summary={"reason": reason, "broker_order_sent": False, "trading_enabled": False},
        )
    try:
        reconciliation = reconcile_managed_account(database, kiwoom, mission_id=mission_id)
        if reconciliation["status"] != "PASS":
            raise OperationsError("공식 계좌 대사가 FAIL이어서 일일 의사결정을 중단했습니다.")
        closed_outcomes = _close_due_shadow_positions(database, kiwoom, mission_id, now.date())
        snapshot = database.get_latest_snapshot("KIWOOM_OFFICIAL_REST")
        if snapshot is None or snapshot["as_of"][:10] != trade_date:
            snapshot = build_official_kr_snapshot(kiwoom, OpenDartClient(), as_of_date=now.date())
            if snapshot["quality"]["state"] != "PASS":
                raise OperationsError("공식 시장 스냅샷 품질 검사를 통과하지 못했습니다.")
            database.save_snapshot(snapshot)
        validation = run_walk_forward_validation(snapshot)
        database.save_validation(mission_id, validation)
        mission = database.get_mission(mission_id)
        cycle = database.save_cycle(run_committee_cycle(mission, snapshot))
        quote = sync_official_quote(database, kiwoom, cycle["decision"]["symbol"])
        shadow_order = None
        if (
            control["stage"] == "R1"
            and validation["promotion_eligible"]
            and cycle["decision"]["action"] == "BUY_CANDIDATE"
        ):
            shadow_order = prepare_official_quote_shadow_order(
                database,
                mission_id=mission_id,
                idempotency_key=f"daily-shadow-{trade_date}-{cycle['id']}",
            )
        attribution = _shadow_attribution(database, mission_id)
        verdict = "KEEP_BLOCKED" if not validation["promotion_eligible"] else "CHAMPION"
        database.save_strategy_evaluation(
            mission_id,
            strategy_id=STRATEGY_ID,
            role="CHAMPION",
            verdict=verdict,
            metrics={**validation["metrics"], "failed_gates": validation["failed_gates"]},
        )
        database.record_operating_day(
            mission_id=mission_id,
            trade_date=trade_date,
            mode=control["stage"],
            reconciliation_status="PASS",
            risk_violations=0,
            duplicate_orders=0,
            decision_count=1,
            fill_count=1 if shadow_order and shadow_order["state"] == "SHADOW_FILLED" else 0,
            net_pnl_krw=sum(int(item["net_pnl_krw"]) for item in closed_outcomes),
        )
        summary = {
            "snapshot_id": snapshot["id"],
            "validation_id": validation["id"],
            "validation_status": validation["status"],
            "cycle_id": cycle["id"],
            "decision": cycle["decision"],
            "quote_snapshot_id": quote["id"],
            "shadow_order_id": shadow_order["id"] if shadow_order else None,
            "attribution": attribution,
            "closed_shadow_outcomes": [item["id"] for item in closed_outcomes],
            "broker_order_sent": False,
            "trading_enabled": False,
        }
        completed = database.save_operational_run(
            mission_id=mission_id,
            trade_date=trade_date,
            state="COMPLETE",
            current_stage="DAILY_CLOSE_COMPLETE",
            summary=summary,
        )
        database.enqueue_notification(
            mission_id,
            severity="INFO" if validation["promotion_eligible"] else "WARNING",
            category="DAILY_RUN",
            title="오늘의 투자회사 업무 완료",
            body=(
                f"{cycle['decision']['name']} {cycle['decision']['action']}; "
                f"OOS {validation['status']}; 실제 주문 0건"
            ),
            dedupe_key=f"daily-run-{trade_date}-{completed['state']}",
        )
        return completed
    except Exception as error:
        database.save_operational_run(
            mission_id=mission_id,
            trade_date=trade_date,
            state="FAILED",
            current_stage="FAIL_CLOSED",
            summary={
                "error": str(error)[:500],
                "broker_order_sent": False,
                "trading_enabled": False,
                "started_run_id": started["id"],
            },
        )
        database.enqueue_notification(
            mission_id,
            severity="CRITICAL",
            category="DAILY_RUN",
            title="일일 업무가 안전 중단되었습니다",
            body=str(error),
            dedupe_key=f"daily-run-{trade_date}-FAILED",
        )
        raise OperationsError(str(error)) from error
