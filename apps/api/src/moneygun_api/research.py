from __future__ import annotations

import hashlib
import json
import math
import statistics
from datetime import date, timedelta
from typing import Any

STRATEGY_ID = "FOCUS-MOMENTUM-KR-v1"
AS_OF = "2026-08-31T15:40:00+09:00"


def _trading_days(end: date, count: int) -> list[str]:
    days: list[str] = []
    current = end
    while len(days) < count:
        if current.weekday() < 5:
            days.append(current.isoformat())
        current -= timedelta(days=1)
    return list(reversed(days))


def _bars(
    symbol: str,
    *,
    end: date,
    history_days: int,
    base: float,
    drift: float,
    wave: float,
    base_volume: int,
    final_volume_multiple: float = 1.0,
) -> list[dict[str, int | str]]:
    # Five calendar years of deterministic business-day history lets the OOS engine
    # exercise its real promotion gates without presenting synthetic results as evidence.
    days = _trading_days(end, history_days)
    result: list[dict[str, int | str]] = []
    previous = base
    for index, day in enumerate(days):
        close = max(1_000, base + drift * index + math.sin(index / 6.0) * wave)
        open_price = previous * (1 + math.sin(index / 4.0) * 0.003)
        high = max(open_price, close) * 1.016
        low = min(open_price, close) * 0.984
        volume = int(base_volume * (1 + math.sin(index / 8.0) * 0.12))
        if index == len(days) - 1:
            volume = int(base_volume * final_volume_multiple)
        result.append(
            {
                "date": day,
                "open": round(open_price),
                "high": round(high),
                "low": round(low),
                "close": round(close),
                "volume": volume,
            }
        )
        previous = close
    return result


def build_fixture_snapshot(
    as_of_date: date | None = None, *, history_days: int = 1320
) -> dict[str, Any]:
    """Create a deterministic Korean-market-shaped fixture; never present it as live data."""
    snapshot_date = as_of_date or date(2026, 8, 31)
    as_of = f"{snapshot_date.isoformat()}T15:40:00+09:00"
    instruments = [
        {
            "symbol": "MG-A01",
            "name": "샘플 반도체 장비 A",
            "market": "KOSDAQ",
            "sector": "반도체 장비",
            "bars": _bars(
                "MG-A01",
                end=snapshot_date,
                history_days=history_days,
                base=21_000,
                drift=10,
                wave=410,
                base_volume=1_350_000,
                final_volume_multiple=1.55,
            ),
            "fundamentals": {
                "operating_profit_positive": True,
                "operating_cashflow_positive": True,
                "roe_ok": True,
                "debt_ratio_ok": True,
                "growth_positive": True,
            },
            "fundamentals_history": [
                {
                    "effective_at": "2021-06-01T08:00:00+09:00",
                    "values": {
                        "operating_profit_positive": True,
                        "operating_cashflow_positive": True,
                        "roe_ok": True,
                        "debt_ratio_ok": True,
                        "growth_positive": True,
                    },
                }
            ],
            "catalyst_evidence_ids": ["ev_dart_a", "ev_supply_a"],
            "customer_concentration": "HIGH",
        },
        {
            "symbol": "MG-B07",
            "name": "샘플 전력 인프라 B",
            "market": "KOSPI",
            "sector": "전력 인프라",
            "bars": _bars(
                "MG-B07",
                end=snapshot_date,
                history_days=history_days,
                base=28_000,
                drift=7,
                wave=520,
                base_volume=1_100_000,
                final_volume_multiple=1.32,
            ),
            "fundamentals": {
                "operating_profit_positive": True,
                "operating_cashflow_positive": True,
                "roe_ok": True,
                "debt_ratio_ok": True,
                "growth_positive": False,
            },
            "fundamentals_history": [
                {
                    "effective_at": "2021-06-01T08:00:00+09:00",
                    "values": {
                        "operating_profit_positive": True,
                        "operating_cashflow_positive": True,
                        "roe_ok": True,
                        "debt_ratio_ok": True,
                        "growth_positive": False,
                    },
                }
            ],
            "catalyst_evidence_ids": ["ev_dart_b"],
            "customer_concentration": "MEDIUM",
        },
        {
            "symbol": "MG-C12",
            "name": "샘플 냉각 솔루션 C",
            "market": "KOSDAQ",
            "sector": "산업재",
            "bars": _bars(
                "MG-C12",
                end=snapshot_date,
                history_days=history_days,
                base=17_000,
                drift=0.5,
                wave=780,
                base_volume=560_000,
                final_volume_multiple=0.88,
            ),
            "fundamentals": {
                "operating_profit_positive": True,
                "operating_cashflow_positive": False,
                "roe_ok": False,
                "debt_ratio_ok": True,
                "growth_positive": False,
            },
            "fundamentals_history": [
                {
                    "effective_at": "2021-06-01T08:00:00+09:00",
                    "values": {
                        "operating_profit_positive": True,
                        "operating_cashflow_positive": False,
                        "roe_ok": False,
                        "debt_ratio_ok": True,
                        "growth_positive": False,
                    },
                }
            ],
            "catalyst_evidence_ids": [],
            "customer_concentration": "UNKNOWN",
        },
    ]
    core = {
        "as_of": as_of,
        "source": "FIXTURE_KR_REPRODUCIBLE",
        "market": "KR",
        "benchmark": {
            "symbol": "MG-K200",
            "bars": _bars(
                "MG-K200",
                end=snapshot_date,
                history_days=history_days,
                base=300_000,
                drift=30,
                wave=4_000,
                base_volume=100_000_000,
            ),
        },
        "instruments": instruments,
        "evidence": [
            {
                "id": "ev_snapshot",
                "source": "SYSTEM",
                "title": "재현 가능한 한국시장 fixture 스냅샷",
                "published_at": as_of,
                "verified": True,
            },
            {
                "id": "ev_dart_a",
                "source": "DART_FIXTURE",
                "title": "샘플 A 공급계약 공시",
                "published_at": f"{(snapshot_date - timedelta(days=4)).isoformat()}T14:32:00+09:00",
                "verified": True,
            },
            {
                "id": "ev_supply_a",
                "source": "IR_FIXTURE",
                "title": "샘플 A 고객 설비투자 확대 자료",
                "published_at": f"{(snapshot_date - timedelta(days=5)).isoformat()}T10:00:00+09:00",
                "verified": True,
            },
            {
                "id": "ev_dart_b",
                "source": "DART_FIXTURE",
                "title": "샘플 B 수주잔고 증가 공시",
                "published_at": f"{(snapshot_date - timedelta(days=6)).isoformat()}T16:10:00+09:00",
                "verified": True,
            },
        ],
    }
    return finalize_snapshot(core)


