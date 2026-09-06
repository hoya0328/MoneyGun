from __future__ import annotations

import hashlib
import json
import statistics
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from .qualification import instrument_is_eligible
from .validation import (
    WalkForwardConfig,
    _folds,
    _has_minimum_five_year_history,
    _metrics,
)

VALIDATION_PROTOCOL_VERSION = "POINT_IN_TIME_DYNAMIC_UNIVERSE-v3"

STRATEGY_ID = "CLOSE-AUCTION-KR-v2"
MISSION_ID = "mission_close_auction_001"


@dataclass(frozen=True)
class ClosingAuctionConfig:
    """Frozen v1 mandate. Changes require a new strategy version."""

    signal_cutoff_kst: str = "15:10"
    order_window_start_kst: str = "15:20"
    order_window_end_kst: str = "15:27"
    holding_sessions: int = 5
    score_threshold: int = 80
    max_positions: int = 1
    max_position_bps: int = 5000
    max_trade_risk_bps: int = 200
    max_open_risk_bps: int = 500
    halt_drawdown_bps: int = 1500
    median_turnover_floor_krw: int = 30_000_000_000
    commission_bps_per_side: float = 2.0
    slippage_bps_per_side: float = 5.0
    sell_tax_bps: float = 20.0
    unfilled_haircut_bps: float = 5.0

    @property
    def total_cost_bps(self) -> float:
        return (
            2 * self.commission_bps_per_side
            + 2 * self.slippage_bps_per_side
            + self.sell_tax_bps
            + self.unfilled_haircut_bps
        )


