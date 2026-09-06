from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any

from .execution import (
    ExecutionGuardian,
    build_live_intent,
    reconcile_managed_account,
    sync_official_quote,
)
from .kiwoom import KiwoomReadOnlyClient
from .pilot import PILOT_MISSION_ID
from .storage import Database

KST = timezone(timedelta(hours=9))
EXIT_STATE_ID = "L0-EXIT-GUARD-v1"
MAX_HOLD_DAYS = {
    "FOCUS-MOMENTUM-KR-v1": 14,
    "FOCUS-MOMENTUM-KR-v2-L0": 14,
    "CLOSE-AUCTION-KR-v3-L0": 10,
    "OPEN-RANGE-KR-v3-L0": 0,
    "BALANCED-TREND-KR-v1-L0": 90,
    "LONG-TREND-KR-v1-L0": 365,
}
TRAILS = {
    "FOCUS-MOMENTUM-KR-v2-L0": (500, 200),
    "CLOSE-AUCTION-KR-v3-L0": (500, 200),
    "OPEN-RANGE-KR-v3-L0": (150, 80),
    "BALANCED-TREND-KR-v1-L0": (1_000, 400),
    "LONG-TREND-KR-v1-L0": (2_000, 700),
}


def _floor_tick(price: float) -> int:
    rounded = round(price)
    if rounded < 2_000:
        unit = 1
    elif rounded < 5_000:
        unit = 5
    elif rounded < 20_000:
        unit = 10
    elif rounded < 50_000:
        unit = 50
    elif rounded < 200_000:
        unit = 100
    elif rounded < 500_000:
        unit = 500
    else:
        unit = 1_000
    return max(unit, math.floor(price / unit) * unit)


def _positions(database: Database) -> list[dict[str, Any]]:
    positions: dict[str, dict[str, Any]] = {}
    for intent in reversed(database.list_live_order_intents(PILOT_MISSION_ID)):
        filled_quantity = sum(int(fill["quantity"]) for fill in intent["fills"])
        if filled_quantity <= 0:
            continue
        symbol = intent["symbol"]
        position = positions.setdefault(
            symbol,
            {
                "symbol": symbol,
                "name": intent["name"],
                "quantity": 0,
                "cost_krw": 0,
                "entry_at": intent["fills"][0]["occurred_at"],
                "invalidation_price_krw": int(intent["invalidation_price_krw"]),
                "strategy_id": intent["strategy_id"],
                "source_mission_id": intent.get("source_mission_id"),
                "shadow_order_id": intent["shadow_order_id"],
            },
        )
        if intent["side"] == "BUY":
            position["quantity"] += filled_quantity
            position["cost_krw"] += sum(
                int(fill["quantity"]) * int(fill["price_krw"]) for fill in intent["fills"]
            )
            position["entry_at"] = min(position["entry_at"], intent["fills"][0]["occurred_at"])
            position["invalidation_price_krw"] = int(intent["invalidation_price_krw"])
            position["strategy_id"] = intent["strategy_id"]
            position["source_mission_id"] = intent.get("source_mission_id")
            position["shadow_order_id"] = intent["shadow_order_id"]
        else:
            average = position["cost_krw"] // position["quantity"] if position["quantity"] else 0
            position["quantity"] = max(0, position["quantity"] - filled_quantity)
            position["cost_krw"] = position["quantity"] * average
    return [item for item in positions.values() if item["quantity"] > 0]


def _exit_reason(
    position: dict[str, Any], current: int, high: int, now_kst: datetime
) -> str | None:
    strategy_id = position["strategy_id"]
    if current <= int(position["invalidation_price_krw"]):
        return "INVALIDATION"
    if strategy_id == "OPEN-RANGE-KR-v3-L0" and now_kst.hour * 60 + now_kst.minute >= 10 * 60 + 55:
        return "FORCE_FLAT_11_00"
    entry_at = datetime.fromisoformat(position["entry_at"])
    held_days = (now_kst.date() - entry_at.astimezone(KST).date()).days
    max_days = MAX_HOLD_DAYS.get(strategy_id)
    if max_days is not None and held_days >= max_days:
        return "MAX_HOLD_REVIEW"
    average = position["cost_krw"] / position["quantity"]
    activation_bps, trail_bps = TRAILS.get(strategy_id, (1_000, 500))
    if high >= average * (1 + activation_bps / 10_000) and current <= high * (
        1 - trail_bps / 10_000
    ):
        return "PROFIT_TRAIL"
    return None


