from __future__ import annotations

import hashlib
import json
import statistics
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .execution import (
    ExecutionGuardian,
    build_live_intent,
    reconcile_managed_account,
    sync_official_quote,
)
from .kiwoom import KiwoomReadOnlyClient
from .opening_range import aggregate_kiwoom_realtime_events, build_opening_range_signal_plan
from .pilot import PILOT_MISSION_ID
from .qualification import instrument_is_eligible
from .storage import Database

KST = timezone(timedelta(hours=9))
MISSION_ID = "mission_opening_range_001"
STRATEGY_ID = "OPEN-RANGE-KR-v3-L0"
BASE_STRATEGY_ID = "OPEN-RANGE-KR-v2"
QUALIFIED_SOURCES = (
    "KRX_AUTHORIZED_EXPORT",
    "LICENSED_VENDOR",
    "OFFICIAL_PUBLIC_DATA_API_AND_KIND_UI_EXPORT",
    "OFFICIAL_PUBLIC_DATA_API_KIND_AND_KIWOOM",
)


def live_opening_spec() -> dict[str, Any]:
    return {
        "mode_code": "OPENING_RANGE",
        "display_name": "장초단타",
        "strategy_id": STRATEGY_ID,
        "clock": {
            "watchlist": "08:50~09:00",
            "observe": "09:00~09:10 주문 금지",
            "entry": "09:10~10:30",
            "force_flat": "11:00",
        },
        "universe": "시장조치 제외 보통주 중 거래대금 상위, 2천~4만5천 원",
        "capture": "키움 공식 실시간 체결·호가·VI, 20초 단위 누적 후 1분봉",
        "capital_krw": 50_000,
        "owner_approval": "매 주문 5분 승인",
        "qualification": "L0 실험 전용; 2년 분봉 OOS 자격을 의미하지 않음",
        "automatic_broker_submission": False,
    }


def _qualified(database: Database, today: date) -> dict[str, Any]:
    snapshot = database.get_latest_snapshot_for_sources(QUALIFIED_SOURCES)
    if snapshot is None:
        raise ValueError("장초 감시목록에 필요한 자격 보통주·시장조치 데이터가 없습니다.")
    age = (today - date.fromisoformat(str(snapshot["as_of"])[:10])).days
    if snapshot.get("quality", {}).get("state") != "PASS" or age > 3:
        raise ValueError("자격 시장조치 데이터가 3일보다 오래되었거나 품질 PASS가 아닙니다.")
    return snapshot


