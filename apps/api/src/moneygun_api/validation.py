from __future__ import annotations

import hashlib
import json
import math
import statistics
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any

from .qualification import instrument_is_eligible
from .research import STRATEGY_ID

VALIDATION_PROTOCOL_VERSION = "POINT_IN_TIME_DYNAMIC_UNIVERSE-v3"


@dataclass(frozen=True)
class WalkForwardConfig:
    train_days: int = 504
    test_days: int = 126
    purge_days: int = 20
    embargo_days: int = 5
    holding_days: int = 10
    score_threshold: int = 70
    commission_bps_per_side: float = 2.0
    slippage_bps_per_side: float = 10.0
    sell_tax_bps: float = 20.0
    unfilled_haircut_bps: float = 5.0
    strategy_trials: int = 1

    @property
    def total_cost_bps(self) -> float:
        return (
            2 * self.commission_bps_per_side
            + 2 * self.slippage_bps_per_side
            + self.sell_tax_bps
            + self.unfilled_haircut_bps
        )


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


def _point_in_time_fundamentals(instrument: dict[str, Any], signal_date: str) -> dict[str, bool]:
    eligible = [
        record
        for record in instrument.get("fundamentals_history", [])
        if record["effective_at"][:10] <= signal_date
    ]
    if not eligible:
        return {}
    return max(eligible, key=lambda item: item["effective_at"])["values"]


def _catalyst_count(snapshot: dict[str, Any], instrument: dict[str, Any], signal_date: str) -> int:
    signal = date.fromisoformat(signal_date)
    evidence = {item["id"]: item for item in snapshot.get("evidence", [])}
    count = 0
    for evidence_id in instrument.get("catalyst_evidence_ids", []):
        item = evidence.get(evidence_id)
        if not item:
            continue
        published = date.fromisoformat(item["published_at"][:10])
        age = (signal - published).days
        if item.get("verified") and 0 <= age <= 35:
            count += 1
    return count


def _score_at(
    snapshot: dict[str, Any],
    instrument: dict[str, Any],
    benchmark_by_date: dict[str, dict[str, Any]],
    benchmark_dates: list[str],
    signal_date: str,
) -> int:
    signal_index = instrument["_date_index"].get(signal_date)
    benchmark_index = instrument["_benchmark_index"].get(signal_date)
    if signal_index is None or benchmark_index is None:
        return 0
    bars = instrument["bars"][: signal_index + 1]
    if len(bars) < 200:
        return 0
    benchmark = [benchmark_by_date[day] for day in benchmark_dates[: benchmark_index + 1]]
    if len(benchmark) < 200:
        return 0
    closes = [bar["close"] for bar in bars]
    volumes = [bar["volume"] for bar in bars]
    benchmark_closes = [bar["close"] for bar in benchmark]
    sma20, sma60, sma120 = (_mean(closes[-20:]), _mean(closes[-60:]), _mean(closes[-120:]))
    recent_return = closes[-1] / closes[-64] - 1
    benchmark_return = benchmark_closes[-1] / benchmark_closes[-64] - 1
    median_volume = statistics.median(volumes[-20:])
    volume_multiple = volumes[-1] / median_volume
    median_value = statistics.median(bar["close"] * bar["volume"] for bar in bars[-20:])
    atr_ratio = _atr(bars) / closes[-1]

    momentum = 15 if closes[-1] > sma20 > sma60 > sma120 else 0
    momentum += 10 if recent_return > benchmark_return else 0
    momentum += 5 if closes[-1] >= max(closes[-20:]) * 0.97 else 0
    momentum += 5 if 1.2 <= volume_multiple <= 3.0 else 0
    momentum += 5 if _mean(closes[-5:]) > _mean(closes[-10:-5]) else 0
    fundamentals = _point_in_time_fundamentals(instrument, signal_date)
    quality = sum(5 for value in fundamentals.values() if value)
    liquidity = 5 if median_value >= 30_000_000_000 else 0
    liquidity += 5 if 0.015 <= atr_ratio <= 0.06 else 0
    liquidity += 5 if snapshot["quality"]["state"] == "PASS" else 0
    regime = 10 if benchmark_closes[-1] > _mean(benchmark_closes[-200:]) else 0
    catalyst_count = _catalyst_count(snapshot, instrument, signal_date)
    catalyst = 10 if catalyst_count >= 2 else 5 if catalyst_count == 1 else 0
    return momentum + quality + liquidity + regime + catalyst


