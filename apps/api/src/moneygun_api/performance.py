from __future__ import annotations

import hashlib
import json
import math
import os
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

from .desktop_live import deployment_is_eligible, desktop_live_readiness
from .storage import Database

STRATEGY_HOLDING_SESSIONS = {
    "FOCUS-MOMENTUM-KR-v1": 10,
    "CLOSE-AUCTION-KR-v2": 5,
}
VALID_EXIT_REASONS = {"HOLDING_COMPLETE", "INVALIDATION", "KILL_SWITCH", "MANUAL_RISK_EXIT"}
MISSION_STRATEGIES = {
    "mission_focus_001": "FOCUS-MOMENTUM-KR-v1",
    "mission_close_auction_001": "CLOSE-AUCTION-KR-v2",
}


class PerformanceError(RuntimeError):
    pass


def _business_sessions_after(start: date, end: date) -> int:
    sessions = 0
    current = start + timedelta(days=1)
    while current <= end:
        if current.weekday() < 5:
            sessions += 1
        current += timedelta(days=1)
    return sessions


def shadow_exit_schedule(order: dict[str, Any], strategy_id: str, as_of: date) -> dict[str, Any]:
    entry_date = date.fromisoformat(str(order["next_session_date"])[:10])
    holding_sessions = _business_sessions_after(entry_date, as_of)
    required_sessions = STRATEGY_HOLDING_SESSIONS.get(strategy_id, 10)
    return {
        "holding_sessions": holding_sessions,
        "required_holding_sessions": required_sessions,
        "holding_complete": holding_sessions >= required_sessions,
    }


def _regime(snapshot: dict[str, Any]) -> str:
    bars = snapshot.get("benchmark", {}).get("bars", [])
    if len(bars) < 200:
        return "UNKNOWN"
    closes = [float(item["close"]) for item in bars[-200:]]
    return "RISK_ON" if closes[-1] > sum(closes) / len(closes) else "RISK_OFF"


def _participants(cycle: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "code": report["code"],
            "name": report["name"],
            "role": report["role"],
            "confidence": float(report.get("confidence", 0)),
        }
        for report in cycle.get("reports", [])
    ]


