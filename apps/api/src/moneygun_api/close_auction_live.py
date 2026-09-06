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
from .pilot import PILOT_DAILY_LOSS_KRW, PILOT_ORDER_BUDGET_KRW
from .qualification import instrument_is_eligible
from .research import finalize_snapshot
from .shadow_orders import prepare_close_auction_shadow_order
from .storage import Database

KST = timezone(timedelta(hours=9))
MISSION_ID = "mission_close_auction_001"
STRATEGY_ID = "CLOSE-AUCTION-KR-v3-L0"
PROTOCOL_VERSION = "FULL_MARKET_RANK_AND_DETAIL-v1"
QUALIFIED_SOURCES = (
    "KRX_AUTHORIZED_EXPORT",
    "LICENSED_VENDOR",
    "OFFICIAL_PUBLIC_DATA_API_AND_KIND_UI_EXPORT",
    "OFFICIAL_PUBLIC_DATA_API_KIND_AND_KIWOOM",
)


@dataclass(frozen=True)
class LiveCloseAuctionConfig:
    signal_window_start_kst: str = "15:05"
    signal_cutoff_kst: str = "15:10"
    order_window_start_kst: str = "15:20"
    order_window_end_kst: str = "15:27"
    score_threshold: int = 80
    min_price_krw: int = 2_000
    max_price_krw: int = PILOT_ORDER_BUDGET_KRW
    median_turnover_floor_krw: int = 5_000_000_000
    current_turnover_floor_krw: int = 3_000_000_000
    min_market_cap_100m_krw: int = 500
    small_cap_max_100m_krw: int = 10_000
    min_day_return_pct: float = 1.0
    max_day_return_pct: float = 15.0
    min_volume_multiple: float = 1.2
    max_volume_multiple: float = 6.0
    min_close_location_pct: float = 70.0
    max_detail_candidates: int = 16
    max_archive_age_days: int = 3


def live_strategy_spec(config: LiveCloseAuctionConfig | None = None) -> dict[str, Any]:
    config = config or LiveCloseAuctionConfig()
    return {
        "strategy_id": STRATEGY_ID,
        "protocol_version": PROTOCOL_VERSION,
        "mode_code": "CLOSE_AUCTION",
        "display_name": "종가매매 · 5만 원 전시장 스캔",
        "market": "KRX",
        "l0_only": True,
        "clock": {
            "signal_window": (f"{config.signal_window_start_kst}~{config.signal_cutoff_kst}"),
            "closing_auction": (f"{config.order_window_start_kst}~{config.order_window_end_kst}"),
        },
        "universe": {
            "markets": ["KOSPI", "KOSDAQ"],
            "security_type": "COMMON_STOCK",
            "price_krw": [config.min_price_krw, config.max_price_krw],
            "small_cap_band_100m_krw": [
                config.min_market_cap_100m_krw,
                config.small_cap_max_100m_krw,
            ],
            "discovery": "KIWOOM ka10030 KOSPI·KOSDAQ 거래대금 순위",
            "detail": "KIWOOM ka10001·ka10081 공식 시세·일봉",
        },
        "risk": {
            "capital_krw": 50_000,
            "order_budget_krw": PILOT_ORDER_BUDGET_KRW,
            "daily_loss_krw": PILOT_DAILY_LOSS_KRW,
            "max_positions": 1,
            "approval": "L0 매 주문 5분 소유자 승인",
            "automatic_submission": False,
        },
        "trading_enabled": False,
    }


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


def _latest_qualified_snapshot(database: Database) -> dict[str, Any]:
    latest = database.get_latest_snapshot_for_sources(QUALIFIED_SOURCES)
    if latest is None:
        raise ValueError("전시장 스캔에 필요한 자격 보통주·시장조치 번들이 없습니다.")
    return latest


