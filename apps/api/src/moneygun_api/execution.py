from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .desktop_live import DesktopLiveError, DesktopLiveRuntime, deployment_is_eligible
from .kiwoom import KiwoomConfig, KiwoomError, KiwoomReadOnlyClient
from .performance import deployment_readiness, runtime_monitor
from .pilot import (
    PILOT_CAPITAL_KRW,
    PILOT_DAILY_LOSS_KRW,
    PILOT_MAX_DAILY_ENTRIES,
    PILOT_MISSION_ID,
    PILOT_ORDER_BUDGET_KRW,
    PILOT_TOTAL_LOSS_KRW,
    pilot_daily_entry_count,
    pilot_risk_snapshot,
)
from .storage import Database, utc_now

KST = timezone(timedelta(hours=9))

MODE_EXECUTION_POLICIES: dict[str, dict[str, Any]] = {
    "FOCUS": {
        "strategy_id": "FOCUS-MOMENTUM-KR-v1",
        "order_window_kst": ("09:05", "09:10"),
        "shadow_decisions_required": 100,
        "sell_order_window_kst": ("09:05", "15:20"),
        "official_shadow_sources": {"KIWOOM_OFFICIAL_QUOTE", "KIWOOM_L0_EXIT_QUOTE"},
    },
    "CLOSE_AUCTION": {
        "strategy_id": "CLOSE-AUCTION-KR-v2",
        "order_window_kst": ("15:20", "15:27"),
        "shadow_decisions_required": 100,
        "official_shadow_sources": {"KIWOOM_CLOSE_AUCTION_QUOTE"},
    },
    "OPENING_RANGE": {
        "strategy_id": "OPEN-RANGE-KR-v2",
        "order_window_kst": ("09:10", "10:30"),
        "shadow_decisions_required": 300,
        "sell_order_window_kst": ("09:10", "11:00"),
        "official_shadow_sources": {"KIWOOM_OPENING_RANGE_REALTIME", "KIWOOM_L0_EXIT_QUOTE"},
    },
}

STRATEGY_EXECUTION_POLICIES = {
    policy["strategy_id"]: policy for policy in MODE_EXECUTION_POLICIES.values()
}
STRATEGY_EXECUTION_POLICIES["CLOSE-AUCTION-KR-v3-L0"] = {
    "strategy_id": "CLOSE-AUCTION-KR-v3-L0",
    "order_window_kst": ("15:20", "15:27"),
    "shadow_decisions_required": 0,
    "sell_order_window_kst": ("09:05", "15:20"),
    "official_shadow_sources": {"KIWOOM_CLOSE_AUCTION_QUOTE", "KIWOOM_L0_EXIT_QUOTE"},
}
STRATEGY_EXECUTION_POLICIES.update(
    {
        "FOCUS-MOMENTUM-KR-v2-L0": {
            "strategy_id": "FOCUS-MOMENTUM-KR-v2-L0",
            "order_window_kst": ("09:05", "09:10"),
            "sell_order_window_kst": ("09:05", "15:20"),
            "shadow_decisions_required": 0,
            "official_shadow_sources": {"KIWOOM_L0_NEXT_OPEN_QUOTE", "KIWOOM_L0_EXIT_QUOTE"},
        },
        "BALANCED-TREND-KR-v1-L0": {
            "strategy_id": "BALANCED-TREND-KR-v1-L0",
            "order_window_kst": ("09:05", "09:10"),
            "sell_order_window_kst": ("09:05", "15:20"),
            "shadow_decisions_required": 0,
            "official_shadow_sources": {"KIWOOM_L0_NEXT_OPEN_QUOTE", "KIWOOM_L0_EXIT_QUOTE"},
        },
        "LONG-TREND-KR-v1-L0": {
            "strategy_id": "LONG-TREND-KR-v1-L0",
            "order_window_kst": ("09:05", "09:10"),
            "sell_order_window_kst": ("09:05", "15:20"),
            "shadow_decisions_required": 0,
            "official_shadow_sources": {"KIWOOM_L0_NEXT_OPEN_QUOTE", "KIWOOM_L0_EXIT_QUOTE"},
        },
        "OPEN-RANGE-KR-v3-L0": {
            "strategy_id": "OPEN-RANGE-KR-v3-L0",
            "order_window_kst": ("09:10", "10:30"),
            "sell_order_window_kst": ("09:10", "11:00"),
            "shadow_decisions_required": 0,
            "official_shadow_sources": {"KIWOOM_OPENING_RANGE_REALTIME", "KIWOOM_L0_EXIT_QUOTE"},
        },
    }
)


class ExecutionError(RuntimeError):
    """A deterministic refusal that is safe to show to the owner."""


class BrokerSubmissionUnknown(RuntimeError):
    """The broker may have received the order; never retry before reconciliation."""


class OrderTransport(Protocol):
    def post(
        self, url: str, headers: dict[str, str], payload: dict[str, Any]
    ) -> dict[str, Any]: ...


class KiwoomOrderTransport:
    def post(self, url: str, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
        request = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=10) as response:  # noqa: S310 - fixed broker hosts only
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            raise ExecutionError(f"키움 주문이 HTTP {error.code}로 거절되었습니다.") from error
        except (URLError, TimeoutError) as error:
            raise BrokerSubmissionUnknown(
                "주문 응답을 확인할 수 없습니다. 조회 전에는 재전송하지 않습니다."
            ) from error
        except json.JSONDecodeError as error:
            raise BrokerSubmissionUnknown(
                "키움 주문 응답을 해석할 수 없어 UNKNOWN으로 보관합니다."
            ) from error


def _enabled(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "enabled"}


@dataclass(frozen=True)
class ExecutionRuntimeConfig:
    trading_enabled: bool
    release_approved: bool
    owner_api_token: str
    capital_limit_krw: int
    l0_capital_limit_krw: int = PILOT_CAPITAL_KRW

    @classmethod
    def from_env(cls) -> ExecutionRuntimeConfig:
        try:
            capital = int(os.getenv("MONEYGUN_LIVE_CAPITAL_KRW", "100000"))
        except ValueError:
            capital = 100_000
        try:
            l0_capital = int(os.getenv("MONEYGUN_L0_CAPITAL_KRW", "50000"))
        except ValueError:
            l0_capital = PILOT_CAPITAL_KRW
        return cls(
            trading_enabled=_enabled("KIWOOM_TRADING_ENABLED"),
            release_approved=_enabled("MONEYGUN_RELEASE_APPROVED"),
            owner_api_token=os.getenv("MONEYGUN_OWNER_API_TOKEN", "").strip(),
            capital_limit_krw=max(10_000, min(capital, 100_000)),
            l0_capital_limit_krw=max(10_000, min(l0_capital, PILOT_CAPITAL_KRW)),
        )

    @property
    def owner_auth_configured(self) -> bool:
        return len(self.owner_api_token) >= 24

    def verify_owner(self, supplied: str | None) -> bool:
        return bool(
            supplied
            and self.owner_auth_configured
            and hmac.compare_digest(supplied, self.owner_api_token)
        )