def _watchlist(
    database: Database, client: KiwoomReadOnlyClient, today: date
) -> dict[str, Any]:
    qualified = _qualified(database, today)
    lifecycle = {item["symbol"]: item for item in qualified.get("universe_history", [])}
    rows: list[dict[str, Any]] = []
    for market in ("KOSPI", "KOSDAQ"):
        rows.extend(client.fetch_today_volume_top(market=market, max_pages=2))
    candidates: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda item: item["estimated_turnover_krw"], reverse=True):
        meta = lifecycle.get(row["symbol"])
        if not meta or meta.get("security_type") != "COMMON_STOCK":
            continue
        if not instrument_is_eligible(qualified, row["symbol"], today.isoformat()):
            continue
        if not 2_000 <= int(row["current_price_krw"]) <= 45_000:
            continue
        bars = client.fetch_daily_bars(
            row["symbol"], base_date=today.strftime("%Y%m%d"), max_pages=2
        )
        completed = [bar for bar in bars if str(bar["date"]) < today.isoformat()]
        if len(completed) < 20:
            continue
        median_daily_turnover = round(
            statistics.median(
                int(bar["close"]) * int(bar["volume"]) for bar in completed[-20:]
            )
        )
        if median_daily_turnover < 30_000_000_000:
            continue
        candidates.append(
            {
                "symbol": row["symbol"],
                "name": meta.get("name", row["name"]),
                "market": meta["market"],
                "previous_close": int(completed[-1]["close"]),
                "median_daily_turnover_krw": median_daily_turnover,
                # The live L0 collector has no historical first-10-minute archive yet.
                # Ten percent of median daily turnover is a conservative, versioned proxy.
                "median_first10_turnover_krw": max(1, median_daily_turnover // 10),
                "designation": {},
            }
        )
        if len(candidates) >= 5:
            break
    if not candidates:
        raise ValueError("가격·유동성·시장조치 조건을 통과한 장초 감시 종목이 없습니다.")
    return {
        "phase": "WATCHLIST_READY",
        "trade_date": today.isoformat(),
        "qualified_snapshot_id": qualified["id"],
        "watchlist": candidates,
        "events": [],
        "event_keys": [],
        "last_error": None,
    }


def _event_key(event: dict[str, Any]) -> str:
    # Reconnects can replay a frame with a new local receive time. Collapsing the
    # broker body is conservative: identical genuine prints may be under-counted,
    # but turnover is never inflated into a false breakout.
    body = json.dumps(
        {key: event.get(key) for key in ("type", "item", "values")},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode()).hexdigest()


def _next_weekday(value: date) -> date:
    result = value + timedelta(days=1)
    while result.weekday() >= 5:
        result += timedelta(days=1)
    return result


def _archive(state: dict[str, Any], now_kst: datetime) -> dict[str, Any]:
    trade_date = state["trade_date"]
    minute_bars = aggregate_kiwoom_realtime_events(state["events"], trade_date)
    benchmark_bars = minute_bars.get("069500", [])
    instruments: list[dict[str, Any]] = []
    for item in state["watchlist"]:
        bars = minute_bars.get(item["symbol"], [])
        if len(bars) < 11:
            continue
        instruments.append({**item, "security_type": "COMMON", "bars": bars})
    issues: list[str] = []
    if len(benchmark_bars) < 11:
        issues.append("KODEX200 09:10까지 연속 분봉 부족")
    if not instruments:
        issues.append("09:10까지 연속 분봉이 있는 감시 종목 없음")
    payload: dict[str, Any] = {
        "as_of": now_kst.isoformat(timespec="seconds"),
        "trade_date": trade_date,
        "source": "KIWOOM_REALTIME_ARCHIVE",
        "quality": {"state": "PASS" if not issues else "HOLD", "issues": issues},
        "history": {"trading_days": 1, "final_126_untouched": False},
        "benchmark": {"symbol": "069500", "name": "KODEX 200", "bars": benchmark_bars},
        "instruments": instruments,
        "collection_manifest": {
            "schema_version": "OPENING_RANGE_L0_LIVE-v1",
            "qualified_snapshot_id": state["qualified_snapshot_id"],
            "event_count": len(state["events"]),
            "first10_turnover_baseline": "20일 중앙 일거래대금의 10% 프록시",
            "point_in_time": True,
        },
        "broker_submitted": False,
        "trading_enabled": False,
    }
    checksum = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {"id": f"snap_{checksum[:16]}", **payload, "checksum": checksum}


async def run_opening_range_pilot_tick(
    database: Database,
    client: KiwoomReadOnlyClient,
    guardian: ExecutionGuardian,
    *,
    now_kst: datetime | None = None,
) -> dict[str, Any]:
    now_kst = (now_kst or datetime.now(KST)).astimezone(KST)
    trade_date = now_kst.date().isoformat()
    minute = now_kst.hour * 60 + now_kst.minute
    base = {
        "at": now_kst.isoformat(timespec="seconds"),
        "spec": live_opening_spec(),
        "automatic_broker_submission": False,
        "owner_action_required_for_submission": True,
    }
    if now_kst.weekday() >= 5:
        return {**base, "phase": "MARKET_CLOSED", "action": "NO_ACTION"}
    if 15 * 60 + 35 <= minute <= 18 * 60:
        target = _next_weekday(now_kst.date())
        prepared = database.get_strategy_runtime_state(STRATEGY_ID, target.isoformat())
        if prepared is None:
            prepared = _watchlist(database, client, now_kst.date())
            prepared["trade_date"] = target.isoformat()
            prepared["prepared_from_session"] = trade_date
            database.save_strategy_runtime_state(STRATEGY_ID, target.isoformat(), prepared)
        return {
            **base,
            "phase": "NEXT_SESSION_WATCHLIST_READY",
            "action": "WATCHLIST_READY",
            "target_trade_date": target.isoformat(),
            "watchlist": prepared["watchlist"],
        }
    state = database.get_strategy_runtime_state(STRATEGY_ID, trade_date)
    if state is None:
        if minute < 8 * 60 + 50 or minute > 10 * 60 + 30:
            return {**base, "phase": "WAITING_FOR_08_50", "action": "NO_ACTION"}
        state = _watchlist(database, client, now_kst.date())
        database.save_strategy_runtime_state(STRATEGY_ID, trade_date, state)
        if minute < 9 * 60:
            return {**base, "phase": "WATCHLIST_READY", "action": "NO_ACTION", "state": state}
    if minute < 9 * 60:
        return {**base, "phase": "WATCHLIST_READY", "action": "NO_ACTION", "state": state}
    if minute > 10 * 60 + 30:
        return {**base, "phase": "ENTRY_WINDOW_ENDED", "action": "NO_ACTION", "state": state}

    symbols = [item["symbol"] for item in state["watchlist"]] + ["069500"]
    events = await client.collect_realtime_market_events(
        symbols, max_messages=500, timeout_seconds=20
    )
    known = set(state.get("event_keys", []))
    for event in events:
        key = _event_key(event)
        if key not in known:
            state["events"].append(event)
            known.add(key)
    state["event_keys"] = sorted(known)
    state["phase"] = "OBSERVING" if minute < 9 * 60 + 10 else "SIGNAL_SEARCH"
    database.save_strategy_runtime_state(STRATEGY_ID, trade_date, state)
    if minute < 9 * 60 + 10:
        return {
            **base,
            "phase": "OBSERVING_NO_ORDERS",
            "action": "CAPTURED",
            "event_count": len(state["events"]),
            "watchlist": state["watchlist"],
        }

    snapshot = _archive(state, now_kst)
    database.save_snapshot(snapshot)
    if snapshot["quality"]["state"] != "PASS":
        return {
            **base,
            "phase": "SIGNAL_SEARCH",
            "action": "COLLECTING",
            "archive": snapshot,
        }
    pilot = database.get_mission(PILOT_MISSION_ID)
    try:
        package = build_opening_range_signal_plan(pilot, snapshot, now=now_kst)
    except ValueError as error:
        state["last_error"] = str(error)
        database.save_strategy_runtime_state(STRATEGY_ID, trade_date, state)
        return {
            **base,
            "phase": "SIGNAL_SEARCH",
            "action": "HOLD",
            "reason": str(error),
            "archive": snapshot,
        }
    package = {
        **package,
        "mission_id": MISSION_ID,
        "strategy_id": STRATEGY_ID,
        "protocol_version": "KIWOOM_REALTIME_L0-v1",
        "risk": {
            **package["risk"],
            "result": "PASS_L0_EXPERIMENTAL",
            "reasons": [
                "2년 분봉 OOS 미충족 L0 실험 전용입니다.",
                "15초 호가·30초 대사·5분 소유자 승인을 다시 검사합니다.",
            ],
        },
        "decision": {**package["decision"], "action": "BUY_CANDIDATE"},
        "l0_only": True,
        "performance_qualified": False,
    }
    material = json.dumps(package, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    package["id"] = f"cycle_{hashlib.sha256(material.encode()).hexdigest()[:16]}"
    cycle = database.save_cycle(package)
    decision = cycle["decision"]
    order = database.create_shadow_order(
        {
            "mission_id": MISSION_ID,
            "cycle_id": cycle["id"],
            "trade_date": trade_date,
            "next_session_date": trade_date,
            "symbol": decision["symbol"],
            "name": decision["name"],
            "side": "BUY",
            "quantity": decision["quantity"],
            "signal_close_krw": decision["limit_price_krw"],
            "limit_price_krw": decision["limit_price_krw"],
            "invalidation_price_krw": decision["invalidation_price_krw"],
            "simulation_source": "KIWOOM_OPENING_RANGE_REALTIME",
            "entry_style": "OPENING_RANGE_BREAKOUT_LIMIT",
            "broker_submitted": False,
            "trading_enabled": False,
        },
        idempotency_key=f"{STRATEGY_ID}:{cycle['id']}:buy",
    )
    sync_official_quote(database, client, decision["symbol"])
    reconciliation = reconcile_managed_account(database, client, mission_id=PILOT_MISSION_ID)
    sync_official_quote(database, client, decision["symbol"])
    intent = build_live_intent(
        database,
        guardian,
        shadow_order_id=order["id"],
        idempotency_key=f"{STRATEGY_ID}:{cycle['id']}:intent",
        execution_mission_id=PILOT_MISSION_ID,
    )
    state["phase"] = "INTENT_PREPARED"
    state["cycle_id"] = cycle["id"]
    state["intent_id"] = intent["id"]
    database.save_strategy_runtime_state(STRATEGY_ID, trade_date, state)
    return {
        **base,
        "phase": "AWAITING_OWNER",
        "action": "INTENT_READY" if intent["state"] == "AWAITING_APPROVAL" else "BLOCKED",
        "cycle": cycle,
        "shadow_order": order,
        "reconciliation": reconciliation,
        "intent": intent,
    }


def opening_range_live_status(
    database: Database, *, now_kst: datetime | None = None
) -> dict[str, Any]:
    now_kst = (now_kst or datetime.now(KST)).astimezone(KST)
    state = database.get_strategy_runtime_state(STRATEGY_ID, now_kst.date().isoformat())
    return {
        "spec": live_opening_spec(),
        "state": state,
        "phase": state.get("phase", "NOT_STARTED") if state else "NOT_STARTED",
        "automatic_broker_submission": False,
    }
