from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from .data_sources import DataSourceError, market_import_directory
from .public_market_data import (
    PublicDataPortalClient,
    build_qualified_bundle_from_normalized_database,
    collect_issuance_history,
    collect_kind_market_action_history,
    collect_price_history,
    inspect_kind_sources,
    merge_kiwoom_benchmark_history,
    normalize_collected_market_data,
)
from .qualification import QUALIFIED_SOURCES, load_qualified_market_bundle
from .research import finalize_snapshot
from .storage import Database

REFRESH_SCHEMA_VERSION = "1.0"
REFRESH_SCHEDULE_KST = "매일 06:00~08:20 보충 실행 · 거래일 18:30~23:55 본 실행"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _five_year_start(end: date) -> date:
    try:
        return end.replace(year=end.year - 5)
    except ValueError:
        return end.replace(year=end.year - 5, day=28)


def latest_public_trading_date(
    client: PublicDataPortalClient,
    reference_date: date,
    *,
    lookback_days: int = 10,
) -> date:
    """Find the newest completed day actually published by the official API."""
    window_start = reference_date - timedelta(days=lookback_days)
    page = client.price_page(
        start=window_start,
        end=reference_date,
        page=1,
        page_size=1_000,
    )
    published: list[date] = []
    for item in page.items:
        raw = str(item.get("basDt", ""))
        if len(raw) == 8 and raw.isdigit():
            candidate = date(int(raw[:4]), int(raw[4:6]), int(raw[6:]))
            if candidate <= reference_date:
                published.append(candidate)
    if page.total_count > 0 and published:
        return max(published)
    raise DataSourceError("최근 10일 안에 공공데이터 주식시세 완료 거래일이 없습니다.")


