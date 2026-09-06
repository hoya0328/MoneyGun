from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any

from .storage import Database

TERMINAL_STATES = {"SHADOW_FILLED", "SHADOW_CANCELLED"}
KST = timezone(timedelta(hours=9))


def _tick_size(price: int) -> int:
    if price < 2_000:
        return 1
    if price < 5_000:
        return 5
    if price < 20_000:
        return 10
    if price < 50_000:
        return 50
    if price < 200_000:
        return 100
    if price < 500_000:
        return 500
    return 1_000


def _ceil_to_tick(price: float) -> int:
    unit = _tick_size(round(price))
    return math.ceil(price / unit) * unit


def _next_business_day(value: str) -> str:
    result = date.fromisoformat(value[:10]) + timedelta(days=1)
    while result.weekday() >= 5:
        result += timedelta(days=1)
    return result.isoformat()


def prepare_shadow_order(
    database: Database,
    *,
    mission_id: str,
    idempotency_key: str,
) -> dict[str, Any]:
    cycle = database.get_latest_cycle(mission_id)
    if cycle is None:
        raise ValueError("먼저 투자위원회 분석을 실행하세요.")
    decision = cycle["decision"]
    risk = cycle["risk"]
    if decision["action"] != "BUY_CANDIDATE":
        raise ValueError("최신 위원회 결정이 매수 후보가 아닙니다.")
    if risk["result"] != "PASS_RESEARCH_ONLY" or int(decision["quantity"]) < 1:
        raise ValueError("그림자 주문을 만들 연구 한도 조건을 통과하지 못했습니다.")

    reference = int(risk["entry_reference_krw"])
    payload = {
        "mission_id": mission_id,
        "cycle_id": cycle["id"],
        "trade_date": cycle["as_of"],
        "next_session_date": _next_business_day(cycle["as_of"]),
        "symbol": decision["symbol"],
        "name": decision["name"],
        "side": "BUY",
        "quantity": int(decision["quantity"]),
        "signal_close_krw": reference,
        "limit_price_krw": _ceil_to_tick(reference * 1.01),
        "invalidation_price_krw": int(risk["invalidation_price_krw"]),
        "simulation_source": "DETERMINISTIC_SHADOW_FIXTURE",
        "broker_submitted": False,
        "trading_enabled": False,
    }
    return database.create_shadow_order(payload, idempotency_key=idempotency_key)


def prepare_official_quote_shadow_order(
    database: Database,
    *,
    mission_id: str,
    idempotency_key: str,
) -> dict[str, Any]:
    cycle = database.get_latest_cycle(mission_id)
    if cycle is None:
        raise ValueError("먼저 실제 종목 투자위원회 분석을 실행하세요.")
    if cycle["strategy_id"] != "FOCUS-MOMENTUM-KR-v1":
        raise ValueError("이 경로는 다음 거래일 지정가 전략만 허용합니다.")
    decision = cycle["decision"]
    risk = cycle["risk"]
    symbol = str(decision["symbol"])
    if not symbol.isdigit() or len(symbol) != 6:
        raise ValueError("실제 국내주식 숫자 6자리 결정만 공식 그림자 주문으로 만들 수 있습니다.")
    if decision["action"] != "BUY_CANDIDATE" or risk["result"] != "PASS_RESEARCH_ONLY":
        raise ValueError("최신 투자위원회가 매수 연구 조건을 통과하지 못했습니다.")
    quote = database.get_latest_broker_quote(symbol)
    if quote is None or quote["payload"].get("source") != "KIWOOM_OFFICIAL_REST":
        raise ValueError("먼저 키움 공식 시세를 동기화하세요.")
    reference = int(risk["entry_reference_krw"])
    payload = {
        "mission_id": mission_id,
        "cycle_id": cycle["id"],
        "trade_date": cycle["as_of"],
        "next_session_date": _next_business_day(cycle["as_of"]),
        "symbol": symbol,
        "name": decision["name"],
        "side": "BUY",
        "quantity": int(decision["quantity"]),
        "signal_close_krw": reference,
        "limit_price_krw": _ceil_to_tick(reference * 1.01),
        "invalidation_price_krw": int(risk["invalidation_price_krw"]),
        "simulation_source": "KIWOOM_OFFICIAL_QUOTE",
        "quote_snapshot_id": quote["id"],
        "broker_submitted": False,
        "trading_enabled": False,
    }
    return database.create_shadow_order(payload, idempotency_key=idempotency_key)


