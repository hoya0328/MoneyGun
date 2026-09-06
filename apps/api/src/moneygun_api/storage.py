from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _postgres_sql(sql: str) -> str:
    translated = sql.replace("BEGIN IMMEDIATE", "BEGIN")
    translated = re.sub(r"\browid\b", "_mg_seq", translated, flags=re.I)
    if re.match(r"^\s*CREATE\s+TABLE", translated, flags=re.I):
        translated = translated.replace("(", "(_mg_seq BIGSERIAL UNIQUE,", 1)
    ignore_insert = bool(re.search(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", translated, re.I))
    translated = re.sub(
        r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", "INSERT INTO", translated, flags=re.I
    )
    translated = translated.replace("?", "%s")
    if ignore_insert:
        stripped = translated.rstrip()
        suffix = ";" if stripped.endswith(";") else ""
        stripped = stripped[:-1] if suffix else stripped
        translated = f"{stripped} ON CONFLICT DO NOTHING{suffix}"
    return translated


class _PostgresCursor:
    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor

    @property
    def rowcount(self) -> int:
        return int(self._cursor.rowcount)

    @staticmethod
    def _public(row: Any | None) -> Any | None:
        if isinstance(row, dict):
            row.pop("_mg_seq", None)
        return row

    def fetchone(self) -> Any | None:
        return self._public(self._cursor.fetchone())

    def fetchall(self) -> list[Any]:
        return [self._public(row) for row in self._cursor.fetchall()]


class _PostgresConnection:
    """Small DB-API compatibility boundary for the storage repository's portable SQL."""

    def __init__(self, url: str) -> None:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as error:  # pragma: no cover - exercised only in production setup
            raise RuntimeError("PostgreSQL 사용에는 psycopg 패키지가 필요합니다.") from error
        self._connection = psycopg.connect(url, row_factory=dict_row)

    def execute(self, sql: str, parameters: tuple[Any, ...] | list[Any] = ()) -> Any:
        if sql.strip().upper() == "BEGIN IMMEDIATE":
            self._connection.execute("BEGIN")
            cursor = self._connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext('moneygun_write'))"
            )
            return _PostgresCursor(cursor)
        return _PostgresCursor(self._connection.execute(_postgres_sql(sql), parameters))

    def executescript(self, script: str) -> None:
        for statement in (item.strip() for item in script.split(";")):
            if statement:
                self.execute(statement)

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> _PostgresConnection:
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        try:
            if exc_type is None:
                self._connection.commit()
            else:
                self._connection.rollback()
        finally:
            self._connection.close()


