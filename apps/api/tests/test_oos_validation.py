import json

from fastapi.testclient import TestClient

from moneygun_api.main import create_app
from moneygun_api.research import build_fixture_snapshot
from moneygun_api.validation import _has_minimum_five_year_history


def test_five_year_gate_uses_validated_calendar_coverage() -> None:
    snapshot = {
        "collection_manifest": {
            "period_start": "2021-08-28",
            "period_end": "2026-08-28",
        }
    }

    assert _has_minimum_five_year_history(
        snapshot, ["2021-08-30", "2026-08-28"]
    ) is True
    assert _has_minimum_five_year_history(
        snapshot, ["2021-09-06", "2026-08-28"]
    ) is False


def test_purged_walk_forward_is_reproducible_and_fails_synthetic_source(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "moneygun-oos.sqlite3"))
    cycle = client.post(
        "/v1/research/cycles/run",
        json={"mission_id": "mission_focus_001", "data_source": "FIXTURE_KR_REPRODUCIBLE"},
        headers={"Idempotency-Key": "oos-cycle-test-001"},
    ).json()
    payload = {"mission_id": "mission_focus_001", "snapshot_id": cycle["snapshot_id"]}
    headers = {"Idempotency-Key": "oos-validation-test-001"}

    first = client.post("/v1/research/validations/run", json=payload, headers=headers)
    second = client.post("/v1/research/validations/run", json=payload, headers=headers)

    assert first.status_code == 200
    assert first.json() == second.json()
    report = first.json()
    assert report["status"] == "NOT_ELIGIBLE"
    assert report["promotion_eligible"] is False
    assert report["trading_enabled"] is False
    assert report["history"]["trading_days"] >= 1260
    assert report["history"]["oos_days"] >= 504
    assert report["gates"]["source_is_real_and_authorized"] is False
    assert report["config"]["total_cost_bps"] > 0
    assert all(fold["train_end"] < fold["purge_start"] for fold in report["folds"])
    assert all(fold["purge_end"] < fold["test_start"] for fold in report["folds"])
    assert client.get("/v1/research/validations/latest").json()["id"] == report["id"]
    audit = client.get("/v1/audit-events").json()
    assert [event["action"] for event in audit].count("research.validation.completed") == 1


def test_authorized_bundle_import_and_imported_cycle(tmp_path, monkeypatch) -> None:
    import_dir = tmp_path / "imports"
    import_dir.mkdir()
    snapshot = build_fixture_snapshot()
    bundle = {
        key: value
        for key, value in snapshot.items()
        if key not in {"id", "quality", "checksum", "source"}
    }
    bundle.update(
        {
            "schema_version": "1.0",
            "source": "KRX_AUTHORIZED_EXPORT",
            "license_basis": "USER_AUTHORIZED_INTERNAL_RESEARCH",
        }
    )
    (import_dir / "authorized-market.json").write_text(
        json.dumps(bundle, ensure_ascii=False), encoding="utf-8"
    )
    monkeypatch.setenv("MONEYGUN_MARKET_DATA_IMPORT_DIR", str(import_dir))
    client = TestClient(create_app(tmp_path / "moneygun-import.sqlite3"))

    imported = client.post(
        "/v1/research/snapshots/import",
        json={"file_name": "authorized-market.json"},
        headers={"Idempotency-Key": "market-import-test-001"},
    )
    assert imported.status_code == 200
    snapshot_id = imported.json()["id"]
    assert imported.json()["source"] == "KRX_AUTHORIZED_EXPORT"
    cycle = client.post(
        "/v1/research/cycles/run",
        json={
            "mission_id": "mission_focus_001",
            "data_source": "IMPORTED_SNAPSHOT",
            "snapshot_id": snapshot_id,
        },
        headers={"Idempotency-Key": "market-cycle-test-001"},
    )
    assert cycle.status_code == 200
    assert cycle.json()["source"] == "KRX_AUTHORIZED_EXPORT"
    assert cycle.json()["decision"]["order_allowed"] is False


def test_import_path_and_unconfigured_open_dart_fail_closed(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MONEYGUN_MARKET_DATA_IMPORT_DIR", str(tmp_path / "imports"))
    monkeypatch.delenv("OPENDART_API_KEY", raising=False)
    client = TestClient(create_app(tmp_path / "moneygun-sources.sqlite3"))

    invalid_path = client.post(
        "/v1/research/snapshots/import",
        json={"file_name": "../secret.json"},
        headers={"Idempotency-Key": "market-import-test-002"},
    )
    assert invalid_path.status_code == 422
    dart = client.post(
        "/v1/research/evidence/open-dart/search",
        json={"corp_code": "00126380", "begin_date": "20260101", "end_date": "20260831"},
    )
    assert dart.status_code == 503
    statements = client.post(
        "/v1/research/evidence/open-dart/financial-statements",
        json={
            "corp_code": "00126380",
            "business_year": "2025",
            "report_code": "11011",
            "fs_div": "CFS",
        },
    )
    assert statements.status_code == 503
