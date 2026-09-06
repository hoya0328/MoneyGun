from __future__ import annotations

import hashlib
import json
import statistics
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .execution import (
    ExecutionGuardian,
    build_live_intent,
    normalize_quote,
    reconcile_managed_account,
    sync_official_quote,
)
from .kiwoom import KiwoomReadOnlyClient
from .pilot import PILOT_DAILY_LOSS_KRW, PILOT_MISSION_ID, PILOT_ORDER_BUDGET_KRW
from .qualification import instrument_is_eligible
from .research import finalize_snapshot
from .shadow_orders import prepare_official_mode_shadow_order
from .storage import Database

KST = timezone(timedelta(hours=9))
QUALIFIED_SOURCES = (
    "KRX_AUTHORIZED_EXPORT",
    "LICENSED_VENDOR",
    "OFFICIAL_PUBLIC_DATA_API_AND_KIND_UI_EXPORT",
    "OFFICIAL_PUBLIC_DATA_API_KIND_AND_KIWOOM",
)


@dataclass(frozen=True)
class L0ModeDefinition:
    mode_code: str
    display_name: str
    mission_id: str
    strategy_id: str
    protocol_version: str
    max_price_krw: int
    min_market_cap_100m_krw: int
    median_turnover_floor_krw: int
    score_threshold: int
    stop_bps: int
    max_hold_calendar_days: int
    scan_window_kst: tuple[str, str] = ("15:35", "18:00")
    buy_window_kst: tuple[str, str] = ("09:05", "09:10")


MODE_DEFINITIONS: dict[str, L0ModeDefinition] = {
    "FOCUS": L0ModeDefinition(
        mode_code="FOCUS",
        display_name="집중투자",
        mission_id="mission_focus_001",
        strategy_id="FOCUS-MOMENTUM-KR-v2-L0",
        protocol_version="FULL_MARKET_DAILY-v1",
        max_price_krw=PILOT_ORDER_BUDGET_KRW,
        min_market_cap_100m_krw=500,
        median_turnover_floor_krw=5_000_000_000,
        score_threshold=75,
        stop_bps=500,
        max_hold_calendar_days=14,
    ),
    "BALANCED": L0ModeDefinition(
        mode_code="BALANCED",
        display_name="안전투자",
        mission_id="mission_balanced_001",
        strategy_id="BALANCED-TREND-KR-v1-L0",
        protocol_version="FULL_MARKET_DAILY-v1",
        max_price_krw=PILOT_ORDER_BUDGET_KRW,
        min_market_cap_100m_krw=3_000,
        median_turnover_floor_krw=10_000_000_000,
        score_threshold=80,
        stop_bps=400,
        max_hold_calendar_days=90,
    ),
    "LONG_TERM": L0ModeDefinition(
        mode_code="LONG_TERM",
        display_name="장기투자",
        mission_id="mission_long_term_001",
        strategy_id="LONG-TREND-KR-v1-L0",
        protocol_version="FULL_MARKET_DAILY-v1",
        max_price_krw=PILOT_ORDER_BUDGET_KRW,
        min_market_cap_100m_krw=10_000,
        median_turnover_floor_krw=20_000_000_000,
        score_threshold=85,
        stop_bps=700,
        max_hold_calendar_days=365,
    ),
}


def _minute(value: str) -> int:
    hour, minute = (int(part) for part in value.split(":"))
    return hour * 60 + minute


def mode_specs() -> list[dict[str, Any]]:
    return [mode_spec(code) for code in MODE_DEFINITIONS]


def mode_spec(mode_code: str) -> dict[str, Any]:
    definition = MODE_DEFINITIONS[mode_code]
    return {
        **asdict(definition),
        "market": "KRX",
        "capital_krw": 50_000,
        "order_budget_krw": PILOT_ORDER_BUDGET_KRW,
        "max_positions_shared": 1,
        "owner_approval": "매 주문 5분 승인",
        "data": "키움 ka10030·ka10001·ka10081 + 자격 시장조치 아카이브",
        "qualification": "L0 실험 전용; 수익성 OOS 자격을 의미하지 않음",
        "automatic_broker_submission": False,
    }