def l0_exit_status(database: Database) -> dict[str, Any]:
    return {
        "positions": _positions(database),
        "existing_broker_holdings_ignored": True,
        "automatic_broker_submission": False,
    }


def run_l0_exit_tick(
    database: Database,
    client: KiwoomReadOnlyClient,
    guardian: ExecutionGuardian,
    *,
    now_kst: datetime | None = None,
) -> dict[str, Any]:
    now_kst = (now_kst or datetime.now(KST)).astimezone(KST)
    positions = _positions(database)
    if not positions:
        return {"phase": "NO_MANAGED_POSITION", "action": "NO_ACTION", **l0_exit_status(database)}
    minute = now_kst.hour * 60 + now_kst.minute
    if now_kst.weekday() >= 5 or not (9 * 60 + 5 <= minute <= 15 * 60 + 20):
        return {
            "phase": "OUTSIDE_EXIT_WINDOW",
            "action": "NO_ACTION",
            **l0_exit_status(database),
        }
    runtime = database.get_strategy_runtime_state(EXIT_STATE_ID, "GLOBAL") or {"highs": {}}
    evaluations: list[dict[str, Any]] = []
    for position in positions:
        quote = sync_official_quote(database, client, position["symbol"])
        current = int(quote["payload"]["current_price_krw"])
        previous_high = int(runtime["highs"].get(position["symbol"], 0))
        high = max(previous_high, current)
        runtime["highs"][position["symbol"]] = high
        reason = _exit_reason(position, current, high, now_kst)
        evaluations.append(
            {
                **position,
                "current_price_krw": current,
                "high_watermark_krw": high,
                "exit_reason": reason,
            }
        )
    database.save_strategy_runtime_state(EXIT_STATE_ID, "GLOBAL", runtime)
    candidate = next((item for item in evaluations if item["exit_reason"]), None)
    if candidate is None:
        return {
            "phase": "MONITORING",
            "action": "HOLD",
            "evaluations": evaluations,
            "automatic_broker_submission": False,
        }
    original = database.get_shadow_order(candidate["shadow_order_id"])
    cycle = database.get_cycle(original["cycle_id"])
    source_mission_id = candidate["source_mission_id"] or original["mission_id"]
    order = database.create_shadow_order(
        {
            "mission_id": source_mission_id,
            "cycle_id": cycle["id"],
            "trade_date": now_kst.date().isoformat(),
            "next_session_date": now_kst.date().isoformat(),
            "symbol": candidate["symbol"],
            "name": candidate["name"],
            "side": "SELL",
            "quantity": candidate["quantity"],
            "signal_close_krw": candidate["current_price_krw"],
            "limit_price_krw": _floor_tick(candidate["current_price_krw"] * 0.997),
            "invalidation_price_krw": candidate["invalidation_price_krw"],
            "simulation_source": "KIWOOM_L0_EXIT_QUOTE",
            "entry_style": "L0_MANAGED_EXIT_LIMIT",
            "exit_reason": candidate["exit_reason"],
            "broker_submitted": False,
            "trading_enabled": False,
        },
        idempotency_key=(
            f"{EXIT_STATE_ID}:{now_kst.date().isoformat()}:{candidate['symbol']}:"
            f"{candidate['exit_reason']}"
        ),
    )
    reconciliation = reconcile_managed_account(database, client, mission_id=PILOT_MISSION_ID)
    sync_official_quote(database, client, candidate["symbol"])
    intent = build_live_intent(
        database,
        guardian,
        shadow_order_id=order["id"],
        idempotency_key=f"{EXIT_STATE_ID}:{order['id']}:intent",
        execution_mission_id=PILOT_MISSION_ID,
    )
    return {
        "phase": "AWAITING_OWNER_EXIT",
        "action": "EXIT_INTENT_READY" if intent["state"] == "AWAITING_APPROVAL" else "BLOCKED",
        "evaluation": candidate,
        "shadow_order": order,
        "reconciliation": reconciliation,
        "intent": intent,
        "automatic_broker_submission": False,
    }
