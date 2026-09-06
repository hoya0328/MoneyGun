from __future__ import annotations

import hashlib
import statistics
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from .research import build_fixture_snapshot, run_committee_cycle
from .storage import Database

REPLAY_SOURCE = "FIXTURE_KR_REPRODUCIBLE"
EVALUATION_HORIZON_DAYS = 10
ROUND_TRIP_COST_BPS = 49


def trading_days(end: date, count: int) -> list[date]:
    result: list[date] = []
    current = end
    while len(result) < count:
        if current.weekday() < 5:
            result.append(current)
        current -= timedelta(days=1)
    return list(reversed(result))


def _program_definition(mission_id: str, end: date, target_days: int) -> dict[str, Any]:
    dates = trading_days(end, target_days)
    material = f"{mission_id}|REPLAY|{REPLAY_SOURCE}|{dates[0]}|{dates[-1]}|{target_days}"
    digest = hashlib.sha256(material.encode()).hexdigest()[:16]
    return {
        "id": f"program_{digest}",
        "mission_id": mission_id,
        "mode": "REPLAY",
        "source": REPLAY_SOURCE,
        "start_date": dates[0].isoformat(),
        "end_date": dates[-1].isoformat(),
        "target_days": target_days,
    }


def _evidence_quality(report: dict[str, Any], cycle: dict[str, Any]) -> float:
    evidence = {item["id"]: item for item in cycle["evidence"]}
    referenced = report["evidence_ids"]
    verified_ratio = (
        sum(bool(evidence.get(item, {}).get("verified")) for item in referenced) / len(referenced)
        if referenced
        else 0
    )
    unknown_penalty = min(len(report["unknowns"]) * 0.05, 0.2)
    return round(max(0.0, verified_ratio - unknown_penalty) * 100, 1)


def _evaluate_day(
    program_id: str,
    trade_date: str,
    cycle: dict[str, Any],
    final_snapshot: dict[str, Any],
) -> list[dict[str, Any]]:
    symbol = cycle["decision"]["symbol"]
    instrument = next(item for item in final_snapshot["instruments"] if item["symbol"] == symbol)
    bars = instrument["bars"]
    index_by_date = {bar["date"]: index for index, bar in enumerate(bars)}
    signal_index = index_by_date[trade_date]
    entry = bars[signal_index + 1]
    exit_bar = bars[signal_index + EVALUATION_HORIZON_DAYS]
    net_return = exit_bar["close"] / entry["open"] - 1 - ROUND_TRIP_COST_BPS / 10_000
    actual_positive = net_return > 0
    stop_breached = (
        min(
            bar["low"]
            for bar in bars[signal_index + 1 : signal_index + EVALUATION_HORIZON_DAYS + 1]
        )
        <= cycle["risk"]["invalidation_price_krw"]
    )

    evaluations: list[dict[str, Any]] = []
    for report in cycle["reports"]:
        code = report["code"]
        confidence = float(report["confidence"])
        if code in {"DATA", "OPS"}:
            predicted_probability = confidence
            actual = True
            outcome = "PROCESS_INTEGRITY"
        elif code == "RISK":
            predicted_probability = confidence
            actual = not stop_breached
            outcome = "STOP_NOT_BREACHED"
        elif code == "BEAR":
            predicted_probability = 1 - confidence
            actual = actual_positive
            outcome = "FORWARD_RETURN_POSITIVE"
        else:
            predicted_probability = confidence
            actual = actual_positive
            outcome = "FORWARD_RETURN_POSITIVE"
        accurate = (predicted_probability >= 0.5) == actual
        brier = (predicted_probability - float(actual)) ** 2
        material = f"{program_id}|{trade_date}|{code}"
        evaluations.append(
            {
                "id": f"agent_eval_{hashlib.sha256(material.encode()).hexdigest()[:20]}",
                "program_id": program_id,
                "trade_date": trade_date,
                "agent_code": code,
                "agent_name": report["name"],
                "outcome_type": outcome,
                "outcome_return_pct": round(net_return * 100, 3),
                "predicted_probability": round(predicted_probability, 3),
                "actual_positive": actual,
                "accurate": accurate,
                "brier_score": round(brier, 4),
                "evidence_quality": _evidence_quality(report, cycle),
                "overconfident": confidence >= 0.75 and not accurate,
                "confidence": confidence,
                "source_mode": "REPLAY",
            }
        )
    return evaluations