class KiwoomExecutionClient:
    """Order-only broker adapter owned exclusively by the Execution Guardian."""

    ORDER_PATH = "/api/dostk/ordr"

    def __init__(
        self,
        config: KiwoomConfig | None = None,
        transport: OrderTransport | None = None,
    ) -> None:
        self.config = config or self._order_config_from_env()
        self._transport = transport or KiwoomOrderTransport()
        self._token: str | None = None
        self._token_cached_at = 0.0

    @staticmethod
    def _order_config_from_env() -> KiwoomConfig:
        """Load credentials from a namespace that the read-only adapter never uses."""
        environment = os.getenv("KIWOOM_ORDER_ENVIRONMENT", "mock").strip().lower()
        if environment not in {"mock", "production"}:
            environment = "mock"
        return KiwoomConfig(
            app_key=os.getenv("KIWOOM_ORDER_APP_KEY", "").strip(),
            secret_key=os.getenv("KIWOOM_ORDER_SECRET_KEY", "").strip(),
            environment=environment,
            account_alias=os.getenv("KIWOOM_ACCOUNT_ALIAS", "키움 주계좌").strip() or "키움 주계좌",
        )

    def _access_token(self) -> str:
        if not self.config.configured:
            raise ExecutionError("키움 주문용 앱 키가 설정되지 않았습니다.")
        if self._token and time.monotonic() - self._token_cached_at < 23 * 60 * 60:
            return self._token
        response = self._transport.post(
            f"{self.config.base_url}/oauth2/token",
            {"Content-Type": "application/json;charset=UTF-8"},
            {
                "grant_type": "client_credentials",
                "appkey": self.config.app_key,
                "secretkey": self.config.secret_key,
            },
        )
        token = response.get("token")
        if response.get("return_code") not in {None, 0} or not isinstance(token, str):
            raise ExecutionError("키움 주문 토큰 발급이 거절되었습니다.")
        self._token = token
        self._token_cached_at = time.monotonic()
        return token

    def submit_limit_order(
        self, *, symbol: str, side: str, quantity: int, limit_price_krw: int
    ) -> dict[str, Any]:
        if not symbol.isdigit() or len(symbol) != 6:
            raise ExecutionError("실주문 종목코드는 숫자 6자리여야 합니다.")
        if side not in {"BUY", "SELL"} or quantity < 1 or limit_price_krw < 1:
            raise ExecutionError("허용되지 않는 주문 방향·수량·가격입니다.")
        api_id = "kt10000" if side == "BUY" else "kt10001"
        return self._order_request(
            api_id,
            {
                "dmst_stex_tp": "KRX",
                "stk_cd": symbol,
                "ord_qty": str(quantity),
                "ord_uv": str(limit_price_krw),
                "trde_tp": "0",
                "cond_uv": "",
            },
        )

    def cancel_order(
        self, *, broker_order_no: str, symbol: str, quantity: int = 0
    ) -> dict[str, Any]:
        if not broker_order_no.isdigit() or len(broker_order_no) != 7:
            raise ExecutionError("취소할 키움 주문번호는 숫자 7자리여야 합니다.")
        return self._order_request(
            "kt10003",
            {
                "dmst_stex_tp": "KRX",
                "orig_ord_no": broker_order_no,
                "stk_cd": symbol,
                "cncl_qty": str(quantity),
            },
        )

    def _order_request(self, api_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        token = self._access_token()
        response = self._transport.post(
            f"{self.config.base_url}{self.ORDER_PATH}",
            {
                "Content-Type": "application/json;charset=UTF-8",
                "authorization": f"Bearer {token}",
                "api-id": api_id,
                "cont-yn": "N",
                "next-key": "",
            },
            payload,
        )
        if response.get("return_code") not in {None, 0}:
            raise ExecutionError("키움이 주문을 거절했습니다.")
        return response


class ExecutionGuardian:
    def __init__(
        self,
        broker: KiwoomExecutionClient | None = None,
        runtime: ExecutionRuntimeConfig | None = None,
        desktop_live: DesktopLiveRuntime | None = None,
    ) -> None:
        self.broker = broker or KiwoomExecutionClient()
        self.runtime = runtime or ExecutionRuntimeConfig.from_env()
        self.desktop_live = desktop_live or DesktopLiveRuntime()

    def status(self, database: Database | None = None) -> dict[str, Any]:
        blockers: list[str] = []
        if not self.runtime.trading_enabled:
            blockers.append("KIWOOM_TRADING_ENABLED가 false입니다.")
        if not self.broker.config.configured:
            blockers.append("키움 주문용 앱 키가 없습니다.")
        if not self.runtime.owner_auth_configured:
            blockers.append("24자 이상의 소유자 승인 토큰이 없습니다.")
        if self.broker.config.environment == "production" and not self.runtime.release_approved:
            blockers.append("운영계좌 릴리스 승인이 없습니다.")
        deployment = deployment_readiness(database)
        if self.broker.config.environment == "production" and not deployment_is_eligible(
            deployment
        ):
            blockers.append(f"운영 배포 관문이 미완료입니다: {deployment['next_action']}")
        if self.desktop_live.enabled and self.desktop_live.state != "ARMED":
            blockers.append("DESKTOP_LIVE 기동 후 복구 대사가 완료되지 않았습니다.")
        return {
            "component": "EXECUTION_GUARDIAN",
            "environment": self.broker.config.environment,
            "configured": not blockers,
            "trading_enabled": self.runtime.trading_enabled,
            "release_approved": self.runtime.release_approved,
            "owner_auth_configured": self.runtime.owner_auth_configured,
            "capital_limit_krw": self.runtime.capital_limit_krw,
            "l0_capital_limit_krw": self.runtime.l0_capital_limit_krw,
            "allowed_order_types": ["LIMIT_BUY", "LIMIT_SELL", "CANCEL"],
            "forbidden": ["MARKET", "CREDIT", "MARGIN", "SHORT", "DERIVATIVE"],
            "blockers": blockers,
            "deployment": deployment,
            "desktop_live": self.desktop_live.status(),
        }


def _integer(value: Any) -> int:
    try:
        return abs(int(float(str(value).replace(",", "").strip() or "0")))
    except ValueError:
        return 0


def _broker_fill_key(
    broker_order_no: str, fill: dict[str, Any], occurrence: int
) -> str:
    broker_execution_id = str(
        fill.get("cntr_no") or fill.get("cntr_seq") or fill.get("exec_id") or ""
    ).strip()
    if broker_execution_id:
        return f"KIWOOM:{broker_order_no}:{broker_execution_id}"
    material = json.dumps(
        {"broker_order_no": broker_order_no, "fill": fill, "occurrence": occurrence},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "KIWOOM_FALLBACK:" + hashlib.sha256(material.encode()).hexdigest()


def normalize_quote(symbol: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        change_rate_pct = float(str(payload.get("flu_rt", "0")).replace(",", "").strip() or "0")
    except ValueError:
        change_rate_pct = 0.0
    return {
        "symbol": symbol,
        "name": str(payload.get("stk_nm", symbol)),
        "current_price_krw": _integer(payload.get("cur_prc", payload.get("lst_pric", 0))),
        "open_price_krw": _integer(payload.get("open_pric", 0)),
        "high_price_krw": _integer(payload.get("high_pric", 0)),
        "low_price_krw": _integer(payload.get("low_pric", 0)),
        "volume": _integer(payload.get("trde_qty", 0)),
        "market_cap_100m_krw": _integer(payload.get("mac", 0)),
        "change_rate_pct": change_rate_pct,
        "source": "KIWOOM_OFFICIAL_REST",
        "observed_at": utc_now(),
    }


def sync_official_quote(
    database: Database, client: KiwoomReadOnlyClient, symbol: str
) -> dict[str, Any]:
    quote = normalize_quote(symbol, client.fetch_domestic_quote(symbol))
    if quote["current_price_krw"] <= 0:
        raise KiwoomError("키움 시세 응답에 정상 현재가가 없습니다.")
    return database.save_broker_quote_snapshot(
        broker="KIWOOM",
        symbol=symbol,
        observed_at=quote["observed_at"],
        payload=quote,
    )


def reconcile_managed_account(
    database: Database,
    client: KiwoomReadOnlyClient,
    *,
    mission_id: str,
) -> dict[str, Any]:
    raw_account = client.fetch_domestic_account_evaluation()
    account_snapshot = database.save_broker_account_snapshot(
        broker="KIWOOM",
        environment=client.config.environment,
        account_alias=client.config.account_alias,
        payload=raw_account,
    )
    mission = database.get_mission(mission_id)
    broker_positions: dict[str, int] = {}
    for item in raw_account.get("acnt_evlt_remn_indv_tot", []):
        symbol = str(item.get("stk_cd", ""))
        if len(symbol) == 7 and symbol[0].isalpha():
            symbol = symbol[1:]
        broker_positions[symbol] = _integer(item.get("rmnd_qty", 0))

    internal_positions: dict[str, int] = {}
    for intent in database.list_live_order_intents(mission_id):
        signed = 1 if intent["side"] == "BUY" else -1
        for fill in intent["fills"]:
            internal_positions[intent["symbol"]] = internal_positions.get(intent["symbol"], 0) + (
                signed * int(fill["quantity"])
            )
    managed_symbols = sorted(set(internal_positions))
    position_differences = [
        {
            "symbol": symbol,
            "internal_quantity": internal_positions.get(symbol, 0),
            "broker_quantity": broker_positions.get(symbol, 0),
        }
        for symbol in managed_symbols
        if internal_positions.get(symbol, 0) != broker_positions.get(symbol, 0)
    ]
    broker_assets = _integer(raw_account.get("prsm_dpst_aset_amt", 0))
    capital_covered = broker_assets >= int(mission["equity_krw"])
    status = "PASS" if capital_covered and not position_differences else "FAIL"
    details = {
        "broker_assets_krw": broker_assets,
        "mission_equity_krw": mission["equity_krw"],
        "capital_covered": capital_covered,
        "managed_symbols": managed_symbols,
        "position_differences": position_differences,
        "unmanaged_broker_positions_ignored": sorted(set(broker_positions) - set(managed_symbols)),
        "source": "KIWOOM_OFFICIAL_REST",
    }
    result = database.save_reconciliation(
        mission_id=mission_id,
        account_snapshot_id=account_snapshot["id"],
        status=status,
        details=details,
    )
    control = database.get_execution_control(mission_id)
    evidence_mode = (
        control["stage"] if control["stage"] in {"R0", "R1", "L0", "L1"} else "R0"
    )
    database.record_operating_day(
        mission_id=mission_id,
        trade_date=datetime.now(KST).date().isoformat(),
        mode=evidence_mode,
        reconciliation_status=status,
        risk_violations=0 if status == "PASS" else 1,
        duplicate_orders=0,
        decision_count=0,
        fill_count=0,
        net_pnl_krw=0,
    )
    return result


def stage_gate_report(database: Database, mission_id: str) -> dict[str, Any]:
    mission = database.get_mission(mission_id)
    policy = MODE_EXECUTION_POLICIES.get(mission["mode_code"])
    required_shadow_decisions = int(
        policy["shadow_decisions_required"] if policy else 100
    )
    validation = database.get_latest_validation(mission_id)
    r1_metrics = database.operating_metrics(mission_id, "R1")
    l1_metrics = database.operating_metrics(mission_id, "L1")
    l0_metrics = database.operating_metrics(mission_id, "L0")
    reviews = database.list_pilot_reviews(mission_id) if mission["mode_code"] == "L0_PILOT" else []
    used_source_missions = {
        str(item.get("source_mission_id", ""))
        for item in database.list_live_order_intents(mission_id)
        if item.get("source_mission_id")
    }
    used_validations = [
        database.get_latest_validation(source_mission_id)
        for source_mission_id in sorted(used_source_missions)
    ]
    l0_strategy_qualified = bool(used_validations) and all(
        validation and validation.get("promotion_eligible") for validation in used_validations
    )
    l0_gates = {
        "live_operating_days_60": l0_metrics["operating_days"] >= 60,
        "day_60_review_frozen": any(int(item["checkpoint_day"]) == 60 for item in reviews),
        "used_strategies_oos_qualified": l0_strategy_qualified,
        "reconciliation_failures_zero": l0_metrics["reconciliation_failures"] == 0,
        "risk_violations_zero": l0_metrics["risk_violations"] == 0,
        "duplicate_orders_zero": l0_metrics["duplicate_orders"] == 0,
        "critical_incidents_zero": l0_metrics["open_critical_incidents"] == 0,
    }
    r1_gates = {
        "oos_promotion_eligible": bool(validation and validation.get("promotion_eligible")),
        "shadow_operating_days_60": r1_metrics["operating_days"] >= 60,
        f"shadow_decisions_{required_shadow_decisions}": (
            r1_metrics["decisions"] >= required_shadow_decisions
        ),
        "reconciliation_failures_zero": r1_metrics["reconciliation_failures"] == 0,
        "risk_violations_zero": r1_metrics["risk_violations"] == 0,
        "duplicate_orders_zero": r1_metrics["duplicate_orders"] == 0,
        "critical_incidents_zero": r1_metrics["open_critical_incidents"] == 0,
    }
    l2_gates = {
        "live_operating_days_60": l1_metrics["operating_days"] >= 60,
        "live_fills_100": l1_metrics["fills"] >= 100,
        "reconciliation_failures_zero": l1_metrics["reconciliation_failures"] == 0,
        "risk_violations_zero": l1_metrics["risk_violations"] == 0,
        "duplicate_orders_zero": l1_metrics["duplicate_orders"] == 0,
        "critical_incidents_zero": l1_metrics["open_critical_incidents"] == 0,
        "net_expectancy_positive": l1_metrics["net_pnl_krw"] > 0,
    }
    return {
        "L0_TO_L1": {
            "eligible": all(l0_gates.values()),
            "gates": l0_gates,
            "metrics": l0_metrics,
            "requirements": {"operating_days": 60, "review_days": [15, 30, 45, 60]},
        },
        "R1_TO_L1": {
            "eligible": all(r1_gates.values()),
            "gates": r1_gates,
            "metrics": r1_metrics,
            "requirements": {
                "operating_days": 60,
                "decisions": required_shadow_decisions,
            },
        },
        "L1_TO_L2": {
            "eligible": all(l2_gates.values()),
            "gates": l2_gates,
            "metrics": l1_metrics,
        },
    }


def transition_stage(
    database: Database, mission_id: str, target_stage: str, *, reason: str
) -> dict[str, Any]:
    control = database.get_execution_control(mission_id)
    allowed = {("R0", "L0"), ("R0", "R1"), ("R1", "L1"), ("L1", "L2")}
    if (control["stage"], target_stage) not in allowed:
        raise ExecutionError("허용된 단계 전환이 아닙니다.")
    gates = stage_gate_report(database, mission_id)
    mission = database.get_mission(mission_id)
    if target_stage == "L0":
        if mission_id != PILOT_MISSION_ID or mission["mode_code"] != "L0_PILOT":
            raise ExecutionError("L0는 5만 원 전용 파일럿 미션에서만 시작할 수 있습니다.")
        if int(mission["seed_capital_krw"]) != PILOT_CAPITAL_KRW:
            raise ExecutionError("L0 파일럿 원장이 정확히 5만 원이 아닙니다.")
    elif target_stage == "R1":
        validation = database.get_latest_validation(mission_id)
        if not validation or not validation.get("promotion_eligible"):
            raise ExecutionError("승인된 실데이터 OOS 게이트가 R1 승격을 차단했습니다.")
    elif target_stage == "L1" and not gates["R1_TO_L1"]["eligible"]:
        requirements = gates["R1_TO_L1"]["requirements"]
        raise ExecutionError(
            f"60일·{requirements['decisions']}결정 그림자 운용 게이트를 통과하지 못했습니다."
        )
    elif target_stage == "L2" and not gates["L1_TO_L2"]["eligible"]:
        raise ExecutionError("60실거래일·100체결 L2 게이트를 통과하지 못했습니다.")
    return database.update_execution_control(
        mission_id,
        actor="OWNER",
        reason=reason,
        stage=target_stage,
        automation_enabled=False,
    )


def _age_seconds(timestamp: str) -> float:
    return max(0.0, (datetime.now(UTC) - datetime.fromisoformat(timestamp)).total_seconds())


def _news_feed_blocker(database: Database) -> str | None:
    if not _enabled("MONEYGUN_NEWS_SCHEDULER_ENABLED"):
        return None
    latest = database.latest_news_poll_run()
    if latest is None:
        return "김뉴스 공식 공시 감시가 아직 한 번도 완료되지 않았습니다."
    if latest["state"] != "PASS":
        return "김뉴스 공식 공시 감시의 최근 실행이 실패했습니다."
    if _age_seconds(latest["completed_at"]) > 15 * 60:
        return "김뉴스 공식 공시 감시가 15분보다 오래되어 신규 매수를 차단합니다."
    return None


def _live_portfolio_metrics(database: Database, mission_id: str) -> tuple[int, int, set[str]]:
    net_positions: dict[str, int] = {}
    current_exposure = 0
    current_open_risk = 0
    for existing in database.list_live_order_intents(mission_id):
        signed = 1 if existing["side"] == "BUY" else -1
        filled_quantity = sum(int(fill["quantity"]) for fill in existing["fills"])
        filled_value = sum(
            int(fill["quantity"]) * int(fill["price_krw"]) for fill in existing["fills"]
        )
        net_positions[existing["symbol"]] = (
            net_positions.get(existing["symbol"], 0) + signed * filled_quantity
        )
        current_exposure += signed * filled_value
        if signed > 0:
            current_open_risk += filled_quantity * max(
                0,
                int(existing["limit_price_krw"]) - int(existing["invalidation_price_krw"]),
            )
    open_symbols = {symbol for symbol, quantity in net_positions.items() if quantity > 0}
    return max(0, current_exposure), current_open_risk, open_symbols


def _managed_position_quantities(database: Database, mission_id: str) -> dict[str, int]:
    quantities: dict[str, int] = {}
    for existing in database.list_live_order_intents(mission_id):
        signed = 1 if existing["side"] == "BUY" else -1
        filled = sum(int(fill["quantity"]) for fill in existing["fills"])
        quantities[existing["symbol"]] = quantities.get(existing["symbol"], 0) + signed * filled
    return {symbol: quantity for symbol, quantity in quantities.items() if quantity > 0}


def build_live_intent(
    database: Database,
    guardian: ExecutionGuardian,
    *,
    shadow_order_id: str,
    idempotency_key: str,
    execution_mission_id: str | None = None,
) -> dict[str, Any]:
    shadow = database.get_shadow_order(shadow_order_id)
    cycle = database.get_cycle(shadow["cycle_id"])
    target_mission_id = execution_mission_id or shadow["mission_id"]
    control = database.get_execution_control(target_mission_id)
    mission = database.get_mission(target_mission_id)
    quote = database.get_latest_broker_quote(shadow["symbol"])
    reconciliation = database.get_latest_reconciliation(target_mission_id)
    blockers: list[str] = []
    is_l0 = target_mission_id == PILOT_MISSION_ID and control["stage"] == "L0"
    policy = (
        STRATEGY_EXECUTION_POLICIES.get(cycle["strategy_id"])
        if is_l0
        else MODE_EXECUTION_POLICIES.get(mission["mode_code"])
    )
    if policy is None or policy["strategy_id"] != cycle["strategy_id"]:
        blockers.append("미션 모드에 승인된 전략 버전이 아닙니다.")
    validation = database.get_latest_validation(shadow["mission_id"])
    if not is_l0 and (not validation or not validation.get("promotion_eligible")):
        blockers.append("현재 전략의 승인된 OOS 자격이 유효하지 않습니다.")
    if control["stage"] not in {"L0", "L1", "L2"}:
        blockers.append("실행 단계가 L0, L1 또는 L2가 아닙니다.")
    if control["kill_switch_active"]:
        blockers.append("킬 스위치가 활성화되어 있습니다.")
    if (
        shadow["side"] == "BUY"
        and runtime_monitor(database, target_mission_id)["state"] == "PAUSE_NEW_BUYS"
    ):
        blockers.append("최근 20건 기대값 또는 슬리피지 기준이 나빠 신규 매수를 중지했습니다.")
    if shadow["side"] == "BUY":
        feed_blocker = _news_feed_blocker(database)
        if feed_blocker:
            blockers.append(feed_blocker)
        news_blocks = database.active_news_blocks(shadow["symbol"])
        if news_blocks:
            blockers.append(
                f"김뉴스 미해결 공시 {len(news_blocks)}건이 신규 매수를 차단합니다: "
                f"{news_blocks[0]['title']}"
            )
    official_sources = policy["official_shadow_sources"] if policy else set()
    if shadow["simulation_source"] not in official_sources:
        blockers.append("공식 키움 시세로 검증된 그림자 주문이 아닙니다.")
    if shadow["state"] != "READY":
        blockers.append("READY 상태의 당일 공식 주문안만 실주문 의도로 전환할 수 있습니다.")
    if not shadow["symbol"].isdigit() or len(shadow["symbol"]) != 6:
        blockers.append("실제 국내주식 종목코드가 아닙니다.")
    if quote is None or _age_seconds(quote["observed_at"]) > 15:
        blockers.append("15초 이내 공식 호가가 없습니다.")
    elif int(quote["payload"].get("current_price_krw", 0)) <= 0:
        blockers.append("공식 호가의 현재가가 유효하지 않습니다.")
    else:
        current_price = int(quote["payload"]["current_price_krw"])
        price_deviation = abs(int(shadow["limit_price_krw"]) - current_price) / current_price
        if price_deviation > 0.03:
            blockers.append("지정가가 마지막 정상 호가에서 3%보다 멀리 떨어져 있습니다.")
    if reconciliation is None or reconciliation["status"] != "PASS":
        blockers.append("최근 계좌 대사가 PASS가 아닙니다.")
    elif _age_seconds(reconciliation["created_at"]) > 30:
        blockers.append("계좌 대사가 30초보다 오래되었습니다.")
    order_value = int(shadow["quantity"]) * int(shadow["limit_price_krw"])
    existing_intents = database.list_live_order_intents(target_mission_id)
    current_exposure, current_open_risk, open_symbols = _live_portfolio_metrics(
        database, target_mission_id
    )
    if is_l0:
        max_exposure = min(
            PILOT_CAPITAL_KRW,
            guardian.runtime.l0_capital_limit_krw,
            int(mission["equity_krw"]),
        )
    else:
        stage_exposure_limit = 0.5 if control["stage"] == "L1" else 1.0
        max_exposure = min(
            guardian.runtime.capital_limit_krw,
            int(mission["equity_krw"] * stage_exposure_limit),
        )
    projected_exposure = max(
        0, current_exposure + (order_value if shadow["side"] == "BUY" else -order_value)
    )
    if projected_exposure > max_exposure:
        blockers.append(
            f"주문 후 총노출 {projected_exposure:,}원이 현재 한도 {max_exposure:,}원을 넘습니다."
        )
    if shadow["side"] == "BUY" and order_value > int(mission["available_krw"]):
        blockers.append("주문금액이 미션 가용현금을 넘습니다.")
    if is_l0 and shadow["side"] == "BUY" and order_value > PILOT_ORDER_BUDGET_KRW:
        blockers.append(f"L0 단일 주문은 {PILOT_ORDER_BUDGET_KRW:,}원을 넘을 수 없습니다.")
    if shadow["side"] == "BUY":
        if (
            shadow["symbol"] not in open_symbols
            and len(open_symbols) >= int(mission["max_positions"])
        ):
            blockers.append(
                f"{mission['mode_display_name']}는 동시에 최대 "
                f"{mission['max_positions']}종목만 보유할 수 있습니다."
            )
        planned_risk = int(shadow["quantity"]) * max(
            0, int(shadow["limit_price_krw"]) - int(shadow["invalidation_price_krw"])
        )
        max_trade_risk = int(mission["equity_krw"] * mission["max_trade_risk_bps"] / 10_000)
        max_open_risk = int(mission["equity_krw"] * mission["max_open_risk_bps"] / 10_000)
        if planned_risk > max_trade_risk:
            blockers.append(
                f"거래당 계획 손실이 {mission['mode_display_name']} 미션 한도를 넘습니다."
            )
        if current_open_risk + planned_risk > max_open_risk:
            blockers.append(
                f"주문 후 열린 위험이 {mission['mode_display_name']} 미션 한도를 넘습니다."
            )
    else:
        managed_quantities = _managed_position_quantities(database, target_mission_id)
        if int(shadow["quantity"]) > managed_quantities.get(shadow["symbol"], 0):
            blockers.append(
                "Signal Guild가 실제 체결로 관리하는 보유 수량보다 많이 매도할 수 없습니다."
            )
    if is_l0 and shadow["side"] == "BUY":
        risk = pilot_risk_snapshot(database, target_mission_id)
        if risk["stale_symbols"]:
            blockers.append("L0 보유 종목의 15초 이내 공식 호가가 없어 신규 매수를 차단합니다.")
        if risk["daily_loss_reached"]:
            blockers.append(f"L0 당일 손실 중지선 -{PILOT_DAILY_LOSS_KRW:,}원에 도달했습니다.")
        if risk["total_loss_reached"]:
            blockers.append(f"L0 누적 손실 중지선 -{PILOT_TOTAL_LOSS_KRW:,}원에 도달했습니다.")
        if pilot_daily_entry_count(database, target_mission_id) >= PILOT_MAX_DAILY_ENTRIES:
            blockers.append(f"L0는 하루 신규 진입 {PILOT_MAX_DAILY_ENTRIES}회까지만 허용합니다.")
    if control["stage"] == "L1":
        today = datetime.now(KST).date().isoformat()
        active_today = [
            item
            for item in existing_intents
            if datetime.fromisoformat(item["created_at"]).astimezone(KST).date().isoformat()
            == today
            and item["state"] not in {"REJECTED", "FAILED", "EXPIRED"}
        ]
        if active_today:
            blockers.append("L1은 하루 신규 매수 1건만 허용합니다.")
    payload = {
        "mission_id": target_mission_id,
        "source_mission_id": shadow["mission_id"],
        "shadow_order_id": shadow_order_id,
        "stage": control["stage"],
        "strategy_id": cycle["strategy_id"],
        "entry_style": cycle["decision"].get("entry_style", "NEXT_OPEN_LIMIT"),
        "symbol": shadow["symbol"],
        "name": shadow["name"],
        "side": shadow["side"],
        "quantity": shadow["quantity"],
        "limit_price_krw": shadow["limit_price_krw"],
        "invalidation_price_krw": shadow["invalidation_price_krw"],
        "order_value_krw": order_value,
        "precheck_blockers": blockers,
        "real_submission_attempted": False,
        "pilot_version": "v1.0" if is_l0 else None,
        "performance_qualified": bool(validation and validation.get("promotion_eligible")),
    }
    scope_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    scope_hash = hashlib.sha256(scope_json.encode()).hexdigest()
    intent = database.create_live_order_intent(
        payload, idempotency_key=idempotency_key, scope_hash=scope_hash
    )
    if intent["state"] != "CREATED":
        return intent
    if blockers:
        database.append_live_order_event(intent["id"], "REJECTED", {"blockers": blockers})
    else:
        database.append_live_order_event(intent["id"], "PRECHECKED", {"checks": "PASS"})
        next_state = "AWAITING_APPROVAL" if control["stage"] in {"L0", "L1"} else "READY"
        database.append_live_order_event(intent["id"], next_state, {"stage": control["stage"]})
    return database.get_live_order_intent(intent["id"])


def approve_live_intent(
    database: Database,
    intent_id: str,
    *,
    decision: str,
    scope_hash: str,
    actor: str = "OWNER",
    validity_seconds: int = 5 * 60,
) -> dict[str, Any]:
    intent = database.get_live_order_intent(intent_id)
    if intent["state"] != "AWAITING_APPROVAL":
        raise ExecutionError("사용자 승인을 기다리는 주문만 승인할 수 있습니다.")
    expires_at = (datetime.now(UTC) + timedelta(seconds=validity_seconds)).isoformat(
        timespec="seconds"
    )
    intent = database.record_owner_approval(
        intent_id,
        decision=decision,
        scope_hash=scope_hash,
        expires_at=expires_at,
        actor=actor,
    )
    terminal = "READY" if decision == "APPROVE" else "REJECTED"
    database.append_live_order_event(
        intent_id,
        terminal,
        {"owner_decision": decision, "approval_actor": actor},
    )
    return database.get_live_order_intent(intent_id)


def submit_live_intent(
    database: Database, guardian: ExecutionGuardian, intent_id: str
) -> dict[str, Any]:
    intent = database.get_live_order_intent(intent_id)
    if intent["state"] == "UNKNOWN":
        raise ExecutionError("UNKNOWN 주문은 키움 조회로 확정하기 전 재전송할 수 없습니다.")
    if intent["state"] != "READY":
        raise ExecutionError("READY 상태의 주문만 제출할 수 있습니다.")
    control = database.get_execution_control(intent["mission_id"])
    if control["kill_switch_active"]:
        raise ExecutionError("킬 스위치가 활성화되어 있습니다.")
    if control["stage"] != intent["stage"]:
        raise ExecutionError("승인 후 실행 단계가 변경되어 주문안을 다시 만들어야 합니다.")
    is_l0 = intent["mission_id"] == PILOT_MISSION_ID and intent["stage"] == "L0"
    validation = database.get_latest_validation(
        intent.get("source_mission_id", intent["mission_id"])
    )
    if not is_l0 and (not validation or not validation.get("promotion_eligible")):
        raise ExecutionError("제출 직전 전략 OOS 자격이 유효하지 않습니다.")
    if (
        intent["side"] == "BUY"
        and runtime_monitor(database, intent["mission_id"])["state"] == "PAUSE_NEW_BUYS"
    ):
        raise ExecutionError("최근 20건 기대값 또는 슬리피지 기준이 나빠 신규 매수를 중지했습니다.")
    if intent["side"] == "BUY":
        feed_blocker = _news_feed_blocker(database)
        if feed_blocker:
            raise ExecutionError(f"제출 직전 {feed_blocker}")
        news_blocks = database.active_news_blocks(intent["symbol"])
        if news_blocks:
            raise ExecutionError(
                f"제출 직전 김뉴스 미해결 공시 {len(news_blocks)}건이 신규 매수를 차단합니다: "
                f"{news_blocks[0]['title']}"
            )
    quote = database.get_latest_broker_quote(intent["symbol"])
    reconciliation = database.get_latest_reconciliation(intent["mission_id"])
    if quote is None or _age_seconds(quote["observed_at"]) > 15:
        raise ExecutionError("제출 직전 15초 이내 공식 호가가 없습니다.")
    current_price = int(quote["payload"].get("current_price_krw", 0))
    price_deviation = (
        abs(int(intent["limit_price_krw"]) - current_price) / current_price
        if current_price > 0
        else 1.0
    )
    if price_deviation > 0.03:
        raise ExecutionError("제출 직전 지정가가 마지막 정상 호가의 3% 보호폭을 벗어났습니다.")
    if (
        reconciliation is None
        or reconciliation["status"] != "PASS"
        or _age_seconds(reconciliation["created_at"]) > 30
    ):
        raise ExecutionError("제출 직전 30초 이내 PASS 계좌 대사가 없습니다.")
    if guardian.broker.config.environment == "production":
        now_kst = datetime.now(KST)
        minute_of_day = now_kst.hour * 60 + now_kst.minute
        mission = database.get_mission(intent["mission_id"])
        policy = (
            STRATEGY_EXECUTION_POLICIES.get(intent["strategy_id"])
            if is_l0
            else MODE_EXECUTION_POLICIES.get(mission["mode_code"])
        )
        if policy is None or policy["strategy_id"] != intent["strategy_id"]:
            raise ExecutionError("제출 직전 승인된 모드·전략 정책을 확인할 수 없습니다.")
        window_key = "sell_order_window_kst" if intent["side"] == "SELL" else "order_window_kst"
        window_start, window_end = policy.get(window_key, policy["order_window_kst"])
        start_hour, start_minute = (int(value) for value in window_start.split(":"))
        end_hour, end_minute = (int(value) for value in window_end.split(":"))
        in_window = (
            start_hour * 60 + start_minute
            <= minute_of_day
            <= end_hour * 60 + end_minute
        )
        if now_kst.weekday() >= 5 or not in_window:
            raise ExecutionError(
                "운영계좌 신규 주문은 한국 거래일 "
                f"{window_start}~{window_end}에만 허용합니다."
            )
    if intent["stage"] in {"L0", "L1"}:
        approval = intent["approval"]
        if not approval or approval["decision"] != "APPROVE":
            raise ExecutionError("L0/L1 실주문에는 사용자 승인이 필요합니다.")
        if datetime.fromisoformat(approval["expires_at"]) < datetime.now(UTC):
            database.append_live_order_event(intent_id, "EXPIRED", {})
            raise ExecutionError("사용자 승인이 만료되었습니다.")
    mission = database.get_mission(intent["mission_id"])
    current_exposure, current_open_risk, open_symbols = _live_portfolio_metrics(
        database, intent["mission_id"]
    )
    order_value = int(intent["order_value_krw"])
    if is_l0:
        max_exposure = min(
            PILOT_CAPITAL_KRW,
            guardian.runtime.l0_capital_limit_krw,
            int(mission["equity_krw"]),
        )
    else:
        exposure_ratio = 0.5 if intent["stage"] == "L1" else 1.0
        max_exposure = min(
            guardian.runtime.capital_limit_krw, int(mission["equity_krw"] * exposure_ratio)
        )
    projected_exposure = max(
        0, current_exposure + (order_value if intent["side"] == "BUY" else -order_value)
    )
    if projected_exposure > max_exposure:
        raise ExecutionError("제출 직전 누적 총노출 한도를 초과했습니다.")
    if is_l0 and intent["side"] == "BUY":
        if order_value > PILOT_ORDER_BUDGET_KRW:
            raise ExecutionError("제출 직전 L0 단일 주문예산을 초과했습니다.")
        risk = pilot_risk_snapshot(database, intent["mission_id"])
        if risk["stale_symbols"]:
            raise ExecutionError("제출 직전 L0 보유 종목 호가가 오래되어 신규 매수를 차단했습니다.")
        if risk["daily_loss_reached"] or risk["total_loss_reached"]:
            database.update_execution_control(
                intent["mission_id"],
                actor="EXECUTION_GUARDIAN",
                reason="L0 손실 중지선 도달",
                kill_switch_active=True,
                automation_enabled=False,
            )
            raise ExecutionError("L0 손실 중지선에 도달해 킬 스위치를 활성화했습니다.")
        if pilot_daily_entry_count(database, intent["mission_id"]) > PILOT_MAX_DAILY_ENTRIES:
            raise ExecutionError("제출 직전 L0 하루 신규 진입 한도를 초과했습니다.")
    if intent["side"] == "BUY":
        if (
            intent["symbol"] not in open_symbols
            and len(open_symbols) >= int(mission["max_positions"])
        ):
            raise ExecutionError("제출 직전 모드별 최대 종목 수를 초과했습니다.")
        planned_risk = int(intent["quantity"]) * max(
            0, int(intent["limit_price_krw"]) - int(intent["invalidation_price_krw"])
        )
        max_trade_risk = int(mission["equity_krw"] * mission["max_trade_risk_bps"] / 10_000)
        max_open_risk = int(mission["equity_krw"] * mission["max_open_risk_bps"] / 10_000)
        if planned_risk > max_trade_risk:
            raise ExecutionError("제출 직전 거래당 모드 위험 한도를 초과했습니다.")
        if current_open_risk + planned_risk > max_open_risk:
            raise ExecutionError("제출 직전 열린 위험 모드 한도를 초과했습니다.")
    else:
        managed_quantities = _managed_position_quantities(database, intent["mission_id"])
        if int(intent["quantity"]) > managed_quantities.get(intent["symbol"], 0):
            raise ExecutionError(
                "제출 직전 Signal Guild 관리 보유 수량보다 많은 매도를 차단했습니다."
            )
    try:
        guardian.desktop_live.require_armed()
    except DesktopLiveError as error:
        raise ExecutionError(str(error)) from error
    status = guardian.status(database)
    if not status["configured"]:
        raise ExecutionError(
            "실주문 다중 잠금이 열리지 않았습니다: " + " ".join(status["blockers"])
        )
    database.append_live_order_event(intent_id, "SUBMITTING", {"retry_allowed": False})
    try:
        response = guardian.broker.submit_limit_order(
            symbol=intent["symbol"],
            side=intent["side"],
            quantity=int(intent["quantity"]),
            limit_price_krw=int(intent["limit_price_krw"]),
        )
    except BrokerSubmissionUnknown as error:
        database.append_live_order_event(
            intent_id, "UNKNOWN", {"reason": str(error), "retry_allowed": False}
        )
        return database.get_live_order_intent(intent_id)
    except ExecutionError as error:
        database.append_live_order_event(intent_id, "FAILED", {"reason": str(error)})
        return database.get_live_order_intent(intent_id)
    broker_order_no = str(response.get("ord_no", ""))
    if not broker_order_no:
        database.append_live_order_event(
            intent_id,
            "UNKNOWN",
            {"reason": "키움 주문번호가 없습니다.", "retry_allowed": False},
        )
    else:
        database.append_live_order_event(
            intent_id,
            "ACKNOWLEDGED",
            {"broker_order_no": broker_order_no, "retry_allowed": False},
        )
    return database.get_live_order_intent(intent_id)


def cancel_live_intent(
    database: Database, guardian: ExecutionGuardian, intent_id: str
) -> dict[str, Any]:
    intent = database.get_live_order_intent(intent_id)
    if intent["state"] not in {"ACKNOWLEDGED", "PARTIALLY_FILLED"}:
        raise ExecutionError("접수되었거나 부분 체결된 주문만 취소할 수 있습니다.")
    broker_order_no = ""
    for event in intent["events"]:
        broker_order_no = str(event["payload"].get("broker_order_no", broker_order_no))
    if not broker_order_no:
        raise ExecutionError("키움 주문번호가 없어 취소하지 않습니다.")
    if not guardian.broker.config.configured:
        raise ExecutionError("키움 주문용 앱 키가 없어 기존 주문을 취소할 수 없습니다.")
    database.append_live_order_event(
        intent_id, "CANCEL_SUBMITTING", {"broker_order_no": broker_order_no}
    )
    try:
        response = guardian.broker.cancel_order(
            broker_order_no=broker_order_no,
            symbol=intent["symbol"],
            quantity=0,
        )
    except BrokerSubmissionUnknown as error:
        database.append_live_order_event(
            intent_id,
            "CANCEL_UNKNOWN",
            {"reason": str(error), "retry_allowed": False},
        )
        return database.get_live_order_intent(intent_id)
    except ExecutionError as error:
        database.append_live_order_event(intent_id, "CANCEL_FAILED", {"reason": str(error)})
        return database.get_live_order_intent(intent_id)
    database.append_live_order_event(
        intent_id,
        "CANCEL_ACKNOWLEDGED",
        {
            "broker_order_no": broker_order_no,
            "cancel_order_no": str(response.get("ord_no", "")),
        },
    )
    return database.get_live_order_intent(intent_id)


def reconcile_broker_orders(
    database: Database, client: KiwoomReadOnlyClient, *, mission_id: str
) -> dict[str, Any]:
    open_payload = client.fetch_open_orders()
    fill_payload = client.fetch_fills()
    open_by_no = {str(item.get("ord_no", "")): item for item in open_payload.get("oso", [])}
    fills_by_no: dict[str, list[dict[str, Any]]] = {}
    for item in fill_payload.get("cntr", []):
        fills_by_no.setdefault(str(item.get("ord_no", "")), []).append(item)
    reconciled = 0
    unresolved = 0
    newly_recorded_fills = 0
    partial_orders = 0
    for intent in database.list_live_order_intents(mission_id):
        if intent["state"] not in {"SUBMITTING", "UNKNOWN", "ACKNOWLEDGED", "PARTIALLY_FILLED"}:
            continue
        broker_order_no = ""
        for event in intent["events"]:
            broker_order_no = str(event["payload"].get("broker_order_no", broker_order_no))
        if not broker_order_no:
            unresolved += 1
            continue
        if broker_order_no in open_by_no:
            if intent["state"] in {"SUBMITTING", "UNKNOWN"}:
                database.append_live_order_event(
                    intent["id"], "ACKNOWLEDGED", {"broker_order_no": broker_order_no}
                )
            reconciled += 1
        for occurrence, fill in enumerate(fills_by_no.get(broker_order_no, [])):
            quantity = _integer(fill.get("cntr_qty", 0))
            price = _integer(fill.get("cntr_pric", 0))
            if quantity and price:
                fill_result = database.record_live_fill(
                    intent["id"],
                    broker_order_no=broker_order_no,
                    quantity=quantity,
                    price_krw=price,
                    fee_krw=_integer(fill.get("tdy_trde_cmsn", 0)),
                    tax_krw=_integer(fill.get("tdy_trde_tax", 0)),
                    occurred_at=utc_now(),
                    broker_fill_key=_broker_fill_key(broker_order_no, fill, occurrence),
                )
                newly_recorded_fills += int(fill_result["inserted"])
                reconciled += 1
        refreshed = database.get_live_order_intent(intent["id"])
        total_filled = sum(int(item["quantity"]) for item in refreshed["fills"])
        requested = int(refreshed["quantity"])
        if total_filled >= requested:
            database.append_live_order_event(
                intent["id"],
                "FILLED",
                {
                    "broker_order_no": broker_order_no,
                    "filled_quantity": total_filled,
                    "requested_quantity": requested,
                },
            )
        elif total_filled > 0:
            partial_orders += 1
            database.append_live_order_event(
                intent["id"],
                "PARTIALLY_FILLED",
                {
                    "broker_order_no": broker_order_no,
                    "filled_quantity": total_filled,
                    "requested_quantity": requested,
                    "remaining_quantity": requested - total_filled,
                },
            )
    from .performance import sync_live_trade_outcomes

    outcomes = sync_live_trade_outcomes(database, mission_id)
    return {
        "reconciled": reconciled,
        "unresolved_unknown": unresolved,
        "newly_recorded_fills": newly_recorded_fills,
        "partial_orders": partial_orders,
        "closed_live_outcomes": len(outcomes),
        "retry_submitted": 0,
        "trading_enabled": False,
    }


def evaluate_auto_demotion(database: Database, mission_id: str) -> dict[str, Any]:
    control = database.get_execution_control(mission_id)
    metrics_mode = "L0" if control["stage"] == "L0" else "L1"
    metrics = database.operating_metrics(mission_id, metrics_mode)
    reasons: list[str] = []
    latest = database.get_latest_reconciliation(mission_id)
    if latest and latest["status"] != "PASS":
        reasons.append("잔고 대사 실패")
    if metrics["risk_violations"] > 0:
        reasons.append("위험 한도 위반")
    if metrics["duplicate_orders"] > 0:
        reasons.append("중복 주문 의심")
    if metrics["open_critical_incidents"] > 0:
        reasons.append("미해결 중대 사고")
    if control["stage"] == "L2" and reasons:
        control = database.update_execution_control(
            mission_id,
            actor="EXECUTION_GUARDIAN",
            reason="자동 강등: " + ", ".join(reasons),
            stage="R1",
            kill_switch_active=True,
            automation_enabled=False,
        )
    elif control["stage"] == "L0" and reasons:
        control = database.update_execution_control(
            mission_id,
            actor="EXECUTION_GUARDIAN",
            reason="L0 자동 중지: " + ", ".join(reasons),
            kill_switch_active=True,
            automation_enabled=False,
        )
    return {"demoted": bool(reasons), "reasons": reasons, "control": control}


def _intent_order_window_ended(intent: dict[str, Any], now_kst: datetime) -> bool:
    policy = STRATEGY_EXECUTION_POLICIES.get(str(intent["strategy_id"]))
    if policy is None:
        return False
    window_key = "sell_order_window_kst" if intent["side"] == "SELL" else "order_window_kst"
    _, window_end = policy.get(window_key, policy["order_window_kst"])
    end_hour, end_minute = (int(value) for value in window_end.split(":"))
    created_day = datetime.fromisoformat(intent["created_at"]).astimezone(KST).date()
    return created_day < now_kst.date() or (
        created_day == now_kst.date()
        and now_kst.hour * 60 + now_kst.minute > end_hour * 60 + end_minute
    )


def run_l0_automation_tick(
    database: Database,
    guardian: ExecutionGuardian,
    client: KiwoomReadOnlyClient | None = None,
    *,
    mission_id: str = PILOT_MISSION_ID,
    worker_id: str = "l0-auto-execution",
    now_kst: datetime | None = None,
) -> dict[str, Any]:
    """Execute only post-mandate L0 intents while preserving every Guardian gate."""
    if mission_id != PILOT_MISSION_ID:
        raise ExecutionError("L0 자동 실행은 5만 원 파일럿 원장에만 허용합니다.")
    if not database.acquire_execution_lease(
        "kiwoom-domestic-order-session", owner_id=worker_id, ttl_seconds=30
    ):
        raise ExecutionError("다른 Execution Guardian이 키움 주문 세션을 소유하고 있습니다.")
    demotion = evaluate_auto_demotion(database, mission_id)
    control = demotion["control"]
    if demotion["demoted"]:
        return {
            "action": "AUTOMATION_HALTED",
            "submitted": False,
            "automatic_broker_submission": True,
            **demotion,
        }
    if control["stage"] != "L0" or not control["automation_enabled"]:
        return {
            "action": "AUTOMATION_DISABLED",
            "submitted": False,
            "automatic_broker_submission": False,
            "control": control,
        }
    try:
        guardian.desktop_live.require_armed()
    except DesktopLiveError as error:
        raise ExecutionError(str(error)) from error

    intents = database.list_live_order_intents(mission_id)
    active_states = {"SUBMITTING", "UNKNOWN", "ACKNOWLEDGED", "PARTIALLY_FILLED"}
    reconciliation: dict[str, Any] | None = None
    if client is not None and any(intent["state"] in active_states for intent in intents):
        reconciliation = reconcile_broker_orders(database, client, mission_id=mission_id)
        intents = database.list_live_order_intents(mission_id)

    ambiguous_states = {"SUBMITTING", "UNKNOWN", "CANCEL_UNKNOWN"}
    ambiguous = [intent for intent in intents if intent["state"] in ambiguous_states]
    if ambiguous:
        control = database.update_execution_control(
            mission_id,
            actor="EXECUTION_GUARDIAN",
            reason="응답 불명 주문 대기 — 자동 실행 중지",
            kill_switch_active=True,
            automation_enabled=False,
        )
        return {
            "action": "AMBIGUOUS_ORDER_HALTED",
            "submitted": False,
            "automatic_broker_submission": True,
            "intent_ids": [intent["id"] for intent in ambiguous],
            "control": control,
            "reconciliation": reconciliation,
        }

    now_kst = (now_kst or datetime.now(KST)).astimezone(KST)
    open_orders = sorted(
        (
            intent
            for intent in intents
            if intent["state"] in {"ACKNOWLEDGED", "PARTIALLY_FILLED"}
            and _intent_order_window_ended(intent, now_kst)
        ),
        key=lambda item: item["created_at"],
    )
    if open_orders:
        cancelled = cancel_live_intent(database, guardian, open_orders[0]["id"])
        return {
            "action": cancelled["state"],
            "submitted": False,
            "automatic_broker_submission": True,
            "intent": cancelled,
            "reconciliation": reconciliation,
        }

    candidates = sorted(
        (
            intent
            for intent in intents
            if intent["state"] in {"AWAITING_APPROVAL", "READY"}
            and intent["created_at"] > control["updated_at"]
        ),
        key=lambda item: item["created_at"],
    )
    if not candidates:
        return {
            "action": "NO_ELIGIBLE_INTENT",
            "submitted": False,
            "automatic_broker_submission": True,
            "reconciliation": reconciliation,
        }
    intent = candidates[0]
    if client is not None:
        sync_official_quote(database, client, intent["symbol"])
        reconcile_managed_account(database, client, mission_id=mission_id)
        sync_official_quote(database, client, intent["symbol"])
    if intent["state"] == "AWAITING_APPROVAL":
        intent = approve_live_intent(
            database,
            intent["id"],
            decision="APPROVE",
            scope_hash=intent["scope_hash"],
            actor="OWNER_DELEGATED_L0_AUTO",
            validity_seconds=120,
        )
    submitted = submit_live_intent(database, guardian, intent["id"])
    return {
        "action": submitted["state"],
        "submitted": submitted["state"]
        in {"ACKNOWLEDGED", "PARTIALLY_FILLED", "FILLED", "UNKNOWN"},
        "automatic_broker_submission": True,
        "intent": submitted,
        "reconciliation": reconciliation,
    }


def run_l2_automation_tick(
    database: Database,
    guardian: ExecutionGuardian,
    *,
    mission_id: str,
    worker_id: str,
) -> dict[str, Any]:
    if not database.acquire_execution_lease("kiwoom-domestic-order-session", owner_id=worker_id):
        raise ExecutionError("다른 Execution Guardian이 키움 주문 세션을 소유하고 있습니다.")
    demotion = evaluate_auto_demotion(database, mission_id)
    control = demotion["control"]
    if demotion["demoted"]:
        return {"action": "DEMOTED", **demotion}
    if control["stage"] != "L2" or not control["automation_enabled"]:
        raise ExecutionError("L2 자동 실행이 활성화되지 않았습니다.")
    candidates = [
        order
        for order in database.list_shadow_orders(mission_id)
        if order["simulation_source"] == "KIWOOM_OFFICIAL_QUOTE" and order["state"] == "READY"
    ]
    if not candidates:
        return {"action": "NO_ELIGIBLE_SHADOW_ORDER", "submitted": False}
    shadow = candidates[0]
    intent = build_live_intent(
        database,
        guardian,
        shadow_order_id=shadow["id"],
        idempotency_key=f"l2-auto-{shadow['id']}",
    )
    if intent["state"] == "READY":
        intent = submit_live_intent(database, guardian, intent["id"])
    return {"action": intent["state"], "intent": intent}
