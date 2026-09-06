from __future__ import annotations

import argparse

from .kiwoom import KiwoomReadOnlyClient
from .operations import run_daily_shadow_operations
from .storage import Database


def main() -> None:
    parser = argparse.ArgumentParser(description="Signal Guild end-of-day shadow operations worker")
    parser.add_argument(
        "--force", action="store_true", help="time gate only; never enables trading"
    )
    args = parser.parse_args()
    database = Database()
    database.initialize()
    result = run_daily_shadow_operations(database, KiwoomReadOnlyClient(), force=args.force)
    print(
        {
            "id": result["id"],
            "state": result["state"],
            "stage": result["current_stage"],
            "trading_enabled": False,
        }
    )


if __name__ == "__main__":
    main()