def close_shadow_trade(
    database: Database,
    order_id: str,
    *,
    exit_reason: str = "HOLDING_COMPLETE",
) -> dict[str, Any]:
    if exit_reason not in VALID_EXIT_REASONS:
        raise PerformanceError("지원하지 않는 그림자 청산 사유입니다.")
    order = database.get_shadow_order(order_id)
    if order["state"] != "SHADOW_FILLED" or order["fill"] is None:
        raise PerformanceError("체결 완료된 그림자 매수만 청산 성과로 기록할 수 있습니다.")
    if order.get("side") != "BUY":
        raise PerformanceError("그림자 성과 원장은 매수 진입 포지션만 지원합니다.")
    quote = database.get_latest_broker_quote(order["symbol"])
    if quote is None or quote["payload"].get("source") != "KIWOOM_OFFICIAL_REST":
        raise PerformanceError("청산에는 같은 종목의 키움 공식 시세가 필요합니다.")
    exit_price = int(quote["payload"].get("current_price_krw", 0))
    if exit_price <= 0:
        raise PerformanceError("공식 청산 가격이 유효하지 않습니다.")

    cycle = database.get_cycle(order["cycle_id"])
    strategy_id = cycle["strategy_id"]
    exit_at = str(quote["observed_at"])
    exit_date = datetime.fromisoformat(exit_at).date()
    schedule = shadow_exit_schedule(order, strategy_id, exit_date)
    holding_sessions = int(schedule["holding_sessions"])
    required_sessions = int(schedule["required_holding_sessions"])
    if exit_reason == "HOLDING_COMPLETE" and holding_sessions < required_sessions:
        raise PerformanceError(
            f"보유기간이 {holding_sessions}거래일로 최소 {required_sessions}거래일보다 짧습니다."
        )

    fill = order["fill"]
    quantity = int(fill["quantity"])
    entry_price = int(fill["price_krw"])
    entry_value = quantity * entry_price
    exit_value = quantity * exit_price
    fee = round(entry_value * 0.0002 + exit_value * 0.0002)
    tax = round(exit_value * 0.002)
    net_pnl = exit_value - entry_value - fee - tax
    return_bps = round(net_pnl / entry_value * 10_000) if entry_value else 0
    limit_price = int(order["limit_price_krw"])
    slippage_bps = round((entry_price - limit_price) / limit_price * 10_000)
    snapshot = database.get_snapshot(cycle["snapshot_id"])
    outcome_state = "WIN" if net_pnl > 0 else "LOSS" if net_pnl < 0 else "FLAT"
    invalidated = exit_price <= int(order["invalidation_price_krw"])
    postmortem = {
        "outcome": outcome_state,
        "exit_reason": exit_reason,
        "holding_sessions": holding_sessions,
        "required_holding_sessions": required_sessions,
        "thesis_invalidated": invalidated,
        "execution_quality": "PASS" if abs(slippage_bps) <= 10 else "REVIEW",
        "causal_attribution_allowed": False,
        "review_notes": [
            "직원별 값은 의사결정 참여 손익이며 인과적 기여도로 해석하지 않습니다.",
            *(["무효화 가격 이하에서 청산됐습니다."] if invalidated else []),
            *(["진입 슬리피지가 10bp를 초과했습니다."] if abs(slippage_bps) > 10 else []),
        ],
    }
    material = {
        "source_type": "SHADOW",
        "source_id": order_id,
        "cycle_id": cycle["id"],
        "entry_at": fill["occurred_at"],
        "exit_at": exit_at,
        "quantity": quantity,
        "entry_price_krw": entry_price,
        "exit_price_krw": exit_price,
        "fee_krw": fee,
        "tax_krw": tax,
        "exit_reason": exit_reason,
    }
    digest = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return database.save_trade_outcome(
        {
            "id": f"outcome_{digest[:20]}",
            "mission_id": order["mission_id"],
            "source_type": "SHADOW",
            "source_id": order_id,
            "cycle_id": cycle["id"],
            "strategy_id": strategy_id,
            "symbol": order["symbol"],
            "regime": _regime(snapshot),
            "entry_at": fill["occurred_at"],
            "exit_at": exit_at,
            "quantity": quantity,
            "entry_price_krw": entry_price,
            "exit_price_krw": exit_price,
            "fee_krw": fee,
            "tax_krw": tax,
            "net_pnl_krw": net_pnl,
            "return_bps": return_bps,
            "slippage_bps": slippage_bps,
            "participants": _participants(cycle),
            "postmortem": postmortem,
        }
    )


def apply_profit_vault(database: Database, mission_id: str) -> list[dict[str, Any]]:
    mission = database.get_mission(mission_id)
    live_outcomes = [
        item for item in database.list_trade_outcomes(mission_id) if item["source_type"] == "LIVE"
    ]
    if not live_outcomes:
        return database.get_profit_vault_milestones(mission_id)
    existing = {
        int(item["target_multiple"])
        for item in database.get_profit_vault_milestones(mission_id)
    }
    equity = int(mission["equity_krw"])
    seed = int(mission["seed_capital_krw"])
    prior_multiple = 1
    for multiple in (2, 5, 10, 25, 50, 100):
        if multiple in existing:
            prior_multiple = multiple
            continue
        target = seed * multiple
        if equity < target:
            break
        rung_profit = seed * (multiple - prior_multiple)
        lock_amount = max(1, math.floor(rung_profit * 0.2))
        database.lock_profit_milestone(
            mission_id,
            target_multiple=multiple,
            target_equity_krw=target,
            locked_amount_krw=lock_amount,
            trigger_outcome_id=live_outcomes[-1]["id"],
        )
        prior_multiple = multiple
        mission = database.get_mission(mission_id)
        equity = int(mission["equity_krw"])
    return database.get_profit_vault_milestones(mission_id)