def prepare_official_mode_shadow_order(
    database: Database,
    *,
    mission_id: str,
    idempotency_key: str,
    now_kst: datetime | None = None,
) -> dict[str, Any]:
    """Convert an approved L0 daily-mode cycle into a fresh next-open order draft."""
    now_kst = (now_kst or datetime.now(KST)).astimezone(KST)
    minute = now_kst.hour * 60 + now_kst.minute
    if now_kst.weekday() >= 5 or not (9 * 60 + 5 <= minute <= 9 * 60 + 10):
        raise ValueError("집중·안전·장기 모드 주문안은 KRX 거래일 09:05~09:10에만 만듭니다.")
    cycle = database.get_latest_cycle(mission_id)
    allowed = {
        "FOCUS-MOMENTUM-KR-v2-L0",
        "BALANCED-TREND-KR-v1-L0",
        "LONG-TREND-KR-v1-L0",
    }
    if cycle is None or cycle.get("strategy_id") not in allowed:
        raise ValueError("먼저 해당 모드의 L0 전시장 스캔을 실행하세요.")
    decision, risk = cycle["decision"], cycle["risk"]
    if decision.get("action") != "BUY_CANDIDATE" or risk.get("result") != "PASS_L0_EXPERIMENTAL":
        raise ValueError("최신 모드 결정이 L0 매수 후보를 통과하지 못했습니다.")
    symbol = str(decision["symbol"])
    quote = database.get_latest_broker_quote(symbol)
    if quote is None or quote["payload"].get("source") != "KIWOOM_OFFICIAL_REST":
        raise ValueError("먼저 키움 공식 시세를 동기화하세요.")
    quote_age = now_kst.astimezone(UTC) - datetime.fromisoformat(quote["observed_at"])
    if quote_age.total_seconds() > 15:
        raise ValueError("키움 공식 시세가 15초보다 오래되었습니다.")
    current_price = int(quote["payload"].get("current_price_krw", 0))
    if current_price <= 0:
        raise ValueError("키움 현재가가 유효하지 않습니다.")
    reference = int(risk["entry_reference_krw"])
    gap = current_price / reference - 1 if reference else 1
    if gap > 0.03 or gap < -0.07:
        raise ValueError("전일 신호 대비 시초가 갭이 -7%~+3% 보호 범위를 벗어났습니다.")
    planned_quantity = int(decision["quantity"])
    quantity = max(1, planned_quantity // 2) if gap > 0.01 else planned_quantity
    payload = {
        "mission_id": mission_id,
        "cycle_id": cycle["id"],
        "trade_date": now_kst.date().isoformat(),
        "next_session_date": now_kst.date().isoformat(),
        "symbol": symbol,
        "name": decision["name"],
        "side": "BUY",
        "quantity": quantity,
        "signal_close_krw": reference,
        "limit_price_krw": _ceil_to_tick(current_price * 1.003),
        "invalidation_price_krw": int(risk["invalidation_price_krw"]),
        "simulation_source": "KIWOOM_L0_NEXT_OPEN_QUOTE",
        "quote_snapshot_id": quote["id"],
        "entry_style": "NEXT_OPEN_LIMIT",
        "gap_pct": round(gap * 100, 2),
        "broker_submitted": False,
        "trading_enabled": False,
    }
    return database.create_shadow_order(payload, idempotency_key=idempotency_key)


def prepare_close_auction_shadow_order(
    database: Database,
    *,
    mission_id: str,
    idempotency_key: str,
    now_kst: datetime | None = None,
) -> dict[str, Any]:
    now_kst = (now_kst or datetime.now(KST)).astimezone(KST)
    minute_of_day = now_kst.hour * 60 + now_kst.minute
    if now_kst.weekday() >= 5 or not (15 * 60 + 20 <= minute_of_day <= 15 * 60 + 27):
        raise ValueError("종가매매 그림자 주문안은 KRX 거래일 15:20~15:27에만 만듭니다.")
    cycle = database.get_latest_cycle(mission_id)
    allowed_cycles = {
        "CLOSE-AUCTION-KR-v2": "PASS_RESEARCH_ONLY",
        "CLOSE-AUCTION-KR-v3-L0": "PASS_L0_EXPERIMENTAL",
    }
    if cycle is None or cycle["strategy_id"] not in allowed_cycles:
        raise ValueError("먼저 종가매매 실데이터 검증을 실행하세요.")
    decision = cycle["decision"]
    risk = cycle["risk"]
    if (
        decision["action"] != "BUY_CANDIDATE"
        or risk["result"] != allowed_cycles[cycle["strategy_id"]]
    ):
        raise ValueError("종가매매 OOS 자격과 당일 후보 조건을 통과하지 못했습니다.")
    symbol = str(decision["symbol"])
    quote = database.get_latest_broker_quote(symbol)
    if quote is None or quote["payload"].get("source") != "KIWOOM_OFFICIAL_REST":
        raise ValueError("15초 이내 키움 공식 시세를 먼저 동기화하세요.")
    quote_age = now_kst.astimezone(UTC) - datetime.fromisoformat(quote["observed_at"])
    if quote_age.total_seconds() > 15:
        raise ValueError("키움 공식 시세가 15초보다 오래되었습니다.")
    current_price = int(quote["payload"].get("current_price_krw", 0))
    if current_price <= 0:
        raise ValueError("키움 현재가가 유효하지 않습니다.")
    payload = {
        "mission_id": mission_id,
        "cycle_id": cycle["id"],
        "trade_date": now_kst.date().isoformat(),
        "next_session_date": now_kst.date().isoformat(),
        "symbol": symbol,
        "name": decision["name"],
        "side": "BUY",
        "quantity": int(decision["quantity"]),
        "signal_close_krw": int(risk["entry_reference_krw"]),
        "limit_price_krw": _ceil_to_tick(current_price * 1.003),
        "invalidation_price_krw": int(risk["invalidation_price_krw"]),
        "simulation_source": "KIWOOM_CLOSE_AUCTION_QUOTE",
        "quote_snapshot_id": quote["id"],
        "entry_style": "KRX_CLOSING_AUCTION_LIMIT",
        "broker_submitted": False,
        "trading_enabled": False,
    }
    return database.create_shadow_order(payload, idempotency_key=idempotency_key)


def settle_close_auction_shadow_order(
    database: Database, order_id: str
) -> dict[str, Any]:
    now_kst = datetime.now(KST)
    if now_kst.weekday() >= 5 or now_kst.hour * 60 + now_kst.minute < 15 * 60 + 30:
        raise ValueError("종가매매 그림자 주문은 KRX 장 종료 15:30 이후에만 판정합니다.")
    order = database.get_shadow_order(order_id)
    if order["state"] in TERMINAL_STATES:
        return order
    if order["simulation_source"] != "KIWOOM_CLOSE_AUCTION_QUOTE":
        raise ValueError("종가 단일가 그림자 주문이 아닙니다.")
    quote = database.get_latest_broker_quote(order["symbol"])
    if quote is None:
        raise ValueError("장 종료 후 키움 공식 종가를 동기화하세요.")
    close_price = int(quote["payload"].get("current_price_krw", 0))
    if close_price <= 0:
        raise ValueError("키움 종가가 유효하지 않습니다.")
    if order["state"] == "READY":
        database.append_shadow_order_event(
            order_id,
            "SHADOW_SUBMITTED",
            {
                "quote_snapshot_id": quote["id"],
                "auction_close_krw": close_price,
                "broker_submitted": False,
            },
        )
    if close_price <= int(order["limit_price_krw"]):
        database.complete_shadow_fill(
            order_id, quantity=int(order["quantity"]), price_krw=close_price
        )
    else:
        database.append_shadow_order_event(
            order_id,
            "SHADOW_CANCELLED",
            {"reason": "공식 종가가 가격 보호 지정가를 초과했습니다."},
        )
    return database.get_shadow_order(order_id)


def settle_shadow_order_from_official_quote(
    database: Database, order_id: str
) -> dict[str, Any]:
    order = database.get_shadow_order(order_id)
    if order["state"] in TERMINAL_STATES:
        return order
    if order["state"] not in {"READY", "SHADOW_SUBMITTED"}:
        raise ValueError("READY 상태의 그림자 주문만 실제 시세로 가상 체결할 수 있습니다.")
    if order["simulation_source"] != "KIWOOM_OFFICIAL_QUOTE":
        raise ValueError("이 주문은 키움 공식 시세 기반 그림자 주문이 아닙니다.")
    quote = database.get_latest_broker_quote(order["symbol"])
    if quote is None:
        raise ValueError("동기화된 키움 공식 시세가 없습니다.")
    quote_date = datetime.fromisoformat(quote["observed_at"]).astimezone(KST).date()
    if quote_date < date.fromisoformat(order["next_session_date"]):
        raise ValueError("다음 거래일 공식 시세가 아직 도착하지 않았습니다.")
    data = quote["payload"]
    reference = int(order["signal_close_krw"])
    shadow_open = int(data.get("open_price_krw", 0))
    shadow_low = int(data.get("low_price_krw", 0))
    if shadow_open <= 0 or shadow_low <= 0:
        raise ValueError("키움 시세에 시가·저가가 없어 체결을 추정하지 않습니다.")
    gap = (shadow_open / reference) - 1
    effective_quantity = int(order["quantity"])
    if 0.01 < gap <= 0.03:
        effective_quantity = max(1, effective_quantity // 2)
    if order["state"] == "READY":
        database.append_shadow_order_event(
            order_id,
            "SHADOW_SUBMITTED",
            {
                "quote_snapshot_id": quote["id"],
                "shadow_open_krw": shadow_open,
                "shadow_low_krw": shadow_low,
                "gap_pct": round(gap * 100, 2),
                "effective_quantity": effective_quantity,
                "broker_submitted": False,
            },
        )
    if gap > 0.03:
        database.append_shadow_order_event(
            order_id,
            "SHADOW_CANCELLED",
            {"reason": "실제 시초가 갭이 3%를 초과했습니다.", "quote_snapshot_id": quote["id"]},
        )
    elif shadow_low <= int(order["limit_price_krw"]):
        database.complete_shadow_fill(
            order_id,
            quantity=effective_quantity,
            price_krw=min(shadow_open, int(order["limit_price_krw"])),
        )
    else:
        database.append_shadow_order_event(
            order_id,
            "SHADOW_CANCELLED",
            {
                "reason": "실제 저가가 지정가에 도달하지 않았습니다.",
                "quote_snapshot_id": quote["id"],
            },
        )
    return database.get_shadow_order(order_id)


def simulate_shadow_order(
    database: Database,
    order_id: str,
    *,
    scenario: str = "AUTO",
) -> dict[str, Any]:
    order = database.get_shadow_order(order_id)
    if order["state"] in TERMINAL_STATES:
        return order
    if order["state"] not in {"READY", "SHADOW_SUBMITTED"}:
        raise ValueError(f"READY 상태에서만 가상 체결할 수 있습니다: {order['state']}")

    reference = int(order["signal_close_krw"])
    gap_by_scenario = {"AUTO": 0.008, "GAP_REDUCE": 0.02, "GAP_UP_CANCEL": 0.035}
    if scenario not in gap_by_scenario:
        raise ValueError("지원하지 않는 그림자 시나리오입니다.")
    if order["state"] == "SHADOW_SUBMITTED":
        submission = order["events"][-1]["payload"]
        gap = float(submission["gap_pct"]) / 100
        shadow_open = int(submission["shadow_open_krw"])
        shadow_low = int(submission["shadow_low_krw"])
        effective_quantity = int(submission["effective_quantity"])
    else:
        gap = gap_by_scenario[scenario]
        shadow_open = _ceil_to_tick(reference * (1 + gap))
        shadow_low = _ceil_to_tick(reference * (1 - 0.004))
        requested_quantity = int(order["quantity"])
        effective_quantity = (
            max(1, requested_quantity // 2) if 0.01 < gap <= 0.03 else requested_quantity
        )
        database.append_shadow_order_event(
            order_id,
            "SHADOW_SUBMITTED",
            {
                "scenario": scenario,
                "shadow_open_krw": shadow_open,
                "shadow_low_krw": shadow_low,
                "gap_pct": round(gap * 100, 2),
                "effective_quantity": effective_quantity,
                "broker_submitted": False,
            },
        )
    if gap > 0.03:
        database.append_shadow_order_event(
            order_id,
            "SHADOW_CANCELLED",
            {"reason": "시초가 갭이 3%를 초과해 그림자 주문을 취소했습니다."},
        )
    elif shadow_low <= int(order["limit_price_krw"]):
        fill_price = min(shadow_open, int(order["limit_price_krw"]))
        database.complete_shadow_fill(
            order_id, quantity=effective_quantity, price_krw=fill_price
        )
    else:
        database.append_shadow_order_event(
            order_id,
            "SHADOW_CANCELLED",
            {"reason": "가상 저가가 지정가에 도달하지 않았습니다."},
        )
    return database.get_shadow_order(order_id)
