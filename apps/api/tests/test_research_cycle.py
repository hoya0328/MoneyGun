from fastapi.testclient import TestClient

from moneygun_api.main import create_app


def test_full_p0_cycle_is_reproducible_and_never_orders(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "moneygun-p0.sqlite3"))
    headers = {"Idempotency-Key": "p0-cycle-test-001"}
    payload = {
        "mission_id": "mission_focus_001",
        "data_source": "FIXTURE_KR_REPRODUCIBLE",
    }

    first = client.post("/v1/research/cycles/run", json=payload, headers=headers)
    second = client.post("/v1/research/cycles/run", json=payload, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    package = first.json()
    assert package == second.json()
    assert package["status"] == "COMPLETE"
    assert package["trading_enabled"] is False
    assert package["risk"]["order_allowed"] is False
    assert package["decision"]["order_allowed"] is False
    assert package["decision"]["action"] == "BUY_CANDIDATE"
    assert len(package["reports"]) == 9
    assert [report["name"] for report in package["reports"]] == [
        "김데이터",
        "김뉴스",
        "김산업",
        "김차트",
        "김찬성",
        "김반대",
        "김안전",
        "김투자",
        "김주문",
    ]
    assert package["candidates"][0]["score"] >= package["candidates"][1]["score"]
    assert package["backtest"]["trade_count"] > 0
    assert package["backtest"]["oos_validated"] is False

    latest = client.get("/v1/research/cycles/latest").json()
    assert latest["id"] == package["id"]
    audit = client.get("/v1/audit-events").json()
    assert [event["action"] for event in audit].count("snapshot.closed") == 1
    assert [event["action"] for event in audit].count("committee.cycle.completed") == 1


def test_unknown_live_source_fails_closed(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "moneygun-fail-closed.sqlite3"))
    response = client.post(
        "/v1/research/cycles/run",
        json={"mission_id": "mission_focus_001", "data_source": "LIVE_KRX"},
        headers={"Idempotency-Key": "p0-cycle-test-002"},
    )
    assert response.status_code == 422