def _folds(day_count: int, config: WalkForwardConfig) -> list[dict[str, int]]:
    folds: list[dict[str, int]] = []
    test_start = config.train_days + config.purge_days
    while test_start + config.test_days <= day_count:
        folds.append(
            {
                "train_start": max(0, test_start - config.purge_days - config.train_days),
                "train_end": test_start - config.purge_days - 1,
                "purge_start": test_start - config.purge_days,
                "purge_end": test_start - 1,
                "test_start": test_start,
                "test_end": test_start + config.test_days - 1,
            }
        )
        test_start += config.test_days + config.embargo_days
    return folds


def _has_minimum_five_year_history(
    snapshot: dict[str, Any], dates: list[str]
) -> bool:
    """Use validated calendar coverage, allowing non-trading boundary days."""
    manifest = snapshot.get("collection_manifest", {})
    period_start = manifest.get("period_start")
    period_end = manifest.get("period_end")
    if period_start and period_end and dates:
        start = date.fromisoformat(period_start)
        end = date.fromisoformat(period_end)
        first_session = date.fromisoformat(dates[0])
        last_session = date.fromisoformat(dates[-1])
        return (
            (end - start).days >= 365 * 5
            and 0 <= (first_session - start).days <= 7
            and 0 <= (end - last_session).days <= 7
        )
    return len(dates) >= 1260


def _moments(values: list[float]) -> tuple[float, float]:
    if len(values) < 3:
        return 0.0, 3.0
    mean = _mean(values)
    variance = _mean([(value - mean) ** 2 for value in values])
    if variance == 0:
        return 0.0, 3.0
    deviation = math.sqrt(variance)
    skew = _mean([((value - mean) / deviation) ** 3 for value in values])
    kurtosis = _mean([((value - mean) / deviation) ** 4 for value in values])
    return skew, kurtosis


def _deflated_sharpe_probability(returns: list[float], config: WalkForwardConfig) -> float:
    if len(returns) < 3:
        return 0.0
    mean = _mean(returns)
    deviation = statistics.stdev(returns)
    if deviation == 0:
        return 1.0 if mean > 0 else 0.0
    observed = mean / deviation
    skew, kurtosis = _moments(returns)
    variance = max(
        (1 - skew * observed + ((kurtosis - 1) / 4) * observed**2) / (len(returns) - 1),
        1e-12,
    )
    expected_max = 0.0
    if config.strategy_trials > 1:
        normal = statistics.NormalDist()
        trials = config.strategy_trials
        gamma = 0.5772156649
        expected_max = math.sqrt(variance) * (
            (1 - gamma) * normal.inv_cdf(1 - 1 / trials)
            + gamma * normal.inv_cdf(1 - 1 / (trials * math.e))
        )
    z_score = (observed - expected_max) / math.sqrt(variance)
    return statistics.NormalDist().cdf(z_score)


def _metrics(trades: list[dict[str, Any]], config: WalkForwardConfig) -> dict[str, Any]:
    returns = [trade["net_return"] for trade in trades]
    excess = [trade["net_return"] - trade["benchmark_return"] for trade in trades]
    equity = peak = 1.0
    max_drawdown = 0.0
    for value in returns:
        equity *= 1 + value
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity / peak - 1)
    annualizer = math.sqrt(252 / config.holding_days)
    deviation = statistics.stdev(returns) if len(returns) > 1 else 0.0
    excess_deviation = statistics.stdev(excess) if len(excess) > 1 else 0.0
    return {
        "trade_count": len(trades),
        "net_return_pct": round((equity - 1) * 100, 2),
        "mean_trade_return_pct": round((_mean(returns) if returns else 0) * 100, 3),
        "win_rate_pct": round(sum(value > 0 for value in returns) / len(returns) * 100, 1)
        if returns
        else 0.0,
        "max_drawdown_pct": round(max_drawdown * 100, 2),
        "sharpe": round((_mean(returns) / deviation * annualizer) if deviation else 0, 3),
        "information_ratio": round(
            (_mean(excess) / excess_deviation * annualizer) if excess_deviation else 0, 3
        ),
        "excess_return_positive": bool(excess and _mean(excess) > 0),
        "deflated_sharpe_probability": round(_deflated_sharpe_probability(returns, config), 4),
    }


