from pathlib import Path

from moneygun_api.operations import (
    create_database_backup,
    run_failure_drills,
    run_recovery_drill,
    verify_accounting_and_audit,
)
from moneygun_api.storage import Database


def test_audit_ledger_backup_and_restore_drill(tmp_path) -> None:
    database = Database(tmp_path / "moneygun.sqlite3")
    database.initialize()

    integrity = verify_accounting_and_audit(database)
    backup = create_database_backup(database, tmp_path / "backups")
    restore = run_recovery_drill(database)

    assert integrity["state"] == "PASS"
    assert backup["state"] == "PASS"
    assert backup["checksum"]
    assert restore["state"] == "PASS"
    assert restore["details"]["production_database_untouched"] is True


def test_desktop_backup_is_encrypted_and_restorable(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MONEYGUN_BACKUP_ENCRYPTION_MODE", "AES256_GCM")
    monkeypatch.setenv(
        "MONEYGUN_OWNER_API_TOKEN", "owner-token-with-at-least-24-characters"
    )
    database = Database(tmp_path / "encrypted.sqlite3")
    database.initialize()

    backup = create_database_backup(database, tmp_path / "backups")
    artifact = Path(backup["artifact_path"])

    assert backup["state"] == "PASS"
    assert backup["details"]["encryption_confirmed"] is True
    assert backup["details"]["encryption_mode"] == "AES256_GCM"
    assert artifact.name.endswith(".sqlite3.aes")
    assert not artifact.read_bytes().startswith(b"SQLite format 3")
    assert run_recovery_drill(database)["state"] == "PASS"


def test_notification_deduplication_and_acknowledgement(tmp_path) -> None:
    database = Database(tmp_path / "notice.sqlite3")
    database.initialize()
    kwargs = {
        "severity": "INFO",
        "category": "DAILY_RUN",
        "title": "완료",
        "body": "실제 주문 0건",
        "dedupe_key": "daily-2026-08-31",
    }

    first = database.enqueue_notification("mission_focus_001", **kwargs)
    second = database.enqueue_notification("mission_focus_001", **kwargs)
    acknowledged = database.acknowledge_notification(first["id"])

    assert first["id"] == second["id"]
    assert len(database.list_notifications("mission_focus_001")) == 1
    assert acknowledged["acknowledged_at"] is not None


def test_operating_day_rollup_is_idempotent_and_fail_dominates(tmp_path) -> None:
    database = Database(tmp_path / "rollup.sqlite3")
    database.initialize()
    base = {
        "mission_id": "mission_focus_001",
        "trade_date": "2026-08-31",
        "mode": "R1",
        "duplicate_orders": 0,
        "fill_count": 0,
        "net_pnl_krw": 0,
    }
    database.record_operating_day(
        **base, reconciliation_status="PASS", risk_violations=0, decision_count=0
    )
    database.record_operating_day(
        **base, reconciliation_status="FAIL", risk_violations=1, decision_count=1
    )

    metrics = database.operating_metrics("mission_focus_001", "R1")
    assert metrics["operating_days"] == 1
    assert metrics["decisions"] == 1
    assert metrics["risk_violations"] == 1
    assert metrics["reconciliation_failures"] == 1


def test_failure_drills_never_send_broker_orders(tmp_path) -> None:
    database = Database(tmp_path / "drills.sqlite3")
    database.initialize()

    results = run_failure_drills(database)

    assert len(results) == 6
    assert all(result["state"] == "PASS" for result in results)
    assert all(result["details"]["broker_order_sent"] is False for result in results)