def _shortlist(
    ranking_rows: list[dict[str, Any]],
    snapshot: dict[str, Any],
    today: str,
    config: LiveCloseAuctionConfig,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    lifecycle = {item["symbol"]: item for item in snapshot.get("universe_history", [])}
    counters = {
        "ranking_rows": len(ranking_rows),
        "not_common_or_inactive": 0,
        "market_action_excluded": 0,
        "price_excluded": 0,
        "affordable_rows": 0,
    }
    allowed: list[dict[str, Any]] = []
    for row in ranking_rows:
        meta = lifecycle.get(row["symbol"])
        if meta is None or meta.get("security_type") != "COMMON_STOCK":
            counters["not_common_or_inactive"] += 1
            continue
        if not instrument_is_eligible(snapshot, row["symbol"], today):
            counters["market_action_excluded"] += 1
            continue
        price = int(row["current_price_krw"])
        if not config.min_price_krw <= price <= config.max_price_krw:
            counters["price_excluded"] += 1
            continue
        allowed.append({**row, "name": meta.get("name", row["name"]), "market": meta["market"]})
    counters["affordable_rows"] = len(allowed)

    overall = sorted(allowed, key=lambda item: item["estimated_turnover_krw"], reverse=True)
    kosdaq_affordable = [item for item in overall if item["market"] == "KOSDAQ"]
    selected: list[dict[str, Any]] = []
    for item in overall[:8] + kosdaq_affordable[:8]:
        if item["symbol"] not in {existing["symbol"] for existing in selected}:
            selected.append(item)
        if len(selected) >= config.max_detail_candidates:
            break
    counters["detail_candidates"] = len(selected)
    return selected, counters


def _score_candidate(
    row: dict[str, Any],
    quote: dict[str, Any],
    bars: list[dict[str, Any]],
    benchmark_bars: list[dict[str, Any]],
    today: date,
    config: LiveCloseAuctionConfig,
) -> dict[str, Any]:
    previous = [bar for bar in bars if bar["date"] < today.isoformat()]
    benchmark_previous = [bar for bar in benchmark_bars if bar["date"] < today.isoformat()]
    reasons: list[str] = []
    if len(previous) < 200 or len(benchmark_previous) < 200:
        return {**row, "score": 0, "eligible": False, "reasons": ["200거래일 이력 부족"]}
    if (today - date.fromisoformat(previous[-1]["date"])).days > 5:
        return {**row, "score": 0, "eligible": False, "reasons": ["직전 일봉이 오래됨"]}

    closes = [float(bar["close"]) for bar in previous]
    volumes = [float(bar["volume"]) for bar in previous]
    benchmark_closes = [float(bar["close"]) for bar in benchmark_previous]
    current = float(quote["current_price_krw"])
    open_price = float(quote["open_price_krw"] or current)
    high = float(quote["high_price_krw"] or max(open_price, current))
    low = float(quote["low_price_krw"] or min(open_price, current))
    current_volume = float(quote["volume"] or row["volume"])
    sma20, sma60 = _mean(closes[-20:]), _mean(closes[-60:])
    relative_20 = current / closes[-20] - benchmark_closes[-1] / benchmark_closes[-20]
    median_volume = statistics.median(volumes[-20:])
    volume_multiple = current_volume / median_volume if median_volume else 0.0
    median_turnover = statistics.median(
        float(bar["close"]) * float(bar["volume"]) for bar in previous[-20:]
    )
    current_turnover = current * current_volume
    day_return_pct = (current / closes[-1] - 1) * 100
    day_range = high - low
    close_location_pct = ((current - low) / day_range * 100) if day_range > 0 else 50.0

    trend = 20 if current > sma20 > sma60 else 0
    trend += 15 if relative_20 > 0 else 0
    trend += 10 if current >= max(closes[-20:]) * 0.97 else 0
    participation = (
        15 if config.min_volume_multiple <= volume_multiple <= config.max_volume_multiple else 0
    )
    participation += (
        10 if config.min_day_return_pct <= day_return_pct <= config.max_day_return_pct else 0
    )
    participation += 5 if close_location_pct >= config.min_close_location_pct else 0
    liquidity = (
        15
        if (
            median_turnover >= config.median_turnover_floor_krw
            and current_turnover >= config.current_turnover_floor_krw
        )
        else 0
    )
    regime = 10 if benchmark_closes[-1] > _mean(benchmark_closes[-200:]) else 0
    score = trend + participation + liquidity + regime
    market_cap = int(quote.get("market_cap_100m_krw", 0))

    if market_cap <= 0:
        reasons.append("공식 시가총액 확인 불가")
    elif market_cap < config.min_market_cap_100m_krw:
        reasons.append("시가총액 500억 원 미만 초소형주 제외")
    if not config.min_day_return_pct <= day_return_pct <= config.max_day_return_pct:
        reasons.append("당일 상승률 1~15% 범위 미통과")
    if not config.min_volume_multiple <= volume_multiple <= config.max_volume_multiple:
        reasons.append("거래량 배수 1.2~6배 범위 미통과")
    if close_location_pct < config.min_close_location_pct:
        reasons.append("현재가가 당일 고가권 70% 미만")
    if liquidity == 0:
        reasons.append("20일·당일 거래대금 하한 미통과")
    if score < config.score_threshold:
        reasons.append(f"종합점수 {score}/{config.score_threshold} 미달")

    stop_distance = min(max(_atr_ratio(previous) * 1.5, 0.03), 0.06)
    return {
        **row,
        "name": quote.get("name") or row["name"],
        "score": score,
        "trend": trend,
        "participation": participation,
        "liquidity": liquidity,
        "market_regime": regime,
        "current_price_krw": round(current),
        "day_return_pct": round(day_return_pct, 2),
        "volume_multiple": round(volume_multiple, 2),
        "median_turnover_krw": round(median_turnover),
        "current_turnover_krw": round(current_turnover),
        "close_location_pct": round(close_location_pct, 1),
        "market_cap_100m_krw": market_cap,
        "size_band": (
            "SMALL_CAP"
            if config.min_market_cap_100m_krw <= market_cap <= config.small_cap_max_100m_krw
            else "LARGE_OR_UNKNOWN"
        ),
        "stop_distance": stop_distance,
        "eligible": not reasons,
        "reasons": reasons,
    }


def run_live_close_auction_scan(
    database: Database,
    client: KiwoomReadOnlyClient,
    *,
    now_kst: datetime | None = None,
    config: LiveCloseAuctionConfig | None = None,
) -> dict[str, Any]:
    config = config or LiveCloseAuctionConfig()
    now_kst = (now_kst or datetime.now(KST)).astimezone(KST)
    today = now_kst.date()
    if today.weekday() >= 5:
        raise ValueError("KRX 거래일에만 종가 전시장 스캔을 실행합니다.")
    minute = now_kst.hour * 60 + now_kst.minute
    if not (15 * 60 + 5 <= minute <= 15 * 60 + 10):
        raise ValueError("종가 전시장 스캔은 15:05~15:10에만 실행합니다.")

    latest_cycle = database.get_latest_cycle(MISSION_ID)
    if (
        latest_cycle
        and latest_cycle.get("strategy_id") == STRATEGY_ID
        and str(latest_cycle.get("as_of", ""))[:10] == today.isoformat()
    ):
        return {"spec": live_strategy_spec(config), "cycle": latest_cycle, "reused": True}

    qualified = _latest_qualified_snapshot(database)
    archive_age = (today - date.fromisoformat(qualified["as_of"][:10])).days
    if qualified["quality"]["state"] != "PASS" or archive_age > config.max_archive_age_days:
        raise ValueError(
            "자격 보통주·시장조치 데이터가 3일보다 오래되었거나 품질을 통과하지 못했습니다."
        )

    ranking_rows: list[dict[str, Any]] = []
    for market in ("KOSPI", "KOSDAQ"):
        for price_filter in ("0", "10"):
            ranking_rows.extend(
                client.fetch_today_volume_top(market=market, price_filter=price_filter, max_pages=2)
            )
    ranking_rows = list({item["symbol"]: item for item in ranking_rows}.values())
    shortlist, coverage = _shortlist(ranking_rows, qualified, today.isoformat(), config)
    benchmark_bars = client.fetch_daily_bars(
        "069500", base_date=today.strftime("%Y%m%d"), max_pages=4
    )
    evaluated: list[dict[str, Any]] = []
    snapshot_instruments: list[dict[str, Any]] = []
    for row in shortlist:
        raw_quote = client.fetch_domestic_quote(row["symbol"])
        quote = normalize_quote(row["symbol"], raw_quote)
        bars = client.fetch_daily_bars(
            row["symbol"], base_date=today.strftime("%Y%m%d"), max_pages=4
        )
        detail = _score_candidate(row, quote, bars, benchmark_bars, today, config)
        evaluated.append(detail)
        snapshot_instruments.append(
            {
                "symbol": row["symbol"],
                "name": detail["name"],
                "market": row["market"],
                "sector": "",
                "bars": bars,
                "live_quote": quote,
            }
        )

    evaluated.sort(
        key=lambda item: (
            bool(item.get("eligible")),
            int(item.get("score", 0)),
            int(item.get("current_turnover_krw", 0)),
        ),
        reverse=True,
    )
    best = next((item for item in evaluated if item.get("eligible")), None)
    price = int(best["current_price_krw"]) if best else 0
    stop_distance = float(best["stop_distance"]) if best else 1.0
    risk_budget = PILOT_DAILY_LOSS_KRW
    position_budget = min(
        PILOT_ORDER_BUDGET_KRW,
        risk_budget / stop_distance if stop_distance else 0,
    )
    quantity = int(position_budget // price) if price else 0
    if best is not None and quantity < 1:
        best = None

    as_of = now_kst.isoformat(timespec="seconds")
    snapshot = finalize_snapshot(
        {
            "as_of": as_of,
            "source": "KIWOOM_CLOSE_AUCTION_LIVE_SCAN",
            "license_basis": "USER_AUTHORIZED_INTERNAL_RESEARCH",
            "market": "KR",
            "benchmark": {
                "symbol": "069500",
                "name": "KODEX 200",
                "role": "KOSPI200_PROXY",
                "bars": benchmark_bars,
            },
            "instruments": snapshot_instruments,
            "evidence": [
                {
                    "id": "ev_kiwoom_live_rank",
                    "source": "KIWOOM_OFFICIAL_REST",
                    "title": "키움 KRX 당일 거래대금 순위·기본정보·일봉",
                    "published_at": as_of,
                    "url": "https://openapi.kiwoom.com/guide/apiguide",
                }
            ],
            "collection_manifest": {
                "qualified_snapshot_id": qualified["id"],
                "qualified_snapshot_age_days": archive_age,
                "survivorship_bias_controlled": True,
                "historical_designation_states_complete": True,
                "ranking_api_id": "ka10030",
                "detail_api_ids": ["ka10001", "ka10081"],
                "coverage": coverage,
                "evaluated_count": len(evaluated),
                "small_cap_evaluated_count": sum(
                    item.get("size_band") == "SMALL_CAP" for item in evaluated
                ),
                "same_day_information_cutoff": config.signal_cutoff_kst,
                "redistribution_allowed": False,
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
        "order_allowed": False,
        "entry_style": "KRX_CLOSING_AUCTION_LIMIT",
        "reason": (
            "전시장·유동성·가격·시장조치 조건을 통과한 L0 실험 후보입니다. "
            "소유자 승인 전 주문하지 않습니다."
            if best
            else "오늘은 5만 원 L0 종가매매의 가격·유동성·추세·과열 방어 조건을 "
            "모두 통과한 종목이 없습니다."
        ),
    }
    risk = {
        "result": "PASS_L0_EXPERIMENTAL" if best else "HOLD",
        "quantity": decision["quantity"],
        "entry_reference_krw": price,
        "invalidation_price_krw": round(price * (1 - stop_distance)) if best else 0,
        "risk_budget_krw": risk_budget,
        "position_budget_krw": round(position_budget) if best else 0,
        "order_allowed": False,
        "reasons": [
            "수익성 OOS 미검증 L0 전용입니다.",
            "실주문은 별도 5분 소유자 승인과 제출이 필요합니다.",
        ],
    }
    reports = [
        (
            "DATA",
            "김데이터",
            f"KOSPI·KOSDAQ 순위 {coverage['ranking_rows']}행과 자격 종목을 대조했습니다.",
        ),
        ("NOVA", "김뉴스", "당일 신규 공시·돌발 뉴스는 별도 반대 근거로 유지합니다."),
        (
            "SERENITY",
            "김차트",
            f"저가·중소형 후보를 포함해 {len(evaluated)}종목을 상세 채점했습니다.",
        ),
        ("PULSE", "김시장", "직전 완료 일봉 추세와 15:10 이전 당일 수급을 분리해 계산했습니다."),
        ("BULL", "김찬성", "추세·거래량·고가권·유동성이 동시에 통과할 때만 찬성합니다."),
        ("BEAR", "김반대", "초소형·과열·저유동성·시장조치 종목을 반대했습니다."),
        (
            "RISK",
            "김안전",
            f"5만 원 원장·4만5천 원 주문·계획손실 {risk_budget:,}원을 적용했습니다.",
        ),
        ("ACE", "김투자", decision["reason"]),
        ("OPS", "김주문", "L0 승인 대기 주문안까지만 자동 준비하고 브로커 제출은 하지 않습니다."),
    ]
    material = {
        "snapshot_id": snapshot["id"],
        "strategy_id": STRATEGY_ID,
        "protocol_version": PROTOCOL_VERSION,
        "decision": decision,
        "risk": risk,
        "config": asdict(config),
    }
    checksum = hashlib.sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    cycle = database.save_cycle(
        {
            "id": f"cycle_{checksum[:16]}",
            "mission_id": MISSION_ID,
            "snapshot_id": snapshot["id"],
            "strategy_id": STRATEGY_ID,
            "protocol_version": PROTOCOL_VERSION,
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
                    "role": "종가 전시장 L0",
                    "state": "READY" if code == "OPS" else "DONE",
                    "summary": summary,
                    "claims": [],
                    "evidence_ids": ["ev_kiwoom_live_rank"],
                    "unknowns": [],
                    "invalidation_conditions": ["데이터 신선도·시장조치·호가·위험 한도 변경"],
                    "confidence": 1.0 if code in {"DATA", "RISK", "OPS"} else 0.7,
                }
                for code, name, summary in reports
            ],
            "coverage": snapshot["collection_manifest"],
            "l0_only": True,
            "performance_qualified": False,
            "trading_enabled": False,
        }
    )
    return {"spec": live_strategy_spec(config), "cycle": cycle, "reused": False}


def live_scan_status(
    database: Database, *, now_kst: datetime | None = None
) -> dict[str, Any]:
    now_kst = (now_kst or datetime.now(KST)).astimezone(KST)
    cycle = database.get_latest_cycle(MISSION_ID)
    is_live = bool(
        cycle
        and cycle.get("strategy_id") == STRATEGY_ID
        and str(cycle.get("as_of", ""))[:10] == now_kst.date().isoformat()
    )
    minute = now_kst.hour * 60 + now_kst.minute
    if is_live:
        state = (
            "BUY_CANDIDATE"
            if cycle["decision"]["action"] == "BUY_CANDIDATE"
            else "HOLD"
        )
    elif now_kst.weekday() >= 5:
        state = "MARKET_CLOSED"
    elif minute > 15 * 60 + 27:
        state = "WINDOW_ENDED"
    elif minute > 15 * 60 + 10:
        state = "SCAN_MISSED"
    else:
        state = "WAITING_FOR_15_05"
    return {
        "spec": live_strategy_spec(),
        "cycle": cycle if is_live else None,
        "state": state,
        "automatic_broker_submission": False,
    }


def run_close_auction_pilot_tick(
    database: Database,
    client: KiwoomReadOnlyClient,
    guardian: ExecutionGuardian,
    *,
    now_kst: datetime | None = None,
) -> dict[str, Any]:
    """Advance one idempotent L0 phase, stopping before owner approval/submission."""
    now_kst = (now_kst or datetime.now(KST)).astimezone(KST)
    today = now_kst.date()
    minute = now_kst.hour * 60 + now_kst.minute
    base = {
        "as_of": now_kst.isoformat(timespec="seconds"),
        "spec": live_strategy_spec(),
        "automatic_broker_submission": False,
        "owner_action_required_for_submission": True,
    }
    if today.weekday() >= 5:
        return {**base, "phase": "MARKET_CLOSED", "action": "NO_ACTION"}
    if 15 * 60 + 5 <= minute <= 15 * 60 + 10:
        scan = run_live_close_auction_scan(database, client, now_kst=now_kst)
        return {**base, "phase": "MARKET_SCAN", "action": "SCAN_READY", **scan}

    cycle = database.get_latest_cycle(MISSION_ID)
    same_day_cycle = bool(
        cycle
        and cycle.get("strategy_id") == STRATEGY_ID
        and str(cycle.get("as_of", ""))[:10] == today.isoformat()
    )
    if minute < 15 * 60 + 5:
        return {**base, "phase": "WAITING_FOR_SCAN", "action": "NO_ACTION"}
    if 15 * 60 + 11 <= minute < 15 * 60 + 20:
        return {
            **base,
            "phase": "WAITING_FOR_AUCTION",
            "action": "CANDIDATE_READY" if same_day_cycle else "SCAN_MISSED",
            "cycle": cycle if same_day_cycle else None,
        }
    if 15 * 60 + 20 <= minute <= 15 * 60 + 27:
        if not same_day_cycle:
            return {
                **base,
                "phase": "AUCTION_PREP",
                "action": "BLOCKED",
                "reason": "15:05 당일 스캔 기록이 없습니다.",
            }
        if cycle["decision"]["action"] != "BUY_CANDIDATE":
            return {**base, "phase": "AUCTION_PREP", "action": "HOLD", "cycle": cycle}
        symbol = str(cycle["decision"]["symbol"])
        sync_official_quote(database, client, symbol)
        order = prepare_close_auction_shadow_order(
            database,
            mission_id=MISSION_ID,
            idempotency_key=f"close-v3-l0-{today.isoformat()}-{symbol}",
            now_kst=now_kst,
        )
        reconciliation = reconcile_managed_account(
            database, client, mission_id="mission_l0_pilot_001"
        )
        # Re-sync after the account call so the intent always sees a <=15 second quote.
        sync_official_quote(database, client, symbol)
        intent = build_live_intent(
            database,
            guardian,
            shadow_order_id=order["id"],
            idempotency_key=f"close-v3-l0-intent-{today.isoformat()}-{symbol}",
            execution_mission_id="mission_l0_pilot_001",
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
    return {
        **base,
        "phase": "WINDOW_ENDED" if minute > 15 * 60 + 27 else "WAITING_FOR_AUCTION",
        "action": "NO_ACTION",
        "cycle": cycle if same_day_cycle else None,
    }
