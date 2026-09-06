from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
API_SOURCE = PROJECT_ROOT / "apps" / "api" / "src"
sys.path.insert(0, str(API_SOURCE))

from moneygun_api.public_market_data import (
    PublicDataPortalClient,
    collect_issuance_history,
    collect_kind_market_action_history,
    collect_price_history,
    inspect_kind_sources,
    merge_kiwoom_benchmark_history,
    normalize_collected_market_data,
    refresh_normalized_kind_data,
)


def _load_local_key() -> None:
    if os.getenv("DATA_GO_KR_SERVICE_KEY"):
        return
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8-sig").splitlines():
        if line.startswith("DATA_GO_KR_SERVICE_KEY="):
            os.environ["DATA_GO_KR_SERVICE_KEY"] = line.split("=", 1)[1].strip()
            return


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="MoneyGun 공식 한국시장 원본 수집")
    parser.add_argument("--start", default="2021-08-28")
    parser.add_argument("--end", default="2026-08-28")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument(
        "--root", default=str(PROJECT_ROOT / "data" / "imports" / "raw")
    )
    parser.add_argument(
        "--rebuild-normalized",
        action="store_true",
        help="기존 정규화 가격 DB를 버리고 원본 페이지에서 다시 생성",
    )
    args = parser.parse_args()
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    root = Path(args.root).resolve()
    _load_local_key()
    client = PublicDataPortalClient()
    market_actions = collect_kind_market_action_history(
        start=start,
        end=end,
        output_dir=root / "kind" / "market-actions",
    )
    prices = collect_price_history(
        client,
        start=start,
        end=end,
        output_dir=root / "public-data" / "prices",
        workers=args.workers,
    )
    issuance = collect_issuance_history(
        client,
        as_of=end,
        output_dir=root / "public-data" / "issuance",
        workers=args.workers,
    )
    kind = inspect_kind_sources(root / "kind", period_start=start, period_end=end)
    normalized_database = root / "public-data" / "normalized-market.sqlite3"
    normalize = (
        normalize_collected_market_data
        if args.rebuild_normalized or not normalized_database.exists()
        else refresh_normalized_kind_data
    )
    normalization = normalize(
        root,
        period_start=start,
        period_end=end,
        output_database=normalized_database,
    )
    snapshot_database = PROJECT_ROOT / "data" / "moneygun.sqlite3"
    if snapshot_database.exists():
        benchmark = merge_kiwoom_benchmark_history(
            normalized_database,
            snapshot_database,
            period_start=start,
            period_end=end,
        )
        normalization.update(benchmark)
        normalization["database_sha256"] = _sha256(normalized_database)
    manifest = {
        "schema_version": "1.0",
        "source": "OFFICIAL_PUBLIC_DATA_API_KIND_AND_KIWOOM",
        "prices": prices,
        "issuance": issuance,
        "kind": kind,
        "kind_market_actions": market_actions,
        "normalization": normalization,
        "trading_enabled": False,
    }
    output = root / "public-data" / "collection-manifest.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "manifest": str(output),
                "price_rows": prices["total_count"],
                "price_pages": prices["page_count"],
                "issuance_rows": issuance["total_count"],
                "designation_complete": kind["historical_designation_states_complete"],
                "instrument_count": normalization["instrument_count"],
                "historical_delisted_count": normalization["historical_delisted_count"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