def _latest_qualified_snapshot(database: Database, today: date) -> dict[str, Any]:
    snapshot = database.get_latest_snapshot_for_sources(QUALIFIED_SOURCES)
    if snapshot is None:
        raise ValueError("자격 보통주·시장조치 스냅샷이 없습니다.")
    age = (today - date.fromisoformat(str(snapshot["as_of"])[:10])).days
    if snapshot.get("quality", {}).get("state") != "PASS" or age > 3:
        raise ValueError("자격 시장조치 데이터가 3일보다 오래되었거나 품질 PASS가 아닙니다.")
    return snapshot


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _atr_ratio(bars: list[dict[str, Any]], length: int = 14) -> float:
    ranges: list[float] = []
    for previous, current in zip(bars[-length - 1 : -1], bars[-length:], strict=True):
        ranges.append(
            max(
                float(current["high"]) - float(current["low"]),
                abs(float(current["high"]) - float(previous["close"])),
                abs(float(current["low"]) - float(previous["close"])),
            )
        )
    return _mean(ranges) / float(bars[-1]["close"])


def _score(
    definition: L0ModeDefinition,
    row: dict[str, Any],
    quote: dict[str, Any],
    bars: list[dict[str, Any]],
    benchmark: list[dict[str, Any]],
    today: date,
) -> dict[str, Any]:
    completed = [bar for bar in bars if str(bar["date"]) < today.isoformat()]
    benchmark_completed = [bar for bar in benchmark if str(bar["date"]) < today.isoformat()]
    if len(completed) < 200 or len(benchmark_completed) < 200:
        return {**row, "eligible": False, "score": 0, "reasons": ["완료 일봉 200개 부족"]}
    closes = [float(bar["close"]) for bar in completed]
    volumes = [float(bar["volume"]) for bar in completed]
    current = float(quote["current_price_krw"])
    market_cap = int(quote.get("market_cap_100m_krw", 0))
    median_turnover = statistics.median(
        float(bar["close"]) * float(bar["volume"]) for bar in completed[-20:]
    )
    sma20, sma60, sma120, sma200 = (
        _mean(closes[-20:]),
        _mean(closes[-60:]),
        _mean(closes[-120:]),
        _mean(closes[-200:]),
    )
    atr = _atr_ratio(completed)
    return_20 = current / closes[-20] - 1
    return_60 = current / closes[-60] - 1
    benchmark_60 = float(benchmark_completed[-1]["close"]) / float(
        benchmark_completed[-60]["close"]
    ) - 1
    volume_multiple = float(quote.get("volume", 0)) / max(
        statistics.median(volumes[-20:]), 1
    )
    reasons: list[str] = []
    if current > definition.max_price_krw or current < 2_000:
        reasons.append("5만 원 파일럿 가격 범위 밖")
    if market_cap < definition.min_market_cap_100m_krw:
        reasons.append("시가총액 하한 미달")
    if median_turnover < definition.median_turnover_floor_krw:
        reasons.append("20일 중앙 거래대금 미달")

    if definition.mode_code == "FOCUS":
        score = 30 if current > sma20 > sma60 else 0
        score += 20 if return_60 > benchmark_60 else 0
        score += 15 if return_20 > 0 else 0
        score += 15 if 1.0 <= volume_multiple <= 5.0 else 0
        score += 10 if atr <= 0.06 else 0
        score += 10 if current >= max(closes[-60:]) * 0.95 else 0
    elif definition.mode_code == "BALANCED":
        score = 30 if current > sma20 > sma60 > sma120 else 0
        score += 20 if return_60 > benchmark_60 else 0
        score += 20 if atr <= 0.035 else 0
        score += 15 if -0.03 <= return_20 <= 0.15 else 0
        score += 15 if market_cap >= definition.min_market_cap_100m_krw else 0
    else:
        score = 35 if current > sma60 > sma120 > sma200 else 0
        score += 20 if return_60 > benchmark_60 else 0
        score += 20 if atr <= 0.03 else 0
        score += 15 if current >= sma200 * 1.02 else 0
        score += 10 if market_cap >= definition.min_market_cap_100m_krw else 0
    if score < definition.score_threshold:
        reasons.append(f"점수 {score} < {definition.score_threshold}")
    return {
        **row,
        "name": quote["name"],
        "current_price_krw": int(current),
        "market_cap_100m_krw": market_cap,
        "median_turnover_krw": round(median_turnover),
        "atr_pct": round(atr * 100, 2),
        "return_20_pct": round(return_20 * 100, 2),
        "return_60_pct": round(return_60 * 100, 2),
        "relative_60_pct": round((return_60 - benchmark_60) * 100, 2),
        "volume_multiple": round(volume_multiple, 2),
        "score": score,
        "eligible": not reasons,
        "reasons": reasons,
    }