def run_walk_forward_validation(
    snapshot: dict[str, Any], config: WalkForwardConfig | None = None
) -> dict[str, Any]:
    config = config or WalkForwardConfig()
    benchmark_by_date = {bar["date"]: bar for bar in snapshot["benchmark"]["bars"]}
    dates = sorted(benchmark_by_date)
    benchmark_index = {day: index for index, day in enumerate(dates)}
    instruments = []
    for instrument in snapshot["instruments"]:
        allowed = set(dates)
        bars = [bar for bar in instrument["bars"] if bar["date"] in allowed]
        instruments.append(
            {
                **instrument,
                "bars": bars,
                "_date_index": {bar["date"]: index for index, bar in enumerate(bars)},
                "_benchmark_index": benchmark_index,
            }
        )
    folds = _folds(len(dates), config)
    trades: list[dict[str, Any]] = []
    for fold_number, fold in enumerate(folds, start=1):
        for signal_index in range(
            fold["test_start"], fold["test_end"] - config.holding_days, config.holding_days
        ):
            signal_date = dates[signal_index]
            entry_date = dates[signal_index + 1]
            exit_date = dates[signal_index + config.holding_days]
            ranked = sorted(
                (
                    (
                        _score_at(
                            snapshot,
                            instrument,
                            benchmark_by_date,
                            dates,
                            signal_date,
                        ),
                        instrument,
                    )
                    for instrument in instruments
                    if instrument_is_eligible(snapshot, instrument["symbol"], signal_date)
                    and instrument_is_eligible(snapshot, instrument["symbol"], entry_date)
                ),
                key=lambda item: item[0],
                reverse=True,
            )
            if not ranked:
                continue
            score, instrument = ranked[0]
            if score < config.score_threshold:
                continue
            signal_bar_index = instrument["_date_index"].get(signal_date)
            entry_bar_index = instrument["_date_index"].get(entry_date)
            if signal_bar_index is None or entry_bar_index is None:
                continue
            signal_bar = instrument["bars"][signal_bar_index]
            entry_bar = instrument["bars"][entry_bar_index]
            exit_bar_index = instrument["_date_index"].get(exit_date)
            exit_bar = instrument["bars"][exit_bar_index] if exit_bar_index is not None else None
            gap = entry_bar["open"] / signal_bar["close"] - 1
            if gap > 0.03:
                continue
            allocation_factor = 0.5 if gap > 0.01 else 1.0
            forced_loss = exit_bar is None
            gross_return = (
                -1.0 if forced_loss else exit_bar["close"] / entry_bar["open"] - 1
            )
            net_return = allocation_factor * (gross_return - config.total_cost_bps / 10_000)
            benchmark_entry = benchmark_by_date[entry_date]["open"]
            benchmark_exit = benchmark_by_date[exit_date]["close"]
            trades.append(
                {
                    "fold": fold_number,
                    "symbol": instrument["symbol"],
                    "score": score,
                    "signal_date": signal_bar["date"],
                    "entry_date": entry_bar["date"],
                    "exit_date": exit_date,
                    "gross_return": gross_return,
                    "net_return": net_return,
                    "benchmark_return": allocation_factor * (benchmark_exit / benchmark_entry - 1),
                    "allocation_factor": allocation_factor,
                    "forced_total_loss_for_missing_exit": forced_loss,
                }
            )

    metrics = _metrics(trades, config)
    oos_days = sum(fold["test_end"] - fold["test_start"] + 1 for fold in folds)
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
        "fundamental_revision_safe": snapshot.get("collection_manifest", {}).get(
            "fundamental_revision_safe", False
        ),
        "survivorship_bias_controlled": snapshot.get("collection_manifest", {}).get(
            "survivorship_bias_controlled", False
        ),
        "minimum_five_year_history": _has_minimum_five_year_history(snapshot, dates),
        "minimum_two_year_oos": oos_days >= 504,
        "minimum_100_trades": metrics["trade_count"] >= 100,
        "positive_net_excess_return": metrics["excess_return_positive"],
        "sharpe_at_least_0_8": metrics["sharpe"] >= 0.8,
        "deflated_sharpe_95pct": metrics["deflated_sharpe_probability"] >= 0.95,
        "drawdown_within_focus_limit": metrics["max_drawdown_pct"] >= -30,
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
        "method": "PURGED_WALK_FORWARD",
        "config": {**asdict(config), "total_cost_bps": config.total_cost_bps},
        "history": {
            "trading_days": len(dates),
            "start": dates[0] if dates else None,
            "end": dates[-1] if dates else None,
            "oos_days": oos_days,
        },
        "folds": fold_report,
        "metrics": metrics,
        "gates": gates,
        "failed_gates": [name for name, passed in gates.items() if not passed],
        "trade_sample": trades[:20],
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "trading_enabled": False,
    }