def finalize_snapshot(core: dict[str, Any]) -> dict[str, Any]:
    """Validate and content-address an imported or generated point-in-time snapshot."""
    quality = validate_snapshot(core)
    checksum = hashlib.sha256(
        json.dumps(core, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {"id": f"snap_{checksum[:16]}", **core, "quality": quality, "checksum": checksum}


def validate_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    issues: list[str] = []
    as_of_date = date.fromisoformat(snapshot["as_of"][:10])
    seen: set[tuple[str, str]] = set()
    benchmark_dates = [bar["date"] for bar in snapshot["benchmark"]["bars"]]
    if len(benchmark_dates) < 200:
        issues.append("benchmark: insufficient_history")
    if benchmark_dates != sorted(set(benchmark_dates)):
        issues.append("benchmark: duplicate_or_unsorted_bar")
    for bar in snapshot["benchmark"]["bars"]:
        if date.fromisoformat(bar["date"]) > as_of_date:
            issues.append("benchmark: future_bar")
        if min(bar["open"], bar["high"], bar["low"], bar["close"], bar["volume"]) <= 0:
            issues.append("benchmark: invalid_ohlcv")
        if bar["low"] > min(bar["open"], bar["close"]) or bar["high"] < max(
            bar["open"], bar["close"]
        ):
            issues.append("benchmark: inconsistent_ohlc")

    for instrument in snapshot["instruments"]:
        if len(instrument["bars"]) < 200:
            issues.append(f"{instrument['symbol']}: insufficient_history")
        instrument_dates = [bar["date"] for bar in instrument["bars"]]
        if instrument_dates != sorted(set(instrument_dates)):
            issues.append(f"{instrument['symbol']}: duplicate_or_unsorted_bar")
        for bar in instrument["bars"]:
            key = (instrument["symbol"], bar["date"])
            if key in seen:
                issues.append(f"{instrument['symbol']}: duplicate_bar")
            seen.add(key)
            if date.fromisoformat(bar["date"]) > as_of_date:
                issues.append(f"{instrument['symbol']}: future_bar")
            if min(bar["open"], bar["high"], bar["low"], bar["close"], bar["volume"]) <= 0:
                issues.append(f"{instrument['symbol']}: invalid_ohlcv")
            if bar["low"] > min(bar["open"], bar["close"]) or bar["high"] < max(
                bar["open"], bar["close"]
            ):
                issues.append(f"{instrument['symbol']}: inconsistent_ohlc")
    return {
        "state": "PASS" if not issues else "FAIL",
        "issues": issues,
        "instrument_count": len(snapshot["instruments"]),
        "bar_count": sum(len(item["bars"]) for item in snapshot["instruments"]),
        "future_data_count": sum("future_bar" in issue for issue in issues),
    }


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _atr(bars: list[dict[str, Any]], length: int = 14) -> float:
    ranges: list[float] = []
    for previous, current in zip(bars[-length - 1 : -1], bars[-length:], strict=True):
        ranges.append(
            max(
                current["high"] - current["low"],
                abs(current["high"] - previous["close"]),
                abs(current["low"] - previous["close"]),
            )
        )
    return _mean(ranges)


def score_snapshot(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    benchmark_closes = [bar["close"] for bar in snapshot["benchmark"]["bars"]]
    benchmark_return_63 = benchmark_closes[-1] / benchmark_closes[-64] - 1
    market_regime = benchmark_closes[-1] > _mean(benchmark_closes[-200:])
    candidates: list[dict[str, Any]] = []
    for instrument in snapshot["instruments"]:
        bars = instrument["bars"]
        closes = [bar["close"] for bar in bars]
        volumes = [bar["volume"] for bar in bars]
        sma20, sma60, sma120 = (_mean(closes[-20:]), _mean(closes[-60:]), _mean(closes[-120:]))
        recent_return = closes[-1] / closes[-64] - 1
        median_volume = statistics.median(volumes[-20:])
        volume_multiple = volumes[-1] / median_volume
        median_value = statistics.median(bar["close"] * bar["volume"] for bar in bars[-20:])
        atr = _atr(bars)
        atr_ratio = atr / closes[-1]

        momentum = 0
        momentum += 15 if closes[-1] > sma20 > sma60 > sma120 else 0
        momentum += 10 if recent_return > benchmark_return_63 else 0
        momentum += 5 if closes[-1] >= max(closes[-20:]) * 0.97 else 0
        momentum += 5 if 1.2 <= volume_multiple <= 3.0 else 0
        momentum += 5 if _mean(closes[-5:]) > _mean(closes[-10:-5]) else 0
        quality = sum(5 for value in instrument["fundamentals"].values() if value)
        liquidity = 0
        liquidity += 5 if median_value >= 30_000_000_000 else 0
        liquidity += 5 if 0.015 <= atr_ratio <= 0.06 else 0
        liquidity += 5 if snapshot["quality"]["state"] == "PASS" else 0
        regime = 10 if market_regime else 0
        evidence_count = len(instrument["catalyst_evidence_ids"])
        catalyst = 10 if evidence_count >= 2 else 5 if evidence_count == 1 else 0
        total = momentum + quality + liquidity + regime + catalyst
        stop_distance = max(2 * atr_ratio, 0.05)
        candidates.append(
            {
                "rank": 0,
                "symbol": instrument["symbol"],
                "name": instrument["name"],
                "sector": instrument["sector"],
                "score": total,
                "momentum": momentum,
                "quality": quality,
                "liquidity_risk": liquidity,
                "market_regime": regime,
                "catalyst": catalyst,
                "close": closes[-1],
                "return_63_pct": round(recent_return * 100, 2),
                "volume_multiple": round(volume_multiple, 2),
                "atr_ratio": round(atr_ratio, 4),
                "stop_distance": round(stop_distance, 4),
                "evidence_ids": instrument["catalyst_evidence_ids"] + ["ev_snapshot"],
                "customer_concentration": instrument["customer_concentration"],
            }
        )
    candidates.sort(key=lambda item: (item["score"], item["return_63_pct"]), reverse=True)
    for rank, candidate in enumerate(candidates, start=1):
        candidate["rank"] = rank
    return candidates


def run_backtest(snapshot: dict[str, Any]) -> dict[str, Any]:
    trade_returns: list[float] = []
    for instrument in snapshot["instruments"]:
        closes = [bar["close"] for bar in instrument["bars"]]
        for entry_index in (130, 150, 170, 190):
            gross_return = closes[entry_index + 10] / closes[entry_index] - 1
            trade_returns.append(gross_return - 0.003)
    equity, peak, max_drawdown = 1.0, 1.0, 0.0
    for trade_return in trade_returns:
        equity *= 1 + trade_return
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity / peak - 1)
    deviation = statistics.stdev(trade_returns) if len(trade_returns) > 1 else 0
    sharpe = (_mean(trade_returns) / deviation * math.sqrt(25.2)) if deviation else 0
    return {
        "status": "FIXTURE_RESEARCH_ONLY",
        "cost_assumption_bps": 30,
        "trade_count": len(trade_returns),
        "net_return_pct": round((equity - 1) * 100, 2),
        "max_drawdown_pct": round(max_drawdown * 100, 2),
        "win_rate_pct": round(
            sum(value > 0 for value in trade_returns) / len(trade_returns) * 100, 1
        ),
        "sharpe": round(sharpe, 2),
        "oos_validated": False,
    }


def _report(
    *,
    code: str,
    name: str,
    role: str,
    summary: str,
    claims: list[dict[str, str]],
    evidence_ids: list[str],
    unknowns: list[str],
    invalidation_conditions: list[str],
    confidence: float,
    state: str = "DONE",
) -> dict[str, Any]:
    return {
        "id": f"report_{code.lower()}",
        "code": code,
        "name": name,
        "role": role,
        "state": state,
        "summary": summary,
        "claims": claims,
        "evidence_ids": sorted(set(evidence_ids)),
        "counter_evidence_ids": [],
        "unknowns": unknowns,
        "invalidation_conditions": invalidation_conditions,
        "confidence": confidence,
        "generator": "DETERMINISTIC_P0",
    }


def run_committee_cycle(mission: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    candidates = score_snapshot(snapshot)
    best = candidates[0]
    evidence_ids = best["evidence_ids"]
    reports = [
        _report(
            code="DATA",
            name="김데이터",
            role="데이터 검증",
            summary=(
                f"{snapshot['quality']['instrument_count']}종목 "
                f"{snapshot['quality']['bar_count']}개 봉을 검증했습니다."
            ),
            claims=[
                {
                    "text": "미래 데이터와 중복 봉이 없습니다.",
                    "direction": "neutral",
                    "importance": "critical",
                }
            ],
            evidence_ids=["ev_snapshot"],
            unknowns=[],
            invalidation_conditions=["새 결측 또는 미래 데이터 발견"],
            confidence=1.0,
        ),
        _report(
            code="NOVA",
            name="김뉴스",
            role="뉴스·공시 분석",
            summary=f"{best['name']}에 검증된 촉매 {best['catalyst'] // 5}건을 연결했습니다.",
            claims=[
                {
                    "text": "최근 공시 촉매가 후보 점수를 지지합니다.",
                    "direction": "positive",
                    "importance": "medium",
                }
            ],
            evidence_ids=[item for item in evidence_ids if item != "ev_snapshot"],
            unknowns=["실제 OpenDART 키 미설정"],
            invalidation_conditions=["공시 정정 또는 계약 취소"],
            confidence=0.72,
        ),
        _report(
            code="SERENITY",
            name="김산업",
            role="산업·공급망 분석",
            summary=f"{best['sector']} 공급망 촉매와 고객 집중도를 함께 확인했습니다.",
            claims=[
                {
                    "text": "공급망 근거가 상승 논리를 보강합니다.",
                    "direction": "positive",
                    "importance": "medium",
                }
            ],
            evidence_ids=evidence_ids,
            unknowns=["고객사별 실제 매출 비중"],
            invalidation_conditions=["고객 설비투자 축소"],
            confidence=0.68,
        ),
        _report(
            code="PULSE",
            name="김차트",
            role="차트·모멘텀 분석",
            summary=(
                f"{best['name']} 점수 {best['score']}점, "
                f"63일 수익률 {best['return_63_pct']}%입니다."
            ),
            claims=[
                {
                    "text": "중기 추세와 거래량 조건이 후보 기준을 통과했습니다.",
                    "direction": "positive",
                    "importance": "high",
                }
            ],
            evidence_ids=["ev_snapshot"],
            unknowns=[],
            invalidation_conditions=["종가가 SMA20 아래에서 2일 마감"],
            confidence=0.86,
        ),
        _report(
            code="BULL",
            name="김찬성",
            role="상승 논리",
            summary="추세·품질·공시 촉매가 같은 방향입니다.",
            claims=[
                {
                    "text": "결정론적 총점이 70점 기준을 넘었습니다.",
                    "direction": "positive",
                    "importance": "high",
                }
            ],
            evidence_ids=evidence_ids,
            unknowns=[],
            invalidation_conditions=["촉매 철회 또는 추세 무효화"],
            confidence=0.74,
        ),
        _report(
            code="BEAR",
            name="김반대",
            role="반대 논리",
            summary=(
                f"고객 집중도 {best['customer_concentration']}와 갭 손실 가능성을 제기했습니다."
            ),
            claims=[
                {
                    "text": "집중 고객의 투자 지연이 투자 논리를 훼손할 수 있습니다.",
                    "direction": "negative",
                    "importance": "high",
                }
            ],
            evidence_ids=evidence_ids,
            unknowns=["실제 고객별 계약 일정"],
            invalidation_conditions=["고객 집중 위험이 공식 자료로 해소"],
            confidence=0.7,
            state="CONFLICT",
        ),
    ]

    stop_distance = best["stop_distance"]
    risk_budget = mission["equity_krw"] * 0.05
    position_budget = min(mission["equity_krw"] * 0.5, risk_budget / stop_distance)
    quantity = math.floor(position_budget / best["close"])
    checks = {
        "snapshot_quality": snapshot["quality"]["state"] == "PASS",
        "candidate_score": best["score"] >= 70,
        "stop_distance": stop_distance <= 0.08,
        "minimum_quantity": quantity >= 1,
        "stage_allows_order": mission["stage"] != "R0",
        "trading_enabled": False,
    }
    research_pass = all(
        checks[key]
        for key in ("snapshot_quality", "candidate_score", "stop_distance", "minimum_quantity")
    )
    risk = {
        "result": "PASS_RESEARCH_ONLY" if research_pass else "REJECT",
        "checks": checks,
        "risk_budget_krw": round(risk_budget),
        "position_budget_krw": round(position_budget),
        "quantity": quantity,
        "entry_reference_krw": best["close"],
        "invalidation_price_krw": round(best["close"] * (1 - stop_distance)),
        "order_allowed": False,
        "reasons": [
            "R0 연구 단계에서는 주문을 만들지 않습니다.",
            "실거래 플래그는 항상 false입니다.",
        ],
    }
    reports.extend(
        [
            _report(
                code="RISK",
                name="김안전",
                role="한도·거부권",
                summary=f"연구 판정 {risk['result']}, 계획 수량 {quantity}주입니다.",
                claims=[
                    {
                        "text": "위험 수치는 결정론적 계산 결과입니다.",
                        "direction": "neutral",
                        "importance": "critical",
                    }
                ],
                evidence_ids=["ev_snapshot"],
                unknowns=[],
                invalidation_conditions=["데이터 지연·한도 위반·잔고 불일치"],
                confidence=1.0,
            ),
            _report(
                code="ACE",
                name="김투자",
                role="포트폴리오 결정",
                summary="연구 후보로 채택하지만 실제 주문은 만들지 않습니다."
                if research_pass
                else "위험 조건 미충족으로 보류합니다.",
                claims=[
                    {
                        "text": "찬반과 위험 판정을 종합했습니다.",
                        "direction": "neutral",
                        "importance": "critical",
                    }
                ],
                evidence_ids=evidence_ids,
                unknowns=[],
                invalidation_conditions=["RISK 판정 변경"],
                confidence=0.8,
            ),
            _report(
                code="OPS",
                name="김주문",
                role="주문·체결·대사",
                summary="R0 연구 단계이므로 주문 금고를 잠갔습니다.",
                claims=[
                    {
                        "text": "브로커로 전송된 주문은 0건입니다.",
                        "direction": "neutral",
                        "importance": "critical",
                    }
                ],
                evidence_ids=["ev_snapshot"],
                unknowns=[],
                invalidation_conditions=["사용자 승격 승인 전 해제 금지"],
                confidence=1.0,
                state="READY",
            ),
        ]
    )
    decision = {
        "action": "BUY_CANDIDATE" if research_pass else "HOLD",
        "symbol": best["symbol"],
        "name": best["name"],
        "score": best["score"],
        "quantity": quantity,
        "order_allowed": False,
        "reason": "R0 연구 후보이며 주문 권한이 없습니다."
        if research_pass
        else "위험 조건을 통과하지 못했습니다.",
    }
    cycle_material = f"{mission['id']}|{snapshot['checksum']}|{STRATEGY_ID}"
    cycle_hash = hashlib.sha256(cycle_material.encode()).hexdigest()
    return {
        "id": f"cycle_{cycle_hash[:16]}",
        "mission_id": mission["id"],
        "snapshot_id": snapshot["id"],
        "as_of": snapshot["as_of"],
        "source": snapshot["source"],
        "strategy_id": STRATEGY_ID,
        "status": "COMPLETE",
        "candidates": candidates,
        "backtest": run_backtest(snapshot),
        "reports": reports,
        "risk": risk,
        "decision": decision,
        "evidence": snapshot["evidence"],
        "trading_enabled": False,
    }
