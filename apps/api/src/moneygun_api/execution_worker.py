from __future__ import annotations

import argparse
import json
import time
from typing import Any
from uuid import uuid4

from .execution import (
    ExecutionError,
    ExecutionGuardian,
    reconcile_broker_orders,
    reconcile_managed_account,
    run_l2_automation_tick,
)
from .kiwoom import KiwoomError, KiwoomReadOnlyClient
from .storage import Database


def run_once(
    database: Database,
    readonly: KiwoomReadOnlyClient,
    guardian: ExecutionGuardian,
    *,
    mission_id: str = "mission_focus_001",
    worker_id: str = "execution-worker",
) -> dict[str, Any]:
    if not readonly.config.configured:
        raise ExecutionError("키움 조회 연결이 없어 실행 워커가 실패 폐쇄했습니다.")
    reconciliation = reconcile_managed_account(
        database, readonly, mission_id=mission_id
    )
    if reconciliation["status"] != "PASS":
        return {"action": "RECONCILIATION_BLOCKED", "reconciliation": reconciliation}
    order_reconciliation = reconcile_broker_orders(
        database, readonly, mission_id=mission_id
    )
    automation = run_l2_automation_tick(
        database, guardian, mission_id=mission_id, worker_id=worker_id
    )
    return {
        "action": automation["action"],
        "reconciliation": reconciliation,
        "order_reconciliation": order_reconciliation,
        "automation": automation,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="MoneyGun single-leader execution guardian")
    parser.add_argument("--once", action="store_true", help="run one guarded cycle and exit")
    parser.add_argument("--interval", type=int, default=60)
    arguments = parser.parse_args()
    database = Database()
    database.initialize()
    readonly = KiwoomReadOnlyClient()
    guardian = ExecutionGuardian()
    worker_id = f"execution-worker-{uuid4().hex}"
    while True:
        try:
            result = run_once(database, readonly, guardian, worker_id=worker_id)
        except (ExecutionError, KiwoomError) as error:
            result = {"action": "BLOCKED", "reason": str(error)}
        print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
        if arguments.once:
            return
        time.sleep(max(10, arguments.interval))


if __name__ == "__main__":
    main()