def run_daily_mode_scan(
    database: Database,
    client: KiwoomReadOnlyClient,
    mode_code: str,
    *,
    now_kst: datetime | None = None,
    force: bool = False,
) -> dict[str, Any]:
    definition = MODE_DEFINITIONS[mode_code]
    now_kst = (now_kst or datetime.now(KST)).astimezone(KST)
    today = now_kst.date()
    if today.weekday() >= 5:
        raise ValueError("KRX 거래일에만 모드 스캔을 실행합니다.")
    current_minute = now_kst.hour * 60 + now_kst.minute
    if not force and not (
        _minute(definition.scan_window_kst[0])
        <= current_minute
        <= _minute(definition.scan_window_kst[1])
    ):
        raise ValueError(
            f"{definition.display_name} 스캔은 완료 일봉이 확정되는 "
            f"{definition.scan_window_kst[0]}~{definition.scan_window_kst[1]}에 실행합니다."
        )
    existing = database.get_latest_cycle(definition.mission_id)
    if (
        existing
        and existing.get("strategy_id") == definition.strategy_id
        and str(existing.get("as_of", ""))[:10] == today.isoformat()
    ):
        return {"spec": mode_spec(mode_code), "cycle": existing, "reused": True}

    qualified = _latest_qualified_snapshot(database, today)
    lifecycle = {item["symbol"]: item for item in qualified.get("universe_history", [])}
    ranking: list[dict[str, Any]] = []
    for market in ("KOSPI", "KOSDAQ"):
        ranking.extend(client.fetch_today_volume_top(market=market, max_pages=2))
    allowed: list[dict[str, Any]] = []
    for row in ranking:
        meta = lifecycle.get(row["symbol"])
        if not meta or meta.get("security_type") != "COMMON_STOCK":
            continue
        if not instrument_is_eligible(qualified, row["symbol"], today.isoformat()):
            continue
        if 2_000 <= int(row["current_price_krw"]) <= definition.max_price_krw:
            allowed.append({**row, "market": meta["market"], "name": meta.get("name", row["name"])})
    allowed.sort(key=lambda item: int(item["estimated_turnover_krw"]), reverse=True)
    shortlist = allowed[:12]
    benchmark = client.fetch_daily_bars(
        "069500", base_date=today.strftime("%Y%m%d"), max_pages=4
    )
    evaluated: list[dict[str, Any]] = []
    instruments: list[dict[str, Any]] = []
    for row in shortlist:
        quote = normalize_quote(row["symbol"], client.fetch_domestic_quote(row["symbol"]))
        bars = client.fetch_daily_bars(
            row["symbol"], base_date=today.strftime("%Y%m%d"), max_pages=4
        )
        evaluated.append(_score(definition, row, quote, bars, benchmark, today))
        instruments.append(
            {
                "symbol": row["symbol"],
                "name": quote["name"],
                "market": row["market"],
                "sector": "",
                "bars": bars,
                "live_quote": quote,
            }
        )
    evaluated.sort(
        key=lambda item: (bool(item.get("eligible")), int(item.get("score", 0))),
        reverse=True,
    )
    best = next((item for item in evaluated if item.get("eligible")), None)
    price = int(best["current_price_krw"]) if best else 0
    risk_budget = min(PILOT_DAILY_LOSS_KRW, 50_000 * definition.stop_bps // 10_000)
    position_budget = min(
        PILOT_ORDER_BUDGET_KRW,
        risk_budget * 10_000 // definition.stop_bps,
    )
    quantity = position_budget // price if price else 0
    if best is not None and quantity < 1:
        best = None
    as_of = now_kst.isoformat(timespec="seconds")
    snapshot = finalize_snapshot(
        {
            "as_of": as_of,
            "source": f"KIWOOM_{mode_code}_L0_SCAN",
            "license_basis": "USER_AUTHORIZED_INTERNAL_RESEARCH",
            "market": "KR",
            "benchmark": {"symbol": "069500", "name": "KODEX 200", "bars": benchmark},
            "instruments": instruments,
            "evidence": [
                {
                    "id": f"ev_kiwoom_{mode_code.lower()}_scan",
                    "source": "KIWOOM_OFFICIAL_REST",
                    "title": "키움 전시장 순위·현재가·완료 일봉",
                    "published_at": as_of,
                }
            ],
            "collection_manifest": {
                "qualified_snapshot_id": qualified["id"],
                "ranking_api_id": "ka10030",
                "detail_api_ids": ["ka10001", "ka10081"],
                "evaluated_count": len(evaluated),
                "same_day_unfinished_bar_not_used_for_trend": True,
            },
        }
    )
    database.save_snapshot(snapshot)
    decision = {
        "action": "BUY_CANDIDATE" if best else "HOLD",
        "symbol": best["symbol"] if best else "",
        "name": best["name"] if best else "후보 없음",
        "score": best["score"] if best else 0,
        "quantity": quantity if best else 0,
        "entry_style": "NEXT_OPEN_LIMIT",
        "order_allowed": False,
        "reason": (
            f"{definition.display_name} L0 조건을 통과했습니다. "
            "다음 장에서 다시 호가·계좌를 검사합니다."
            if best
            else f"오늘 {definition.display_name} 조건을 모두 통과한 종목이 없습니다."
        ),
    }
    risk = {
        "result": "PASS_L0_EXPERIMENTAL" if best else "HOLD",
        "quantity": decision["quantity"],
        "entry_reference_krw": price,
        "invalidation_price_krw": price * (10_000 - definition.stop_bps) // 10_000 if best else 0,
        "risk_budget_krw": risk_budget,
        "position_budget_krw": position_budget if best else 0,
        "max_hold_calendar_days": definition.max_hold_calendar_days,
        "order_allowed": False,
        "reasons": [
            "L0 실험 전략이며 OOS 수익성 자격이 아닙니다.",
            "다음 장 주문에는 15초 호가·30초 대사·5분 소유자 승인이 필요합니다.",
        ],
    }
    material = {
        "snapshot_id": snapshot["id"],
        "strategy_id": definition.strategy_id,
        "decision": decision,
        "risk": risk,
    }
    digest = hashlib.sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    reports = [
        ("DATA", "김데이터", "자격 보통주·시장조치·완료 일봉을 대조했습니다."),
        ("NOVA", "김뉴스", "새 공시·뉴스는 주문 직전 반대 근거로 다시 확인해야 합니다."),
        ("PULSE", "김차트", f"{len(evaluated)}개 유동성 후보를 모드별로 채점했습니다."),
        ("BULL", "김찬성", "추세·상대강도·유동성의 동시 통과만 찬성했습니다."),
        ("BEAR", "김반대", "과열·저유동성·시장조치·가격초과를 거부했습니다."),
        ("RISK", "김안전", f"계획손실 {risk_budget:,}원과 1종목 한도를 적용했습니다."),
        ("ACE", "김투자", decision["reason"]),
        ("OPS", "김주문", "후보만 기록했으며 브로커 주문은 제출하지 않았습니다."),
    ]
    cycle = database.save_cycle(
        {
            "id": f"cycle_{digest[:16]}",
            "mission_id": definition.mission_id,
            "snapshot_id": snapshot["id"],
            "strategy_id": definition.strategy_id,
            "protocol_version": definition.protocol_version,
            "as_of": as_of,
            "source": snapshot["source"],
            "status": "COMPLETE",
            "candidates": evaluated[:10],
            "decision": decision,
            "risk": risk,
            "reports": [
                {
                    "code": code,
                    "name": name,
                    "role": f"{definition.display_name} L0",
                    "state": "READY" if code == "OPS" else "DONE",
                    "summary": summary,
                    "claims": [],
                    "evidence_ids": [f"ev_kiwoom_{mode_code.lower()}_scan"],
                    "unknowns": [],
                    "invalidation_conditions": ["시장조치·호가·계좌·위험 한도 변경"],
                    "confidence": 1.0 if code in {"DATA", "RISK", "OPS"} else 0.7,
                }
                for code, name, summary in reports
            ],
            "l0_only": True,
            "performance_qualified": False,
            "trading_enabled": False,
        }
    )
    return {"spec": mode_spec(mode_code), "cycle": cycle, "reused": False}


def run_daily_mode_pilot_tick(
    database: Database,
    client: KiwoomReadOnlyClient,
    guardian: ExecutionGuardian,
    mode_code: str,
    *,
    now_kst: datetime | None = None,
    force_scan: bool = False,
) -> dict[str, Any]:
    definition = MODE_DEFINITIONS[mode_code]
    now_kst = (now_kst or datetime.now(KST)).astimezone(KST)
    minute = now_kst.hour * 60 + now_kst.minute
    base = {
        "at": now_kst.isoformat(timespec="seconds"),
        "spec": mode_spec(mode_code),
        "automatic_broker_submission": False,
        "owner_action_required_for_submission": True,
    }
    if now_kst.weekday() >= 5:
        return {**base, "phase": "MARKET_CLOSED", "action": "NO_ACTION"}
    if force_scan or _minute(definition.scan_window_kst[0]) <= minute <= _minute(
        definition.scan_window_kst[1]
    ):
        scan = run_daily_mode_scan(
            database, client, mode_code, now_kst=now_kst, force=force_scan
        )
        return {**base, "phase": "DAILY_SCAN", "action": "SCAN_READY", **scan}

    cycle = database.get_latest_cycle(definition.mission_id)
    if not cycle or cycle.get("strategy_id") != definition.strategy_id:
        return {**base, "phase": "WAITING_FOR_SCAN", "action": "NO_ACTION"}
    cycle_day = date.fromisoformat(str(cycle["as_of"])[:10])
    if (now_kst.date() - cycle_day).days > 4:
        return {**base, "phase": "STALE_SCAN", "action": "BLOCKED", "cycle": cycle}
    if not (
        _minute(definition.buy_window_kst[0])
        <= minute
        <= _minute(definition.buy_window_kst[1])
    ):
        return {**base, "phase": "WAITING_FOR_NEXT_OPEN", "action": "NO_ACTION", "cycle": cycle}
    if cycle["decision"]["action"] != "BUY_CANDIDATE":
        return {**base, "phase": "NEXT_OPEN", "action": "HOLD", "cycle": cycle}
    symbol = str(cycle["decision"]["symbol"])
    sync_official_quote(database, client, symbol)
    order = prepare_official_mode_shadow_order(
        database,
        mission_id=definition.mission_id,
        idempotency_key=f"{definition.strategy_id}:{cycle['id']}:buy",
        now_kst=now_kst,
    )
    reconciliation = reconcile_managed_account(database, client, mission_id=PILOT_MISSION_ID)
    sync_official_quote(database, client, symbol)
    intent = build_live_intent(
        database,
        guardian,
        shadow_order_id=order["id"],
        idempotency_key=f"{definition.strategy_id}:{cycle['id']}:intent",
        execution_mission_id=PILOT_MISSION_ID,
    )
    return {
        **base,
        "phase": "AWAITING_OWNER",
        "action": "INTENT_READY" if intent["state"] == "AWAITING_APPROVAL" else "BLOCKED",
        "cycle": cycle,
        "shadow_order": order,
        "reconciliation": reconciliation,
        "intent": intent,
    }


def daily_modes_status(database: Database) -> dict[str, Any]:
    modes: list[dict[str, Any]] = []
    for mode_code, definition in MODE_DEFINITIONS.items():
        cycle = database.get_latest_cycle(definition.mission_id)
        modes.append(
            {
                "mode_code": mode_code,
                "spec": mode_spec(mode_code),
                "latest_cycle": (
                    cycle
                    if cycle and cycle.get("strategy_id") == definition.strategy_id
                    else None
                ),
                "state": (
                    cycle["decision"]["action"]
                    if cycle and cycle.get("strategy_id") == definition.strategy_id
                    else "NOT_RUN"
                ),
            }
        )
    return {"modes": modes, "automatic_broker_submission": False}