class Database:
    """Local persistence adapter. Domain writes are append-only where money is involved."""

    def __init__(self, path: str | Path | None = None) -> None:
        backend = os.getenv("MONEYGUN_DATABASE_BACKEND", "sqlite").strip().lower()
        configured = path or (
            os.getenv("MONEYGUN_DATABASE_URL", "")
            if backend == "postgresql"
            else os.getenv("MONEYGUN_DATABASE_PATH", "data/moneygun.sqlite3")
        )
        if not configured:
            raise RuntimeError("PostgreSQL 백엔드를 선택했지만 MONEYGUN_DATABASE_URL이 없습니다.")
        self.path = str(configured)
        self.backend = (
            "postgresql"
            if self.path.startswith(("postgresql://", "postgres://"))
            else "sqlite"
        )
        if self.backend == "sqlite" and self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> Any:
        if self.backend == "postgresql":
            return _PostgresConnection(self.path)
        connection = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS mode_profiles (
                    id TEXT PRIMARY KEY,
                    code TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    display_name TEXT NOT NULL,
                    max_positions INTEGER NOT NULL,
                    max_single_position_bps INTEGER NOT NULL,
                    max_trade_risk_bps INTEGER NOT NULL,
                    max_open_risk_bps INTEGER NOT NULL,
                    halt_drawdown_bps INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(code, version)
                );

                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version TEXT PRIMARY KEY,
                    description TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS missions (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    mode_profile_id TEXT NOT NULL REFERENCES mode_profiles(id),
                    stage TEXT NOT NULL,
                    state TEXT NOT NULL,
                    seed_capital_krw INTEGER NOT NULL CHECK(seed_capital_krw > 0),
                    goal_capital_krw INTEGER NOT NULL CHECK(goal_capital_krw >= seed_capital_krw),
                    idempotency_key TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS ledger_transactions (
                    id TEXT PRIMARY KEY,
                    mission_id TEXT NOT NULL REFERENCES missions(id),
                    kind TEXT NOT NULL,
                    correlation_id TEXT NOT NULL,
                    note TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS ledger_postings (
                    id TEXT PRIMARY KEY,
                    transaction_id TEXT NOT NULL REFERENCES ledger_transactions(id),
                    account_code TEXT NOT NULL,
                    amount_krw INTEGER NOT NULL CHECK(amount_krw != 0)
                );

                CREATE TABLE IF NOT EXISTS audit_events (
                    id TEXT PRIMARY KEY,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    aggregate_type TEXT NOT NULL,
                    aggregate_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL UNIQUE,
                    occurred_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS market_snapshots (
                    id TEXT PRIMARY KEY,
                    as_of TEXT NOT NULL,
                    source TEXT NOT NULL,
                    quality_state TEXT NOT NULL,
                    checksum TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS committee_cycles (
                    id TEXT PRIMARY KEY,
                    mission_id TEXT NOT NULL REFERENCES missions(id),
                    snapshot_id TEXT NOT NULL REFERENCES market_snapshots(id),
                    strategy_id TEXT NOT NULL,
                    protocol_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    package_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(mission_id, snapshot_id, strategy_id, protocol_version)
                );

                CREATE TABLE IF NOT EXISTS research_validations (
                    id TEXT PRIMARY KEY,
                    mission_id TEXT NOT NULL REFERENCES missions(id),
                    snapshot_id TEXT NOT NULL REFERENCES market_snapshots(id),
                    strategy_id TEXT NOT NULL,
                    protocol_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    report_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(mission_id, snapshot_id, strategy_id, protocol_version)
                );

                CREATE TABLE IF NOT EXISTS committee_programs (
                    id TEXT PRIMARY KEY,
                    mission_id TEXT NOT NULL REFERENCES missions(id),
                    mode TEXT NOT NULL,
                    source TEXT NOT NULL,
                    start_date TEXT NOT NULL,
                    end_date TEXT NOT NULL,
                    target_days INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    summary_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS committee_program_days (
                    id TEXT PRIMARY KEY,
                    program_id TEXT NOT NULL REFERENCES committee_programs(id),
                    trade_date TEXT NOT NULL,
                    state TEXT NOT NULL,
                    attempts INTEGER NOT NULL,
                    snapshot_id TEXT REFERENCES market_snapshots(id),
                    cycle_id TEXT REFERENCES committee_cycles(id),
                    error TEXT,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    UNIQUE(program_id, trade_date)
                );

                CREATE TABLE IF NOT EXISTS agent_evaluations (
                    id TEXT PRIMARY KEY,
                    program_id TEXT NOT NULL REFERENCES committee_programs(id),
                    trade_date TEXT NOT NULL,
                    agent_code TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(program_id, trade_date, agent_code)
                );

                CREATE TABLE IF NOT EXISTS broker_account_snapshots (
                    id TEXT PRIMARY KEY,
                    broker TEXT NOT NULL,
                    environment TEXT NOT NULL,
                    account_alias TEXT NOT NULL,
                    checksum TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS shadow_orders (
                    id TEXT PRIMARY KEY,
                    mission_id TEXT NOT NULL REFERENCES missions(id),
                    cycle_id TEXT NOT NULL REFERENCES committee_cycles(id),
                    idempotency_key TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS shadow_order_events (
                    id TEXT PRIMARY KEY,
                    order_id TEXT NOT NULL REFERENCES shadow_orders(id),
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    UNIQUE(order_id, event_type)
                );

                CREATE TABLE IF NOT EXISTS shadow_fills (
                    id TEXT PRIMARY KEY,
                    order_id TEXT NOT NULL UNIQUE REFERENCES shadow_orders(id),
                    quantity INTEGER NOT NULL CHECK(quantity > 0),
                    price_krw INTEGER NOT NULL CHECK(price_krw > 0),
                    occurred_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS broker_quote_snapshots (
                    id TEXT PRIMARY KEY,
                    broker TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    checksum TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS reconciliation_runs (
                    id TEXT PRIMARY KEY,
                    mission_id TEXT NOT NULL REFERENCES missions(id),
                    account_snapshot_id TEXT NOT NULL REFERENCES broker_account_snapshots(id),
                    status TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS execution_controls (
                    mission_id TEXT PRIMARY KEY REFERENCES missions(id),
                    stage TEXT NOT NULL,
                    kill_switch_active INTEGER NOT NULL,
                    automation_enabled INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS execution_leases (
                    name TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS live_order_intents (
                    id TEXT PRIMARY KEY,
                    mission_id TEXT NOT NULL REFERENCES missions(id),
                    shadow_order_id TEXT NOT NULL UNIQUE REFERENCES shadow_orders(id),
                    idempotency_key TEXT NOT NULL UNIQUE,
                    scope_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS live_order_events (
                    id TEXT PRIMARY KEY,
                    intent_id TEXT NOT NULL REFERENCES live_order_intents(id),
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    UNIQUE(intent_id, event_type)
                );

                CREATE TABLE IF NOT EXISTS owner_approvals (
                    id TEXT PRIMARY KEY,
                    intent_id TEXT NOT NULL UNIQUE REFERENCES live_order_intents(id),
                    decision TEXT NOT NULL,
                    scope_hash TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS live_fills (
                    id TEXT PRIMARY KEY,
                    intent_id TEXT NOT NULL REFERENCES live_order_intents(id),
                    broker_order_no TEXT NOT NULL,
                    broker_fill_key TEXT NOT NULL,
                    quantity INTEGER NOT NULL CHECK(quantity > 0),
                    price_krw INTEGER NOT NULL CHECK(price_krw > 0),
                    fee_krw INTEGER NOT NULL,
                    tax_krw INTEGER NOT NULL,
                    occurred_at TEXT NOT NULL,
                    UNIQUE(intent_id, broker_fill_key)
                );

                CREATE TABLE IF NOT EXISTS operating_days (
                    id TEXT PRIMARY KEY,
                    mission_id TEXT NOT NULL REFERENCES missions(id),
                    trade_date TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    reconciliation_status TEXT NOT NULL,
                    risk_violations INTEGER NOT NULL,
                    duplicate_orders INTEGER NOT NULL,
                    decision_count INTEGER NOT NULL,
                    fill_count INTEGER NOT NULL,
                    net_pnl_krw INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(mission_id, trade_date, mode)
                );

                CREATE TABLE IF NOT EXISTS pilot_reviews (
                    id TEXT PRIMARY KEY,
                    mission_id TEXT NOT NULL REFERENCES missions(id),
                    checkpoint_day INTEGER NOT NULL,
                    candidate_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    report_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(mission_id, checkpoint_day)
                );

                CREATE TABLE IF NOT EXISTS incidents (
                    id TEXT PRIMARY KEY,
                    mission_id TEXT NOT NULL REFERENCES missions(id),
                    severity TEXT NOT NULL,
                    code TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    resolved_at TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS operational_runs (
                    id TEXT PRIMARY KEY,
                    mission_id TEXT NOT NULL REFERENCES missions(id),
                    trade_date TEXT NOT NULL,
                    run_type TEXT NOT NULL,
                    state TEXT NOT NULL,
                    current_stage TEXT NOT NULL,
                    summary_json TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    UNIQUE(mission_id, trade_date, run_type)
                );

                CREATE TABLE IF NOT EXISTS notification_outbox (
                    id TEXT PRIMARY KEY,
                    mission_id TEXT NOT NULL REFERENCES missions(id),
                    severity TEXT NOT NULL,
                    category TEXT NOT NULL,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    acknowledged_at TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS news_events (
                    id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    source_event_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    corp_code TEXT NOT NULL,
                    company_name TEXT NOT NULL,
                    title TEXT NOT NULL,
                    category TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    sentiment TEXT NOT NULL,
                    review_required INTEGER NOT NULL CHECK(review_required IN (0, 1)),
                    blocks_new_buy INTEGER NOT NULL CHECK(blocks_new_buy IN (0, 1)),
                    published_at TEXT NOT NULL,
                    collected_at TEXT NOT NULL,
                    url TEXT NOT NULL,
                    analysis_json TEXT NOT NULL,
                    acknowledged_at TEXT,
                    acknowledged_by TEXT,
                    UNIQUE(source, source_event_id)
                );

                CREATE TABLE IF NOT EXISTS news_poll_runs (
                    id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    state TEXT NOT NULL,
                    watched_symbols INTEGER NOT NULL,
                    fetched_count INTEGER NOT NULL,
                    inserted_count INTEGER NOT NULL,
                    blocking_count INTEGER NOT NULL,
                    error TEXT,
                    started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS strategy_evaluations (
                    id TEXT PRIMARY KEY,
                    mission_id TEXT NOT NULL REFERENCES missions(id),
                    strategy_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    verdict TEXT NOT NULL,
                    metrics_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(mission_id, strategy_id, role, created_at)
                );

                CREATE TABLE IF NOT EXISTS trade_outcomes (
                    id TEXT PRIMARY KEY,
                    mission_id TEXT NOT NULL REFERENCES missions(id),
                    source_type TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    cycle_id TEXT REFERENCES committee_cycles(id),
                    strategy_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    regime TEXT NOT NULL,
                    entry_at TEXT NOT NULL,
                    exit_at TEXT NOT NULL,
                    quantity INTEGER NOT NULL CHECK(quantity > 0),
                    entry_price_krw INTEGER NOT NULL CHECK(entry_price_krw > 0),
                    exit_price_krw INTEGER NOT NULL CHECK(exit_price_krw > 0),
                    fee_krw INTEGER NOT NULL CHECK(fee_krw >= 0),
                    tax_krw INTEGER NOT NULL CHECK(tax_krw >= 0),
                    net_pnl_krw INTEGER NOT NULL,
                    return_bps INTEGER NOT NULL,
                    slippage_bps INTEGER NOT NULL,
                    participants_json TEXT NOT NULL,
                    postmortem_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(source_type, source_id)
                );

                CREATE TABLE IF NOT EXISTS profit_vault_milestones (
                    id TEXT PRIMARY KEY,
                    mission_id TEXT NOT NULL REFERENCES missions(id),
                    target_multiple INTEGER NOT NULL,
                    target_equity_krw INTEGER NOT NULL,
                    locked_amount_krw INTEGER NOT NULL CHECK(locked_amount_krw > 0),
                    trigger_outcome_id TEXT REFERENCES trade_outcomes(id),
                    created_at TEXT NOT NULL,
                    UNIQUE(mission_id, target_multiple)
                );

                CREATE TABLE IF NOT EXISTS change_requests (
                    id TEXT PRIMARY KEY,
                    mission_id TEXT NOT NULL REFERENCES missions(id),
                    change_type TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    scope_hash TEXT NOT NULL,
                    proposal_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    decided_at TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(mission_id, change_type, target_id, scope_hash)
                );

                CREATE TABLE IF NOT EXISTS recovery_runs (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    state TEXT NOT NULL,
                    artifact_path TEXT,
                    checksum TEXT,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS drill_runs (
                    id TEXT PRIMARY KEY,
                    scenario TEXT NOT NULL,
                    state TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS strategy_runtime_states (
                    strategy_id TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(strategy_id, trade_date)
                );

                CREATE INDEX IF NOT EXISTS idx_ledger_mission
                    ON ledger_transactions(mission_id, occurred_at);
                CREATE INDEX IF NOT EXISTS idx_audit_aggregate
                    ON audit_events(aggregate_type, aggregate_id, occurred_at);
                CREATE INDEX IF NOT EXISTS idx_cycles_mission
                    ON committee_cycles(mission_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_validations_mission
                    ON research_validations(mission_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_programs_mission
                    ON committee_programs(mission_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_program_days
                    ON committee_program_days(program_id, trade_date);
                CREATE INDEX IF NOT EXISTS idx_agent_evaluations
                    ON agent_evaluations(program_id, agent_code, trade_date);
                CREATE INDEX IF NOT EXISTS idx_broker_snapshots
                    ON broker_account_snapshots(broker, created_at);
                CREATE INDEX IF NOT EXISTS idx_shadow_orders
                    ON shadow_orders(mission_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_shadow_events
                    ON shadow_order_events(order_id, occurred_at);
                CREATE INDEX IF NOT EXISTS idx_quotes_symbol
                    ON broker_quote_snapshots(symbol, observed_at);
                CREATE INDEX IF NOT EXISTS idx_reconciliations
                    ON reconciliation_runs(mission_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_live_intents
                    ON live_order_intents(mission_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_live_order_events
                    ON live_order_events(intent_id, occurred_at);
                CREATE INDEX IF NOT EXISTS idx_operating_days
                    ON operating_days(mission_id, trade_date);
                CREATE INDEX IF NOT EXISTS idx_pilot_reviews
                    ON pilot_reviews(mission_id, checkpoint_day);
                CREATE INDEX IF NOT EXISTS idx_operational_runs
                    ON operational_runs(mission_id, trade_date);
                CREATE INDEX IF NOT EXISTS idx_notification_outbox
                    ON notification_outbox(mission_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_news_events_symbol
                    ON news_events(symbol, collected_at);
                CREATE INDEX IF NOT EXISTS idx_news_events_blocking
                    ON news_events(blocks_new_buy, acknowledged_at, collected_at);
                CREATE INDEX IF NOT EXISTS idx_news_poll_runs
                    ON news_poll_runs(source, completed_at);
                CREATE INDEX IF NOT EXISTS idx_trade_outcomes
                    ON trade_outcomes(mission_id, exit_at);
                CREATE INDEX IF NOT EXISTS idx_change_requests
                    ON change_requests(mission_id, created_at);
                """
            )
            if self.backend == "sqlite":
                self._migrate_protocol_versioned_records(connection)
                self._migrate_live_fill_identity(connection)
            connection.execute(
                """
                INSERT OR IGNORE INTO schema_migrations (version, description, applied_at)
                VALUES (?, ?, ?)
                """,
                (
                    "20260831_01",
                    "performance, fill identity, profit vault, change control, postgres boundary",
                    utc_now(),
                ),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO schema_migrations (version, description, applied_at)
                VALUES (?, ?, ?)
                """,
                (
                    "20260901_01",
                    "L0 micro-live pilot mission and immutable 15-day review snapshots",
                    utc_now(),
                ),
            )
        self.seed_defaults()

    @staticmethod
    def _migrate_protocol_versioned_records(connection: sqlite3.Connection) -> None:
        """Add protocol identity without rewriting historical validation/cycle payloads."""

        cycle_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(committee_cycles)")
        }
        validation_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(research_validations)")
        }
        migrate_cycles = "protocol_version" not in cycle_columns
        migrate_validations = "protocol_version" not in validation_columns
        if not migrate_cycles and not migrate_validations:
            return

        connection.commit()
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("BEGIN IMMEDIATE")
        try:
            if migrate_cycles:
                connection.execute(
                    """
                    CREATE TABLE committee_cycles_protocol_new (
                        id TEXT PRIMARY KEY,
                        mission_id TEXT NOT NULL REFERENCES missions(id),
                        snapshot_id TEXT NOT NULL REFERENCES market_snapshots(id),
                        strategy_id TEXT NOT NULL,
                        protocol_version TEXT NOT NULL,
                        status TEXT NOT NULL,
                        package_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        UNIQUE(mission_id, snapshot_id, strategy_id, protocol_version)
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO committee_cycles_protocol_new (
                        id, mission_id, snapshot_id, strategy_id, protocol_version,
                        status, package_json, created_at
                    )
                    SELECT id, mission_id, snapshot_id, strategy_id,
                        CASE
                            WHEN strategy_id LIKE 'CLOSE-AUCTION%'
                                THEN 'LEGACY_COMMON_DATES-v1'
                            ELSE 'COMMITTEE_BASELINE-v1'
                        END,
                        status, package_json, created_at
                    FROM committee_cycles
                    """
                )
                connection.execute("DROP TABLE committee_cycles")
                connection.execute(
                    "ALTER TABLE committee_cycles_protocol_new RENAME TO committee_cycles"
                )
            if migrate_validations:
                connection.execute(
                    """
                    CREATE TABLE research_validations_protocol_new (
                        id TEXT PRIMARY KEY,
                        mission_id TEXT NOT NULL REFERENCES missions(id),
                        snapshot_id TEXT NOT NULL REFERENCES market_snapshots(id),
                        strategy_id TEXT NOT NULL,
                        protocol_version TEXT NOT NULL,
                        status TEXT NOT NULL,
                        report_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        UNIQUE(mission_id, snapshot_id, strategy_id, protocol_version)
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO research_validations_protocol_new (
                        id, mission_id, snapshot_id, strategy_id, protocol_version,
                        status, report_json, created_at
                    )
                    SELECT id, mission_id, snapshot_id, strategy_id,
                        'LEGACY_COMMON_DATES-v1', status, report_json, created_at
                    FROM research_validations
                    """
                )
                connection.execute("DROP TABLE research_validations")
                connection.execute(
                    "ALTER TABLE research_validations_protocol_new "
                    "RENAME TO research_validations"
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_cycles_mission "
                "ON committee_cycles(mission_id, created_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_validations_mission "
                "ON research_validations(mission_id, created_at)"
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.execute("PRAGMA foreign_keys = ON")

        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError("protocol migration failed foreign-key validation")

    @staticmethod
    def _migrate_live_fill_identity(connection: sqlite3.Connection) -> None:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(live_fills)")}
        if "broker_fill_key" in columns:
            return
        connection.commit()
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                """
                CREATE TABLE live_fills_identity_new (
                    id TEXT PRIMARY KEY,
                    intent_id TEXT NOT NULL REFERENCES live_order_intents(id),
                    broker_order_no TEXT NOT NULL,
                    broker_fill_key TEXT NOT NULL,
                    quantity INTEGER NOT NULL CHECK(quantity > 0),
                    price_krw INTEGER NOT NULL CHECK(price_krw > 0),
                    fee_krw INTEGER NOT NULL,
                    tax_krw INTEGER NOT NULL,
                    occurred_at TEXT NOT NULL,
                    UNIQUE(intent_id, broker_fill_key)
                )
                """
            )
            connection.execute(
                """
                INSERT INTO live_fills_identity_new (
                    id, intent_id, broker_order_no, broker_fill_key, quantity,
                    price_krw, fee_krw, tax_krw, occurred_at
                )
                SELECT id, intent_id, broker_order_no, 'legacy:' || id, quantity,
                    price_krw, fee_krw, tax_krw, occurred_at
                FROM live_fills
                """
            )
            connection.execute("DROP TABLE live_fills")
            connection.execute("ALTER TABLE live_fills_identity_new RENAME TO live_fills")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.execute("PRAGMA foreign_keys = ON")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("live fill migration failed foreign-key validation")

    def seed_defaults(self) -> None:
        with self.connect() as connection:
            now = utc_now()
            connection.execute(
                """
                INSERT OR IGNORE INTO mode_profiles (
                    id, code, version, display_name, max_positions,
                    max_single_position_bps, max_trade_risk_bps,
                    max_open_risk_bps, halt_drawdown_bps, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("mode_focus_v1", "FOCUS", 1, "집중투자", 3, 5000, 500, 1500, 3000, now),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO mode_profiles (
                    id, code, version, display_name, max_positions,
                    max_single_position_bps, max_trade_risk_bps,
                    max_open_risk_bps, halt_drawdown_bps, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "mode_close_auction_v1",
                    "CLOSE_AUCTION",
                    1,
                    "종가매매",
                    1,
                    5000,
                    200,
                    500,
                    1500,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO mode_profiles (
                    id, code, version, display_name, max_positions,
                    max_single_position_bps, max_trade_risk_bps,
                    max_open_risk_bps, halt_drawdown_bps, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "mode_l0_pilot_v1",
                    "L0_PILOT",
                    1,
                    "5만 원 실거래 파일럿",
                    1,
                    9_000,
                    300,
                    300,
                    1_000,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO mode_profiles (
                    id, code, version, display_name, max_positions,
                    max_single_position_bps, max_trade_risk_bps,
                    max_open_risk_bps, halt_drawdown_bps, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "mode_opening_range_v1",
                    "OPENING_RANGE",
                    1,
                    "장초 단타",
                    1,
                    10_000,
                    50,
                    50,
                    1_000,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO mode_profiles (
                    id, code, version, display_name, max_positions,
                    max_single_position_bps, max_trade_risk_bps,
                    max_open_risk_bps, halt_drawdown_bps, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("mode_balanced_v1", "BALANCED", 1, "안전투자", 12, 1000, 50, 400, 1200, now),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO mode_profiles (
                    id, code, version, display_name, max_positions,
                    max_single_position_bps, max_trade_risk_bps,
                    max_open_risk_bps, halt_drawdown_bps, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("mode_long_term_v1", "LONG_TERM", 1, "장기투자", 10, 1500, 70, 500, 2000, now),
            )
            connection.commit()

        self.create_mission(
            name="10만 원 집중투자 미션",
            mode_code="FOCUS",
            seed_capital_krw=100_000,
            goal_capital_krw=10_000_000,
            idempotency_key="seed:mission_focus_001",
            mission_id="mission_focus_001",
        )
        self.create_mission(
            name="10만 원 종가매매 미션",
            mode_code="CLOSE_AUCTION",
            seed_capital_krw=100_000,
            goal_capital_krw=10_000_000,
            idempotency_key="seed:mission_close_auction_001",
            mission_id="mission_close_auction_001",
        )
        self.create_mission(
            name="10만 원 장초 단타 미션",
            mode_code="OPENING_RANGE",
            seed_capital_krw=100_000,
            goal_capital_krw=10_000_000,
            idempotency_key="seed:mission_opening_range_001",
            mission_id="mission_opening_range_001",
        )
        self.create_mission(
            name="5만 원 L0 실거래 파일럿",
            mode_code="L0_PILOT",
            seed_capital_krw=50_000,
            goal_capital_krw=10_000_000,
            idempotency_key="seed:mission_l0_pilot_001",
            mission_id="mission_l0_pilot_001",
        )
        self.create_mission(
            name="10만 원 안전투자 미션",
            mode_code="BALANCED",
            seed_capital_krw=100_000,
            goal_capital_krw=10_000_000,
            idempotency_key="seed:mission_balanced_001",
            mission_id="mission_balanced_001",
        )
        self.create_mission(
            name="10만 원 장기투자 미션",
            mode_code="LONG_TERM",
            seed_capital_krw=100_000,
            goal_capital_krw=10_000_000,
            idempotency_key="seed:mission_long_term_001",
            mission_id="mission_long_term_001",
        )
        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO execution_controls (
                    mission_id, stage, kill_switch_active, automation_enabled, reason, updated_at
                ) VALUES ('mission_focus_001', 'R0', 0, 0, '초기 연구 단계', ?)
                """,
                (utc_now(),),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO execution_controls (
                    mission_id, stage, kill_switch_active, automation_enabled, reason, updated_at
                ) VALUES ('mission_close_auction_001', 'R0', 0, 0, '종가매매 연구 단계', ?)
                """,
                (utc_now(),),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO execution_controls (
                    mission_id, stage, kill_switch_active, automation_enabled, reason, updated_at
                ) VALUES ('mission_opening_range_001', 'R0', 0, 0, '장초 단타 그림자 연구 단계', ?)
                """,
                (utc_now(),),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO execution_controls (
                    mission_id, stage, kill_switch_active, automation_enabled, reason, updated_at
                ) VALUES ('mission_l0_pilot_001', 'R0', 0, 0, 'L0 파일럿 활성화 대기', ?)
                """,
                (utc_now(),),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO execution_controls (
                    mission_id, stage, kill_switch_active, automation_enabled, reason, updated_at
                ) VALUES ('mission_balanced_001', 'R0', 0, 0, '안전투자 L0 실험 후보 연구 단계', ?)
                """,
                (utc_now(),),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO execution_controls (
                    mission_id, stage, kill_switch_active, automation_enabled, reason, updated_at
                ) VALUES ('mission_long_term_001', 'R0', 0, 0, '장기투자 L0 실험 후보 연구 단계', ?)
                """,
                (utc_now(),),
            )
            connection.commit()

    def create_mission(
        self,
        *,
        name: str,
        mode_code: str,
        seed_capital_krw: int,
        goal_capital_krw: int,
        idempotency_key: str,
        mission_id: str | None = None,
    ) -> dict[str, Any]:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT id FROM missions WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if existing:
                connection.rollback()
                return self.get_mission(existing["id"])

            mode = connection.execute(
                "SELECT * FROM mode_profiles WHERE code = ? ORDER BY version DESC LIMIT 1",
                (mode_code,),
            ).fetchone()
            if mode is None:
                connection.rollback()
                raise ValueError(f"Unknown mode: {mode_code}")

            created_at = utc_now()
            new_mission_id = mission_id or f"mission_{uuid4().hex[:16]}"
            connection.execute(
                """
                INSERT INTO missions (
                    id, name, mode_profile_id, stage, state, seed_capital_krw,
                    goal_capital_krw, idempotency_key, created_at
                ) VALUES (?, ?, ?, 'R0', 'ACTIVE', ?, ?, ?, ?)
                """,
                (
                    new_mission_id,
                    name,
                    mode["id"],
                    seed_capital_krw,
                    goal_capital_krw,
                    idempotency_key,
                    created_at,
                ),
            )
            self._append_ledger_transaction(
                connection,
                mission_id=new_mission_id,
                kind="MISSION_SEEDED",
                correlation_id=idempotency_key,
                note="사용자가 승인한 초기 미션 자본",
                postings=[
                    ("AVAILABLE_CASH", seed_capital_krw),
                    ("OWNER_CAPITAL", -seed_capital_krw),
                ],
            )
            self._append_audit_event(
                connection,
                actor="SYSTEM",
                action="mission.created",
                aggregate_type="Mission",
                aggregate_id=new_mission_id,
                payload={
                    "mode_profile_id": mode["id"],
                    "seed_capital_krw": seed_capital_krw,
                    "goal_capital_krw": goal_capital_krw,
                },
            )
            connection.commit()
        return self.get_mission(new_mission_id)

    def _append_ledger_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        mission_id: str,
        kind: str,
        correlation_id: str,
        note: str,
        postings: list[tuple[str, int]],
    ) -> None:
        if sum(amount for _, amount in postings) != 0:
            raise ValueError("Ledger postings must balance to zero")
        transaction_id = f"ltx_{uuid4().hex}"
        connection.execute(
            """
            INSERT INTO ledger_transactions (
                id, mission_id, kind, correlation_id, note, occurred_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (transaction_id, mission_id, kind, correlation_id, note, utc_now()),
        )
        connection.executemany(
            """
            INSERT INTO ledger_postings (id, transaction_id, account_code, amount_krw)
            VALUES (?, ?, ?, ?)
            """,
            [
                (f"lp_{uuid4().hex}", transaction_id, account, amount)
                for account, amount in postings
            ],
        )

    def _append_audit_event(
        self,
        connection: sqlite3.Connection,
        *,
        actor: str,
        action: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: dict[str, Any],
    ) -> None:
        previous = connection.execute(
            "SELECT event_hash FROM audit_events ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        previous_hash = previous["event_hash"] if previous else "GENESIS"
        occurred_at = utc_now()
        payload_json = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        material = "|".join(
            [previous_hash, actor, action, aggregate_type, aggregate_id, payload_json, occurred_at]
        )
        event_hash = hashlib.sha256(material.encode()).hexdigest()
        connection.execute(
            """
            INSERT INTO audit_events (
                id, actor, action, aggregate_type, aggregate_id, payload_json,
                previous_hash, event_hash, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"evt_{uuid4().hex}",
                actor,
                action,
                aggregate_type,
                aggregate_id,
                payload_json,
                previous_hash,
                event_hash,
                occurred_at,
            ),
        )

    def list_modes(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM mode_profiles ORDER BY code, version DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_mission(self, mission_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            mission = connection.execute(
                """
                SELECT m.*, p.code AS mode_code, p.version AS mode_version,
                       p.display_name AS mode_display_name, p.max_positions,
                       p.max_single_position_bps, p.max_trade_risk_bps,
                       p.max_open_risk_bps, p.halt_drawdown_bps
                FROM missions m JOIN mode_profiles p ON p.id = m.mode_profile_id
                WHERE m.id = ?
                """,
                (mission_id,),
            ).fetchone()
            if mission is None:
                raise KeyError(mission_id)
            balances = connection.execute(
                """
                SELECT p.account_code, COALESCE(SUM(p.amount_krw), 0) AS balance
                FROM ledger_postings p
                JOIN ledger_transactions t ON t.id = p.transaction_id
                WHERE t.mission_id = ? GROUP BY p.account_code
                """,
                (mission_id,),
            ).fetchall()
        result = dict(mission)
        account_balances = {row["account_code"]: row["balance"] for row in balances}
        result["balances"] = account_balances
        result["available_krw"] = account_balances.get("AVAILABLE_CASH", 0)
        result["exposed_krw"] = account_balances.get("EXPOSED_COST", 0)
        result["reserved_profit_krw"] = account_balances.get("RESERVED_PROFIT", 0)
        result["equity_krw"] = sum(
            account_balances.get(code, 0)
            for code in ("AVAILABLE_CASH", "EXPOSED_COST", "RESERVED_PROFIT")
        )
        result["halt_equity_krw"] = (
            result["seed_capital_krw"] * (10_000 - result["halt_drawdown_bps"]) // 10_000
        )
        return result

    def list_ledger(self, mission_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT t.id AS transaction_id, t.kind, t.correlation_id, t.note,
                       t.occurred_at, p.account_code, p.amount_krw
                FROM ledger_transactions t
                JOIN ledger_postings p ON p.transaction_id = t.id
                WHERE t.mission_id = ? ORDER BY t.occurred_at, p.rowid
                """,
                (mission_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_audit_events(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM audit_events ORDER BY rowid DESC LIMIT ?", (limit,)
            ).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload_json"])} for row in rows]

    def record_data_refresh_event(
        self, outcome: str, run_id: str, payload: dict[str, Any]
    ) -> None:
        """Append lifecycle evidence for the read-only official-data refresher."""
        if outcome not in {"started", "completed", "failed"}:
            raise ValueError("Unsupported data refresh outcome")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._append_audit_event(
                connection,
                actor="김데이터",
                action=f"data.refresh.{outcome}",
                aggregate_type="DataRefreshRun",
                aggregate_id=run_id,
                payload=payload,
            )
            connection.commit()

    def save_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        payload_json = json.dumps(
            snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT payload_json FROM market_snapshots WHERE id = ?", (snapshot["id"],)
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO market_snapshots (
                        id, as_of, source, quality_state, checksum, payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot["id"],
                        snapshot["as_of"],
                        snapshot["source"],
                        snapshot["quality"]["state"],
                        snapshot["checksum"],
                        payload_json,
                        utc_now(),
                    ),
                )
                self._append_audit_event(
                    connection,
                    actor="김데이터",
                    action="snapshot.closed",
                    aggregate_type="MarketSnapshot",
                    aggregate_id=snapshot["id"],
                    payload={
                        "source": snapshot["source"],
                        "quality_state": snapshot["quality"]["state"],
                        "checksum": snapshot["checksum"],
                    },
                )
            connection.commit()
        return snapshot

    def get_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM market_snapshots WHERE id = ?", (snapshot_id,)
            ).fetchone()
        if row is None:
            raise KeyError(snapshot_id)
        return json.loads(row["payload_json"])

    def get_latest_snapshot(self, source: str | None = None) -> dict[str, Any] | None:
        with self.connect() as connection:
            if source:
                row = connection.execute(
                    """
                    SELECT payload_json FROM market_snapshots
                    WHERE source = ? ORDER BY as_of DESC, rowid DESC LIMIT 1
                    """,
                    (source,),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT payload_json FROM market_snapshots
                    ORDER BY as_of DESC, rowid DESC LIMIT 1
                    """
                ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def get_latest_snapshot_for_sources(self, sources: tuple[str, ...]) -> dict[str, Any] | None:
        """Load one newest snapshot without decoding every large source archive."""
        if not sources:
            return None
        placeholders = ",".join("?" for _ in sources)
        with self.connect() as connection:
            row = connection.execute(
                f"""
                SELECT payload_json FROM market_snapshots
                WHERE source IN ({placeholders})
                ORDER BY as_of DESC, rowid DESC LIMIT 1
                """,  # noqa: S608 - placeholders are generated from tuple length only
                sources,
            ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def get_latest_snapshot_metadata_for_sources(
        self, sources: tuple[str, ...]
    ) -> dict[str, Any] | None:
        """Read freshness metadata without decoding a potentially large archive."""
        if not sources:
            return None
        placeholders = ",".join("?" for _ in sources)
        with self.connect() as connection:
            row = connection.execute(
                f"""
                SELECT id, as_of, source, quality_state, checksum
                FROM market_snapshots
                WHERE source IN ({placeholders})
                ORDER BY as_of DESC, rowid DESC LIMIT 1
                """,  # noqa: S608 - placeholders are generated from tuple length only
                sources,
            ).fetchone()
        return dict(row) if row else None

    def save_strategy_runtime_state(
        self, strategy_id: str, trade_date: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO strategy_runtime_states (
                    strategy_id, trade_date, payload_json, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(strategy_id, trade_date) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (strategy_id, trade_date, encoded, utc_now()),
            )
            connection.commit()
        return self.get_strategy_runtime_state(strategy_id, trade_date) or payload

    def get_strategy_runtime_state(
        self, strategy_id: str, trade_date: str
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM strategy_runtime_states
                WHERE strategy_id = ? AND trade_date = ?
                """,
                (strategy_id, trade_date),
            ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def save_cycle(self, package: dict[str, Any]) -> dict[str, Any]:
        protocol_version = package.get("protocol_version", "COMMITTEE_BASELINE-v1")
        package_json = json.dumps(
            package, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT package_json FROM committee_cycles WHERE id = ?", (package["id"],)
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO committee_cycles (
                        id, mission_id, snapshot_id, strategy_id, protocol_version, status,
                        package_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        package["id"],
                        package["mission_id"],
                        package["snapshot_id"],
                        package["strategy_id"],
                        protocol_version,
                        package["status"],
                        package_json,
                        utc_now(),
                    ),
                )
                self._append_audit_event(
                    connection,
                    actor="김투자",
                    action="committee.cycle.completed",
                    aggregate_type="CommitteeCycle",
                    aggregate_id=package["id"],
                    payload={
                        "snapshot_id": package["snapshot_id"],
                        "strategy_id": package["strategy_id"],
                        "protocol_version": protocol_version,
                        "decision": package["decision"]["action"],
                        "trading_enabled": False,
                    },
                )
            connection.commit()
        return package if existing is None else json.loads(existing["package_json"])

    def get_cycle(self, cycle_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT package_json FROM committee_cycles WHERE id = ?", (cycle_id,)
            ).fetchone()
        if row is None:
            raise KeyError(cycle_id)
        return json.loads(row["package_json"])

    def get_latest_cycle(self, mission_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT c.package_json FROM committee_cycles c
                JOIN market_snapshots s ON s.id = c.snapshot_id
                WHERE c.mission_id = ? ORDER BY s.as_of DESC, c.rowid DESC LIMIT 1
                """,
                (mission_id,),
            ).fetchone()
        return json.loads(row["package_json"]) if row else None

    def save_validation(self, mission_id: str, report: dict[str, Any]) -> dict[str, Any]:
        protocol_version = report.get("protocol_version", "LEGACY_COMMON_DATES-v1")
        report_json = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT report_json FROM research_validations WHERE id = ?", (report["id"],)
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO research_validations (
                        id, mission_id, snapshot_id, strategy_id, protocol_version,
                        status, report_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        report["id"],
                        mission_id,
                        report["snapshot_id"],
                        report["strategy_id"],
                        protocol_version,
                        report["status"],
                        report_json,
                        utc_now(),
                    ),
                )
                self._append_audit_event(
                    connection,
                    actor="김데이터",
                    action="research.validation.completed",
                    aggregate_type="ResearchValidation",
                    aggregate_id=report["id"],
                    payload={
                        "snapshot_id": report["snapshot_id"],
                        "protocol_version": protocol_version,
                        "status": report["status"],
                        "promotion_eligible": report["promotion_eligible"],
                    },
                )
            connection.commit()
        return report if existing is None else json.loads(existing["report_json"])

    def get_validation(self, validation_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT report_json FROM research_validations WHERE id = ?", (validation_id,)
            ).fetchone()
        if row is None:
            raise KeyError(validation_id)
        return json.loads(row["report_json"])

    def get_latest_validation(self, mission_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT report_json FROM research_validations
                WHERE mission_id = ? ORDER BY rowid DESC LIMIT 1
                """,
                (mission_id,),
            ).fetchone()
        return json.loads(row["report_json"]) if row else None

    def create_committee_program(self, program: dict[str, Any]) -> dict[str, Any]:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM committee_programs WHERE id = ?", (program["id"],)
            ).fetchone()
            if existing is None:
                now = utc_now()
                connection.execute(
                    """
                    INSERT INTO committee_programs (
                        id, mission_id, mode, source, start_date, end_date,
                        target_days, state, summary_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'READY', '{}', ?, ?)
                    """,
                    (
                        program["id"],
                        program["mission_id"],
                        program["mode"],
                        program["source"],
                        program["start_date"],
                        program["end_date"],
                        program["target_days"],
                        now,
                        now,
                    ),
                )
                self._append_audit_event(
                    connection,
                    actor="SYSTEM",
                    action="committee.program.created",
                    aggregate_type="CommitteeProgram",
                    aggregate_id=program["id"],
                    payload={
                        "mode": program["mode"],
                        "source": program["source"],
                        "target_days": program["target_days"],
                    },
                )
            connection.commit()
        return self.get_committee_program(program["id"])

    def get_committee_program(self, program_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM committee_programs WHERE id = ?", (program_id,)
            ).fetchone()
        if row is None:
            raise KeyError(program_id)
        result = dict(row)
        result["summary"] = json.loads(result.pop("summary_json"))
        return result

    def get_latest_committee_program(self, mission_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id FROM committee_programs
                WHERE mission_id = ? ORDER BY rowid DESC LIMIT 1
                """,
                (mission_id,),
            ).fetchone()
        return self.get_committee_program(row["id"]) if row else None

    def claim_committee_day(self, program_id: str, trade_date: str) -> bool:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT state, attempts FROM committee_program_days
                WHERE program_id = ? AND trade_date = ?
                """,
                (program_id, trade_date),
            ).fetchone()
            if row and row["state"] in {"RUNNING", "COMPLETE"}:
                connection.rollback()
                return False
            now = utc_now()
            if row:
                connection.execute(
                    """
                    UPDATE committee_program_days
                    SET state = 'RUNNING', attempts = ?, error = NULL,
                        started_at = ?, completed_at = NULL
                    WHERE program_id = ? AND trade_date = ?
                    """,
                    (row["attempts"] + 1, now, program_id, trade_date),
                )
            else:
                day_id = hashlib.sha256(f"{program_id}|{trade_date}".encode()).hexdigest()[:20]
                connection.execute(
                    """
                    INSERT INTO committee_program_days (
                        id, program_id, trade_date, state, attempts, started_at
                    ) VALUES (?, ?, ?, 'RUNNING', 1, ?)
                    """,
                    (f"program_day_{day_id}", program_id, trade_date, now),
                )
            connection.execute(
                "UPDATE committee_programs SET state = 'RUNNING', updated_at = ? WHERE id = ?",
                (now, program_id),
            )
            connection.commit()
        return True

    def complete_committee_day(
        self, program_id: str, trade_date: str, snapshot_id: str, cycle_id: str
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE committee_program_days
                SET state = 'COMPLETE', snapshot_id = ?, cycle_id = ?,
                    completed_at = ?, error = NULL
                WHERE program_id = ? AND trade_date = ? AND state = 'RUNNING'
                """,
                (snapshot_id, cycle_id, utc_now(), program_id, trade_date),
            )
            connection.commit()

    def fail_committee_day(self, program_id: str, trade_date: str, error: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE committee_program_days
                SET state = 'FAILED', error = ?, completed_at = ?
                WHERE program_id = ? AND trade_date = ? AND state = 'RUNNING'
                """,
                (error[:500], utc_now(), program_id, trade_date),
            )
            connection.commit()

    def list_committee_days(self, program_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM committee_program_days
                WHERE program_id = ? ORDER BY trade_date
                """,
                (program_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_agent_evaluation(self, evaluation: dict[str, Any]) -> None:
        payload_json = json.dumps(
            evaluation, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO agent_evaluations (
                    id, program_id, trade_date, agent_code, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    evaluation["id"],
                    evaluation["program_id"],
                    evaluation["trade_date"],
                    evaluation["agent_code"],
                    payload_json,
                    utc_now(),
                ),
            )
            connection.commit()

    def list_agent_evaluations(self, program_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM agent_evaluations
                WHERE program_id = ? ORDER BY trade_date, agent_code
                """,
                (program_id,),
            ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def finish_committee_program(
        self, program_id: str, *, state: str, summary: dict[str, Any]
    ) -> dict[str, Any]:
        summary_json = json.dumps(
            summary, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                "SELECT state FROM committee_programs WHERE id = ?", (program_id,)
            ).fetchone()
            if previous is None:
                connection.rollback()
                raise KeyError(program_id)
            connection.execute(
                """
                UPDATE committee_programs
                SET state = ?, summary_json = ?, updated_at = ? WHERE id = ?
                """,
                (state, summary_json, utc_now(), program_id),
            )
            if state == "COMPLETE" and previous["state"] != "COMPLETE":
                self._append_audit_event(
                    connection,
                    actor="김투자",
                    action="committee.program.completed",
                    aggregate_type="CommitteeProgram",
                    aggregate_id=program_id,
                    payload={
                        "completed_days": summary["completed_days"],
                        "failed_days": summary["failed_days"],
                        "mode": summary["mode"],
                    },
                )
            connection.commit()
        return self.get_committee_program(program_id)

    def save_broker_account_snapshot(
        self,
        *,
        broker: str,
        environment: str,
        account_alias: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        payload_json = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        checksum = hashlib.sha256(payload_json.encode()).hexdigest()
        snapshot_id = f"broker_snap_{checksum[:16]}"
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT id FROM broker_account_snapshots WHERE checksum = ?", (checksum,)
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO broker_account_snapshots (
                        id, broker, environment, account_alias, checksum, payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot_id,
                        broker,
                        environment,
                        account_alias,
                        checksum,
                        payload_json,
                        utc_now(),
                    ),
                )
                self._append_audit_event(
                    connection,
                    actor="김데이터",
                    action="broker.account_snapshot.closed",
                    aggregate_type="BrokerAccountSnapshot",
                    aggregate_id=snapshot_id,
                    payload={"broker": broker, "environment": environment, "checksum": checksum},
                )
            else:
                snapshot_id = existing["id"]
            connection.commit()
        return self.get_broker_account_snapshot(snapshot_id)

    def get_broker_account_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM broker_account_snapshots WHERE id = ?", (snapshot_id,)
            ).fetchone()
        if row is None:
            raise KeyError(snapshot_id)
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        result["trading_enabled"] = False
        return result

    def create_shadow_order(
        self, payload: dict[str, Any], *, idempotency_key: str
    ) -> dict[str, Any]:
        payload_json = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        digest = hashlib.sha256(f"{idempotency_key}|{payload_json}".encode()).hexdigest()[:20]
        order_id = f"shadow_{digest}"
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT id FROM shadow_orders WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if existing is None:
                now = utc_now()
                connection.execute(
                    """
                    INSERT INTO shadow_orders (
                        id, mission_id, cycle_id, idempotency_key, payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        order_id,
                        payload["mission_id"],
                        payload["cycle_id"],
                        idempotency_key,
                        payload_json,
                        now,
                    ),
                )
                for event_type, event_payload in (
                    ("CREATED", {"source": payload["simulation_source"]}),
                    ("PRECHECKED", {"risk_checked": True, "trading_enabled": False}),
                    ("READY", {"broker_submitted": False}),
                ):
                    connection.execute(
                        """
                        INSERT INTO shadow_order_events (
                            id, order_id, event_type, payload_json, occurred_at
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            f"shadow_evt_{uuid4().hex}",
                            order_id,
                            event_type,
                            json.dumps(event_payload, ensure_ascii=False, sort_keys=True),
                            now,
                        ),
                    )
                self._append_audit_event(
                    connection,
                    actor="김주문",
                    action="shadow_order.ready",
                    aggregate_type="ShadowOrder",
                    aggregate_id=order_id,
                    payload={"symbol": payload["symbol"], "broker_submitted": False},
                )
            else:
                order_id = existing["id"]
            connection.commit()
        return self.get_shadow_order(order_id)

    def append_shadow_order_event(
        self, order_id: str, event_type: str, payload: dict[str, Any]
    ) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            order = connection.execute(
                "SELECT id FROM shadow_orders WHERE id = ?", (order_id,)
            ).fetchone()
            if order is None:
                connection.rollback()
                raise KeyError(order_id)
            connection.execute(
                """
                INSERT OR IGNORE INTO shadow_order_events (
                    id, order_id, event_type, payload_json, occurred_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    f"shadow_evt_{uuid4().hex}",
                    order_id,
                    event_type,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    utc_now(),
                ),
            )
            if event_type in {"SHADOW_FILLED", "SHADOW_CANCELLED"}:
                self._append_audit_event(
                    connection,
                    actor="김주문",
                    action=f"shadow_order.{event_type.lower()}",
                    aggregate_type="ShadowOrder",
                    aggregate_id=order_id,
                    payload={**payload, "broker_submitted": False},
                )
            connection.commit()

    def complete_shadow_fill(self, order_id: str, *, quantity: int, price_krw: int) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT OR IGNORE INTO shadow_fills (
                    id, order_id, quantity, price_krw, occurred_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (f"shadow_fill_{uuid4().hex}", order_id, quantity, price_krw, utc_now()),
            )
            inserted = connection.execute(
                """
                INSERT OR IGNORE INTO shadow_order_events (
                    id, order_id, event_type, payload_json, occurred_at
                ) VALUES (?, ?, 'SHADOW_FILLED', ?, ?)
                """,
                (
                    f"shadow_evt_{uuid4().hex}",
                    order_id,
                    json.dumps(
                        {
                            "fill_quantity": quantity,
                            "fill_price_krw": price_krw,
                            "broker_submitted": False,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    utc_now(),
                ),
            )
            if inserted.rowcount:
                self._append_audit_event(
                    connection,
                    actor="김주문",
                    action="shadow_order.shadow_filled",
                    aggregate_type="ShadowOrder",
                    aggregate_id=order_id,
                    payload={
                        "fill_quantity": quantity,
                        "fill_price_krw": price_krw,
                        "broker_submitted": False,
                    },
                )
            connection.commit()

    def get_shadow_order(self, order_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            order = connection.execute(
                "SELECT * FROM shadow_orders WHERE id = ?", (order_id,)
            ).fetchone()
            if order is None:
                raise KeyError(order_id)
            events = connection.execute(
                """
                SELECT event_type, payload_json, occurred_at FROM shadow_order_events
                WHERE order_id = ? ORDER BY rowid
                """,
                (order_id,),
            ).fetchall()
            fill = connection.execute(
                "SELECT quantity, price_krw, occurred_at FROM shadow_fills WHERE order_id = ?",
                (order_id,),
            ).fetchone()
        result = json.loads(order["payload_json"])
        result.update({"id": order["id"], "created_at": order["created_at"]})
        result["events"] = [
            {
                "state": event["event_type"],
                "payload": json.loads(event["payload_json"]),
                "occurred_at": event["occurred_at"],
            }
            for event in events
        ]
        result["state"] = result["events"][-1]["state"]
        result["fill"] = dict(fill) if fill else None
        result["broker_submitted"] = False
        result["trading_enabled"] = False
        return result

    def list_shadow_orders(self, mission_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id FROM shadow_orders WHERE mission_id = ?
                ORDER BY rowid DESC
                """,
                (mission_id,),
            ).fetchall()
        return [self.get_shadow_order(row["id"]) for row in rows]

    def save_broker_quote_snapshot(
        self, *, broker: str, symbol: str, observed_at: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        payload_json = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        checksum = hashlib.sha256(
            f"{broker}|{symbol}|{observed_at}|{payload_json}".encode()
        ).hexdigest()
        snapshot_id = f"quote_{checksum[:20]}"
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT OR IGNORE INTO broker_quote_snapshots (
                    id, broker, symbol, observed_at, checksum, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    broker,
                    symbol,
                    observed_at,
                    checksum,
                    payload_json,
                    utc_now(),
                ),
            )
            connection.commit()
        return self.get_broker_quote_snapshot(snapshot_id)

    def get_broker_quote_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM broker_quote_snapshots WHERE id = ?", (snapshot_id,)
            ).fetchone()
        if row is None:
            raise KeyError(snapshot_id)
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        result["trading_enabled"] = False
        return result

    def get_latest_broker_quote(self, symbol: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id FROM broker_quote_snapshots
                WHERE symbol = ? ORDER BY rowid DESC LIMIT 1
                """,
                (symbol,),
            ).fetchone()
        return self.get_broker_quote_snapshot(row["id"]) if row else None

    def save_reconciliation(
        self,
        *,
        mission_id: str,
        account_snapshot_id: str,
        status: str,
        details: dict[str, Any],
    ) -> dict[str, Any]:
        details_json = json.dumps(
            details, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        digest = hashlib.sha256(
            f"{mission_id}|{account_snapshot_id}|{details_json}".encode()
        ).hexdigest()[:20]
        reconciliation_id = f"recon_{digest}"
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT id FROM reconciliation_runs WHERE id = ?", (reconciliation_id,)
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO reconciliation_runs (
                        id, mission_id, account_snapshot_id, status, details_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        reconciliation_id,
                        mission_id,
                        account_snapshot_id,
                        status,
                        details_json,
                        utc_now(),
                    ),
                )
                self._append_audit_event(
                    connection,
                    actor="김주문",
                    action=f"reconciliation.{status.lower()}",
                    aggregate_type="Reconciliation",
                    aggregate_id=reconciliation_id,
                    payload={"mission_id": mission_id, "status": status},
                )
                if status != "PASS":
                    connection.execute(
                        """
                        UPDATE execution_controls
                        SET kill_switch_active = 1, automation_enabled = 0,
                            reason = '잔고 대사 불일치', updated_at = ?
                        WHERE mission_id = ?
                        """,
                        (utc_now(), mission_id),
                    )
            connection.commit()
        return self.get_reconciliation(reconciliation_id)

    def get_reconciliation(self, reconciliation_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM reconciliation_runs WHERE id = ?", (reconciliation_id,)
            ).fetchone()
        if row is None:
            raise KeyError(reconciliation_id)
        result = dict(row)
        result["details"] = json.loads(result.pop("details_json"))
        return result

    def get_latest_reconciliation(self, mission_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id FROM reconciliation_runs
                WHERE mission_id = ? ORDER BY rowid DESC LIMIT 1
                """,
                (mission_id,),
            ).fetchone()
        return self.get_reconciliation(row["id"]) if row else None

    def get_execution_control(self, mission_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM execution_controls WHERE mission_id = ?", (mission_id,)
            ).fetchone()
        if row is None:
            raise KeyError(mission_id)
        result = dict(row)
        result["kill_switch_active"] = bool(result["kill_switch_active"])
        result["automation_enabled"] = bool(result["automation_enabled"])
        return result

    def acquire_execution_lease(self, name: str, *, owner_id: str, ttl_seconds: int = 55) -> bool:
        now = datetime.now(UTC)
        expires_at = datetime.fromtimestamp(now.timestamp() + ttl_seconds, UTC).isoformat(
            timespec="seconds"
        )
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT owner_id, expires_at FROM execution_leases WHERE name = ?", (name,)
            ).fetchone()
            if (
                row is not None
                and row["owner_id"] != owner_id
                and datetime.fromisoformat(row["expires_at"]) > now
            ):
                connection.rollback()
                return False
            connection.execute(
                """
                INSERT INTO execution_leases (name, owner_id, expires_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    owner_id = excluded.owner_id,
                    expires_at = excluded.expires_at,
                    updated_at = excluded.updated_at
                """,
                (name, owner_id, expires_at, utc_now()),
            )
            connection.commit()
        return True

    def update_execution_control(
        self,
        mission_id: str,
        *,
        actor: str,
        reason: str,
        stage: str | None = None,
        kill_switch_active: bool | None = None,
        automation_enabled: bool | None = None,
    ) -> dict[str, Any]:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM execution_controls WHERE mission_id = ?", (mission_id,)
            ).fetchone()
            if row is None:
                connection.rollback()
                raise KeyError(mission_id)
            next_stage = stage or row["stage"]
            next_kill = (
                int(kill_switch_active)
                if kill_switch_active is not None
                else row["kill_switch_active"]
            )
            next_automation = (
                int(automation_enabled)
                if automation_enabled is not None
                else row["automation_enabled"]
            )
            if next_kill:
                next_automation = 0
            connection.execute(
                """
                UPDATE execution_controls
                SET stage = ?, kill_switch_active = ?, automation_enabled = ?,
                    reason = ?, updated_at = ? WHERE mission_id = ?
                """,
                (next_stage, next_kill, next_automation, reason, utc_now(), mission_id),
            )
            self._append_audit_event(
                connection,
                actor=actor,
                action="execution.control.changed",
                aggregate_type="ExecutionControl",
                aggregate_id=mission_id,
                payload={
                    "stage": next_stage,
                    "kill_switch_active": bool(next_kill),
                    "automation_enabled": bool(next_automation),
                    "reason": reason,
                },
            )
            connection.commit()
        return self.get_execution_control(mission_id)

    def create_live_order_intent(
        self, payload: dict[str, Any], *, idempotency_key: str, scope_hash: str
    ) -> dict[str, Any]:
        payload_json = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        digest = hashlib.sha256(
            f"{idempotency_key}|{scope_hash}|{payload_json}".encode()
        ).hexdigest()[:20]
        intent_id = f"intent_{digest}"
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT id FROM live_order_intents WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is None:
                now = utc_now()
                connection.execute(
                    """
                    INSERT INTO live_order_intents (
                        id, mission_id, shadow_order_id, idempotency_key,
                        scope_hash, payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        intent_id,
                        payload["mission_id"],
                        payload["shadow_order_id"],
                        idempotency_key,
                        scope_hash,
                        payload_json,
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO live_order_events (
                        id, intent_id, event_type, payload_json, occurred_at
                    ) VALUES (?, ?, 'CREATED', '{}', ?)
                    """,
                    (f"live_evt_{uuid4().hex}", intent_id, now),
                )
                self._append_audit_event(
                    connection,
                    actor="김주문",
                    action="live_order_intent.created",
                    aggregate_type="LiveOrderIntent",
                    aggregate_id=intent_id,
                    payload={
                        "symbol": payload["symbol"],
                        "stage": payload["stage"],
                        "scope_hash": scope_hash,
                    },
                )
            else:
                intent_id = existing["id"]
            connection.commit()
        return self.get_live_order_intent(intent_id)

    def append_live_order_event(
        self, intent_id: str, event_type: str, payload: dict[str, Any]
    ) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            inserted = connection.execute(
                """
                INSERT OR IGNORE INTO live_order_events (
                    id, intent_id, event_type, payload_json, occurred_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    f"live_evt_{uuid4().hex}",
                    intent_id,
                    event_type,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    utc_now(),
                ),
            )
            if inserted.rowcount:
                self._append_audit_event(
                    connection,
                    actor="EXECUTION_GUARDIAN",
                    action=f"live_order.{event_type.lower()}",
                    aggregate_type="LiveOrderIntent",
                    aggregate_id=intent_id,
                    payload=payload,
                )
            connection.commit()

    def record_owner_approval(
        self,
        intent_id: str,
        *,
        decision: str,
        scope_hash: str,
        expires_at: str,
        actor: str = "OWNER",
    ) -> dict[str, Any]:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            intent = connection.execute(
                "SELECT scope_hash FROM live_order_intents WHERE id = ?", (intent_id,)
            ).fetchone()
            if intent is None:
                connection.rollback()
                raise KeyError(intent_id)
            if intent["scope_hash"] != scope_hash:
                connection.rollback()
                raise ValueError("승인 범위 해시가 현재 주문안과 다릅니다.")
            existing = connection.execute(
                "SELECT decision FROM owner_approvals WHERE intent_id = ?", (intent_id,)
            ).fetchone()
            if existing and existing["decision"] != decision:
                connection.rollback()
                raise ValueError("이미 기록된 승인은 변경할 수 없습니다.")
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO owner_approvals (
                        id, intent_id, decision, scope_hash, expires_at, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"approval_{uuid4().hex}",
                        intent_id,
                        decision,
                        scope_hash,
                        expires_at,
                        utc_now(),
                    ),
                )
                self._append_audit_event(
                    connection,
                    actor=actor,
                    action=f"order.approval.{decision.lower()}",
                    aggregate_type="LiveOrderIntent",
                    aggregate_id=intent_id,
                    payload={
                        "scope_hash": scope_hash,
                        "expires_at": expires_at,
                        "approval_actor": actor,
                    },
                )
            connection.commit()
        return self.get_live_order_intent(intent_id)

    @staticmethod
    def _fifo_cost_basis(
        connection: sqlite3.Connection,
        mission_id: str,
        symbol: str,
        sell_quantity: int,
    ) -> tuple[int, int]:
        lots: list[list[int]] = []
        rows = connection.execute(
            """
            SELECT f.quantity, f.price_krw, i.payload_json
            FROM live_fills f
            JOIN live_order_intents i ON i.id = f.intent_id
            WHERE i.mission_id = ?
            ORDER BY f.occurred_at, f.rowid
            """,
            (mission_id,),
        ).fetchall()
        for row in rows:
            payload = json.loads(row["payload_json"])
            if payload.get("symbol") != symbol:
                continue
            quantity = int(row["quantity"])
            if payload.get("side") == "BUY":
                lots.append([quantity, int(row["price_krw"])])
                continue
            remaining = quantity
            while remaining > 0 and lots:
                used = min(remaining, lots[0][0])
                lots[0][0] -= used
                remaining -= used
                if lots[0][0] == 0:
                    lots.pop(0)

        remaining = sell_quantity
        cost_basis = 0
        while remaining > 0 and lots:
            used = min(remaining, lots[0][0])
            cost_basis += used * lots[0][1]
            lots[0][0] -= used
            remaining -= used
            if lots[0][0] == 0:
                lots.pop(0)
        return cost_basis, remaining

    def record_live_fill(
        self,
        intent_id: str,
        *,
        broker_order_no: str,
        quantity: int,
        price_krw: int,
        fee_krw: int,
        tax_krw: int,
        occurred_at: str,
        broker_fill_key: str | None = None,
    ) -> dict[str, Any]:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            intent = connection.execute(
                "SELECT mission_id, payload_json FROM live_order_intents WHERE id = ?",
                (intent_id,),
            ).fetchone()
            if intent is None:
                connection.rollback()
                raise KeyError(intent_id)
            intent_payload = json.loads(intent["payload_json"])
            stable_fill_key = broker_fill_key or hashlib.sha256(
                (
                    f"{broker_order_no}|{quantity}|{price_krw}|{fee_krw}|"
                    f"{tax_krw}|{occurred_at}"
                ).encode()
            ).hexdigest()
            fill_id = f"live_fill_{uuid4().hex}"
            cost_basis = 0
            unmatched_quantity = 0
            if intent_payload["side"] == "SELL":
                cost_basis, unmatched_quantity = self._fifo_cost_basis(
                    connection,
                    intent["mission_id"],
                    intent_payload["symbol"],
                    quantity,
                )
            inserted = connection.execute(
                """
                INSERT OR IGNORE INTO live_fills (
                    id, intent_id, broker_order_no, broker_fill_key, quantity,
                    price_krw, fee_krw, tax_krw, occurred_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fill_id,
                    intent_id,
                    broker_order_no,
                    stable_fill_key,
                    quantity,
                    price_krw,
                    fee_krw,
                    tax_krw,
                    occurred_at,
                ),
            )
            if inserted.rowcount:
                gross_value = quantity * price_krw
                total_cost = fee_krw + tax_krw
                if intent_payload["side"] == "BUY":
                    postings = [
                        ("AVAILABLE_CASH", -(gross_value + total_cost)),
                        ("EXPOSED_COST", gross_value),
                        ("REALIZED_PNL", total_cost),
                    ]
                else:
                    net_proceeds = gross_value - total_cost
                    postings = [
                        ("AVAILABLE_CASH", net_proceeds),
                        ("EXPOSED_COST", -cost_basis),
                        ("REALIZED_PNL", cost_basis - net_proceeds),
                    ]
                postings = [(code, amount) for code, amount in postings if amount != 0]
                self._append_ledger_transaction(
                    connection,
                    mission_id=intent["mission_id"],
                    kind=f"LIVE_{intent_payload['side']}_FILL",
                    correlation_id=fill_id,
                    note=(
                        f"키움 실체결 {intent_payload['side']} "
                        f"{intent_payload['symbol']} {quantity}주"
                    ),
                    postings=postings,
                )
                self._append_audit_event(
                    connection,
                    actor="EXECUTION_GUARDIAN",
                    action="fill.received",
                    aggregate_type="LiveOrderIntent",
                    aggregate_id=intent_id,
                    payload={
                        "broker_order_no": broker_order_no,
                        "quantity": quantity,
                        "price_krw": price_krw,
                        "fee_krw": fee_krw,
                        "tax_krw": tax_krw,
                        "unmatched_sell_quantity": unmatched_quantity,
                    },
                )
            connection.commit()
        return {
            "inserted": bool(inserted.rowcount),
            "fill_id": fill_id if inserted.rowcount else None,
            "cost_basis_krw": cost_basis,
            "unmatched_sell_quantity": unmatched_quantity,
        }

    def get_live_order_intent(self, intent_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            intent = connection.execute(
                "SELECT * FROM live_order_intents WHERE id = ?", (intent_id,)
            ).fetchone()
            if intent is None:
                raise KeyError(intent_id)
            events = connection.execute(
                """
                SELECT event_type, payload_json, occurred_at FROM live_order_events
                WHERE intent_id = ? ORDER BY rowid
                """,
                (intent_id,),
            ).fetchall()
            approval = connection.execute(
                "SELECT * FROM owner_approvals WHERE intent_id = ?", (intent_id,)
            ).fetchone()
            fills = connection.execute(
                "SELECT * FROM live_fills WHERE intent_id = ? ORDER BY rowid", (intent_id,)
            ).fetchall()
        result = json.loads(intent["payload_json"])
        result.update(
            {
                "id": intent["id"],
                "scope_hash": intent["scope_hash"],
                "created_at": intent["created_at"],
            }
        )
        result["events"] = [
            {
                "state": event["event_type"],
                "payload": json.loads(event["payload_json"]),
                "occurred_at": event["occurred_at"],
            }
            for event in events
        ]
        result["state"] = result["events"][-1]["state"]
        result["approval"] = dict(approval) if approval else None
        result["fills"] = [dict(fill) for fill in fills]
        return result

    def list_live_order_intents(self, mission_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id FROM live_order_intents
                WHERE mission_id = ? ORDER BY rowid DESC
                """,
                (mission_id,),
            ).fetchall()
        return [self.get_live_order_intent(row["id"]) for row in rows]

    def record_operating_day(
        self,
        *,
        mission_id: str,
        trade_date: str,
        mode: str,
        reconciliation_status: str,
        risk_violations: int,
        duplicate_orders: int,
        decision_count: int,
        fill_count: int,
        net_pnl_krw: int,
    ) -> None:
        material = f"{mission_id}|{trade_date}|{mode}"
        day_id = f"opday_{hashlib.sha256(material.encode()).hexdigest()[:20]}"
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO operating_days (
                    id, mission_id, trade_date, mode, reconciliation_status,
                    risk_violations, duplicate_orders, decision_count, fill_count,
                    net_pnl_krw, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(mission_id, trade_date, mode) DO UPDATE SET
                    reconciliation_status = CASE
                        WHEN excluded.reconciliation_status = 'FAIL'
                             OR operating_days.reconciliation_status = 'FAIL' THEN 'FAIL'
                        ELSE excluded.reconciliation_status END,
                    risk_violations = MAX(operating_days.risk_violations, excluded.risk_violations),
                    duplicate_orders = MAX(
                        operating_days.duplicate_orders, excluded.duplicate_orders
                    ),
                    decision_count = MAX(operating_days.decision_count, excluded.decision_count),
                    fill_count = MAX(operating_days.fill_count, excluded.fill_count),
                    net_pnl_krw = excluded.net_pnl_krw
                """,
                (
                    day_id,
                    mission_id,
                    trade_date,
                    mode,
                    reconciliation_status,
                    risk_violations,
                    duplicate_orders,
                    decision_count,
                    fill_count,
                    net_pnl_krw,
                    utc_now(),
                ),
            )
            connection.commit()

    def list_operating_days(self, mission_id: str, mode: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM operating_days
                WHERE mission_id = ? AND mode = ?
                ORDER BY trade_date, rowid
                """,
                (mission_id, mode),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_pilot_review(
        self,
        mission_id: str,
        *,
        checkpoint_day: int,
        candidate_version: str,
        report: dict[str, Any],
    ) -> dict[str, Any]:
        material = f"{mission_id}|{checkpoint_day}|{candidate_version}"
        review_id = f"pilot_review_{hashlib.sha256(material.encode()).hexdigest()[:20]}"
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT id FROM pilot_reviews
                WHERE mission_id = ? AND checkpoint_day = ?
                """,
                (mission_id, checkpoint_day),
            ).fetchone()
            if existing is not None:
                connection.rollback()
                return self.get_pilot_review(existing["id"])
            connection.execute(
                """
                INSERT OR IGNORE INTO pilot_reviews (
                    id, mission_id, checkpoint_day, candidate_version,
                    status, report_json, created_at
                ) VALUES (?, ?, ?, ?, 'PENDING_OWNER', ?, ?)
                """,
                (
                    review_id,
                    mission_id,
                    checkpoint_day,
                    candidate_version,
                    json.dumps(report, ensure_ascii=False, sort_keys=True),
                    utc_now(),
                ),
            )
            self._append_audit_event(
                connection,
                actor="김감사",
                action="pilot.review.frozen",
                aggregate_type="PilotReview",
                aggregate_id=review_id,
                payload={
                    "mission_id": mission_id,
                    "checkpoint_day": checkpoint_day,
                    "candidate_version": candidate_version,
                    "automatic_apply": False,
                },
            )
            connection.commit()
        return self.get_pilot_review(review_id)

    def get_pilot_review(self, review_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM pilot_reviews WHERE id = ?", (review_id,)
            ).fetchone()
        if row is None:
            raise KeyError(review_id)
        return {**dict(row), "report": json.loads(row["report_json"])}

    def list_pilot_reviews(self, mission_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id FROM pilot_reviews
                WHERE mission_id = ? ORDER BY checkpoint_day
                """,
                (mission_id,),
            ).fetchall()
        return [self.get_pilot_review(row["id"]) for row in rows]

    def save_operational_run(
        self,
        *,
        mission_id: str,
        trade_date: str,
        state: str,
        current_stage: str,
        summary: dict[str, Any],
        run_type: str = "DAILY_SHADOW",
    ) -> dict[str, Any]:
        material = f"{mission_id}|{trade_date}|{run_type}"
        run_id = f"oprun_{hashlib.sha256(material.encode()).hexdigest()[:20]}"
        now = utc_now()
        summary_json = json.dumps(summary, ensure_ascii=False, sort_keys=True)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO operational_runs (
                    id, mission_id, trade_date, run_type, state, current_stage,
                    summary_json, started_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(mission_id, trade_date, run_type) DO UPDATE SET
                    state = excluded.state,
                    current_stage = excluded.current_stage,
                    summary_json = excluded.summary_json,
                    completed_at = excluded.completed_at
                """,
                (
                    run_id,
                    mission_id,
                    trade_date,
                    run_type,
                    state,
                    current_stage,
                    summary_json,
                    now,
                    now if state in {"COMPLETE", "FAILED", "SKIPPED"} else None,
                ),
            )
            self._append_audit_event(
                connection,
                actor="김운영",
                action=f"operations.run.{state.lower()}",
                aggregate_type="OperationalRun",
                aggregate_id=run_id,
                payload={"trade_date": trade_date, "run_type": run_type, "state": state},
            )
            connection.commit()
        return self.get_operational_run(run_id)

    def get_operational_run(self, run_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM operational_runs WHERE id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return {**dict(row), "summary": json.loads(row["summary_json"])}

    def latest_operational_run(self, mission_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id FROM operational_runs
                WHERE mission_id = ? ORDER BY trade_date DESC, rowid DESC LIMIT 1
                """,
                (mission_id,),
            ).fetchone()
        return self.get_operational_run(row["id"]) if row else None

    def enqueue_notification(
        self,
        mission_id: str,
        *,
        severity: str,
        category: str,
        title: str,
        body: str,
        dedupe_key: str,
    ) -> dict[str, Any]:
        notification_id = f"notice_{hashlib.sha256(dedupe_key.encode()).hexdigest()[:20]}"
        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO notification_outbox (
                    id, mission_id, severity, category, title, body,
                    dedupe_key, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    notification_id,
                    mission_id,
                    severity,
                    category,
                    title[:120],
                    body[:1000],
                    dedupe_key,
                    utc_now(),
                ),
            )
            connection.commit()
        return self.get_notification(notification_id)

    def get_notification(self, notification_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM notification_outbox WHERE id = ?", (notification_id,)
            ).fetchone()
        if row is None:
            raise KeyError(notification_id)
        return dict(row)

    def list_notifications(self, mission_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM notification_outbox WHERE mission_id = ?
                ORDER BY rowid DESC LIMIT ?
                """,
                (mission_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def acknowledge_notification(self, notification_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE notification_outbox SET acknowledged_at = COALESCE(acknowledged_at, ?)
                WHERE id = ?
                """,
                (utc_now(), notification_id),
            )
            connection.commit()
        return self.get_notification(notification_id)

    @staticmethod
    def _news_event_payload(row: Any) -> dict[str, Any]:
        payload = dict(row)
        payload["review_required"] = bool(payload["review_required"])
        payload["blocks_new_buy"] = bool(payload["blocks_new_buy"])
        payload["analysis"] = json.loads(payload.pop("analysis_json"))
        return payload

    def save_news_event(self, event: dict[str, Any]) -> dict[str, Any]:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO news_events (
                    id, source, source_event_id, symbol, corp_code, company_name,
                    title, category, severity, sentiment, review_required,
                    blocks_new_buy, published_at, collected_at, url, analysis_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event["id"],
                    event["source"],
                    event["source_event_id"],
                    event["symbol"],
                    event["corp_code"],
                    event["company_name"],
                    event["title"][:300],
                    event["category"],
                    event["severity"],
                    event["sentiment"],
                    int(bool(event["review_required"])),
                    int(bool(event["blocks_new_buy"])),
                    event["published_at"],
                    event["collected_at"],
                    event["url"],
                    json.dumps(event["analysis"], ensure_ascii=False, sort_keys=True),
                ),
            )
            inserted = cursor.rowcount > 0
            if inserted:
                self._append_audit_event(
                    connection,
                    actor="김뉴스",
                    action="news.event.collected",
                    aggregate_type="NewsEvent",
                    aggregate_id=event["id"],
                    payload={
                        "source": event["source"],
                        "symbol": event["symbol"],
                        "severity": event["severity"],
                        "blocks_new_buy": bool(event["blocks_new_buy"]),
                    },
                )
            connection.commit()
        return {**self.get_news_event(event["id"]), "inserted": inserted}

    def get_news_event(self, event_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM news_events WHERE id = ?", (event_id,)
            ).fetchone()
        if row is None:
            raise KeyError(event_id)
        return self._news_event_payload(row)

    def list_news_events(
        self,
        *,
        limit: int = 100,
        symbol: str | None = None,
        unresolved_only: bool = False,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if symbol:
            clauses.append("symbol = ?")
            parameters.append(symbol)
        if unresolved_only:
            clauses.append("review_required = 1 AND acknowledged_at IS NULL")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(limit)
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM news_events {where}
                ORDER BY collected_at DESC, rowid DESC LIMIT ?
                """,  # noqa: S608 - clauses are fixed strings selected above
                parameters,
            ).fetchall()
        return [self._news_event_payload(row) for row in rows]

    def active_news_blocks(
        self,
        symbol: str,
        *,
        lookback_hours: int = 72,
        now_utc: datetime | None = None,
    ) -> list[dict[str, Any]]:
        reference = (now_utc or datetime.now(UTC)).astimezone(UTC)
        cutoff = (reference - timedelta(hours=lookback_hours)).isoformat(
            timespec="seconds"
        )
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM news_events
                WHERE symbol = ? AND blocks_new_buy = 1
                  AND (severity = 'CRITICAL' OR acknowledged_at IS NULL)
                  AND collected_at >= ?
                ORDER BY collected_at DESC, rowid DESC
                """,
                (symbol, cutoff),
            ).fetchall()
        return [self._news_event_payload(row) for row in rows]

    def acknowledge_news_event(self, event_id: str, *, actor: str = "OWNER") -> dict[str, Any]:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT id, acknowledged_at FROM news_events WHERE id = ?", (event_id,)
            ).fetchone()
            if row is None:
                connection.rollback()
                raise KeyError(event_id)
            if row["acknowledged_at"]:
                connection.rollback()
                return self.get_news_event(event_id)
            acknowledged_at = utc_now()
            connection.execute(
                """
                UPDATE news_events
                SET acknowledged_at = COALESCE(acknowledged_at, ?),
                    acknowledged_by = COALESCE(acknowledged_by, ?)
                WHERE id = ?
                """,
                (acknowledged_at, actor, event_id),
            )
            self._append_audit_event(
                connection,
                actor=actor,
                action="news.event.acknowledged",
                aggregate_type="NewsEvent",
                aggregate_id=event_id,
                payload={"acknowledged": True},
            )
            connection.commit()
        return self.get_news_event(event_id)

    def save_news_poll_run(self, run: dict[str, Any]) -> dict[str, Any]:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO news_poll_runs (
                    id, source, state, watched_symbols, fetched_count,
                    inserted_count, blocking_count, error, started_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run["id"],
                    run["source"],
                    run["state"],
                    run["watched_symbols"],
                    run["fetched_count"],
                    run["inserted_count"],
                    run["blocking_count"],
                    run.get("error"),
                    run["started_at"],
                    run["completed_at"],
                ),
            )
            connection.commit()
        return run

    def latest_news_poll_run(self, source: str = "OPENDART_OFFICIAL") -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM news_poll_runs WHERE source = ?
                ORDER BY completed_at DESC, rowid DESC LIMIT 1
                """,
                (source,),
            ).fetchone()
        return dict(row) if row else None

    def save_strategy_evaluation(
        self,
        mission_id: str,
        *,
        strategy_id: str,
        role: str,
        verdict: str,
        metrics: dict[str, Any],
    ) -> dict[str, Any]:
        now = utc_now()
        material = f"{mission_id}|{strategy_id}|{role}|{now}"
        evaluation_id = f"eval_{hashlib.sha256(material.encode()).hexdigest()[:20]}"
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO strategy_evaluations (
                    id, mission_id, strategy_id, role, verdict, metrics_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evaluation_id,
                    mission_id,
                    strategy_id,
                    role,
                    verdict,
                    json.dumps(metrics, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
            connection.commit()
        return {
            "id": evaluation_id,
            "mission_id": mission_id,
            "strategy_id": strategy_id,
            "role": role,
            "verdict": verdict,
            "metrics": metrics,
            "created_at": now,
        }

    def list_strategy_evaluations(self, mission_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM strategy_evaluations WHERE mission_id = ?
                ORDER BY rowid DESC LIMIT 50
                """,
                (mission_id,),
            ).fetchall()
        return [{**dict(row), "metrics": json.loads(row["metrics_json"])} for row in rows]

    def save_trade_outcome(self, outcome: dict[str, Any]) -> dict[str, Any]:
        participants_json = json.dumps(
            outcome["participants"], ensure_ascii=False, sort_keys=True
        )
        postmortem_json = json.dumps(
            outcome["postmortem"], ensure_ascii=False, sort_keys=True
        )
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT id FROM trade_outcomes WHERE source_type = ? AND source_id = ?",
                (outcome["source_type"], outcome["source_id"]),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO trade_outcomes (
                        id, mission_id, source_type, source_id, cycle_id, strategy_id,
                        symbol, regime, entry_at, exit_at, quantity, entry_price_krw,
                        exit_price_krw, fee_krw, tax_krw, net_pnl_krw, return_bps,
                        slippage_bps, participants_json, postmortem_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        outcome["id"],
                        outcome["mission_id"],
                        outcome["source_type"],
                        outcome["source_id"],
                        outcome.get("cycle_id"),
                        outcome["strategy_id"],
                        outcome["symbol"],
                        outcome["regime"],
                        outcome["entry_at"],
                        outcome["exit_at"],
                        outcome["quantity"],
                        outcome["entry_price_krw"],
                        outcome["exit_price_krw"],
                        outcome["fee_krw"],
                        outcome["tax_krw"],
                        outcome["net_pnl_krw"],
                        outcome["return_bps"],
                        outcome["slippage_bps"],
                        participants_json,
                        postmortem_json,
                        utc_now(),
                    ),
                )
                self._append_audit_event(
                    connection,
                    actor="김감사",
                    action="performance.trade_outcome.closed",
                    aggregate_type="TradeOutcome",
                    aggregate_id=outcome["id"],
                    payload={
                        "source_type": outcome["source_type"],
                        "strategy_id": outcome["strategy_id"],
                        "symbol": outcome["symbol"],
                        "net_pnl_krw": outcome["net_pnl_krw"],
                    },
                )
                outcome_id = outcome["id"]
            else:
                outcome_id = existing["id"]
            connection.commit()
        return self.get_trade_outcome(outcome_id)

    def get_trade_outcome(self, outcome_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM trade_outcomes WHERE id = ?", (outcome_id,)
            ).fetchone()
        if row is None:
            raise KeyError(outcome_id)
        result = dict(row)
        result["participants"] = json.loads(result.pop("participants_json"))
        result["postmortem"] = json.loads(result.pop("postmortem_json"))
        return result

    def list_trade_outcomes(self, mission_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id FROM trade_outcomes WHERE mission_id = ?
                ORDER BY exit_at, rowid
                """,
                (mission_id,),
            ).fetchall()
        return [self.get_trade_outcome(row["id"]) for row in rows]

    def lock_profit_milestone(
        self,
        mission_id: str,
        *,
        target_multiple: int,
        target_equity_krw: int,
        locked_amount_krw: int,
        trigger_outcome_id: str,
    ) -> dict[str, Any]:
        if locked_amount_krw <= 0:
            raise ValueError("이익 금고 잠금액은 0원보다 커야 합니다.")
        material = f"{mission_id}|{target_multiple}"
        milestone_id = f"vault_{hashlib.sha256(material.encode()).hexdigest()[:20]}"
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT * FROM profit_vault_milestones
                WHERE mission_id = ? AND target_multiple = ?
                """,
                (mission_id, target_multiple),
            ).fetchone()
            if existing is None:
                available = connection.execute(
                    """
                    SELECT COALESCE(SUM(p.amount_krw), 0) AS balance
                    FROM ledger_postings p
                    JOIN ledger_transactions t ON t.id = p.transaction_id
                    WHERE t.mission_id = ? AND p.account_code = 'AVAILABLE_CASH'
                    """,
                    (mission_id,),
                ).fetchone()["balance"]
                if available < locked_amount_krw:
                    connection.rollback()
                    raise ValueError("가용 현금보다 많은 금액을 이익 금고에 잠글 수 없습니다.")
                now = utc_now()
                connection.execute(
                    """
                    INSERT INTO profit_vault_milestones (
                        id, mission_id, target_multiple, target_equity_krw,
                        locked_amount_krw, trigger_outcome_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        milestone_id,
                        mission_id,
                        target_multiple,
                        target_equity_krw,
                        locked_amount_krw,
                        trigger_outcome_id,
                        now,
                    ),
                )
                self._append_ledger_transaction(
                    connection,
                    mission_id=mission_id,
                    kind="PROFIT_VAULT_LOCKED",
                    correlation_id=milestone_id,
                    note=f"{target_multiple}배 사다리 달성 이익 20% 자동 금고",
                    postings=[
                        ("AVAILABLE_CASH", -locked_amount_krw),
                        ("RESERVED_PROFIT", locked_amount_krw),
                    ],
                )
                self._append_audit_event(
                    connection,
                    actor="김안전",
                    action="profit_vault.locked",
                    aggregate_type="ProfitVaultMilestone",
                    aggregate_id=milestone_id,
                    payload={
                        "target_multiple": target_multiple,
                        "locked_amount_krw": locked_amount_krw,
                        "trigger_outcome_id": trigger_outcome_id,
                    },
                )
            connection.commit()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM profit_vault_milestones WHERE id = ?", (milestone_id,)
            ).fetchone()
        if row is None:
            raise KeyError(milestone_id)
        return dict(row)

    def get_profit_vault_milestones(self, mission_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM profit_vault_milestones
                WHERE mission_id = ? ORDER BY target_multiple
                """,
                (mission_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_change_request(
        self,
        mission_id: str,
        *,
        change_type: str,
        target_id: str,
        proposal: dict[str, Any],
        reason: str,
    ) -> dict[str, Any]:
        proposal_json = json.dumps(
            proposal, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        scope_hash = hashlib.sha256(
            f"{mission_id}|{change_type}|{target_id}|{proposal_json}".encode()
        ).hexdigest()
        request_id = f"change_{scope_hash[:20]}"
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            inserted = connection.execute(
                """
                INSERT OR IGNORE INTO change_requests (
                    id, mission_id, change_type, target_id, scope_hash,
                    proposal_json, status, reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'PENDING_OWNER', ?, ?)
                """,
                (
                    request_id,
                    mission_id,
                    change_type,
                    target_id,
                    scope_hash,
                    proposal_json,
                    reason,
                    utc_now(),
                ),
            )
            if inserted.rowcount:
                self._append_audit_event(
                    connection,
                    actor="김감사",
                    action="change_request.proposed",
                    aggregate_type="ChangeRequest",
                    aggregate_id=request_id,
                    payload={"change_type": change_type, "target_id": target_id},
                )
            connection.commit()
        return self.get_change_request(request_id)

    def decide_change_request(
        self, request_id: str, *, decision: str, scope_hash: str
    ) -> dict[str, Any]:
        if decision not in {"APPROVE", "REJECT"}:
            raise ValueError("변경 요청 결정은 APPROVE 또는 REJECT여야 합니다.")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM change_requests WHERE id = ?", (request_id,)
            ).fetchone()
            if row is None:
                connection.rollback()
                raise KeyError(request_id)
            if row["scope_hash"] != scope_hash:
                connection.rollback()
                raise ValueError("변경 승인 범위 해시가 현재 제안과 다릅니다.")
            target_status = "APPROVED" if decision == "APPROVE" else "REJECTED"
            if row["status"] not in {"PENDING_OWNER", target_status}:
                connection.rollback()
                raise ValueError("이미 결정된 변경 요청은 뒤집을 수 없습니다.")
            if row["status"] == "PENDING_OWNER":
                connection.execute(
                    "UPDATE change_requests SET status = ?, decided_at = ? WHERE id = ?",
                    (target_status, utc_now(), request_id),
                )
                self._append_audit_event(
                    connection,
                    actor="OWNER",
                    action=f"change_request.{target_status.lower()}",
                    aggregate_type="ChangeRequest",
                    aggregate_id=request_id,
                    payload={"scope_hash": scope_hash},
                )
            connection.commit()
        return self.get_change_request(request_id)

    def get_change_request(self, request_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM change_requests WHERE id = ?", (request_id,)
            ).fetchone()
        if row is None:
            raise KeyError(request_id)
        return {**dict(row), "proposal": json.loads(row["proposal_json"])}

    def list_change_requests(self, mission_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id FROM change_requests WHERE mission_id = ?
                ORDER BY rowid DESC LIMIT 100
                """,
                (mission_id,),
            ).fetchall()
        return [self.get_change_request(row["id"]) for row in rows]

    def save_recovery_run(
        self,
        *,
        kind: str,
        state: str,
        details: dict[str, Any],
        artifact_path: str | None = None,
        checksum: str | None = None,
    ) -> dict[str, Any]:
        run_id = f"recovery_{uuid4().hex}"
        created_at = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO recovery_runs (
                    id, kind, state, artifact_path, checksum, details_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    kind,
                    state,
                    artifact_path,
                    checksum,
                    json.dumps(details, ensure_ascii=False, sort_keys=True),
                    created_at,
                ),
            )
            connection.commit()
        return {
            "id": run_id,
            "kind": kind,
            "state": state,
            "artifact_path": artifact_path,
            "checksum": checksum,
            "details": details,
            "created_at": created_at,
        }

    def latest_recovery_run(self, kind: str | None = None) -> dict[str, Any] | None:
        with self.connect() as connection:
            if kind:
                row = connection.execute(
                    """
                    SELECT * FROM recovery_runs WHERE kind = ?
                    ORDER BY rowid DESC LIMIT 1
                    """,
                    (kind,),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM recovery_runs ORDER BY rowid DESC LIMIT 1"
                ).fetchone()
        if row is None:
            return None
        return {**dict(row), "details": json.loads(row["details_json"])}

    def save_drill_run(
        self, *, scenario: str, state: str, details: dict[str, Any]
    ) -> dict[str, Any]:
        drill_id = f"drill_{uuid4().hex}"
        created_at = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO drill_runs (id, scenario, state, details_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    drill_id,
                    scenario,
                    state,
                    json.dumps(details, ensure_ascii=False, sort_keys=True),
                    created_at,
                ),
            )
            connection.commit()
        return {
            "id": drill_id,
            "scenario": scenario,
            "state": state,
            "details": details,
            "created_at": created_at,
        }

    def list_drill_runs(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM drill_runs ORDER BY rowid DESC LIMIT 50"
            ).fetchall()
        return [{**dict(row), "details": json.loads(row["details_json"])} for row in rows]

    def operating_metrics(self, mission_id: str, mode: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS operating_days,
                       COALESCE(SUM(decision_count), 0) AS decisions,
                       COALESCE(SUM(fill_count), 0) AS fills,
                       COALESCE(SUM(risk_violations), 0) AS risk_violations,
                       COALESCE(SUM(duplicate_orders), 0) AS duplicate_orders,
                       COALESCE(SUM(net_pnl_krw), 0) AS net_pnl_krw,
                       COALESCE(SUM(CASE WHEN reconciliation_status != 'PASS' THEN 1 ELSE 0 END), 0)
                           AS reconciliation_failures
                FROM operating_days WHERE mission_id = ? AND mode = ?
                """,
                (mission_id, mode),
            ).fetchone()
            incidents = connection.execute(
                """
                SELECT COUNT(*) AS count FROM incidents
                WHERE mission_id = ? AND severity = 'CRITICAL' AND resolved_at IS NULL
                """,
                (mission_id,),
            ).fetchone()
        return {**dict(row), "open_critical_incidents": incidents["count"]}

    def create_incident(
        self, mission_id: str, *, severity: str, code: str, detail: str
    ) -> dict[str, Any]:
        incident_id = f"incident_{uuid4().hex}"
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO incidents (
                    id, mission_id, severity, code, detail, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (incident_id, mission_id, severity, code, detail[:500], utc_now()),
            )
            connection.execute(
                """
                UPDATE execution_controls
                SET stage = CASE WHEN stage IN ('L1', 'L2') THEN 'R1' ELSE stage END,
                    kill_switch_active = 1, automation_enabled = 0,
                    reason = ?, updated_at = ? WHERE mission_id = ?
                """,
                (f"사고 {code}", utc_now(), mission_id),
            )
            self._append_audit_event(
                connection,
                actor="EXECUTION_GUARDIAN",
                action="incident.created",
                aggregate_type="Incident",
                aggregate_id=incident_id,
                payload={"severity": severity, "code": code},
            )
            connection.commit()
        return self.get_incident(incident_id)

    def get_incident(self, incident_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM incidents WHERE id = ?", (incident_id,)
            ).fetchone()
        if row is None:
            raise KeyError(incident_id)
        return dict(row)

    def list_incidents(self, mission_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM incidents WHERE mission_id = ? ORDER BY rowid DESC",
                (mission_id,),
            ).fetchall()
        return [dict(row) for row in rows]
