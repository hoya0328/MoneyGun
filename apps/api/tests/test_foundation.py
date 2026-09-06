from fastapi.testclient import TestClient

from moneygun_api.main import create_app


def test_seeded_mission_has_balanced_ledger_and_audit_chain(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "moneygun-test.sqlite3"))

    mission = client.get("/v1/missions/mission_focus_001")
    assert mission.status_code == 200
    assert mission.json()["equity_krw"] == 100_000
    assert mission.json()["available_krw"] == 100_000
    assert mission.json()["trading_enabled"] is False

    postings = client.get("/v1/missions/mission_focus_001/ledger").json()
    assert sum(posting["amount_krw"] for posting in postings) == 0
    assert {posting["account_code"] for posting in postings} == {
        "AVAILABLE_CASH",
        "OWNER_CAPITAL",
    }

    events = client.get("/v1/audit-events").json()
    assert events[0]["action"] == "mission.created"
    assert len(events[0]["event_hash"]) == 64


def test_mission_creation_is_idempotent(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "moneygun-idempotency.sqlite3"))
    payload = {
        "name": "두 번째 집중투자 미션",
        "mode_code": "FOCUS",
        "seed_capital_krw": 200_000,
        "goal_capital_krw": 20_000_000,
    }
    headers = {"Idempotency-Key": "mission-create-test-001"}

    first = client.post("/v1/missions", json=payload, headers=headers)
    second = client.post("/v1/missions", json=payload, headers=headers)

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["equity_krw"] == 200_000
    postings = client.get(f"/v1/missions/{first.json()['id']}/ledger").json()
    assert len(postings) == 2
    events = client.get("/v1/audit-events").json()
    assert events[0]["previous_hash"] == events[1]["event_hash"]
