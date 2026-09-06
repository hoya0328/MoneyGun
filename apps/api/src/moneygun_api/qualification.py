from __future__ import annotations

import gzip
import hashlib
import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .data_sources import DataSourceError, market_import_directory

QUALIFIED_SCHEMA_VERSION = "2.0"
QUALIFIED_SUFFIXES = (".qualified.json", ".qualified.json.gz")
REQUIRED_ARTIFACT_KINDS = {
    "KRX_DAILY_PRICES",
    "KRX_LISTING_LIFECYCLE",
    "KRX_DESIGNATION_HISTORY",
}
DISQUALIFYING_STATES = {
    "CAUTION",
    "WARNING",
    "DANGER",
    "HALTED",
    "MANAGED",
    "OVERHEATED",
    "WATCHLIST",
    "LIQUIDATION",
}
QUALIFIED_SOURCES = {
    "KRX_AUTHORIZED_EXPORT",
    "LICENSED_VENDOR",
    "OFFICIAL_PUBLIC_DATA_API_AND_KIND_UI_EXPORT",
    "OFFICIAL_PUBLIC_DATA_API_KIND_AND_KIWOOM",
}


def instrument_is_eligible(snapshot: dict[str, Any], symbol: str, day: str) -> bool:
    """Return point-in-time eligibility; missing histories remain permissive but fail gates."""
    index = snapshot.get("_eligibility_index")
    if index is None:
        lifecycle_rows = snapshot.get("universe_history", [])
        designations_by_symbol: dict[str, list[dict[str, Any]]] = {}
        for state in snapshot.get("designation_history", []):
            designations_by_symbol.setdefault(state["symbol"], []).append(state)
        index = {
            "lifecycle_by_symbol": {item["symbol"]: item for item in lifecycle_rows},
            "designations_by_symbol": designations_by_symbol,
        }
        snapshot["_eligibility_index"] = index

    lifecycle_rows = snapshot.get("universe_history", [])
    if lifecycle_rows:
        lifecycle = index["lifecycle_by_symbol"].get(symbol)
        if lifecycle is None:
            return False
        if day < lifecycle["listed_on"]:
            return False
        if lifecycle.get("delisted_on") and day > lifecycle["delisted_on"]:
            return False
    for state in index["designations_by_symbol"].get(symbol, []):
        if state["effective_from"] <= day <= state["effective_to"]:
            return False
    return True


def qualification_spec() -> dict[str, Any]:
    return {
        "schema_version": QUALIFIED_SCHEMA_VERSION,
        "accepted_suffixes": list(QUALIFIED_SUFFIXES),
        "accepted_sources": sorted(QUALIFIED_SOURCES),
        "required_artifacts": sorted(REQUIRED_ARTIFACT_KINDS),
        "required_history": {
            "minimum_calendar_years": 5,
            "minimum_instruments": 2,
            "delisted_instrument_required": True,
            "designation_record_required": True,
            "exact_universe_to_price_symbol_match": True,
        },
        "disqualifying_states": sorted(DISQUALIFYING_STATES),
        "official_downloads": [
            {
                "name": "KRX Data Marketplace",
                "url": "https://data.krx.co.kr/contents/MDC/MAIN/main/index.cmd",
                "covers": "전종목 시세·기본정보·지정내역·상장폐지종목",
            },
            {
                "name": "KIND 투자유의사항",
                "url": (
                    "https://kind.krx.co.kr/investwarn/"
                    "investattentwarnrisky.do?method=investattentwarnriskyMain"
                ),
                "covers": "관리·거래정지·투자주의·경고·위험 기간 조회",
            },
        ],
        "sample_shape": {
            "schema_version": QUALIFIED_SCHEMA_VERSION,
            "as_of": "2026-08-31T15:40:00+09:00",
            "source": "KRX_AUTHORIZED_EXPORT",
            "license_basis": "USER_AUTHORIZED_INTERNAL_RESEARCH",
            "benchmark": "기존 market bundle benchmark 객체",
            "instruments": "상장폐지 포함 instrument 배열",
            "qualification": {
                "period_start": "2021-08-31",
                "period_end": "2026-08-31",
                "universe_history": [
                    {
                        "symbol": "005930",
                        "name": "삼성전자",
                        "market": "KOSPI",
                        "security_type": "COMMON_STOCK",
                        "listed_on": "1975-06-11",
                        "delisted_on": None,
                    }
                ],
                "designation_history": [
                    {
                        "symbol": "000000",
                        "state": "HALTED",
                        "effective_from": "2023-01-02",
                        "effective_to": "2023-01-03",
                    }
                ],
                "source_artifacts": [
                    {
                        "kind": "KRX_DAILY_PRICES",
                        "source_url": "https://data.krx.co.kr/...",
                        "sha256": "64자리 원본 파일 SHA-256",
                        "normalized_sha256": "정규화된 번들 구간의 SHA-256",
                        "coverage_start": "2021-08-31",
                        "coverage_end": "2026-08-31",
                    }
                ],
            },
        },
        "trading_enabled": False,
    }


