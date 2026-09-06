import json
import sqlite3

from moneygun_api.storage import Database


def _build_legacy_database(path: str) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE mode_profiles (
            id TEXT PRIMARY KEY, code TEXT NOT NULL, version INTEGER NOT NULL,
            display_name TEXT NOT NULL, max_positions INTEGER NOT NULL,
            max_single_position_bps INTEGER NOT NULL, max_trade_risk_bps INTEGER NOT NULL,
            max_open_risk_bps INTEGER NOT NULL, halt_drawdown_bps INTEGER NOT NULL,
            created_at TEXT NOT NULL, UNIQUE(code, version)
        );
        CREATE TABLE missions (
            id TEXT PRIMARY KEY, name TEXT NOT NULL,
            mode_profile_id TEXT NOT NULL REFERENCES mode_profiles(id),
            stage TEXT NOT NULL, state TEXT NOT NULL,
            seed_capital_krw INTEGER NOT NULL, goal_capital_krw INTEGER NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL
        );
        CREATE TABLE market_snapshots (
            id TEXT PRIMARY KEY, as_of TEXT NOT NULL, source TEXT NOT NULL,
            quality_state TEXT NOT NULL, checksum TEXT NOT NULL UNIQUE,
            payload_json TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE committee_cycles (
            id TEXT PRIMARY KEY,
            mission_id TEXT NOT NULL REFERENCES missions(id),
            snapshot_id TEXT NOT NULL REFERENCES market_snapshots(id),
            strategy_id TEXT NOT NULL, status TEXT NOT NULL,
            package_json TEXT NOT NULL, created_at TEXT NOT NULL,
            UNIQUE(mission_id, snapshot_id, strategy_id)
        );
        CREATE TABLE research_validations (
            id TEXT PRIMARY KEY,
            mission_id TEXT NOT NULL REFERENCES missions(id),
            snapshot_id TEXT NOT NULL REFERENCES market_snapshots(id),
            strategy_id TEXT NOT NULL, status TEXT NOT NULL,
            report_json TEXT NOT NULL, created_at TEXT NOT NULL,
            UNIQUE(mission_id, snapshot_id, strategy_id)
        );
        """
    )
    connection.execute(
        "INSERT INTO mode_profiles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("legacy_mode", "LEGACY", 1, "legacy", 1, 1000, 100, 100, 1000, "2025-01-01"),
    )
    connection.execute(
        "INSERT INTO missions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "legacy_mission",
            "legacy",
            "legacy_mode",
            "R0",
            "ACTIVE",
            100_000,
            10_000_000,
            "legacy-key",
            "2025-01-01",
        ),
    )
    connection.execute(
        "INSERT INTO market_snapshots VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "legacy_snapshot",
            "2025-01-01T00:00:00+09:00",
            "KRX_AUTHORIZED_EXPORT",
            "PASS",
            "legacy-checksum",
            "{}",
            "2025-01-01",
        ),
    )
    legacy_cycle = {
        "id": "legacy_cycle",
        "mission_id": "legacy_mission",
        "snapshot_id": "legacy_snapshot",
        "strategy_id": "CLOSE-AUCTION-KR-v2",
        "status": "COMPLETE",
        "decision": {"action": "HOLD"},
    }
    legacy_report = {
        "id": "legacy_validation",
        "snapshot_id": "legacy_snapshot",
        "strategy_id": "CLOSE-AUCTION-KR-v2",
        "status": "NOT_ELIGIBLE",
        "promotion_eligible": False,
    }
    connection.execute(
        "INSERT INTO committee_cycles VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "legacy_cycle",
            "legacy_mission",
            "legacy_snapshot",
            "CLOSE-AUCTION-KR-v2",
            "COMPLETE",
            json.dumps(legacy_cycle),
            "2025-01-01",
        ),
    )
    connection.execute(
        "INSERT INTO research_validations VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "legacy_validation",
            "legacy_mission",
            "legacy_snapshot",
            "CLOSE-AUCTION-KR-v2",
            "NOT_ELIGIBLE",
            json.dumps(legacy_report),
            "2025-01-01",
        ),
    )
    connection.commit()
    connection.close()


def test_protocol_migration_preserves_legacy_and_allows_new_method(tmp_path) -> None:
    path = tmp_path / "legacy.sqlite3"
    _build_legacy_database(str(path))
    database = Database(path)

    database.initialize()

    with database.connect() as connection:
        cycle_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(committee_cycles)")
        }
        validation_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(research_validations)")
        }
        assert "protocol_version" in cycle_columns
        assert "protocol_version" in validation_columns
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    new_report = {
        "id": "dynamic_validation",
        "snapshot_id": "legacy_snapshot",
        "strategy_id": "CLOSE-AUCTION-KR-v2",
        "protocol_version": "POINT_IN_TIME_DYNAMIC_UNIVERSE-v2",
        "status": "NOT_ELIGIBLE",
        "promotion_eligible": False,
    }
    new_cycle = {
        "id": "dynamic_cycle",
        "mission_id": "legacy_mission",
        "snapshot_id": "legacy_snapshot",
        "strategy_id": "CLOSE-AUCTION-KR-v2",
        "protocol_version": "POINT_IN_TIME_DYNAMIC_UNIVERSE-v2",
        "status": "COMPLETE",
        "decision": {"action": "HOLD"},
    }
    database.save_validation("legacy_mission", new_report)
    database.save_cycle(new_cycle)

    assert database.get_validation("legacy_validation")["id"] == "legacy_validation"
    assert database.get_cycle("legacy_cycle")["id"] == "legacy_cycle"
    assert database.get_latest_validation("legacy_mission")["id"] == "dynamic_validation"
    assert database.get_latest_cycle("legacy_mission")["id"] == "dynamic_cycle"