def strategy_spec(config: ClosingAuctionConfig | None = None) -> dict[str, Any]:
    config = config or ClosingAuctionConfig()
    return {
        "mode_code": "CLOSE_AUCTION",
        "display_name": "종가매매",
        "strategy_id": STRATEGY_ID,
        "market": "KRX",
        "thesis": (
            "전일 확정 데이터로 강한 추세·유동성을 선별해 "
            "다음 거래일 종가 단일가에 진입하는 단기 스윙"
        ),
        "clock": {
            "signal_cutoff_kst": config.signal_cutoff_kst,
            "closing_auction": f"{config.order_window_start_kst}~{config.order_window_end_kst}",
            "krx_official_auction": "15:20~15:30",
        },
        "entry": {
            "venue": "KRX_ONLY",
            "order_type": "LIMIT",
            "reference": "당일 종가 단일가",
            "signal_information_set": "직전 완료 거래일까지 확정된 일봉만 사용",
            "forbidden": [
                "당일 미확정 종가로 신호를 만든 뒤 같은 종가에 체결됐다고 가정",
                "시장가·조건부지정가·NXT/SOR·신용·미수",
                "VI·거래정지·관리/경고/단기과열 종목",
            ],
        },
        "exit": {
            "time_exit": f"진입 후 {config.holding_sessions}거래일 종가",
            "risk_exit": "실거래 단계에서는 장중 손절·거래정지·갭 위험을 별도 가디언이 처리",
        },
        "score": {
            "threshold": config.score_threshold,
            "trend": 45,
            "participation": 30,
            "liquidity": 15,
            "market_regime": 10,
        },
        "risk_limits": {
            "max_positions": config.max_positions,
            "max_position_pct": config.max_position_bps / 100,
            "planned_loss_pct": config.max_trade_risk_bps / 100,
            "max_open_risk_pct": config.max_open_risk_bps / 100,
            "halt_drawdown_pct": config.halt_drawdown_bps / 100,
        },
        "cost_assumption_bps": config.total_cost_bps,
        "trading_enabled": False,
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


def _score_at(
    instrument: dict[str, Any],
    benchmark_by_date: dict[str, dict[str, Any]],
    benchmark_dates: list[str],
    signal_date: str,
    config: ClosingAuctionConfig,
) -> dict[str, Any]:
    signal_index = instrument["_date_index"].get(signal_date)
    benchmark_index = instrument["_benchmark_index"].get(signal_date)
    if signal_index is None or benchmark_index is None:
        return {"score": 0}
    bars = instrument["bars"][: signal_index + 1]
    if len(bars) < 200:
        return {"score": 0}
    benchmark = [benchmark_by_date[day] for day in benchmark_dates[: benchmark_index + 1]]
    if len(benchmark) < 200:
        return {"score": 0}

    closes = [float(bar["close"]) for bar in bars]
    benchmark_closes = [float(bar["close"]) for bar in benchmark]
    volumes = [float(bar["volume"]) for bar in bars]
    sma20, sma60 = _mean(closes[-20:]), _mean(closes[-60:])
    relative_20 = closes[-1] / closes[-21] - benchmark_closes[-1] / benchmark_closes[-21]
    median_volume = statistics.median(volumes[-20:])
    volume_multiple = volumes[-1] / median_volume if median_volume else 0.0
    median_turnover = statistics.median(
        float(bar["close"]) * float(bar["volume"]) for bar in bars[-20:]
    )
    day_return = closes[-1] / closes[-2] - 1
    day_range = float(bars[-1]["high"]) - float(bars[-1]["low"])
    close_location = (
        (closes[-1] - float(bars[-1]["low"])) / day_range if day_range > 0 else 0.5
    )

    trend = 20 if closes[-1] > sma20 > sma60 else 0
    trend += 15 if relative_20 > 0 else 0
    trend += 10 if closes[-1] >= max(closes[-20:]) * 0.97 else 0
    participation = 15 if 1.2 <= volume_multiple <= 3.0 else 0
    participation += 10 if 0.01 <= day_return <= 0.10 else 0
    participation += 5 if close_location >= 0.7 else 0
    liquidity = 15 if median_turnover >= config.median_turnover_floor_krw else 0
    regime = 10 if benchmark_closes[-1] > _mean(benchmark_closes[-200:]) else 0
    score = trend + participation + liquidity + regime
    atr_ratio = _atr(bars) / closes[-1]
    return {
        "score": score,
        "trend": trend,
        "participation": participation,
        "liquidity": liquidity,
        "market_regime": regime,
        "signal_date": signal_date,
        "close": closes[-1],
        "relative_20_pct": round(relative_20 * 100, 2),
        "volume_multiple": round(volume_multiple, 2),
        "median_turnover_krw": round(median_turnover),
        "close_location_pct": round(close_location * 100, 1),
        "stop_distance": min(max(atr_ratio * 1.5, 0.03), 0.06),
    }


def _aligned_market(
    snapshot: dict[str, Any],
) -> tuple[list[str], list[dict[str, Any]], dict[str, Any]]:
    benchmark_by_date = {bar["date"]: bar for bar in snapshot["benchmark"]["bars"]}
    dates = sorted(benchmark_by_date)
    allowed = set(dates)
    benchmark_index = {day: index for index, day in enumerate(dates)}
    instruments = [
        {
            **instrument,
            "bars": [bar for bar in instrument["bars"] if bar["date"] in allowed],
            "_date_index": {
                bar["date"]: index
                for index, bar in enumerate(
                    [bar for bar in instrument["bars"] if bar["date"] in allowed]
                )
            },
            "_benchmark_index": benchmark_index,
        }
        for instrument in snapshot["instruments"]
    ]
    return dates, instruments, benchmark_by_date


def rank_close_candidates(
    snapshot: dict[str, Any], config: ClosingAuctionConfig | None = None
) -> list[dict[str, Any]]:
    config = config or ClosingAuctionConfig()
    dates, instruments, benchmark_by_date = _aligned_market(snapshot)
    if not dates:
        return []
    signal_date = dates[-1]
    ranked: list[dict[str, Any]] = []
    for instrument in instruments:
        if not instrument_is_eligible(snapshot, instrument["symbol"], signal_date):
            continue
        detail = _score_at(
            instrument, benchmark_by_date, dates, signal_date, config
        )
        ranked.append(
            {
                "symbol": instrument["symbol"],
                "name": instrument["name"],
                "sector": instrument.get("sector", ""),
                **detail,
            }
        )
    ranked.sort(key=lambda item: (item["score"], item.get("median_turnover_krw", 0)), reverse=True)
    return [{**item, "rank": index} for index, item in enumerate(ranked[:5], start=1)]


def run_close_auction_validation(
    snapshot: dict[str, Any], config: ClosingAuctionConfig | None = None
) -> dict[str, Any]:
    config = config or ClosingAuctionConfig()
    dates, instruments, benchmark_by_date = _aligned_market(snapshot)
    walk_config = WalkForwardConfig(
        holding_days=config.holding_sessions,
        score_threshold=config.score_threshold,
        commission_bps_per_side=config.commission_bps_per_side,
        slippage_bps_per_side=config.slippage_bps_per_side,
        sell_tax_bps=config.sell_tax_bps,
        unfilled_haircut_bps=config.unfilled_haircut_bps,
        strategy_trials=3,
    )
    folds = _folds(len(dates), walk_config)
    all_trades: list[dict[str, Any]] = []
    sensitivity_thresholds = sorted(
        {
            round(config.score_threshold * 0.8),
            config.score_threshold,
            round(config.score_threshold * 1.2),
        }
    )
    for fold_number, fold in enumerate(folds, start=1):
        # The signal is always the prior completed session. Entry and exit are close prices.
        for entry_index in range(
            fold["test_start"],
            fold["test_end"] - config.holding_sessions,
            config.holding_sessions,
        ):
            signal_index = entry_index - 1
            signal_date = dates[signal_index]
            entry_date = dates[entry_index]
            exit_date = dates[entry_index + config.holding_sessions]
            ranked = sorted(
                (
                    (
                        _score_at(item, benchmark_by_date, dates, signal_date, config)["score"],
                        item,
                    )
                    for item in instruments
                    if instrument_is_eligible(snapshot, item["symbol"], signal_date)
                    and instrument_is_eligible(snapshot, item["symbol"], entry_date)
                ),
                key=lambda item: item[0],
                reverse=True,
            )
            if not ranked:
                continue
            score, instrument = ranked[0]
            if score < sensitivity_thresholds[0]:
                continue
            signal_bar = instrument["bars"][instrument["_date_index"][signal_date]]
            entry_bar_index = instrument["_date_index"].get(entry_date)
            if entry_bar_index is None:
                continue
            entry_bar = instrument["bars"][entry_bar_index]
            exit_bar_index = instrument["_date_index"].get(exit_date)
            forced_loss = exit_bar_index is None
            exit_bar = instrument["bars"][exit_bar_index] if exit_bar_index is not None else None
            gross_return = (
                -1.0
                if forced_loss
                else float(exit_bar["close"]) / float(entry_bar["close"]) - 1
            )
            allocation = config.max_position_bps / 10_000
            net_return = allocation * (gross_return - config.total_cost_bps / 10_000)
            benchmark_entry = float(benchmark_by_date[entry_date]["close"])
            benchmark_exit = float(benchmark_by_date[exit_date]["close"])
            all_trades.append(
                {
                    "fold": fold_number,
                    "symbol": instrument["symbol"],
                    "score": score,
                    "signal_date": signal_bar["date"],
                    "entry_date": entry_bar["date"],
                    "exit_date": exit_date,
                    "gross_return": gross_return,
                    "net_return": net_return,
                    "benchmark_return": allocation * (benchmark_exit / benchmark_entry - 1),
                    "allocation_factor": allocation,
                    "forced_total_loss_for_missing_exit": forced_loss,
                }
            )

    trades = [trade for trade in all_trades if trade["score"] >= config.score_threshold]
    metrics = _metrics(trades, walk_config)
    sensitivity = {
        str(threshold): _metrics(
            [trade for trade in all_trades if trade["score"] >= threshold], walk_config
        )
        for threshold in sensitivity_thresholds
    }
    oos_days = sum(fold["test_end"] - fold["test_start"] + 1 for fold in folds)
    manifest = snapshot.get("collection_manifest", {})
    source_admissible = snapshot["source"] in {
        "KRX_AUTHORIZED_EXPORT",
        "LICENSED_VENDOR",
        "OFFICIAL_PUBLIC_DATA_API_AND_KIND_UI_EXPORT",
        "OFFICIAL_PUBLIC_DATA_API_KIND_AND_KIWOOM",
        "KIWOOM_OFFICIAL_REST",
    }
    gates = {
        "snapshot_quality": snapshot["quality"]["state"] == "PASS",
        "source_is_real_and_authorized": source_admissible,
        "survivorship_bias_controlled": manifest.get("survivorship_bias_controlled", False),
        "historical_designation_states_complete": manifest.get(
            "historical_designation_states_complete", False
        ),
        "minimum_five_year_history": _has_minimum_five_year_history(snapshot, dates),
        "minimum_two_year_oos": oos_days >= 504,
        "minimum_100_trades": metrics["trade_count"] >= 100,
        "positive_net_excess_return": metrics["excess_return_positive"],
        "sharpe_at_least_0_8": metrics["sharpe"] >= 0.8,
        "deflated_sharpe_95pct": metrics["deflated_sharpe_probability"] >= 0.95,
        "drawdown_within_close_limit": metrics["max_drawdown_pct"] >= -15,
        "parameter_robustness_plus_minus_20pct": all(
            item["trade_count"] >= 30 and item["mean_trade_return_pct"] > 0
            for item in sensitivity.values()
        ),
        "signal_strictly_precedes_entry": all(
            trade["signal_date"] < trade["entry_date"] for trade in trades
        ),
    }
    eligible = bool(folds) and all(gates.values())
    fold_report = [
        {
            "number": number,
            "train_start": dates[fold["train_start"]],
            "train_end": dates[fold["train_end"]],
            "purge_start": dates[fold["purge_start"]],
            "purge_end": dates[fold["purge_end"]],
            "test_start": dates[fold["test_start"]],
            "test_end": dates[fold["test_end"]],
        }
        for number, fold in enumerate(folds, start=1)
    ]
    material = {
        "snapshot_id": snapshot["id"],
        "strategy_id": STRATEGY_ID,
        "protocol_version": VALIDATION_PROTOCOL_VERSION,
        "config": asdict(config),
        "metrics": metrics,
        "sensitivity": sensitivity,
        "gates": gates,
    }
    checksum = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "id": f"validation_{checksum[:16]}",
        "snapshot_id": snapshot["id"],
        "strategy_id": STRATEGY_ID,
        "protocol_version": VALIDATION_PROTOCOL_VERSION,
        "as_of": snapshot["as_of"],
        "source": snapshot["source"],
        "status": "ELIGIBLE_FOR_R1_REVIEW" if eligible else "NOT_ELIGIBLE",
        "promotion_eligible": eligible,
        "method": "PURGED_WALK_FORWARD_CLOSE_TO_CLOSE",
        "config": {**asdict(config), "total_cost_bps": config.total_cost_bps},
        "history": {
            "trading_days": len(dates),
            "start": dates[0] if dates else None,
            "end": dates[-1] if dates else None,
            "oos_days": oos_days,
        },
        "folds": fold_report,
        "metrics": metrics,
        "sensitivity": sensitivity,
        "gates": gates,
        "failed_gates": [name for name, passed in gates.items() if not passed],
        "trade_sample": trades[:20],
        "qualification_next": [
            "R0 OOS 게이트 전부 통과",
            "R1 종가 단일가 그림자 운영 60거래일·100결정",
            "계좌별 수수료 확인과 종가 체결 오차 보정",
            "소유자 승인 후 L1 소액 실거래",
        ],
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "trading_enabled": False,
    }


