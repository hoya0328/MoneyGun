from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from .performance import performance_report
from .storage import Database

KST = timezone(timedelta(hours=9))

PILOT_MISSION_ID = "mission_l0_pilot_001"
PILOT_CAPITAL_KRW = 50_000
PILOT_ORDER_BUDGET_KRW = 45_000
PILOT_DAILY_LOSS_KRW = 1_500
PILOT_TOTAL_LOSS_KRW = 5_000
PILOT_MAX_POSITIONS = 1
PILOT_MAX_DAILY_ENTRIES = 3
PILOT_ACTIVE_VERSION = "v1.0"
PILOT_CHECKPOINTS = {15: "v1.1", 30: "v1.2", 45: "v1.3", 60: "v2.0"}


def _age_seconds(timestamp: str) -> float:
    return max(0.0, (datetime.now(UTC) - datetime.fromisoformat(timestamp)).total_seconds())


def _open_lots(database: Database, mission_id: str) -> dict[str, list[list[int]]]:
    lots: dict[str, list[list[int]]] = defaultdict(list)
    intents = sorted(
        database.list_live_order_intents(mission_id),
        key=lambda item: (
            min(
                (fill["occurred_at"] for fill in item["fills"]),
                default=item["created_at"],
            ),
            0 if item["side"] == "BUY" else 1,
            item["created_at"],
            item["id"],
        ),
    )
    for intent in intents:
        symbol = intent["symbol"]
        for fill in sorted(intent["fills"], key=lambda item: item["occurred_at"]):
            quantity = int(fill["quantity"])
            price = int(fill["price_krw"])
            if intent["side"] == "BUY":
                lots[symbol].append([quantity, price])
                continue
            remaining = quantity
            while remaining > 0 and lots[symbol]:
                used = min(remaining, lots[symbol][0][0])
                remaining -= used
                lots[symbol][0][0] -= used
                if lots[symbol][0][0] == 0:
                    lots[symbol].pop(0)
    return {symbol: rows for symbol, rows in lots.items() if rows}


def pilot_risk_snapshot(database: Database, mission_id: str = PILOT_MISSION_ID) -> dict[str, Any]:
    mission = database.get_mission(mission_id)
    lots = _open_lots(database, mission_id)
    positions: list[dict[str, Any]] = []
    stale_symbols: list[str] = []
    market_value = 0
    open_cost = 0
    for symbol, rows in sorted(lots.items()):
        quantity = sum(row[0] for row in rows)
        cost = sum(row[0] * row[1] for row in rows)
        quote = database.get_latest_broker_quote(symbol)
        quote_is_fresh = bool(quote and _age_seconds(quote["observed_at"]) <= 15)
        price = int((quote or {}).get("payload", {}).get("current_price_krw", 0))
        if not quote_is_fresh or price <= 0:
            stale_symbols.append(symbol)
            price = 0
        value = quantity * price
        market_value += value
        open_cost += cost
        positions.append(
            {
                "symbol": symbol,
                "quantity": quantity,
                "cost_krw": cost,
                "mark_price_krw": price,
                "market_value_krw": value,
                "unrealized_pnl_krw": value - cost if price else None,
                "quote_fresh": quote_is_fresh,
            }
        )

    marked_equity = (
        int(mission["available_krw"])
        + market_value
        + int(mission["reserved_profit_krw"])
    )
    if stale_symbols:
        marked_equity = int(mission["equity_krw"])
    unrealized_pnl = market_value - open_cost if not stale_symbols else 0
    today = datetime.now(KST).date()
    closed_today = sum(
        int(item["net_pnl_krw"])
        for item in database.list_trade_outcomes(mission_id)
        if item["source_type"] == "LIVE"
        and datetime.fromisoformat(item["exit_at"]).astimezone(KST).date() == today
    )
    daily_defense_pnl = closed_today + unrealized_pnl
    total_pnl = marked_equity - PILOT_CAPITAL_KRW
    return {
        "mission_id": mission_id,
        "seed_capital_krw": PILOT_CAPITAL_KRW,
        "available_krw": int(mission["available_krw"]),
        "marked_equity_krw": marked_equity,
        "market_value_krw": market_value,
        "total_pnl_krw": total_pnl,
        "closed_today_pnl_krw": closed_today,
        "open_unrealized_pnl_krw": unrealized_pnl,
        "daily_defense_pnl_krw": daily_defense_pnl,
        "positions": positions,
        "stale_symbols": stale_symbols,
        "daily_loss_reached": daily_defense_pnl <= -PILOT_DAILY_LOSS_KRW,
        "total_loss_reached": total_pnl <= -PILOT_TOTAL_LOSS_KRW,
        "can_open_new_position": (
            not stale_symbols
            and daily_defense_pnl > -PILOT_DAILY_LOSS_KRW
            and total_pnl > -PILOT_TOTAL_LOSS_KRW
        ),
        "valuation_note": (
            "당일 청산손익과 현재 미청산손익을 합친 보수적 신규진입 방어값입니다."
        ),
    }