def _agent_scorecards(
    evaluations: list[dict[str, Any]], target_days: int, evaluated_days: int
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for evaluation in evaluations:
        grouped[evaluation["agent_code"]].append(evaluation)
    scorecards: list[dict[str, Any]] = []
    for code, items in grouped.items():
        accuracy = sum(item["accurate"] for item in items) / len(items) * 100
        mean_brier = statistics.mean(item["brier_score"] for item in items)
        evidence_quality = statistics.mean(item["evidence_quality"] for item in items)
        calibration = max(0.0, (1 - mean_brier) * 100)
        overconfidence_count = sum(item["overconfident"] for item in items)
        overall = max(
            0.0,
            0.45 * accuracy
            + 0.35 * evidence_quality
            + 0.20 * calibration
            - overconfidence_count / len(items) * 10,
        )
        scorecards.append(
            {
                "code": code,
                "name": items[0]["agent_name"],
                "scored_days": len(items),
                "pending_days": target_days - evaluated_days,
                "accuracy_pct": round(accuracy, 1),
                "evidence_quality_pct": round(evidence_quality, 1),
                "calibration_pct": round(calibration, 1),
                "overconfidence_count": overconfidence_count,
                "overall_score": round(overall, 1),
                "source_mode": "REPLAY",
            }
        )
    return sorted(scorecards, key=lambda item: item["overall_score"], reverse=True)


def _summary(database: Database, program: dict[str, Any]) -> dict[str, Any]:
    days = database.list_committee_days(program["id"])
    evaluations = database.list_agent_evaluations(program["id"])
    completed = sum(day["state"] == "COMPLETE" for day in days)
    failed = sum(day["state"] == "FAILED" for day in days)
    evaluated_dates = {item["trade_date"] for item in evaluations}
    evaluated_days = len(evaluated_dates)
    return {
        "mode": program["mode"],
        "source": program["source"],
        "target_days": program["target_days"],
        "completed_days": completed,
        "failed_days": failed,
        "missing_days": program["target_days"] - completed - failed,
        "evaluated_days": evaluated_days,
        "pending_outcome_days": completed - evaluated_days,
        "package_continuity_pct": round(completed / program["target_days"] * 100, 1),
        "agent_scorecards": _agent_scorecards(evaluations, program["target_days"], evaluated_days),
        "readiness": {
            "replay_complete": completed == program["target_days"] and failed == 0,
            "real_operating_days": 0,
            "qualifies_20_day_gate": False,
            "reason": "REPLAY는 배관 검증이며 실제 20거래일 운영 증거가 아닙니다.",
        },
        "trading_enabled": False,
    }


def run_committee_replay(
    database: Database,
    *,
    mission_id: str,
    end_date: date,
    target_days: int = 20,
) -> dict[str, Any]:
    mission = database.get_mission(mission_id)
    definition = _program_definition(mission_id, end_date, target_days)
    program = database.create_committee_program(definition)
    if program["state"] == "COMPLETE" and program["summary"].get("completed_days") == target_days:
        return get_committee_program_report(database, program["id"])
    dates = trading_days(end_date, target_days)
    for trade_date in dates:
        iso_date = trade_date.isoformat()
        if not database.claim_committee_day(program["id"], iso_date):
            continue
        try:
            snapshot = database.save_snapshot(build_fixture_snapshot(trade_date, history_days=220))
            cycle = database.save_cycle(run_committee_cycle(mission, snapshot))
            database.complete_committee_day(program["id"], iso_date, snapshot["id"], cycle["id"])
        except Exception as error:  # day state must survive an individual worker failure
            database.fail_committee_day(program["id"], iso_date, str(error))

    completed_days = [
        day for day in database.list_committee_days(program["id"]) if day["state"] == "COMPLETE"
    ]
    if len(completed_days) > EVALUATION_HORIZON_DAYS:
        final_snapshot = database.get_snapshot(completed_days[-1]["snapshot_id"])
        mature_days = completed_days[:-EVALUATION_HORIZON_DAYS]
        for day in mature_days:
            cycle = database.get_cycle(day["cycle_id"])
            for evaluation in _evaluate_day(
                program["id"], day["trade_date"], cycle, final_snapshot
            ):
                database.save_agent_evaluation(evaluation)

    program = database.get_committee_program(program["id"])
    summary = _summary(database, program)
    state = "COMPLETE" if summary["readiness"]["replay_complete"] else "PARTIAL"
    program = database.finish_committee_program(program["id"], state=state, summary=summary)
    return {**program, "days": database.list_committee_days(program["id"])}


def get_committee_program_report(database: Database, program_id: str) -> dict[str, Any]:
    program = database.get_committee_program(program_id)
    summary = _summary(database, program)
    if summary != program["summary"]:
        program = database.finish_committee_program(
            program_id, state=program["state"], summary=summary
        )
    return {**program, "days": database.list_committee_days(program_id)}
