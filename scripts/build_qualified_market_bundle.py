from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
API_SOURCE = PROJECT_ROOT / "apps" / "api" / "src"
sys.path.insert(0, str(API_SOURCE))

from moneygun_api.public_market_data import (
    build_qualified_bundle_from_normalized_database,
)


def main() -> int:
    report = build_qualified_bundle_from_normalized_database(
        PROJECT_ROOT
        / "data"
        / "imports"
        / "raw"
        / "public-data"
        / "normalized-market.sqlite3",
        PROJECT_ROOT
        / "data"
        / "imports"
        / "raw"
        / "public-data"
        / "collection-manifest.json",
        PROJECT_ROOT
        / "data"
        / "imports"
        / "moneygun-official-20260828.qualified.json.gz",
    )
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
