from datetime import date

from fastapi.testclient import TestClient

from moneygun_api.execution import reconcile_broker_orders
from moneygun_api.main import create_app
from moneygun_api.operations import verify_accounting_and_audit
from moneygun_api.performance import (
    autonomy_readiness,
    close_shadow_trade,
    deployment_readiness,
    performance_report,
    runtime_monitor,
    shadow_exit_schedule,
    strategy_comparison,
    sync_live_trade_outcomes,
)
from moneygun_api.research import build_fixture_snapshot, run_committee_cycle
from moneygun_api.storage import Database, _postgres_sql, utc_now


def _database_with_cycle(tmp_path) -> tuple[Database, dict[str, object]]:
    database = Database(tmp_path / "maturity.sqlite3")
    database.initialize()
    snapshot = database.save_snapshot(build_fixture_snapshot())
    cycle = database.save_cycle(
        run_committee_cycle(database.get_mission("mission_focus_001"), snapshot)
    )
    return database, cycle


def _shadow(
    database: Database,
    cycle: dict[str, object],
    *,
    side: str,
    idempotency_key: str,
) -> dict[str, object]:
    return database.create_shadow_order(
        {
            "mission_id": "mission_focus_001",
            "cycle_id": cycle["id"],
            "trade_date": "2026-01-02",
            "next_session_date": "2026-01-05",
            "symbol": "005930",
            "name": "테스트 주식",
            "side": side,
            "quantity": 5,
            "signal_close_krw": 10_000,
            "limit_price_krw": 10_000 if side == "BUY" else 30_000,
            "invalidation_price_krw": 9_000,
            "simulation_source": "KIWOOM_OFFICIAL_QUOTE",
            "broker_submitted": False,
            "trading_enabled": False,
        },
        idempotency_key=idempotency_key,
    )


def _intent(
    database: Database,
    shadow: dict[str, object],
    *,
    side: str,
    idempotency_key: str,
) -> dict[str, object]:
    payload = {
        "mission_id": "mission_focus_001",
        "shadow_order_id": shadow["id"],
        "stage": "L1",
        "strategy_id": "FOCUS-MOMENTUM-KR-v1",
        "entry_style": "TEST",
        "symbol": "005930",
        "name": "테스트 주식",
        "side": side,
        "quantity": 5,
        "limit_price_krw": 10_000 if side == "BUY" else 30_000,
        "invalidation_price_krw": 9_000,
        "order_value_krw": 50_000 if side == "BUY" else 150_000,
        "precheck_blockers": [],
        "real_submission_attempted": False,
    }
    return database.create_live_order_intent(
        payload,
        idempotency_key=idempotency_key,
        scope_hash="a" * 64 if side == "BUY" else "b" * 64,
    )


def test_shadow_close_creates_idempotent_postmortem_and_attribution(tmp_path) -> None:
    database, cycle = _database_with_cycle(tmp_path)
    shadow = _shadow(
        database, cycle, side="BUY", idempotency_key="shadow-performance-buy"
    )
    database.complete_shadow_fill(shadow["id"], quantity=2, price_krw=10_000)
    database.save_broker_quote_snapshot(
        broker="KIWOOM",
        symbol="005930",
        observed_at=utc_now(),
        payload={
            "source": "KIWOOM_OFFICIAL_REST",
            "current_price_krw": 12_000,
        },
    )

    first = close_shadow_trade(database, shadow["id"])
    second = close_shadow_trade(database, shadow["id"])
    report = performance_report(database, "mission_focus_001")

    assert first["id"] == second["id"]
    assert first["net_pnl_krw"] > 0
    assert first["postmortem"]["causal_attribution_allowed"] is False
    assert len(first["participants"]) == 9
    assert report["metrics"]["closed_trades"] == 1
    assert report["pending_shadow_exits"] == 0
    assert runtime_monitor(database, "mission_focus_001")["state"] == (
        "INSUFFICIENT_EVIDENCE"
    )


def test_live_buy_sell_posts_ledger_creates_fifo_outcome_and_locks_profit(tmp_path) -> None:
    database, cycle = _database_with_cycle(tmp_path)
    buy_shadow = _shadow(
        database, cycle, side="BUY", idempotency_key="shadow-ledger-buy"
    )
    sell_shadow = _shadow(
        database, cycle, side="SELL", idempotency_key="shadow-ledger-sell"
    )
    buy = _intent(database, buy_shadow, side="BUY", idempotency_key="intent-ledger-buy")
    sell = _intent(database, sell_shadow, side="SELL", idempotency_key="intent-ledger-sell")

    database.record_live_fill(
        buy["id"],
        broker_order_no="B1",
        broker_fill_key="B1-F1",
        quantity=5,
        price_krw=10_000,
        fee_krw=0,
        tax_krw=0,
        occurred_at="2026-01-05T09:06:00+09:00",
    )
    after_buy = database.get_mission("mission_focus_001")
    assert after_buy["available_krw"] == 50_000
    assert after_buy["exposed_krw"] == 50_000

    database.record_live_fill(
        sell["id"],
        broker_order_no="S1",
        broker_fill_key="S1-F1",
        quantity=5,
        price_krw=30_000,
        fee_krw=0,
        tax_krw=0,
        occurred_at="2026-01-20T09:06:00+09:00",
    )
    outcomes = sync_live_trade_outcomes(database, "mission_focus_001")
    mission = database.get_mission("mission_focus_001")

    assert len(outcomes) == 1
    assert outcomes[0]["net_pnl_krw"] == 100_000
    assert mission["equity_krw"] == 200_000
    assert mission["available_krw"] == 180_000
    assert mission["reserved_profit_krw"] == 20_000
    assert verify_accounting_and_audit(database)["state"] == "PASS"