def pilot_daily_entry_count(database: Database, mission_id: str = PILOT_MISSION_ID) -> int:
    today = datetime.now(KST).date()
    return sum(
        1
        for intent in database.list_live_order_intents(mission_id)
        if intent["side"] == "BUY"
        and datetime.fromisoformat(intent["created_at"]).astimezone(KST).date() == today
        and intent["state"] not in {"REJECTED", "FAILED", "EXPIRED"}
    )


def record_pilot_operating_day(database: Database) -> dict[str, Any]:
    risk = pilot_risk_snapshot(database)
    today = datetime.now(KST).date().isoformat()
    intents = [
        item
        for item in database.list_live_order_intents(PILOT_MISSION_ID)
        if datetime.fromisoformat(item["created_at"]).astimezone(KST).date().isoformat() == today
    ]
    reconciliation = database.get_latest_reconciliation(PILOT_MISSION_ID)
    database.record_operating_day(
        mission_id=PILOT_MISSION_ID,
        trade_date=today,
        mode="L0",
        reconciliation_status=(reconciliation or {}).get("status", "FAIL"),
        risk_violations=int(risk["daily_loss_reached"] or risk["total_loss_reached"]),
        duplicate_orders=0,
        decision_count=len(intents),
        fill_count=sum(len(item["fills"]) for item in intents),
        net_pnl_krw=int(risk["daily_defense_pnl_krw"]),
    )
    return risk


def generate_due_pilot_reviews(database: Database) -> list[dict[str, Any]]:
    days = database.list_operating_days(PILOT_MISSION_ID, "L0")
    reviews = database.list_pilot_reviews(PILOT_MISSION_ID)
    completed = {int(item["checkpoint_day"]) for item in reviews}
    performance = performance_report(database, PILOT_MISSION_ID)
    risk = pilot_risk_snapshot(database)
    for checkpoint, candidate_version in PILOT_CHECKPOINTS.items():
        if len(days) < checkpoint or checkpoint in completed:
            continue
        window = days[:checkpoint]
        report = {
            "checkpoint_day": checkpoint,
            "candidate_version": candidate_version,
            "active_version": PILOT_ACTIVE_VERSION,
            "period": {"start": window[0]["trade_date"], "end": window[-1]["trade_date"]},
            "operating": {
                "days": checkpoint,
                "decisions": sum(int(item["decision_count"]) for item in window),
                "fills": sum(int(item["fill_count"]) for item in window),
                "reconciliation_failures": sum(
                    item["reconciliation_status"] != "PASS" for item in window
                ),
                "risk_violations": sum(int(item["risk_violations"]) for item in window),
                "duplicate_orders": sum(int(item["duplicate_orders"]) for item in window),
            },
            "performance": performance["metrics"],
            "by_strategy": performance["by_strategy"],
            "risk_snapshot": risk,
            "automatic_apply": False,
            "owner_decision_required": True,
            "notes": [
                "이 보고서는 과거 주문·체결 기록을 변경하지 않습니다.",
                "진입·청산 변경은 새 챌린저 규칙과 별도 OOS 검증이 필요합니다.",
                "v2.0 후보 생성은 자동 증액 또는 자동 적용이 아닙니다."
                if checkpoint == 60
                else "v1.x 후보는 소유자 승인 전 운영 규칙을 바꾸지 않습니다.",
            ],
        }
        database.save_pilot_review(
            PILOT_MISSION_ID,
            checkpoint_day=checkpoint,
            candidate_version=candidate_version,
            report=report,
        )
    return database.list_pilot_reviews(PILOT_MISSION_ID)


def pilot_status(database: Database) -> dict[str, Any]:
    mission = database.get_mission(PILOT_MISSION_ID)
    control = database.get_execution_control(PILOT_MISSION_ID)
    days = database.list_operating_days(PILOT_MISSION_ID, "L0")
    reviews = database.list_pilot_reviews(PILOT_MISSION_ID)
    risk = pilot_risk_snapshot(database)
    entries_today = pilot_daily_entry_count(database)
    return {
        "mission": mission,
        "control": control,
        "active_version": PILOT_ACTIVE_VERSION,
        "strategy_performance_qualified": False,
        "performance_warning": (
            "L0는 전략 수익성 미검증 실거래입니다. 5만 원 전액 손실 가능성이 있습니다."
        ),
        "limits": {
            "capital_krw": PILOT_CAPITAL_KRW,
            "order_budget_krw": PILOT_ORDER_BUDGET_KRW,
            "max_positions": PILOT_MAX_POSITIONS,
            "max_daily_entries": PILOT_MAX_DAILY_ENTRIES,
            "daily_loss_krw": PILOT_DAILY_LOSS_KRW,
            "total_loss_krw": PILOT_TOTAL_LOSS_KRW,
            "automatic_deposit": False,
            "leverage": False,
        },
        "progress": {
            "operating_days": len(days),
            "target_days": 60,
            "entries_today": entries_today,
            "next_review_day": next(
                (day for day in PILOT_CHECKPOINTS if len(days) < day), None
            ),
        },
        "risk": risk,
        "reviews": reviews,
        "v2_candidate_ready": any(item["checkpoint_day"] == 60 for item in reviews),
        "automatic_version_apply": False,
        "live_order_count": len(database.list_live_order_intents(PILOT_MISSION_ID)),
    }