def sync_live_trade_outcomes(database: Database, mission_id: str) -> list[dict[str, Any]]:
    lots_by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
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
        fills = sorted(intent["fills"], key=lambda item: (item["occurred_at"], item["id"]))
        if intent["side"] == "BUY":
            shadow = database.get_shadow_order(intent["shadow_order_id"])
            cycle = database.get_cycle(shadow["cycle_id"])
            snapshot = database.get_snapshot(cycle["snapshot_id"])
            for fill in fills:
                lots_by_symbol[intent["symbol"]].append(
                    {
                        "remaining": int(fill["quantity"]),
                        "fill": fill,
                        "intent": intent,
                        "cycle": cycle,
                        "regime": _regime(snapshot),
                    }
                )
            continue

        for sell_fill in fills:
            remaining = int(sell_fill["quantity"])
            portion = 0
            while remaining > 0 and lots_by_symbol[intent["symbol"]]:
                lot = lots_by_symbol[intent["symbol"]][0]
                matched = min(remaining, int(lot["remaining"]))
                buy_fill = lot["fill"]
                buy_quantity = int(buy_fill["quantity"])
                sell_quantity = int(sell_fill["quantity"])
                entry_fee = round(int(buy_fill["fee_krw"]) * matched / buy_quantity)
                exit_fee = round(int(sell_fill["fee_krw"]) * matched / sell_quantity)
                exit_tax = round(int(sell_fill["tax_krw"]) * matched / sell_quantity)
                entry_price = int(buy_fill["price_krw"])
                exit_price = int(sell_fill["price_krw"])
                entry_value = matched * entry_price
                net_pnl = matched * (exit_price - entry_price) - entry_fee - exit_fee - exit_tax
                return_bps = round(net_pnl / entry_value * 10_000) if entry_value else 0
                limit_price = int(lot["intent"]["limit_price_krw"])
                slippage_bps = round((entry_price - limit_price) / limit_price * 10_000)
                source_id = f"{buy_fill['id']}:{sell_fill['id']}:{portion}"
                digest = hashlib.sha256(source_id.encode()).hexdigest()
                outcome_state = "WIN" if net_pnl > 0 else "LOSS" if net_pnl < 0 else "FLAT"
                database.save_trade_outcome(
                    {
                        "id": f"outcome_{digest[:20]}",
                        "mission_id": mission_id,
                        "source_type": "LIVE",
                        "source_id": source_id,
                        "cycle_id": lot["cycle"]["id"],
                        "strategy_id": lot["cycle"]["strategy_id"],
                        "symbol": intent["symbol"],
                        "regime": lot["regime"],
                        "entry_at": buy_fill["occurred_at"],
                        "exit_at": sell_fill["occurred_at"],
                        "quantity": matched,
                        "entry_price_krw": entry_price,
                        "exit_price_krw": exit_price,
                        "fee_krw": entry_fee + exit_fee,
                        "tax_krw": exit_tax,
                        "net_pnl_krw": net_pnl,
                        "return_bps": return_bps,
                        "slippage_bps": slippage_bps,
                        "participants": _participants(lot["cycle"]),
                        "postmortem": {
                            "outcome": outcome_state,
                            "exit_reason": "LIVE_SELL_FILL",
                            "execution_quality": (
                                "PASS" if abs(slippage_bps) <= 10 else "REVIEW"
                            ),
                            "causal_attribution_allowed": False,
                            "review_notes": [
                                "브로커 실체결 수수료·세금을 포함한 FIFO 청산 결과입니다.",
                                "직원별 손익은 인과적 기여도로 해석하지 않습니다.",
                            ],
                        },
                    }
                )
                lot["remaining"] -= matched
                remaining -= matched
                portion += 1
                if lot["remaining"] == 0:
                    lots_by_symbol[intent["symbol"]].pop(0)
            if remaining > 0:
                detail = f"{intent['symbol']} 매도 체결 {remaining}주에 대응하는 " + (
                    "내부 매수 lot이 없습니다."
                )
                duplicate = any(
                    item["code"] == "UNMATCHED_LIVE_SELL_FILL"
                    and item["detail"] == detail
                    and item["resolved_at"] is None
                    for item in database.list_incidents(mission_id)
                )
                if not duplicate:
                    database.create_incident(
                        mission_id,
                        severity="CRITICAL",
                        code="UNMATCHED_LIVE_SELL_FILL",
                        detail=detail,
                    )
    apply_profit_vault(database, mission_id)
    return [
        item for item in database.list_trade_outcomes(mission_id) if item["source_type"] == "LIVE"
    ]


