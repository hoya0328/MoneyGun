from fastapi.testclient import TestClient

from moneygun_api.closing_auction import (
    STRATEGY_ID,
    run_close_auction_validation,
    strategy_spec,
)
from moneygun_api.main import create_app
from moneygun_api.research import build_fixture_snapshot


def test_close_auction_validation_has_no_same_close_lookahead() -> None:
    snapshot = build_fixture_snapshot()
    report = run_close_auction_validation(snapshot)

    assert report["strategy_id"] == STRATEGY_ID
    assert report["method"] == "PURGED_WALK_FORWARD_CLOSE_TO_CLOSE"
    assert report["trading_enabled"] is False
    assert report["gates"]["signal_strictly_precedes_entry"] is True
    assert all(
        trade["signal_date"] < trade["entry_date"] < trade["exit_date"]
        for trade in report["trade_sample"]
    )
    assert report["config"]["holding_sessions"] == 5
    assert report["config"]["total_cost_bps"] == 39.0


def test_close_auction_mode_is_seeded_and_research_route_is_fail_closed(tmp_path) -> None:
    app = create_app(tmp_path / "moneygun-close.sqlite3")
    client = TestClient(app)
    mission = client.get("/v1/missions/mission_close_auction_001")

    assert mission.status_code == 200
    assert mission.json()["mode_code"] == "CLOSE_AUCTION"
    assert mission.json()["mode_display_name"] == "종가매매"
    assert mission.json()["halt_equity_krw"] == 85_000
    assert mission.json()["trading_enabled"] is False

    snapshot = build_fixture_snapshot()
    snapshot["source"] = "KRX_AUTHORIZED_EXPORT"
    app.state.database.save_snapshot(snapshot)
    result = client.post(
        "/v1/strategies/close-auction/run",
        json={
            "mission_id": "mission_close_auction_001",
            "snapshot_id": snapshot["id"],
        },
        headers={"Idempotency-Key": "close-auction-test-001"},
    )

    assert result.status_code == 200
    payload = result.json()
    assert payload["spec"]["entry"]["order_type"] == "LIMIT"
    assert payload["cycle"]["decision"]["order_allowed"] is False
    assert payload["validation"]["promotion_eligible"] is False
    assert "historical_designation_states_complete" in payload["validation"]["failed_gates"]
    assert client.get("/v1/strategies/close-auction/latest").status_code == 200


def test_close_auction_spec_forbids_same_close_assumption() -> None:
    spec = strategy_spec()

    assert spec["clock"]["krx_official_auction"] == "15:20~15:30"
    assert spec["risk_limits"]["max_position_pct"] == 50
    assert spec["risk_limits"]["halt_drawdown_pct"] == 15
    assert any("같은 종가" in rule for rule in spec["entry"]["forbidden"])