def _qualified_path(file_name: str, root: str | Path | None = None) -> Path:
    if not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\.qualified\.json(?:\.gz)?", file_name
    ):
        raise DataSourceError(
            "자격 번들은 중첩 경로가 아닌 .qualified.json(.gz) 파일이어야 합니다."
        )
    import_root = Path(root).resolve() if root else market_import_directory()
    path = (import_root / file_name).resolve()
    if path.parent != import_root:
        raise DataSourceError("가져오기 디렉터리 밖의 파일은 읽을 수 없습니다.")
    if not path.is_file():
        raise DataSourceError("자격 번들 파일을 찾을 수 없습니다.")
    if path.stat().st_size > 1024 * 1024 * 1024:
        raise DataSourceError("압축된 자격 번들은 1GB를 넘을 수 없습니다.")
    return path


def _read_payload(path: Path) -> dict[str, Any]:
    try:
        if path.name.endswith(".gz"):
            with gzip.open(path, "rt", encoding="utf-8-sig") as handle:
                payload = json.load(handle)
        else:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise DataSourceError("자격 번들 JSON을 해석할 수 없습니다.") from error
    if not isinstance(payload, dict):
        raise DataSourceError("자격 번들의 최상위 값은 객체여야 합니다.")
    return payload


def _iso_date(value: Any, label: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as error:
        raise DataSourceError(f"{label}은 YYYY-MM-DD 날짜여야 합니다.") from error


def _validate_artifacts(
    artifacts: list[dict[str, Any]],
    *,
    source: str,
    period_start: date,
    period_end: date,
) -> dict[str, int]:
    if not isinstance(artifacts, list):
        raise DataSourceError("qualification.source_artifacts는 배열이어야 합니다.")
    kinds: set[str] = set()
    for index, artifact in enumerate(artifacts):
        kind = str(artifact.get("kind", ""))
        checksum = str(artifact.get("sha256", ""))
        source_url = str(artifact.get("source_url", ""))
        start = _iso_date(artifact.get("coverage_start"), f"artifact[{index}].coverage_start")
        end = _iso_date(artifact.get("coverage_end"), f"artifact[{index}].coverage_end")
        if not re.fullmatch(r"[a-f0-9]{64}", checksum):
            raise DataSourceError(f"artifact[{index}] 원본 SHA-256이 유효하지 않습니다.")
        parsed = urlparse(source_url)
        if parsed.scheme != "https":
            raise DataSourceError(f"artifact[{index}] source_url은 HTTPS여야 합니다.")
        if source == "KRX_AUTHORIZED_EXPORT" and parsed.hostname not in {
            "data.krx.co.kr",
            "kind.krx.co.kr",
        }:
            raise DataSourceError(f"artifact[{index}]는 KRX/KIND 공식 URL이어야 합니다.")
        if (
            source
            in {
                "OFFICIAL_PUBLIC_DATA_API_AND_KIND_UI_EXPORT",
                "OFFICIAL_PUBLIC_DATA_API_KIND_AND_KIWOOM",
            }
            and parsed.hostname
            not in {
                "www.data.go.kr",
                "apis.data.go.kr",
                "kind.krx.co.kr",
                "openapi.kiwoom.com",
            }
        ):
            raise DataSourceError(
                f"artifact[{index}]는 공공데이터포털/KIND 공식 URL이어야 합니다."
            )
        if start > period_start or end < period_end:
            raise DataSourceError(f"artifact[{index}]가 전체 검증 기간을 덮지 않습니다.")
        kinds.add(kind)
    missing = REQUIRED_ARTIFACT_KINDS - kinds
    if missing:
        raise DataSourceError("자격 원본 종류 누락: " + ", ".join(sorted(missing)))
    return {kind: sum(item.get("kind") == kind for item in artifacts) for kind in kinds}


def _canonical_checksum(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _validate_normalized_artifacts(
    artifacts: list[dict[str, Any]],
    *,
    benchmark: dict[str, Any],
    instruments: list[dict[str, Any]],
    universe: list[dict[str, Any]],
    designations: list[dict[str, Any]],
) -> dict[str, str]:
    expected = {
        "KRX_DAILY_PRICES": _canonical_checksum(
            {
                "benchmark": benchmark,
                "instruments": [
                    {"symbol": item["symbol"], "bars": item["bars"]}
                    for item in instruments
                ],
            }
        ),
        "KRX_LISTING_LIFECYCLE": _canonical_checksum(universe),
        "KRX_DESIGNATION_HISTORY": _canonical_checksum(designations),
    }
    for kind, checksum in expected.items():
        matches = [item for item in artifacts if item.get("kind") == kind]
        if not any(item.get("normalized_sha256") == checksum for item in matches):
            raise DataSourceError(f"{kind}: 정규화 구간 체크섬이 번들 내용과 일치하지 않습니다.")
    return expected


def _validate_universe(
    records: list[dict[str, Any]],
    instruments: list[dict[str, Any]],
    *,
    period_start: date,
    period_end: date,
) -> dict[str, Any]:
    if not isinstance(records, list) or len(records) < 2:
        raise DataSourceError("상장폐지 포함 universe_history에는 최소 2종목이 필요합니다.")
    symbols: set[str] = set()
    delisted: set[str] = set()
    for index, record in enumerate(records):
        symbol = str(record.get("symbol", ""))
        if not re.fullmatch(r"\d{6}", symbol):
            raise DataSourceError(f"universe_history[{index}] 종목코드는 숫자 6자리여야 합니다.")
        if symbol in symbols:
            raise DataSourceError(f"universe_history 종목이 중복됐습니다: {symbol}")
        if record.get("market") not in {"KOSPI", "KOSDAQ"}:
            raise DataSourceError(f"{symbol}: KOSPI·KOSDAQ 보통주만 허용합니다.")
        if record.get("security_type") != "COMMON_STOCK":
            raise DataSourceError(f"{symbol}: security_type은 COMMON_STOCK이어야 합니다.")
        listed_on = _iso_date(record.get("listed_on"), f"{symbol}.listed_on")
        delisted_value = record.get("delisted_on")
        if delisted_value:
            delisted_on = _iso_date(delisted_value, f"{symbol}.delisted_on")
            if delisted_on <= listed_on:
                raise DataSourceError(f"{symbol}: 상장폐지일은 상장일보다 뒤여야 합니다.")
            if period_start <= delisted_on <= period_end:
                delisted.add(symbol)
        symbols.add(symbol)
    instrument_symbols = {str(item.get("symbol", "")) for item in instruments}
    if len(instrument_symbols) != len(instruments):
        raise DataSourceError("instruments 종목코드가 중복됐습니다.")
    if symbols != instrument_symbols:
        missing_prices = sorted(symbols - instrument_symbols)
        missing_lifecycle = sorted(instrument_symbols - symbols)
        raise DataSourceError(
            "유니버스와 가격 종목이 정확히 일치해야 합니다. "
            f"가격 누락={missing_prices[:5]}, 생애주기 누락={missing_lifecycle[:5]}"
        )
    if not delisted:
        raise DataSourceError("검증 기간에 상장폐지된 보통주와 가격 이력이 최소 1개 필요합니다.")
    return {
        "universe_symbol_count": len(symbols),
        "historical_delisted_count": len(delisted),
        "delisted_symbols": sorted(delisted),
    }


def _validate_designations(
    records: list[dict[str, Any]],
    universe_symbols: set[str],
    *,
    period_start: date,
    period_end: date,
) -> dict[str, Any]:
    if not isinstance(records, list) or not records:
        raise DataSourceError("완전성 검증을 위해 과거 지정 상태가 최소 1건 필요합니다.")
    states: set[str] = set()
    for index, record in enumerate(records):
        symbol = str(record.get("symbol", ""))
        state = str(record.get("state", ""))
        if symbol not in universe_symbols:
            raise DataSourceError(f"designation_history[{index}] 종목이 유니버스에 없습니다.")
        if state not in DISQUALIFYING_STATES:
            raise DataSourceError(f"designation_history[{index}] 지원하지 않는 상태: {state}")
        start = _iso_date(record.get("effective_from"), f"designation[{index}].effective_from")
        end = _iso_date(record.get("effective_to"), f"designation[{index}].effective_to")
        if end < start:
            raise DataSourceError(f"designation_history[{index}] 종료일이 시작일보다 빠릅니다.")
        if start > period_end or end < period_start:
            raise DataSourceError(f"designation_history[{index}]가 검증 기간 밖입니다.")
        states.add(state)
    return {"designation_record_count": len(records), "designation_states": sorted(states)}


def _validate_price_lifecycle(
    instruments: list[dict[str, Any]],
    universe_by_symbol: dict[str, dict[str, Any]],
    *,
    period_start: date,
    period_end: date,
) -> None:
    for instrument in instruments:
        symbol = str(instrument["symbol"])
        lifecycle = universe_by_symbol[symbol]
        listed_on = _iso_date(lifecycle["listed_on"], f"{symbol}.listed_on")
        delisted_on = (
            _iso_date(lifecycle["delisted_on"], f"{symbol}.delisted_on")
            if lifecycle.get("delisted_on")
            else None
        )
        bars = instrument.get("bars", [])
        if len(bars) < 200:
            raise DataSourceError(f"{symbol}: 최소 200거래일 가격 이력이 필요합니다.")
        bar_dates = [_iso_date(item.get("date"), f"{symbol}.bar.date") for item in bars]
        if bar_dates != sorted(set(bar_dates)):
            raise DataSourceError(f"{symbol}: 가격 날짜가 중복 또는 역순입니다.")
        if bar_dates[0] < listed_on:
            raise DataSourceError(f"{symbol}: 상장 전 가격이 포함됐습니다.")
        if bar_dates[-1] > period_end:
            raise DataSourceError(f"{symbol}: 검증 종료일 뒤 가격이 포함됐습니다.")
        if delisted_on and bar_dates[-1] > delisted_on:
            raise DataSourceError(f"{symbol}: 상장폐지일 뒤 가격이 포함됐습니다.")


def load_qualified_market_bundle(
    file_name: str, root: str | Path | None = None
) -> dict[str, Any]:
    path = _qualified_path(file_name, root)
    payload = _read_payload(path)
    required = {
        "schema_version",
        "as_of",
        "source",
        "license_basis",
        "benchmark",
        "instruments",
        "qualification",
    }
    if not required.issubset(payload):
        raise DataSourceError("자격 번들 필수 필드: " + ", ".join(sorted(required)))
    if payload["schema_version"] != QUALIFIED_SCHEMA_VERSION:
        raise DataSourceError(
            f"자격 번들 schema_version은 {QUALIFIED_SCHEMA_VERSION}이어야 합니다."
        )
    if payload["source"] not in QUALIFIED_SOURCES:
        raise DataSourceError(
            "자격 번들은 KRX export, 공식 공공데이터+KIND, 또는 계약 공급자 데이터여야 합니다."
        )
    if payload["license_basis"] not in {
        "USER_AUTHORIZED_INTERNAL_RESEARCH",
        "OFFICIAL_DATA_CONTRACT",
    }:
        raise DataSourceError("내부 연구 이용 권한 또는 공식 데이터 계약 근거가 필요합니다.")
    try:
        as_of = datetime.fromisoformat(str(payload["as_of"]))
    except ValueError as error:
        raise DataSourceError("as_of는 timezone을 포함한 ISO-8601 시각이어야 합니다.") from error
    if as_of.tzinfo is None:
        raise DataSourceError("as_of에는 timezone이 필요합니다.")
    qualification = payload["qualification"]
    if not isinstance(qualification, dict):
        raise DataSourceError("qualification은 객체여야 합니다.")
    period_start = _iso_date(qualification.get("period_start"), "qualification.period_start")
    period_end = _iso_date(qualification.get("period_end"), "qualification.period_end")
    if period_end != as_of.date():
        raise DataSourceError("qualification.period_end는 as_of 날짜와 같아야 합니다.")
    if (period_end - period_start).days < 365 * 5:
        raise DataSourceError("자격 데이터는 최소 5년의 달력 기간을 덮어야 합니다.")
    instruments = payload["instruments"]
    if not isinstance(instruments, list):
        raise DataSourceError("instruments는 배열이어야 합니다.")
    universe = qualification.get("universe_history")
    universe_metrics = _validate_universe(
        universe,
        instruments,
        period_start=period_start,
        period_end=period_end,
    )
    universe_by_symbol = {str(item["symbol"]): item for item in universe}
    _validate_price_lifecycle(
        instruments,
        universe_by_symbol,
        period_start=period_start,
        period_end=period_end,
    )
    designation_metrics = _validate_designations(
        qualification.get("designation_history"),
        set(universe_by_symbol),
        period_start=period_start,
        period_end=period_end,
    )
    artifact_metrics = _validate_artifacts(
        qualification.get("source_artifacts"),
        source=payload["source"],
        period_start=period_start,
        period_end=period_end,
    )
    normalized_checksums = _validate_normalized_artifacts(
        qualification["source_artifacts"],
        benchmark=payload["benchmark"],
        instruments=instruments,
        universe=universe,
        designations=qualification["designation_history"],
    )
    file_checksum = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "as_of": payload["as_of"],
        "source": payload["source"],
        "license_basis": payload["license_basis"],
        "market": "KR",
        "benchmark": payload["benchmark"],
        "instruments": instruments,
        "evidence": payload.get("evidence", []),
        "universe_history": universe,
        "designation_history": qualification["designation_history"],
        "collection_manifest": {
            "qualification_schema_version": QUALIFIED_SCHEMA_VERSION,
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "survivorship_bias_controlled": True,
            "historical_designation_states_complete": True,
            "fundamental_revision_safe": False,
            "fundamental_revision_verification": "NOT_YET_IMPLEMENTED",
            "source_artifact_counts": artifact_metrics,
            "normalized_artifact_sha256": normalized_checksums,
            **universe_metrics,
            **designation_metrics,
            "source_bundle_file": file_name,
            "source_bundle_sha256": file_checksum,
            "derived_by": "MONEYGUN_QUALIFICATION_VALIDATOR_V1",
            "redistribution_allowed": False,
        },
    }


def qualification_status(
    root: str | Path | None, latest_snapshot: dict[str, Any] | None
) -> dict[str, Any]:
    import_root = Path(root).resolve() if root else market_import_directory()
    files: list[str] = []
    if import_root.is_dir():
        files = sorted(
            item.name
            for item in import_root.iterdir()
            if item.is_file() and item.name.endswith(QUALIFIED_SUFFIXES)
        )
    manifest = latest_snapshot.get("collection_manifest", {}) if latest_snapshot else {}
    collection_path = import_root / "raw" / "public-data" / "collection-manifest.json"
    collection: dict[str, Any] | None = None
    if collection_path.is_file():
        try:
            candidate = json.loads(collection_path.read_text(encoding="utf-8"))
            kind = candidate.get("kind", {})
            normalization = candidate.get("normalization", {})
            candidate_checks = {
                "public_price_history_collected": int(
                    candidate.get("prices", {}).get("total_count", 0)
                )
                > 0,
                "issuance_history_collected": int(
                    candidate.get("issuance", {}).get("total_count", 0)
                )
                > 0,
                "normalized_market_database": bool(
                    normalization.get("database_sha256")
                    and Path(str(normalization.get("database", ""))).is_file()
                ),
                "benchmark_history_complete": int(
                    normalization.get("benchmark_rows", 0)
                )
                >= 1_200,
                **{
                    key: bool(value)
                    for key, value in kind.get("checks", {}).items()
                },
            }
            blockers = [key for key, value in candidate_checks.items() if not value]
            collection = {
                "state": "READY_FOR_QUALIFIED_BUNDLE" if not blockers else "INCOMPLETE",
                "checks": candidate_checks,
                "metrics": {
                    "price_rows": int(candidate.get("prices", {}).get("total_count", 0)),
                    "issuance_rows": int(
                        candidate.get("issuance", {}).get("total_count", 0)
                    ),
                    "instrument_count": int(normalization.get("instrument_count", 0)),
                    "historical_delisted_count": int(
                        normalization.get("historical_delisted_count", 0)
                    ),
                    "designation_interval_count": int(
                        normalization.get("designation_interval_count", 0)
                    ),
                },
                "blockers": blockers,
                "manifest": str(collection_path),
            }
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            collection = {
                "state": "INVALID",
                "checks": {},
                "metrics": {},
                "blockers": ["collection_manifest_invalid"],
                "manifest": str(collection_path),
            }
    checks = {
        "authorized_real_source": bool(
            latest_snapshot
            and latest_snapshot.get("source")
            in QUALIFIED_SOURCES
        ),
        "minimum_five_year_period": bool(
            manifest.get("period_start") and manifest.get("period_end")
        ),
        "survivorship_bias_controlled": bool(manifest.get("survivorship_bias_controlled")),
        "historical_designation_states_complete": bool(
            manifest.get("historical_designation_states_complete")
        ),
        "historical_delisted_included": int(manifest.get("historical_delisted_count", 0)) > 0,
        "source_artifacts_verified": bool(
            manifest.get("source_artifact_counts")
            and manifest.get("normalized_artifact_sha256")
        ),
    }
    return {
        "state": "QUALIFIED_DATA_READY" if all(checks.values()) else "DATA_REQUIRED",
        "checks": checks,
        "latest_snapshot_id": latest_snapshot.get("id") if latest_snapshot else None,
        "latest_snapshot_source": latest_snapshot.get("source") if latest_snapshot else None,
        "inbox": str(import_root),
        "qualified_bundle_files": files,
        "collection": collection,
        "next_action": (
            "자격 스냅샷으로 전략 OOS를 실행하세요."
            if all(checks.values())
            else (
                "관리·환기·거래정지의 5년 지정/해제 이력을 보완하세요."
                if collection and collection.get("state") == "INCOMPLETE"
                else "공식 원본을 schema 2.0 자격 번들로 가져오세요."
            )
        ),
        "trading_enabled": False,
    }
