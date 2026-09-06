import hashlib
import json
from datetime import date, timedelta

from fastapi.testclient import TestClient

from moneygun_api.closing_auction import run_close_auction_validation
from moneygun_api.main import create_app
from moneygun_api.qualification import instrument_is_eligible, qualification_status
from moneygun_api.research import build_fixture_snapshot


def _checksum(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _qualified_bundle() -> dict[str, object]:
    snapshot = build_fixture_snapshot()
    symbols = ["100001", "100002", "100003"]
    instruments = []
    for symbol, instrument in zip(symbols, snapshot["instruments"], strict=True):
        instruments.append({**instrument, "symbol": symbol})
    period_start = snapshot["benchmark"]["bars"][0]["date"]
    period_end = snapshot["benchmark"]["bars"][-1]["date"]
    universe = [
        {
            "symbol": symbol,
            "name": instrument["name"],
            "market": "KOSPI" if index == 0 else "KOSDAQ",
            "security_type": "COMMON_STOCK",
            "listed_on": "2010-01-04",
            "delisted_on": period_end if index == 2 else None,
        }
        for index, (symbol, instrument) in enumerate(
            zip(symbols, instruments, strict=True)
        )
    ]
    designation_start = date.fromisoformat(period_start) + timedelta(days=100)
    designations = [
        {
            "symbol": symbols[0],
            "state": "CAUTION",
            "effective_from": designation_start.isoformat(),
            "effective_to": (designation_start + timedelta(days=2)).isoformat(),
        }
    ]
    normalized = {
        "KRX_DAILY_PRICES": _checksum(
            {
                "benchmark": snapshot["benchmark"],
                "instruments": [
                    {"symbol": item["symbol"], "bars": item["bars"]}
                    for item in instruments
                ],
            }
        ),
        "KRX_LISTING_LIFECYCLE": _checksum(universe),
        "KRX_DESIGNATION_HISTORY": _checksum(designations),
    }
    artifacts = [
        {
            "kind": kind,
            "source_url": "https://data.krx.co.kr/contents/MDC/MAIN/main/index.cmd",
            "sha256": hashlib.sha256(f"original-{kind}".encode()).hexdigest(),
            "normalized_sha256": checksum,
            "coverage_start": period_start,
            "coverage_end": period_end,
        }
        for kind, checksum in normalized.items()
    ]
    return {
        "schema_version": "2.0",
        "as_of": snapshot["as_of"],
        "source": "KRX_AUTHORIZED_EXPORT",
        "license_basis": "USER_AUTHORIZED_INTERNAL_RESEARCH",
        "benchmark": snapshot["benchmark"],
        "instruments": instruments,
        "evidence": snapshot["evidence"],
        "qualification": {
            "period_start": period_start,
            "period_end": period_end,
            "universe_history": universe,
            "designation_history": designations,
            "source_artifacts": artifacts,
            "fundamental_revision_safe": False,
        },
    }


def test_qualified_import_derives_bias_gates_and_keeps_trading_locked(
    tmp_path, monkeypatch
) -> None:
    import_dir = tmp_path / "imports"
    import_dir.mkdir()
    bundle = _qualified_bundle()
    (import_dir / "krx-history.qualified.json").write_text(
        json.dumps(bundle, ensure_ascii=False), encoding="utf-8"
    )
    monkeypatch.setenv("MONEYGUN_MARKET_DATA_IMPORT_DIR", str(import_dir))
    client = TestClient(create_app(tmp_path / "qualified.sqlite3"))

    response = client.post(
        "/v1/research/snapshots/qualified-import",
        json={"file_name": "krx-history.qualified.json"},
        headers={"Idempotency-Key": "qualified-import-test-001"},
    )

    assert response.status_code == 200
    snapshot = response.json()
    manifest = snapshot["collection_manifest"]
    assert manifest["survivorship_bias_controlled"] is True
    assert manifest["historical_designation_states_complete"] is True
    assert manifest["fundamental_revision_safe"] is False
    assert manifest["historical_delisted_count"] == 1
    assert manifest["derived_by"] == "MONEYGUN_QUALIFICATION_VALIDATOR_V1"
    assert client.get("/v1/data-qualification/status").json()["state"] == (
        "QUALIFIED_DATA_READY"
    )
    assert client.get("/v1/data-qualification/status").json()["trading_enabled"] is False


def test_qualified_import_rejects_tampered_normalized_section(tmp_path, monkeypatch) -> None:
    import_dir = tmp_path / "imports"
    import_dir.mkdir()
    bundle = _qualified_bundle()
    bundle["qualification"]["designation_history"][0]["state"] = "WARNING"
    (import_dir / "tampered.qualified.json").write_text(
        json.dumps(bundle, ensure_ascii=False), encoding="utf-8"
    )
    monkeypatch.setenv("MONEYGUN_MARKET_DATA_IMPORT_DIR", str(import_dir))
    client = TestClient(create_app(tmp_path / "tampered.sqlite3"))

    response = client.post(
        "/v1/research/snapshots/qualified-import",
        json={"file_name": "tampered.qualified.json"},
        headers={"Idempotency-Key": "qualified-import-test-002"},
    )

    assert response.status_code == 422
    assert "체크섬" in response.json()["detail"]


def test_dynamic_oos_does_not_intersect_away_delisted_history() -> None:
    bundle = _qualified_bundle()
    snapshot = {
        "id": "snap_0000000000000000",
        "quality": {"state": "PASS"},
        "checksum": "0" * 64,
        "market": "KR",
        "collection_manifest": {
            "survivorship_bias_controlled": True,
            "historical_designation_states_complete": True,
        },
        "universe_history": bundle["qualification"]["universe_history"],
        "designation_history": bundle["qualification"]["designation_history"],
        **{
            key: bundle[key]
            for key in ("as_of", "source", "benchmark", "instruments", "evidence")
        },
    }
    snapshot["instruments"][2]["bars"] = snapshot["instruments"][2]["bars"][:-20]

    report = run_close_auction_validation(snapshot)

    assert report["history"]["trading_days"] == len(snapshot["benchmark"]["bars"])
    assert report["gates"]["survivorship_bias_controlled"] is True
    assert report["gates"]["historical_designation_states_complete"] is True
    assert report["trading_enabled"] is False


def test_collection_candidate_reports_exact_missing_kind_histories(tmp_path) -> None:
    import_dir = tmp_path / "imports"
    manifest_dir = import_dir / "raw" / "public-data"
    manifest_dir.mkdir(parents=True)
    normalized = manifest_dir / "normalized-market.sqlite3"
    normalized.write_bytes(b"sqlite-placeholder")
    manifest = {
        "prices": {"total_count": 3_381_406},
        "issuance": {"total_count": 17_596},
        "normalization": {
            "database": str(normalized.resolve()),
            "database_sha256": "a" * 64,
            "benchmark_rows": 1_230,
            "instrument_count": 2_100,
            "historical_delisted_count": 50,
            "designation_interval_count": 12_000,
        },
        "kind": {
            "checks": {
                "caution_quarter_files": True,
                "caution_period_start_covered": True,
                "caution_period_end_covered": True,
                "warning_intervals_present": True,
                "danger_intervals_present": True,
                "managed_intervals_complete": False,
                "watchlist_intervals_complete": False,
                "halted_intervals_complete": False,
            }
        },
    }
    (manifest_dir / "collection-manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )

    status = qualification_status(import_dir, None)

    assert status["state"] == "DATA_REQUIRED"
    assert status["collection"]["state"] == "INCOMPLETE"
    assert status["collection"]["blockers"] == [
        "managed_intervals_complete",
        "watchlist_intervals_complete",
        "halted_intervals_complete",
    ]


def test_eligibility_builds_symbol_index_without_changing_point_in_time_result() -> None:
    snapshot = {
        "universe_history": [
            {
                "symbol": "005930",
                "listed_on": "1975-06-11",
                "delisted_on": None,
            },
            {
                "symbol": "000001",
                "listed_on": "2020-01-02",
                "delisted_on": "2022-12-29",
            },
        ],
        "designation_history": [
            {
                "symbol": "005930",
                "state": "HALTED",
                "effective_from": "2024-01-08",
                "effective_to": "2024-01-09",
            }
        ],
    }

    assert instrument_is_eligible(snapshot, "005930", "2024-01-05") is True
    assert instrument_is_eligible(snapshot, "005930", "2024-01-08") is False
    assert instrument_is_eligible(snapshot, "000001", "2023-01-02") is False
    assert instrument_is_eligible(snapshot, "999999", "2024-01-05") is False
    assert set(snapshot["_eligibility_index"]["lifecycle_by_symbol"]) == {
        "005930",
        "000001",
    }