def refresh_qualified_market_data(
    database: Database,
    *,
    reference_date: date | None = None,
    import_root: str | Path | None = None,
    workers: int = 4,
    progress: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Collect, validate, and promote one complete official five-year snapshot.

    Every run uses an as-of-specific staging directory. A failed run can never
    overwrite the active snapshot or masquerade as a newer qualification.
    """

    def report(stage: str, **details: Any) -> None:
        if progress:
            progress(stage, details)

    root = Path(import_root).resolve() if import_root else market_import_directory()
    client = PublicDataPortalClient()
    if not client.configured:
        raise DataSourceError("DATA_GO_KR_SERVICE_KEY 인증키가 필요합니다.")
    target_day = latest_public_trading_date(client, reference_date or date.today())
    latest = database.get_latest_snapshot_metadata_for_sources(
        tuple(sorted(QUALIFIED_SOURCES))
    )
    if (
        latest
        and latest.get("quality_state") == "PASS"
        and str(latest.get("as_of", ""))[:10] >= target_day.isoformat()
    ):
        return {
            "state": "CURRENT",
            "as_of": str(latest["as_of"])[:10],
            "snapshot_id": latest["id"],
            "message": "이미 최신 공식 완료 거래일까지 갱신되어 있습니다.",
        }

    period_start = _five_year_start(target_day)
    dated_root = root / "daily-refresh" / target_day.strftime("%Y%m%d")
    raw_root = dated_root / "raw"
    report("COLLECTING_KIND", as_of=target_day.isoformat())
    market_actions = collect_kind_market_action_history(
        start=period_start,
        end=target_day,
        output_dir=raw_root / "kind" / "market-actions",
    )
    report("COLLECTING_PRICES", as_of=target_day.isoformat())
    prices = collect_price_history(
        client,
        start=period_start,
        end=target_day,
        output_dir=raw_root / "public-data" / "prices",
        workers=workers,
    )
    report("COLLECTING_ISSUANCE", as_of=target_day.isoformat())
    issuance = collect_issuance_history(
        client,
        as_of=target_day,
        output_dir=raw_root / "public-data" / "issuance",
        workers=workers,
    )
    if issuance["total_count"] <= 0:
        raise DataSourceError("완료 거래일 기준 주식발행정보가 아직 게시되지 않았습니다.")
    kind = inspect_kind_sources(
        raw_root / "kind", period_start=period_start, period_end=target_day
    )
    if not kind["historical_designation_states_complete"]:
        failed = [key for key, value in kind["checks"].items() if not value]
        raise DataSourceError("KIND 6종 이력 완전성 실패: " + ", ".join(failed))

    report("NORMALIZING", as_of=target_day.isoformat())
    normalized_database = raw_root / "public-data" / "normalized-market.sqlite3"
    normalization = normalize_collected_market_data(
        raw_root,
        period_start=period_start,
        period_end=target_day,
        output_database=normalized_database,
    )
    if int(normalization.get("benchmark_rows", 0)) < 1_200:
        if database.backend != "sqlite":
            raise DataSourceError(
                "PostgreSQL 운영에서는 감사된 KODEX 200 벤치마크 병합 경로가 필요합니다."
            )
        report("MERGING_BENCHMARK", as_of=target_day.isoformat())
        normalization.update(
            merge_kiwoom_benchmark_history(
                normalized_database,
                database.path,
                period_start=period_start,
                period_end=target_day,
            )
        )
    if int(normalization.get("benchmark_rows", 0)) < 1_200:
        raise DataSourceError("KODEX 200 공식 벤치마크가 5년 구간을 충분히 덮지 않습니다.")
    manifest = {
        "schema_version": REFRESH_SCHEMA_VERSION,
        "source": "OFFICIAL_PUBLIC_DATA_API_KIND_AND_KIWOOM",
        "prices": prices,
        "issuance": issuance,
        "kind": kind,
        "kind_market_actions": market_actions,
        "normalization": normalization,
        "generated_at": _utc_now(),
        "trading_enabled": False,
    }
    manifest_path = raw_root / "public-data" / "collection-manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_manifest = manifest_path.with_suffix(".json.tmp")
    temporary_manifest.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary_manifest.replace(manifest_path)

    report("BUILDING_BUNDLE", as_of=target_day.isoformat())
    bundle_name = f"moneygun-official-{target_day:%Y%m%d}.qualified.json.gz"
    bundle_path = root / bundle_name
    bundle = build_qualified_bundle_from_normalized_database(
        normalized_database, manifest_path, bundle_path
    )
    report("VALIDATING", as_of=target_day.isoformat(), bundle=bundle_name)
    qualified = finalize_snapshot(load_qualified_market_bundle(bundle_name, root=root))
    if qualified["quality"]["state"] != "PASS":
        raise DataSourceError("생성된 자격 번들이 최종 품질 검증을 통과하지 못했습니다.")
    saved = database.save_snapshot(qualified)
    return {
        "state": "SUCCEEDED",
        "as_of": target_day.isoformat(),
        "snapshot_id": saved["id"],
        "bundle_file": bundle_name,
        "bundle_sha256": _sha256(bundle_path),
        "instrument_count": saved["quality"]["instrument_count"],
        "bar_count": saved["quality"]["bar_count"],
        "designation_interval_count": bundle["designation_interval_count"],
        "message": "공식 5년 데이터가 검증되어 활성 스냅샷으로 승격됐습니다.",
    }


class DailyMarketRefreshService:
    def __init__(
        self,
        database: Database,
        *,
        import_root: str | Path | None = None,
        status_path: str | Path | None = None,
        runner: Callable[..., dict[str, Any]] = refresh_qualified_market_data,
    ) -> None:
        self.database = database
        self.import_root = (
            Path(import_root).resolve() if import_root else market_import_directory()
        )
        self.status_path = (
            Path(status_path).resolve()
            if status_path
            else Path("data/desktop-live/daily-market-refresh-status.json").resolve()
        )
        self.runner = runner
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def status(self) -> dict[str, Any]:
        default = {
            "schema_version": REFRESH_SCHEMA_VERSION,
            "state": "NOT_RUN",
            "stage": "WAITING",
            "schedule_kst": REFRESH_SCHEDULE_KST,
            "last_started_at": None,
            "last_finished_at": None,
            "last_success_as_of": None,
            "snapshot_id": None,
            "error": None,
            "automatic_order_submission": False,
        }
        if not self.status_path.is_file():
            return default
        try:
            stored = json.loads(self.status_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return {**default, "state": "FAILED", "error": "갱신 상태 파일을 읽지 못했습니다."}
        if stored.get("state") == "RUNNING" and not (
            self._thread and self._thread.is_alive()
        ):
            stored = {
                **stored,
                "state": "INTERRUPTED",
                "error": "이전 갱신이 PC 또는 서비스 종료로 중단됐습니다. 자동 재시도합니다.",
            }
        return {**default, **stored, "automatic_order_submission": False}

    def _write_status(self, payload: dict[str, Any]) -> dict[str, Any]:
        value = {
            "schema_version": REFRESH_SCHEMA_VERSION,
            "schedule_kst": REFRESH_SCHEDULE_KST,
            **payload,
            "automatic_order_submission": False,
        }
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.status_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.status_path)
        return value

    def _run(self, run_id: str, reference_date: date | None) -> None:
        def progress(stage: str, details: dict[str, Any]) -> None:
            current = self.status()
            self._write_status({**current, "state": "RUNNING", "stage": stage, **details})

        try:
            result = self.runner(
                self.database,
                reference_date=reference_date,
                import_root=self.import_root,
                progress=progress,
            )
            finished = _utc_now()
            self._write_status(
                {
                    "run_id": run_id,
                    "state": result["state"],
                    "stage": "COMPLETE",
                    "last_started_at": self.status().get("last_started_at"),
                    "last_finished_at": finished,
                    "last_success_as_of": result.get("as_of"),
                    "snapshot_id": result.get("snapshot_id"),
                    "bundle_file": result.get("bundle_file"),
                    "metrics": {
                        key: result[key]
                        for key in (
                            "instrument_count",
                            "bar_count",
                            "designation_interval_count",
                        )
                        if key in result
                    },
                    "message": result.get("message"),
                    "error": None,
                }
            )
            self.database.record_data_refresh_event("completed", run_id, result)
        except Exception as error:  # keep the active qualified snapshot unchanged
            finished = _utc_now()
            message = str(error) or error.__class__.__name__
            current = self.status()
            self._write_status(
                {
                    **current,
                    "run_id": run_id,
                    "state": "FAILED",
                    "stage": "FAILED",
                    "last_finished_at": finished,
                    "error": message[:500],
                }
            )
            self.database.record_data_refresh_event(
                "failed", run_id, {"error": message[:500], "failed_at": finished}
            )
        finally:
            self._lock.release()

    def trigger(
        self,
        *,
        reference_date: date | None = None,
        background: bool = True,
    ) -> dict[str, Any]:
        if not self._lock.acquire(blocking=False):
            return {**self.status(), "accepted": False, "reason": "ALREADY_RUNNING"}
        run_id = f"refresh_{uuid4().hex[:16]}"
        started = _utc_now()
        previous = self.status()
        self._write_status(
            {
                "run_id": run_id,
                "state": "RUNNING",
                "stage": "DISCOVERING_AS_OF",
                "last_started_at": started,
                "last_finished_at": None,
                "last_success_as_of": previous.get("last_success_as_of"),
                "snapshot_id": previous.get("snapshot_id"),
                "error": None,
            }
        )
        self.database.record_data_refresh_event(
            "started", run_id, {"reference_date": (reference_date or date.today()).isoformat()}
        )
        if background:
            self._thread = threading.Thread(
                target=self._run,
                args=(run_id, reference_date),
                name="moneygun-daily-market-refresh",
                daemon=True,
            )
            self._thread.start()
            return {**self.status(), "accepted": True}
        self._run(run_id, reference_date)
        return {**self.status(), "accepted": True}