class PartialFillClient:
    def fetch_open_orders(self) -> dict[str, object]:
        return {"oso": [{"ord_no": "P100"}]}

    def fetch_fills(self) -> dict[str, object]:
        return {
            "cntr": [
                {
                    "ord_no": "P100",
                    "cntr_no": "E1",
                    "cntr_qty": "2",
                    "cntr_pric": "10000",
                    "tdy_trde_cmsn": "0",
                    "tdy_trde_tax": "0",
                },
                {
                    "ord_no": "P100",
                    "cntr_no": "E2",
                    "cntr_qty": "2",
                    "cntr_pric": "10000",
                    "tdy_trde_cmsn": "0",
                    "tdy_trde_tax": "0",
                },
            ]
        }


def test_partial_fills_with_same_price_and_quantity_keep_broker_identity(tmp_path) -> None:
    database, cycle = _database_with_cycle(tmp_path)
    shadow = _shadow(
        database, cycle, side="BUY", idempotency_key="shadow-partial-fill"
    )
    intent = _intent(
        database, shadow, side="BUY", idempotency_key="intent-partial-fill"
    )
    database.append_live_order_event(
        intent["id"], "ACKNOWLEDGED", {"broker_order_no": "P100"}
    )

    first = reconcile_broker_orders(
        database, PartialFillClient(), mission_id="mission_focus_001"  # type: ignore[arg-type]
    )
    second = reconcile_broker_orders(
        database, PartialFillClient(), mission_id="mission_focus_001"  # type: ignore[arg-type]
    )
    refreshed = database.get_live_order_intent(intent["id"])

    assert first["newly_recorded_fills"] == 2
    assert second["newly_recorded_fills"] == 0
    assert len(refreshed["fills"]) == 2
    assert sum(fill["quantity"] for fill in refreshed["fills"]) == 4
    assert refreshed["state"] == "PARTIALLY_FILLED"


def test_change_request_is_scope_bound_and_never_applies_automatically(tmp_path) -> None:
    database = Database(tmp_path / "changes.sqlite3")
    database.initialize()
    request = database.create_change_request(
        "mission_focus_001",
        change_type="RISK_LIMIT",
        target_id="mode_focus_v1",
        proposal={"max_trade_risk_bps": 400},
        reason="낙폭 감소 여부를 연구하기 위한 변경 후보",
    )
    replay = database.create_change_request(
        "mission_focus_001",
        change_type="RISK_LIMIT",
        target_id="mode_focus_v1",
        proposal={"max_trade_risk_bps": 400},
        reason="같은 요청 재실행",
    )

    assert request["id"] == replay["id"]
    assert request["status"] == "PENDING_OWNER"
    assert database.get_mission("mission_focus_001")["max_trade_risk_bps"] == 500

    approved = database.decide_change_request(
        request["id"], decision="APPROVE", scope_hash=request["scope_hash"]
    )
    assert approved["status"] == "APPROVED"
    assert database.get_mission("mission_focus_001")["max_trade_risk_bps"] == 500


def test_readiness_reports_distinguish_software_from_external_evidence(tmp_path) -> None:
    database = Database(tmp_path / "readiness.sqlite3")
    database.initialize()

    autonomy = autonomy_readiness(database, "mission_focus_001")
    deployment = deployment_readiness()
    comparison = strategy_comparison(database)

    assert autonomy["software_completed"] == autonomy["software_total"]
    assert autonomy["state"] == "WAITING_EXTERNAL_EVIDENCE"
    assert autonomy["external"]["qualified_market_data"] is False
    assert deployment["state"] == "LOCAL_ONLY"
    assert comparison["state"] == "KEEP_ALL_BLOCKED"
    assert comparison["automatic_promotion"] is False


def test_shadow_exit_schedule_and_postgres_query_boundary() -> None:
    order = {"next_session_date": "2026-01-05"}
    schedule = shadow_exit_schedule(
        order, "CLOSE-AUCTION-KR-v2", date.fromisoformat("2026-01-12")
    )

    assert schedule["holding_complete"] is True
    assert schedule["holding_sessions"] == 5
    translated = _postgres_sql("INSERT OR IGNORE INTO sample (id) VALUES (?)")
    assert translated == "INSERT INTO sample (id) VALUES (%s) ON CONFLICT DO NOTHING"
    assert _postgres_sql("BEGIN IMMEDIATE") == "BEGIN"
    assert "ORDER BY t._mg_seq" in _postgres_sql("SELECT * FROM sample ORDER BY t.rowid")
    assert "_mg_seq BIGSERIAL UNIQUE" in _postgres_sql(
        "CREATE TABLE IF NOT EXISTS sample (id TEXT PRIMARY KEY)"
    )


def test_production_api_requires_oidc_bearer_before_domain_routes(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("MONEYGUN_ENV", "production")
    application = create_app(tmp_path / "production-auth.sqlite3")
    client = TestClient(application)

    assert client.get("/health").status_code == 200
    response = client.get("/v1/operations/autonomy-readiness")
    assert response.status_code == 401
    assert response.json()["detail"] == "로그인이 필요합니다."