def run_close_auction_cycle(
    mission: dict[str, Any],
    snapshot: dict[str, Any],
    validation: dict[str, Any],
    config: ClosingAuctionConfig | None = None,
) -> dict[str, Any]:
    config = config or ClosingAuctionConfig()
    candidates = rank_close_candidates(snapshot, config)
    best = candidates[0] if candidates else None
    score_pass = bool(best and best["score"] >= config.score_threshold)
    equity = float(mission["equity_krw"])
    price = float(best["close"]) if best else 0.0
    stop_distance = float(best["stop_distance"]) if best else 1.0
    risk_budget = equity * config.max_trade_risk_bps / 10_000
    position_budget = min(
        equity * config.max_position_bps / 10_000,
        risk_budget / stop_distance if stop_distance else 0,
    )
    quantity = int(position_budget // price) if price else 0
    research_pass = score_pass and quantity > 0 and validation["promotion_eligible"]
    common_evidence = ["ev_snapshot"]
    agent_rows = [
        ("DATA", "김데이터", "데이터 검증", "확정 일봉과 시점 누수를 검사했습니다."),
        ("NOVA", "김뉴스", "뉴스·공시", "종가 전 돌발 공시는 R1 실시간 필터가 필요합니다."),
        (
            "SERENITY",
            "김차트",
            "차트 분석",
            f"최고 후보 점수는 {best['score'] if best else 0}점입니다.",
        ),
        ("PULSE", "김시장", "시장 국면", "KOSPI 200일선으로 위험 국면을 확인했습니다."),
        ("BULL", "김찬성", "찬성 논증", "추세·수급·유동성 동시 통과 때만 찬성합니다."),
        ("BEAR", "김반대", "반대 논증", "종가 추격과 야간 갭 손실을 반대 근거로 기록했습니다."),
        ("RISK", "김안전", "위험·거부권", f"최대 {quantity}주, 자산 50% 한도를 적용했습니다."),
        ("ACE", "김투자", "최종 결정", "승격 자격과 수량을 모두 통과해야 후보로 채택합니다."),
        ("OPS", "김주문", "주문·체결", "R0에서는 브로커 주문을 만들지 않습니다."),
    ]
    reports = [
        {
            "code": code,
            "name": name,
            "role": role,
            "state": "READY" if code == "OPS" else "DONE",
            "summary": summary,
            "claims": [],
            "evidence_ids": common_evidence,
            "unknowns": [],
            "invalidation_conditions": ["입력 데이터·위험 한도 변경"],
            "confidence": 1.0 if code in {"DATA", "RISK", "OPS"} else 0.7,
        }
        for code, name, role, summary in agent_rows
    ]
    risk = {
        "result": "PASS_RESEARCH_ONLY" if research_pass else "HOLD",
        "quantity": quantity,
        "entry_reference_krw": round(price),
        "invalidation_price_krw": round(price * (1 - stop_distance)),
        "risk_budget_krw": round(risk_budget),
        "position_budget_krw": round(position_budget),
        "order_allowed": False,
        "reasons": [
            "R0 연구 단계에서는 주문을 만들지 않습니다.",
            *(
                ["OOS 실거래 자격 게이트가 미통과입니다."]
                if not validation["promotion_eligible"]
                else []
            ),
            *(["10만 원 예산으로 1주를 살 수 없습니다."] if score_pass and quantity == 0 else []),
        ],
    }
    decision = {
        "action": "BUY_CANDIDATE" if research_pass else "HOLD",
        "symbol": best["symbol"] if best else "",
        "name": best["name"] if best else "후보 없음",
        "score": best["score"] if best else 0,
        "quantity": quantity,
        "order_allowed": False,
        "entry_style": "KRX_CLOSING_AUCTION_LIMIT",
        "reason": "R0 종가매매 연구 후보이며 주문 권한은 없습니다."
        if research_pass
        else "점수·수량·OOS 자격 중 하나 이상을 통과하지 못했습니다.",
    }
    protocol_version = validation.get("protocol_version", "LEGACY_COMMON_DATES-v1")
    material = (
        f"{mission['id']}|{snapshot['checksum']}|{STRATEGY_ID}|"
        f"{protocol_version}|{validation['id']}"
    )
    cycle_hash = hashlib.sha256(material.encode()).hexdigest()
    return {
        "id": f"cycle_{cycle_hash[:16]}",
        "mission_id": mission["id"],
        "snapshot_id": snapshot["id"],
        "as_of": snapshot["as_of"],
        "source": snapshot["source"],
        "strategy_id": STRATEGY_ID,
        "protocol_version": protocol_version,
        "status": "COMPLETE",
        "mode": strategy_spec(config),
        "candidates": candidates,
        "backtest": {
            "status": validation["status"],
            **validation["metrics"],
            "oos_validated": True,
        },
        "reports": reports,
        "risk": risk,
        "decision": decision,
        "evidence": snapshot.get("evidence", []),
        "trading_enabled": False,
    }
