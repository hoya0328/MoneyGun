from fastapi.testclient import TestClient

from moneygun_api.main import create_app
from moneygun_api.storage import Database


def test_twenty_day_committee_replay_is_complete_scored_and_idempotent(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "moneygun-committee.sqlite3"))
    payload = {
        "mission_id": "mission_focus_001",
        "end_date": "2026-08-31",
        "target_days": 20,
    }
    headers = {"Idempotency-Key": "committee-replay-test-001"}

    first = client.post("/v1/committee/programs/replay", json=payload, headers=headers)
    second = client.post("/v1/committee/programs/replay", json=payload, headers=headers)

    assert first.status_code == 200
    assert first.json() == second.json()
    program = first.json()
    assert program["state"] == "COMPLETE"
    assert len(program["days"]) == 20
    assert all(day["state"] == "COMPLETE" for day in program["days"])
    assert all(day["attempts"] == 1 for day in program["days"])
    summary = program["summary"]
    assert summary["completed_days"] == 20
    assert summary["failed_days"] == 0
    assert summary["missing_days"] == 0
    assert summary["evaluated_days"] == 10
    assert summary["pending_outcome_days"] == 10
    assert summary["package_continuity_pct"] == 100
    assert summary["readiness"]["replay_complete"] is True
    assert summary["readiness"]["qualifies_20_day_gate"] is False
    assert summary["readiness"]["real_operating_days"] == 0
    assert summary["trading_enabled"] is False
    assert len(summary["agent_scorecards"]) == 9
    assert all(card["scored_days"] == 10 for card in summary["agent_scorecards"])

    latest = client.get("/v1/committee/programs/latest").json()
    assert latest["id"] == program["id"]
    audit = client.get("/v1/audit-events", params={"limit": 100}).json()
    actions = [event["action"] for event in audit]
    assert actions.count("committee.program.created") == 1
    assert actions.count("committee.program.completed") == 1


def test_failed_committee_day_retry_increments_attempt_count(tmp_path) -> None:
    database = Database(tmp_path / "moneygun-retry.sqlite3")
    database.initialize()
    program = database.create_committee_program(
        {
            "id": "program_retry_test",
            "mission_id": "mission_focus_001",
            "mode": "REPLAY",
            "source": "FIXTURE_KR_REPRODUCIBLE",
            "start_date": "2026-08-31",
            "end_date": "2026-08-31",
            "target_days": 20,
        }
    )
    assert database.claim_committee_day(program["id"], "2026-08-31") is True
    database.fail_committee_day(program["id"], "2026-08-31", "temporary failure")
    assert database.claim_committee_day(program["id"], "2026-08-31") is True
    database.fail_committee_day(program["id"], "2026-08-31", "temporary failure")
    day = database.list_committee_days(program["id"])[0]
    assert day["state"] == "FAILED"
    assert day["attempts"] == 2