def _group_summary(outcomes: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for outcome in outcomes:
        grouped[str(outcome[key])].append(outcome)
    return [
        {
            key: label,
            "trades": len(items),
            "net_pnl_krw": sum(int(item["net_pnl_krw"]) for item in items),
            "mean_return_pct": round(
                sum(int(item["return_bps"]) for item in items) / len(items) / 100, 2
            ),
            "win_rate_pct": round(
                sum(int(item["net_pnl_krw"]) > 0 for item in items) / len(items) * 100, 1
            ),
        }
        for label, items in sorted(grouped.items())
    ]


def performance_report(database: Database, mission_id: str) -> dict[str, Any]:
    outcomes = database.list_trade_outcomes(mission_id)
    pnl = [int(item["net_pnl_krw"]) for item in outcomes]
    gains = sum(value for value in pnl if value > 0)
    losses = -sum(value for value in pnl if value < 0)
    cumulative = 0
    peak = 0
    max_drawdown = 0
    for value in pnl:
        cumulative += value
        peak = max(peak, cumulative)
        max_drawdown = min(max_drawdown, cumulative - peak)
    recent = outcomes[-20:]
    slippage = sorted(abs(int(item["slippage_bps"])) for item in outcomes)
    participant_rows: dict[str, dict[str, Any]] = {}
    for outcome in outcomes:
        for participant in outcome["participants"]:
            code = participant["code"]
            row = participant_rows.setdefault(
                code,
                {
                    "code": code,
                    "name": participant["name"],
                    "participating_trades": 0,
                    "participating_pnl_krw": 0,
                },
            )
            row["participating_trades"] += 1
            row["participating_pnl_krw"] += int(outcome["net_pnl_krw"])
    pending_shadow = [
        item
        for item in database.list_shadow_orders(mission_id)
        if item["state"] == "SHADOW_FILLED"
        and all(outcome["source_id"] != item["id"] for outcome in outcomes)
    ]
    return {
        "mission_id": mission_id,
        "state": "EVIDENCE_READY" if outcomes else "AWAITING_CLOSED_TRADES",
        "metrics": {
            "closed_trades": len(outcomes),
            "net_pnl_krw": sum(pnl),
            "win_rate_pct": round(sum(value > 0 for value in pnl) / len(pnl) * 100, 1)
            if pnl
            else 0,
            "mean_return_pct": round(
                sum(int(item["return_bps"]) for item in outcomes) / len(outcomes) / 100, 2
            )
            if outcomes
            else 0,
            "profit_factor": round(gains / losses, 3) if losses else None,
            "max_drawdown_krw": max_drawdown,
            "recent_20_expectancy_krw": round(
                sum(int(item["net_pnl_krw"]) for item in recent) / len(recent)
            )
            if recent
            else 0,
            "mean_abs_slippage_bps": round(sum(slippage) / len(slippage), 2)
            if slippage
            else 0,
            "p95_abs_slippage_bps": slippage[max(0, math.ceil(len(slippage) * 0.95) - 1)]
            if slippage
            else 0,
        },
        "by_strategy": _group_summary(outcomes, "strategy_id"),
        "by_regime": _group_summary(outcomes, "regime"),
        "by_participant": sorted(
            participant_rows.values(), key=lambda item: item["code"]
        ),
        "pending_shadow_exits": len(pending_shadow),
        "latest_postmortems": outcomes[-10:][::-1],
        "profit_vault": database.get_profit_vault_milestones(mission_id),
        "attribution_note": (
            "직원별 손익은 해당 결정 참여 거래의 결과이며 인과적 알파 기여도가 아닙니다."
        ),
        "trading_enabled": False,
    }


def runtime_monitor(database: Database, mission_id: str) -> dict[str, Any]:
    report = performance_report(database, mission_id)
    metrics = report["metrics"]
    closed = int(metrics["closed_trades"])
    checks = {
        "minimum_20_closed_trades": closed >= 20,
        "recent_20_expectancy_positive": closed >= 20
        and int(metrics["recent_20_expectancy_krw"]) > 0,
        "mean_slippage_within_15bps": closed >= 20
        and float(metrics["mean_abs_slippage_bps"]) <= 15,
        "p95_slippage_within_30bps": closed >= 20
        and int(metrics["p95_abs_slippage_bps"]) <= 30,
    }
    if closed < 20:
        state = "INSUFFICIENT_EVIDENCE"
    elif all(checks.values()):
        state = "PASS"
    else:
        state = "PAUSE_NEW_BUYS"
    return {
        "state": state,
        "checks": checks,
        "metrics": metrics,
        "action": (
            "증거를 계속 축적합니다."
            if state == "INSUFFICIENT_EVIDENCE"
            else "현재 전략을 유지합니다."
            if state == "PASS"
            else "신규 매수를 중지하고 비용·실행 품질을 재검토합니다."
        ),
        "automatic_strategy_change": False,
        "trading_enabled": False,
    }


def strategy_comparison(database: Database) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for mission_id, strategy_id in MISSION_STRATEGIES.items():
        report = database.get_latest_validation(mission_id)
        if report is None:
            continue
        rows.append(
            {
                "mission_id": mission_id,
                "strategy_id": strategy_id,
                "validation_id": report["id"],
                "protocol_version": report.get("protocol_version", "LEGACY"),
                "promotion_eligible": bool(report["promotion_eligible"]),
                "metrics": report["metrics"],
                "failed_gates": report["failed_gates"],
            }
        )
    eligible = [item for item in rows if item["promotion_eligible"]]
    ranked = sorted(
        rows,
        key=lambda item: (
            item["promotion_eligible"],
            float(item["metrics"].get("information_ratio", -999)),
            float(item["metrics"].get("max_drawdown_pct", -999)),
        ),
        reverse=True,
    )
    return {
        "state": "CHAMPION_SELECTED" if eligible else "KEEP_ALL_BLOCKED",
        "champion_strategy_id": ranked[0]["strategy_id"] if eligible else None,
        "rows": ranked,
        "selection_rule": (
            "모든 사전등록 게이트 통과 후 정보비율, 최대낙폭 순으로 비교합니다."
        ),
        "automatic_promotion": False,
        "trading_enabled": False,
    }


def deployment_readiness(database: Database | None = None) -> dict[str, Any]:
    if os.getenv("MONEYGUN_ENV", "local").strip().lower() == "desktop-live":
        return desktop_live_readiness(database)
    database_url = os.getenv("MONEYGUN_DATABASE_URL", "").strip()
    secret_backend = os.getenv("MONEYGUN_SECRET_BACKEND", "").strip().upper()
    checks = {
        "postgresql_configured": os.getenv("MONEYGUN_DATABASE_BACKEND", "").strip().lower()
        == "postgresql"
        and database_url.startswith(("postgresql://", "postgres://"))
        and "change-me" not in database_url,
        "oidc_issuer_configured": os.getenv("MONEYGUN_OIDC_ISSUER", "")
        .strip()
        .startswith("https://"),
        "oidc_client_configured": bool(os.getenv("MONEYGUN_OIDC_CLIENT_ID", "").strip()),
        "external_secret_manager_configured": secret_backend
        in {"AWS_SECRETS_MANAGER", "GCP_SECRET_MANAGER", "AZURE_KEY_VAULT", "VAULT"},
        "error_monitoring_configured": bool(os.getenv("MONEYGUN_ERROR_MONITOR_DSN", "").strip()),
        "public_https_origin_configured": os.getenv("MONEYGUN_PUBLIC_ORIGIN", "")
        .strip()
        .startswith("https://"),
        "managed_backup_configured": os.getenv("MONEYGUN_MANAGED_BACKUP_CONFIGURED", "")
        .strip()
        .lower()
        in {"1", "true", "yes", "enabled"},
    }
    return {
        "state": "DEPLOYMENT_READY" if all(checks.values()) else "LOCAL_ONLY",
        "checks": checks,
        "software_boundaries": {
            "research_process_separate_from_execution": True,
            "read_and_order_credentials_separate": True,
            "unknown_orders_never_retried": True,
            "append_only_audit": True,
            "postgresql_runtime_adapter": True,
            "production_oidc_middleware": True,
        },
        "next_action": "외부 배포 공급자와 인증·비밀관리 구성을 선택하세요.",
        "trading_enabled": False,
    }


def autonomy_readiness(database: Database, mission_id: str) -> dict[str, Any]:
    performance = performance_report(database, mission_id)
    monitor = runtime_monitor(database, mission_id)
    deployment = deployment_readiness(database)
    changes = database.list_change_requests(mission_id)
    qualified = database.get_latest_snapshot("KRX_AUTHORIZED_EXPORT")
    qualified = qualified or database.get_latest_snapshot("LICENSED_VENDOR")
    qualified = qualified or database.get_latest_snapshot(
        "OFFICIAL_PUBLIC_DATA_API_AND_KIND_UI_EXPORT"
    )
    qualified = qualified or database.get_latest_snapshot(
        "OFFICIAL_PUBLIC_DATA_API_KIND_AND_KIWOOM"
    )
    manifest = qualified.get("collection_manifest", {}) if qualified else {}
    r1_metrics = database.operating_metrics(mission_id, "R1")
    software = {
        "closed_trade_attribution": True,
        "postmortem_journal": True,
        "champion_challenger_comparison": True,
        "partial_fill_reconciliation": True,
        "network_unknown_no_retry": True,
        "profit_vault_automation": True,
        "rolling_expectancy_monitor": True,
        "strategy_change_owner_approval": True,
        "deployment_configuration_gate": True,
    }
    external = {
        "qualified_market_data": bool(
            manifest.get("survivorship_bias_controlled")
            and manifest.get("historical_designation_states_complete")
        ),
        "oos_strategy_eligible": bool(
            (database.get_latest_validation(mission_id) or {}).get("promotion_eligible")
        ),
        "shadow_60_days_100_decisions": r1_metrics["operating_days"] >= 60
        and r1_metrics["decisions"] >= 100,
        "production_identity_and_secrets": deployment_is_eligible(deployment),
        "release_review_approved": os.getenv("MONEYGUN_RELEASE_APPROVED", "")
        .strip()
        .lower()
        in {"1", "true", "yes", "enabled"},
        "owner_live_activation": os.getenv("KIWOOM_TRADING_ENABLED", "")
        .strip()
        .lower()
        in {"1", "true", "yes", "enabled"},
    }
    return {
        "state": "WAITING_EXTERNAL_EVIDENCE",
        "software": software,
        "software_completed": sum(software.values()),
        "software_total": len(software),
        "external": external,
        "performance": performance,
        "runtime_monitor": monitor,
        "deployment": deployment,
        "pending_change_requests": sum(
            item["status"] == "PENDING_OWNER" for item in changes
        ),
        "user_action_required_now": all(software.values()) and not all(external.values()),
        "trading_enabled": False,
    }
