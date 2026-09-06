from __future__ import annotations

import gzip
import hashlib
import json
import math
import re
import statistics
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

STRATEGY_ID = "OPEN-RANGE-KR-v2"
MISSION_ID = "mission_opening_range_001"
PROTOCOL_VERSION = "POINT_IN_TIME_INTRADAY-v2"
KST = timezone(timedelta(hours=9))
AUTHORIZED_INTRADAY_SOURCES = {"LICENSED_INTRADAY_VENDOR", "KIWOOM_REALTIME_ARCHIVE"}


def load_opening_range_archive(
    file_name: str, root: str | Path
) -> dict[str, Any]:
    if not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\.intraday\.json(?:\.gz)?", file_name
    ):
        raise ValueError("장초 아카이브 파일명 형식이 올바르지 않습니다.")
    import_root = Path(root).resolve()
    path = (import_root / file_name).resolve()
    if path.parent != import_root or not path.is_file():
        raise ValueError("장초 아카이브 파일을 가져오기함에서 찾을 수 없습니다.")
    opener = gzip.open if path.suffix == ".gz" else open
    try:
        with opener(path, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("장초 아카이브 JSON을 읽을 수 없습니다.") from error
    if not isinstance(payload, dict):
        raise ValueError("장초 아카이브 최상위 값은 객체여야 합니다.")
    source = payload.get("source")
    if source not in AUTHORIZED_INTRADAY_SOURCES:
        raise ValueError("승인된 장초 분봉·호가·VI 출처가 아닙니다.")
    if payload.get("quality", {}).get("state") != "PASS":
        raise ValueError("장초 아카이브 품질 검사가 PASS가 아닙니다.")
    manifest = payload.get("collection_manifest", {})
    required_manifest = {
        "schema_version": "OPENING_RANGE_ARCHIVE-v1",
        "point_in_time": True,
        "includes_quotes": True,
        "includes_vi": True,
    }
    if any(manifest.get(key) != value for key, value in required_manifest.items()):
        raise ValueError("시점고정·호가·VI 아카이브 manifest가 불완전합니다.")
    sessions = payload.get("sessions")
    if not isinstance(sessions, list) or not sessions:
        raise ValueError("장초 아카이브에 거래일 세션이 없습니다.")
    trade_dates = [str(item.get("trade_date", "")) for item in sessions]
    if len(set(trade_dates)) != len(trade_dates) or any(
        not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) for day in trade_dates
    ):
        raise ValueError("거래일이 없거나 중복된 장초 세션이 있습니다.")
    if any(item.get("source") != source for item in sessions):
        raise ValueError("아카이브와 거래일 세션의 출처가 일치하지 않습니다.")
    supplied_checksum = str(payload.get("checksum", ""))
    checksum_body = {
        key: value for key, value in payload.items() if key not in {"checksum", "id"}
    }
    computed_checksum = hashlib.sha256(
        json.dumps(
            checksum_body,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    if supplied_checksum != computed_checksum:
        raise ValueError("장초 아카이브 SHA-256이 일치하지 않습니다.")
    return {
        **payload,
        "id": f"snap_{computed_checksum[:16]}",
        "checksum": computed_checksum,
    }


@dataclass(frozen=True)
class OpeningRangeConfig:
    observe_minutes: int = 10
    entry_start_kst: str = "09:10"
    entry_end_kst: str = "10:30"
    force_flat_kst: str = "11:00"
    price_floor_krw: int = 2_000
    price_ceiling_krw: int = 50_000
    median_turnover_floor_krw: int = 30_000_000_000
    spread_ceiling_bps: int = 20
    depth_order_multiple: int = 20
    gap_floor_bps: int = 50
    gap_ceiling_bps: int = 400
    range_floor_bps: int = 40
    range_ceiling_bps: int = 200
    relative_turnover_floor: float = 2.0
    breakout_ticks: int = 2
    chase_ceiling_bps: int = 30
    fill_timeout_seconds: int = 5
    runner_activation_r: float = 1.5
    runner_trail_bps: int = 80
    runner_tighten_r: float = 3.0
    runner_tight_trail_bps: int = 50
    runner_stagnation_minutes: int = 7
    time_stop_minutes: int = 10
    time_stop_progress_r: float = 0.3
    max_round_trips: int = 3
    max_trade_risk_bps: int = 50
    second_trade_risk_bps_after_win: int = 25
    daily_loss_limit_bps: int = 100
    daily_new_entry_stop_bps: int = 500
    daily_trail_tighten_bps: int = 1_000
    daily_hard_profit_lock_bps: int = 1_500
    peak_giveback_bps: int = 70
    max_consecutive_losses: int = 2
    max_data_age_seconds: int = 2
    commission_bps_per_side: float = 2.0
    slippage_bps_per_side: float = 5.0
    sell_tax_bps: float = 20.0

    @property
    def round_trip_cost_bps(self) -> float:
        return 2 * self.commission_bps_per_side + 2 * self.slippage_bps_per_side + self.sell_tax_bps


def strategy_spec(config: OpeningRangeConfig | None = None) -> dict[str, Any]:
    config = config or OpeningRangeConfig()
    return {
        "mode_code": "OPENING_RANGE",
        "display_name": "장초 단타",
        "strategy_id": STRATEGY_ID,
        "validation_protocol_version": PROTOCOL_VERSION,
        "market": "KRX",
        "thesis": "첫 10분 가격발견 뒤 유동성·시장 방향·거래량이 확인된 돌파만 당일 거래",
        "clock": {
            "premarket": "08:40~08:55",
            "observe": "09:00~09:10 (주문 금지)",
            "entry": f"{config.entry_start_kst}~{config.entry_end_kst}",
            "force_flat": config.force_flat_kst,
        },
        "universe": {
            "markets": ["KOSPI", "KOSDAQ"],
            "security_type": "COMMON_STOCK_ONLY",
            "price_krw": [config.price_floor_krw, config.price_ceiling_krw],
            "median_daily_turnover_krw": config.median_turnover_floor_krw,
            "spread_ceiling_bps": config.spread_ceiling_bps,
            "depth_order_multiple": config.depth_order_multiple,
            "excluded": [
                "관리·환기·주의·경고·위험",
                "거래정지·정리매매",
                "SPAC·우선주·레버리지",
                "VI 발동 중",
                "신규상장일",
                "상하한가 근접",
            ],
        },
        "entry": {
            "order_type": "LIMIT_ONLY",
            "opening_range_width_pct": [
                config.range_floor_bps / 100,
                config.range_ceiling_bps / 100,
            ],
            "first_10m_turnover_multiple": config.relative_turnover_floor,
            "breakout_ticks": config.breakout_ticks,
            "chase_ceiling_pct": config.chase_ceiling_bps / 100,
            "fill_timeout_seconds": config.fill_timeout_seconds,
            "benchmark_filter": "KODEX 200 > 당일 VWAP",
        },
        "exit": {
            "initial_stop": "max(장초 범위 중간값, 진입가 -0.8%)",
            "runner_activation_r": config.runner_activation_r,
            "partial_take_profit": "2주 이상이면 +1.5R에서 절반, 1주면 분할 없음",
            "runner_trail_pct": config.runner_trail_bps / 100,
            "runner_tighten_r": config.runner_tighten_r,
            "runner_tight_trail_pct": config.runner_tight_trail_bps / 100,
            "runner_stagnation": (
                f"완성 1분봉 신고가 {config.runner_stagnation_minutes}분 부재 + VWAP 하회"
            ),
            "time_stop": (
                f"{config.time_stop_minutes}분 안에 +{config.time_stop_progress_r}R 미도달"
            ),
            "vwap_exit": "VWAP 아래 1분봉 2개 연속 마감",
            "force_flat": config.force_flat_kst,
        },
        "risk_limits": {
            "max_positions": 1,
            "max_round_trips": config.max_round_trips,
            "max_trade_risk_pct": config.max_trade_risk_bps / 100,
            "daily_loss_limit_pct": config.daily_loss_limit_bps / 100,
            "daily_new_entry_stop_pct": config.daily_new_entry_stop_bps / 100,
            "daily_trail_tighten_pct": config.daily_trail_tighten_bps / 100,
            "daily_hard_profit_lock_pct": config.daily_hard_profit_lock_bps / 100,
            "max_consecutive_losses": config.max_consecutive_losses,
            "averaging_down": False,
            "overnight_position": False,
        },
        "cost_assumption_bps": config.round_trip_cost_bps,
        "promotion_gate": "2년 시점고정 분봉 OOS + 60거래일·300신호 그림자",
        "broker_submitted": False,
        "trading_enabled": False,
    }


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


def _ceil_tick(price: float) -> int:
    tick = _tick_size(round(price))
    return math.ceil(price / tick) * tick


def _floor_tick(price: float) -> int:
    tick = _tick_size(round(price))
    return math.floor(price / tick) * tick


def _time_of(bar: dict[str, Any]) -> str:
    return datetime.fromisoformat(str(bar["timestamp"])).astimezone(KST).strftime("%H:%M")


def _vwap(bars: list[dict[str, Any]]) -> float:
    volume = sum(int(bar["volume"]) for bar in bars)
    return sum(float(bar["close"]) * int(bar["volume"]) for bar in bars) / volume if volume else 0.0


def _safe_flags(instrument: dict[str, Any]) -> bool:
    flags = instrument.get("designation", {})
    forbidden = (
        "managed",
        "watchlist",
        "caution",
        "warning",
        "danger",
        "halted",
        "liquidation",
        "vi_active",
        "new_listing",
    )
    return not any(bool(flags.get(key, False)) for key in forbidden)


def _validate_session(snapshot: dict[str, Any]) -> None:
    if snapshot.get("quality", {}).get("state") != "PASS":
        raise ValueError("장초 세션 데이터 품질이 PASS가 아닙니다.")
    if not snapshot.get("benchmark", {}).get("bars"):
        raise ValueError("장초 세션에 벤치마크 분봉이 없습니다.")
    for instrument in snapshot.get("instruments", []):
        bars = instrument.get("bars", [])
        if len(bars) < 11:
            raise ValueError(
                f"{instrument.get('symbol', 'UNKNOWN')}의 09:10까지 분봉이 부족합니다."
            )
        timestamps = [datetime.fromisoformat(str(bar["timestamp"])) for bar in bars]
        if timestamps != sorted(timestamps) or len(set(timestamps)) != len(timestamps):
            raise ValueError("분봉 시각은 중복 없이 오름차순이어야 합니다.")
        if any(
            (right - left).total_seconds() > 60
            for left, right in zip(timestamps, timestamps[1:], strict=False)
        ):
            raise ValueError("1분보다 긴 데이터 공백이 있어 세션을 닫았습니다.")


def _candidate(
    instrument: dict[str, Any], benchmark: dict[str, Any], config: OpeningRangeConfig
) -> dict[str, Any]:
    bars = instrument["bars"]
    opening = [bar for bar in bars if "09:00" <= _time_of(bar) < config.entry_start_kst]
    entry_bars = [
        bar for bar in bars if config.entry_start_kst <= _time_of(bar) <= config.entry_end_kst
    ]
    if len(opening) != config.observe_minutes or not entry_bars:
        return {"eligible": False, "failed": ["opening_window_incomplete"]}
    previous_close = float(instrument["previous_close"])
    open_price = float(opening[0]["open"])
    opening_high = max(float(bar["high"]) for bar in opening)
    opening_low = min(float(bar["low"]) for bar in opening)
    opening_mid = (opening_high + opening_low) / 2
    range_bps = (opening_high - opening_low) / open_price * 10_000
    gap_bps = abs(open_price / previous_close - 1) * 10_000
    opening_turnover = sum(
        int(bar.get("turnover_krw", float(bar["close"]) * int(bar["volume"]))) for bar in opening
    )
    median_first10 = max(int(instrument.get("median_first10_turnover_krw", 0)), 1)
    relative_turnover = opening_turnover / median_first10
    benchmark_bars = [bar for bar in benchmark["bars"] if _time_of(bar) <= config.entry_start_kst]
    benchmark_last = benchmark_bars[-1]
    benchmark_pass = float(benchmark_last["close"]) > _vwap(benchmark_bars)
    trigger = _ceil_tick(opening_high + config.breakout_ticks * _tick_size(round(opening_high)))
    failed: list[str] = []
    if (
        instrument.get("market") not in {"KOSPI", "KOSDAQ"}
        or instrument.get("security_type") != "COMMON"
    ):
        failed.append("common_stock_only")
    if not config.price_floor_krw <= previous_close <= config.price_ceiling_krw:
        failed.append("price_band")
    if int(instrument.get("median_daily_turnover_krw", 0)) < config.median_turnover_floor_krw:
        failed.append("daily_liquidity")
    if not config.gap_floor_bps <= gap_bps <= config.gap_ceiling_bps:
        failed.append("opening_gap")
    if not config.range_floor_bps <= range_bps <= config.range_ceiling_bps:
        failed.append("opening_range_width")
    if relative_turnover < config.relative_turnover_floor:
        failed.append("relative_turnover")
    if not benchmark_pass:
        failed.append("benchmark_below_vwap")
    if not _safe_flags(instrument):
        failed.append("designation_or_vi")
    signal_bar: dict[str, Any] | None = None
    for index, bar in enumerate(entry_bars):
        history = opening + entry_bars[: index + 1]
        bid, ask = float(bar.get("bid", 0)), float(bar.get("ask", 0))
        mid = (bid + ask) / 2 if bid and ask else 0
        spread_bps = (ask - bid) / mid * 10_000 if mid else float("inf")
        depth_krw = min(int(bar.get("bid_depth_krw", 0)), int(bar.get("ask_depth_krw", 0)))
        close = float(bar["close"])
        if (
            close >= trigger
            and close > _vwap(history)
            and close <= trigger * (1 + config.chase_ceiling_bps / 10_000)
            and spread_bps <= config.spread_ceiling_bps
            and depth_krw >= config.depth_order_multiple * 100_000
            and not bool(bar.get("vi_active", False))
        ):
            signal_bar = bar
            break
    if signal_bar is None:
        failed.append("no_executable_breakout")
    return {
        "eligible": not failed,
        "failed": failed,
        "symbol": instrument["symbol"],
        "name": instrument["name"],
        "opening_high_krw": round(opening_high),
        "opening_low_krw": round(opening_low),
        "opening_mid_krw": round(opening_mid),
        "range_pct": round(range_bps / 100, 2),
        "gap_pct": round((open_price / previous_close - 1) * 100, 2),
        "relative_turnover": round(relative_turnover, 2),
        "trigger_price_krw": trigger,
        "signal_at": signal_bar["timestamp"] if signal_bar else None,
        "signal_bar": signal_bar,
    }


def _simulate_trade(
    instrument: dict[str, Any],
    candidate: dict[str, Any],
    equity_krw: int,
    session_start_equity_krw: int,
    realized_daily_pnl_krw: int,
    config: OpeningRangeConfig,
) -> dict[str, Any]:
    bars = instrument["bars"]
    signal_index = next(
        index for index, bar in enumerate(bars) if bar["timestamp"] == candidate["signal_at"]
    )
    limit_price = int(candidate["trigger_price_krw"])
    fill_bar = next(
        (
            bar
            for bar in bars[signal_index + 1 : signal_index + 2]
            if float(bar["low"]) <= limit_price <= float(bar["high"])
        ),
        None,
    )
    if fill_bar is None:
        return {
            "symbol": instrument["symbol"],
            "name": instrument["name"],
            "state": "SHADOW_CANCELLED",
            "signal_at": candidate["signal_at"],
            "limit_price_krw": limit_price,
            "reason": (
                f"{config.fill_timeout_seconds}초 체결 조건을 분봉에서 보수적으로 확인하지 못함"
            ),
            "broker_submitted": False,
            "trading_enabled": False,
        }
    entry = _ceil_tick(limit_price * (1 + config.slippage_bps_per_side / 10_000))
    stop = _floor_tick(max(float(candidate["opening_mid_krw"]), entry * 0.992))
    per_share_risk = max(entry - stop, _tick_size(entry))
    risk_budget = equity_krw * config.max_trade_risk_bps / 10_000
    quantity = min(int(equity_krw // entry), int(risk_budget // per_share_risk))
    if quantity < 1:
        return {
            "symbol": instrument["symbol"],
            "name": instrument["name"],
            "state": "RISK_REJECTED",
            "signal_at": candidate["signal_at"],
            "limit_price_krw": limit_price,
            "reason": "현금 또는 0.5% 계획손실 한도 안에서 1주를 살 수 없음",
            "broker_submitted": False,
            "trading_enabled": False,
        }
    runner_activation = _ceil_tick(entry + config.runner_activation_r * per_share_risk)
    fill_index = bars.index(fill_bar)
    exit_price, exit_at, exit_reason = entry, fill_bar["timestamp"], "FORCE_FLAT"
    below_vwap = 0
    max_favorable = 0.0
    runner_activated = False
    runner_activated_at: str | None = None
    high_watermark_close = 0
    last_high_watermark_minute = 0
    tightened_trail = False
    partial_exits: list[dict[str, Any]] = []
    remaining_quantity = quantity

    def estimated_trade_net(mark_price: int) -> int:
        sell_legs = partial_exits + [{"price_krw": mark_price, "quantity": remaining_quantity}]
        gross = sum(
            (int(leg["price_krw"]) - entry) * int(leg["quantity"]) for leg in sell_legs
        )
        costs = sum(
            round(
                (entry + int(leg["price_krw"]))
                * int(leg["quantity"])
                * config.round_trip_cost_bps
                / 20_000
            )
            for leg in sell_legs
        )
        return gross - costs

    for held_minutes, bar in enumerate(bars[fill_index + 1 :], start=1):
        if _time_of(bar) > config.force_flat_kst:
            break
        history = bars[: bars.index(bar) + 1]
        close = float(bar["close"])
        close_tick = _floor_tick(close)
        current_vwap = _vwap(history)
        max_favorable = max(max_favorable, float(bar["high"]) - entry)
        below_vwap = below_vwap + 1 if close < current_vwap else 0
        if float(bar["low"]) <= stop:
            exit_price, exit_reason = stop, "STOP_LOSS"
        elif not runner_activated and (
            held_minutes >= config.time_stop_minutes
            and max_favorable < config.time_stop_progress_r * per_share_risk
        ):
            exit_price, exit_reason = close_tick, "TIME_STOP"
        elif below_vwap >= 2:
            exit_price, exit_reason = close_tick, "VWAP_EXIT"
        elif _time_of(bar) == config.force_flat_kst:
            exit_price, exit_reason = close_tick, "FORCE_FLAT"
        else:
            if not runner_activated and close >= runner_activation:
                runner_activated = True
                runner_activated_at = bar["timestamp"]
                high_watermark_close = close_tick
                last_high_watermark_minute = held_minutes
                if quantity >= 2:
                    partial_quantity = quantity // 2
                    partial_exits.append(
                        {
                            "at": bar["timestamp"],
                            "price_krw": runner_activation,
                            "quantity": partial_quantity,
                            "reason": "PARTIAL_AT_1_5R",
                        }
                    )
                    remaining_quantity -= partial_quantity
            if not runner_activated:
                continue
            if close_tick > high_watermark_close:
                high_watermark_close = close_tick
                last_high_watermark_minute = held_minutes
            current_r = (high_watermark_close - entry) / per_share_risk
            marked_daily_pnl = realized_daily_pnl_krw + estimated_trade_net(close_tick)
            if (
                marked_daily_pnl
                >= session_start_equity_krw * config.daily_hard_profit_lock_bps / 10_000
            ):
                exit_price, exit_reason = close_tick, "DAILY_HARD_PROFIT_LOCK"
            else:
                tightened_trail = tightened_trail or (
                    current_r >= config.runner_tighten_r
                    or marked_daily_pnl
                    >= session_start_equity_krw * config.daily_trail_tighten_bps / 10_000
                )
                trail_bps = (
                    config.runner_tight_trail_bps
                    if tightened_trail
                    else config.runner_trail_bps
                )
                trailing_stop = _floor_tick(high_watermark_close * (1 - trail_bps / 10_000))
                if close_tick <= trailing_stop:
                    exit_price, exit_reason = close_tick, "RUNNER_TRAILING_STOP"
                elif (
                    held_minutes - last_high_watermark_minute
                    >= config.runner_stagnation_minutes
                    and close < current_vwap
                ):
                    exit_price, exit_reason = close_tick, "RUNNER_STAGNATION_EXIT"
                else:
                    continue
        exit_at = bar["timestamp"]
        break
    sell_legs = partial_exits + [
        {
            "at": exit_at,
            "price_krw": exit_price,
            "quantity": remaining_quantity,
            "reason": exit_reason,
        }
    ]
    gross_pnl = sum(
        (int(leg["price_krw"]) - entry) * int(leg["quantity"]) for leg in sell_legs
    )
    costs = sum(
        round(
            (entry + int(leg["price_krw"]))
            * int(leg["quantity"])
            * config.round_trip_cost_bps
            / 20_000
        )
        for leg in sell_legs
    )
    net_pnl = gross_pnl - costs
    return {
        "symbol": instrument["symbol"],
        "name": instrument["name"],
        "state": "SHADOW_FILLED_AND_EXITED",
        "signal_at": candidate["signal_at"],
        "entry_at": fill_bar["timestamp"],
        "exit_at": exit_at,
        "quantity": quantity,
        "limit_price_krw": limit_price,
        "entry_price_krw": entry,
        "stop_price_krw": stop,
        "runner_activation_price_krw": runner_activation,
        "runner_activated": runner_activated,
        "runner_activated_at": runner_activated_at,
        "runner_high_watermark_krw": high_watermark_close or None,
        "runner_trail_tightened": tightened_trail,
        "partial_exits": partial_exits,
        "exit_legs": sell_legs,
        "exit_price_krw": exit_price,
        "exit_reason": exit_reason,
        "planned_risk_krw": per_share_risk * quantity,
        "gross_pnl_krw": gross_pnl,
        "cost_krw": costs,
        "net_pnl_krw": net_pnl,
        "result_r": round(net_pnl / (per_share_risk * quantity), 3),
        "broker_submitted": False,
        "trading_enabled": False,
    }


def build_opening_range_signal_plan(
    mission: dict[str, Any],
    snapshot: dict[str, Any],
    config: OpeningRangeConfig | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Create a current-bar order plan without using any future bar or broker capability."""
    config = config or OpeningRangeConfig()
    _validate_session(snapshot)
    if snapshot.get("source") != "KIWOOM_REALTIME_ARCHIVE":
        raise ValueError("정규화된 키움 실시간 아카이브만 장초 주문안에 사용할 수 있습니다.")
    current = (now or datetime.now(KST)).astimezone(KST)
    current_minute = current.strftime("%H:%M")
    if not config.entry_start_kst <= current_minute <= config.entry_end_kst:
        raise ValueError("장초 신규 주문안은 09:10~10:30에만 만들 수 있습니다.")
    candidates = [
        _candidate(item, snapshot["benchmark"], config) for item in snapshot["instruments"]
    ]
    candidates.sort(
        key=lambda item: (item.get("eligible", False), item.get("relative_turnover", 0)),
        reverse=True,
    )
    candidate = next((item for item in candidates if item.get("eligible")), None)
    if candidate is None:
        raise ValueError("현재 분봉에 실행 가능한 장초 돌파가 없습니다.")
    signal_at = datetime.fromisoformat(str(candidate["signal_at"])).astimezone(KST)
    if signal_at.date() != current.date() or abs((current - signal_at).total_seconds()) > 90:
        raise ValueError("현재 완성 분봉의 신규 신호가 아니므로 과거 신호를 재사용하지 않습니다.")
    instrument = next(
        item for item in snapshot["instruments"] if item["symbol"] == candidate["symbol"]
    )
    if instrument["bars"][-1]["timestamp"] != candidate["signal_at"]:
        raise ValueError("첫 돌파 이후의 오래된 신호는 새 주문안으로 만들지 않습니다.")
    limit_price = int(candidate["trigger_price_krw"])
    stop_price = _floor_tick(
        max(float(candidate["opening_mid_krw"]), limit_price * 0.992)
    )
    per_share_risk = max(limit_price - stop_price, _tick_size(limit_price))
    risk_budget = int(mission["equity_krw"] * config.max_trade_risk_bps / 10_000)
    quantity = min(
        int(mission["available_krw"] // limit_price),
        int(risk_budget // per_share_risk),
    )
    if quantity < 1:
        raise ValueError("현금과 0.5% 계획손실 한도 안에서 1주를 살 수 없습니다.")
    material = {
        "mission_id": mission["id"],
        "snapshot_id": snapshot["id"],
        "strategy_id": STRATEGY_ID,
        "signal_at": candidate["signal_at"],
        "symbol": candidate["symbol"],
        "quantity": quantity,
        "limit_price_krw": limit_price,
        "stop_price_krw": stop_price,
    }
    digest = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "id": f"cycle_{digest[:16]}",
        "mission_id": mission["id"],
        "snapshot_id": snapshot["id"],
        "strategy_id": STRATEGY_ID,
        "protocol_version": PROTOCOL_VERSION,
        "as_of": snapshot["as_of"],
        "source": snapshot["source"],
        "status": "SIGNAL_READY",
        "candidates": candidates,
        "reports": [],
        "risk": {
            "result": "PASS_SHADOW_OR_GUARDIAN_ONLY",
            "quantity": quantity,
            "risk_budget_krw": risk_budget,
            "planned_risk_krw": quantity * per_share_risk,
            "order_allowed": False,
            "reasons": ["R1은 그림자만, L1/L2는 Execution Guardian 재검사가 필요합니다."],
        },
        "decision": {
            "action": "OPENING_RANGE_SIGNAL_READY",
            "entry_style": "OPENING_RANGE_BREAKOUT_LIMIT",
            "symbol": candidate["symbol"],
            "name": candidate["name"],
            "quantity": quantity,
            "limit_price_krw": limit_price,
            "invalidation_price_krw": stop_price,
            "order_allowed": False,
            "reason": "현재 완성 1분봉 장초 돌파가 결정론적 조건을 통과했습니다.",
        },
        "broker_submitted": False,
        "trading_enabled": False,
    }


def run_opening_range_session(
    mission: dict[str, Any], snapshot: dict[str, Any], config: OpeningRangeConfig | None = None
) -> dict[str, Any]:
    config = config or OpeningRangeConfig()
    _validate_session(snapshot)
    candidates = [
        _candidate(item, snapshot["benchmark"], config) for item in snapshot["instruments"]
    ]
    candidates.sort(
        key=lambda item: (item.get("eligible", False), item.get("relative_turnover", 0)),
        reverse=True,
    )
    trades: list[dict[str, Any]] = []
    equity = int(mission["equity_krw"])
    consecutive_losses = 0
    peak_pnl = 0
    stop_reason = "NO_MORE_SIGNALS"
    by_symbol = {item["symbol"]: item for item in snapshot["instruments"]}
    for candidate in candidates:
        if not candidate.get("eligible") or len(trades) >= config.max_round_trips:
            continue
        daily_realized_pnl = equity - int(mission["equity_krw"])
        trade = _simulate_trade(
            by_symbol[candidate["symbol"]],
            candidate,
            equity,
            int(mission["equity_krw"]),
            daily_realized_pnl,
            config,
        )
        trades.append(trade)
        net = int(trade.get("net_pnl_krw", 0))
        equity += net
        peak_pnl = max(peak_pnl, equity - int(mission["equity_krw"]))
        consecutive_losses = consecutive_losses + 1 if net < 0 else 0
        daily_pnl = equity - int(mission["equity_krw"])
        if daily_pnl <= -int(mission["equity_krw"] * config.daily_loss_limit_bps / 10_000):
            stop_reason = "DAILY_LOSS_LIMIT"
            break
        if consecutive_losses >= config.max_consecutive_losses:
            stop_reason = "TWO_CONSECUTIVE_LOSSES"
            break
        if trade.get("exit_reason") == "DAILY_HARD_PROFIT_LOCK":
            stop_reason = "DAILY_HARD_PROFIT_LOCK"
            break
        if daily_pnl >= int(
            mission["equity_krw"] * config.daily_new_entry_stop_bps / 10_000
        ):
            stop_reason = "DAILY_NEW_ENTRY_PROFIT_STOP"
            break
        if peak_pnl - daily_pnl >= int(mission["equity_krw"] * config.peak_giveback_bps / 10_000):
            stop_reason = "PEAK_GIVEBACK_STOP"
            break
    completed = [trade for trade in trades if trade["state"] == "SHADOW_FILLED_AND_EXITED"]
    net_pnl = sum(int(trade["net_pnl_krw"]) for trade in completed)
    material = {
        "mission_id": mission["id"],
        "snapshot_id": snapshot["id"],
        "strategy_id": STRATEGY_ID,
        "config": asdict(config),
        "candidates": candidates,
        "trades": trades,
    }
    digest = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    reports = [
        ("DATA", "김데이터", "분봉·호가·VI 정렬", "누락·중복·시간 역전을 검사했습니다."),
        ("NOVA", "김뉴스", "장전 공시 필터", "장전 금지 종목 표시는 입력 스냅샷에서만 읽습니다."),
        (
            "PULSE",
            "김차트",
            "장초 범위·VWAP",
            f"실행 가능 돌파 {sum(bool(c.get('eligible')) for c in candidates)}건입니다.",
        ),
        ("BULL", "김찬성", "찬성 사전조건", "거래량과 시장 방향 동시 확인 때만 후보입니다."),
        ("BEAR", "김반대", "가짜 돌파 점검", "추격·넓은 스프레드·VI를 거부했습니다."),
        (
            "RISK",
            "김안전",
            "수량·일손실",
            f"완결 거래 {len(completed)}건, 순손익 {net_pnl:,}원입니다.",
        ),
        ("ACE", "김투자", "그림자 결정", "R0에서는 그림자 결정만 기록합니다."),
        ("OPS", "김주문", "가상 체결·청산", "브로커 주문 제출은 0건입니다."),
    ]
    return {
        "id": f"cycle_{digest[:16]}",
        "mission_id": mission["id"],
        "snapshot_id": snapshot["id"],
        "as_of": snapshot["as_of"],
        "source": snapshot["source"],
        "strategy_id": STRATEGY_ID,
        "protocol_version": PROTOCOL_VERSION,
        "status": "COMPLETE",
        "mode": strategy_spec(config),
        "config": asdict(config),
        "candidates": candidates,
        "shadow_trades": trades,
        "session": {
            "round_trips": len(completed),
            "net_pnl_krw": net_pnl,
            "stop_reason": stop_reason,
            "broker_order_count": 0,
            "force_flat_verified": all(
                t.get("exit_at", "") <= f"{snapshot['trade_date']}T{config.force_flat_kst}:00+09:00"
                for t in completed
            ),
        },
        "backtest": {
            "status": "NOT_ELIGIBLE",
            "trade_count": len(completed),
            "net_return_pct": round(net_pnl / mission["equity_krw"] * 100, 3),
            "max_drawdown_pct": min(0, round(net_pnl / mission["equity_krw"] * 100, 3)),
            "win_rate_pct": round(
                sum(t["net_pnl_krw"] > 0 for t in completed) / len(completed) * 100, 1
            )
            if completed
            else 0,
            "sharpe": 0,
            "oos_validated": False,
        },
        "reports": [
            {
                "code": code,
                "name": name,
                "role": role,
                "state": "READY" if code == "OPS" else "DONE",
                "summary": summary,
                "claims": [],
                "evidence_ids": ["ev_opening_range_session"],
                "unknowns": [],
                "invalidation_conditions": ["입력 데이터·위험 한도 변경"],
                "confidence": 1.0 if code in {"DATA", "RISK", "OPS"} else 0.7,
            }
            for code, name, role, summary in reports
        ],
        "risk": {
            "result": "PASS_SHADOW_ONLY" if completed else "HOLD",
            "quantity": completed[0]["quantity"] if completed else 0,
            "entry_reference_krw": completed[0]["entry_price_krw"] if completed else 0,
            "invalidation_price_krw": completed[0]["stop_price_krw"] if completed else 0,
            "risk_budget_krw": round(mission["equity_krw"] * config.max_trade_risk_bps / 10_000),
            "order_allowed": False,
            "reasons": ["R0/R1 그림자 세션이며 브로커 주문 권한이 없습니다."],
        },
        "decision": {
            "action": "SHADOW_SESSION_COMPLETE" if completed else "HOLD",
            "symbol": completed[0]["symbol"] if completed else "",
            "name": completed[0]["name"] if completed else "후보 없음",
            "score": 100 if completed else 0,
            "quantity": completed[0]["quantity"] if completed else 0,
            "order_allowed": False,
            "reason": "규칙 기반 가상 진입·당일 청산을 완료했습니다."
            if completed
            else "실행 가능한 돌파가 없습니다.",
        },
        "evidence": snapshot.get("evidence", []),
        "broker_submitted": False,
        "trading_enabled": False,
    }


def validation_report(snapshot: dict[str, Any], cycle: dict[str, Any]) -> dict[str, Any]:
    completed = [
        item for item in cycle["shadow_trades"] if item["state"] == "SHADOW_FILLED_AND_EXITED"
    ]
    trade_count = len(completed)
    initial_equity = int(cycle.get("initial_equity_krw", 100_000))
    strategy_trials = int(cycle.get("validation_evidence", {}).get("strategy_trials", 3))
    metrics = _opening_performance_metrics(
        completed,
        initial_equity_krw=initial_equity,
        strategy_trials=strategy_trials,
    )
    sensitivity = cycle.get("validation_evidence", {}).get("parameter_sensitivity", {})
    double_slippage = cycle.get("validation_evidence", {}).get("double_slippage", {})
    history = snapshot.get("history", {})
    gates = {
        "point_in_time_two_year_intraday_history": int(
            history.get("trading_days", 0)
        )
        >= 504,
        "final_126_sessions_untouched": bool(history.get("final_126_untouched", False)),
        "minimum_300_oos_trades": trade_count >= 300,
        "expectancy_at_least_0_15r": metrics["expectancy_r"] >= 0.15,
        "sharpe_at_least_1_2": metrics["sharpe"] >= 1.2,
        "deflated_sharpe_95pct": metrics["deflated_sharpe_probability"] >= 0.95,
        "profit_factor_at_least_1_2": metrics["profit_factor"] >= 1.2,
        "max_drawdown_within_8pct": metrics["max_drawdown_pct"] >= -8,
        "worst_month_within_4pct": metrics["worst_month_pct"] >= -4,
        "parameter_robustness_plus_minus_20pct": bool(sensitivity)
        and all(
            int(item.get("trade_count", 0)) >= 100
            and float(item.get("expectancy_r", 0)) > 0
            for item in sensitivity.values()
        ),
        "double_slippage_expectancy_positive": bool(double_slippage)
        and float(double_slippage.get("expectancy_r", 0)) > 0,
        "zero_broker_orders_during_research": cycle["session"]["broker_order_count"] == 0,
        "all_positions_flat_by_1100": cycle["session"]["force_flat_verified"],
        "authorized_non_fixture_source": snapshot["source"]
        in {"LICENSED_INTRADAY_VENDOR", "KIWOOM_REALTIME_ARCHIVE"},
    }
    material = {
        "snapshot_id": snapshot["id"],
        "strategy_id": STRATEGY_ID,
        "gates": gates,
        "trades": completed,
    }
    digest = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "id": f"validation_{digest[:16]}",
        "snapshot_id": snapshot["id"],
        "strategy_id": STRATEGY_ID,
        "protocol_version": PROTOCOL_VERSION,
        "as_of": snapshot["as_of"],
        "source": snapshot["source"],
        "status": "ELIGIBLE_FOR_R1_REVIEW" if all(gates.values()) else "NOT_ELIGIBLE",
        "promotion_eligible": all(gates.values()),
        "method": "PURGED_WALK_FORWARD_INTRADAY",
        "config": cycle.get("config", asdict(OpeningRangeConfig())),
        "history": {
            "trading_days": int(history.get("trading_days", 1)),
            "start": history.get("start", snapshot.get("trade_date")),
            "end": history.get("end", snapshot.get("trade_date")),
            "oos_days": int(history.get("oos_days", 0)),
        },
        "metrics": metrics,
        "sensitivity": sensitivity,
        "double_slippage": double_slippage,
        "gates": gates,
        "failed_gates": [key for key, passed in gates.items() if not passed],
        "trade_sample": completed[:20],
        "trading_enabled": False,
    }


def _moments(values: list[float]) -> tuple[float, float]:
    if len(values) < 3:
        return 0.0, 3.0
    mean = statistics.fmean(values)
    variance = statistics.fmean((value - mean) ** 2 for value in values)
    if variance == 0:
        return 0.0, 3.0
    deviation = math.sqrt(variance)
    skew = statistics.fmean(((value - mean) / deviation) ** 3 for value in values)
    kurtosis = statistics.fmean(((value - mean) / deviation) ** 4 for value in values)
    return skew, kurtosis


def _deflated_sharpe_probability(returns: list[float], strategy_trials: int) -> float:
    if len(returns) < 3:
        return 0.0
    mean = statistics.fmean(returns)
    deviation = statistics.stdev(returns)
    if deviation == 0:
        return 1.0 if mean > 0 else 0.0
    observed = mean / deviation
    skew, kurtosis = _moments(returns)
    variance = max(
        (1 - skew * observed + ((kurtosis - 1) / 4) * observed**2)
        / (len(returns) - 1),
        1e-12,
    )
    expected_max = 0.0
    if strategy_trials > 1:
        normal = statistics.NormalDist()
        gamma = 0.5772156649
        expected_max = math.sqrt(variance) * (
            (1 - gamma) * normal.inv_cdf(1 - 1 / strategy_trials)
            + gamma * normal.inv_cdf(1 - 1 / (strategy_trials * math.e))
        )
    z_score = (observed - expected_max) / math.sqrt(variance)
    return statistics.NormalDist().cdf(z_score)


def _opening_performance_metrics(
    trades: list[dict[str, Any]],
    *,
    initial_equity_krw: int,
    strategy_trials: int,
) -> dict[str, Any]:
    ordered = sorted(trades, key=lambda item: str(item.get("exit_at", "")))
    daily_pnl: dict[str, int] = {}
    monthly_pnl: dict[str, int] = {}
    gains = losses = 0
    result_rs: list[float] = []
    for trade in ordered:
        pnl = int(trade.get("net_pnl_krw", 0))
        exit_at = str(trade.get("exit_at", ""))
        day = exit_at[:10] if len(exit_at) >= 10 else "UNKNOWN"
        month = exit_at[:7] if len(exit_at) >= 7 else "UNKNOWN"
        daily_pnl[day] = daily_pnl.get(day, 0) + pnl
        monthly_pnl[month] = monthly_pnl.get(month, 0) + pnl
        gains += max(pnl, 0)
        losses += abs(min(pnl, 0))
        result_rs.append(float(trade.get("result_r", 0)))

    equity = peak = float(max(initial_equity_krw, 1))
    max_drawdown = 0.0
    daily_returns: list[float] = []
    for day in sorted(daily_pnl):
        starting_equity = max(equity, 1.0)
        equity += daily_pnl[day]
        daily_returns.append(daily_pnl[day] / starting_equity)
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity / peak - 1)
    deviation = statistics.stdev(daily_returns) if len(daily_returns) > 1 else 0.0
    sharpe = (
        statistics.fmean(daily_returns) / deviation * math.sqrt(252)
        if deviation
        else 0.0
    )
    worst_month = (
        min(monthly_pnl.values()) / max(initial_equity_krw, 1) * 100
        if monthly_pnl
        else 0.0
    )
    return {
        "trade_count": len(ordered),
        "net_return_pct": round((equity / max(initial_equity_krw, 1) - 1) * 100, 3),
        "win_rate_pct": round(
            sum(int(item.get("net_pnl_krw", 0)) > 0 for item in ordered)
            / len(ordered)
            * 100,
            1,
        )
        if ordered
        else 0.0,
        "max_drawdown_pct": round(max_drawdown * 100, 3),
        "worst_month_pct": round(worst_month, 3),
        "sharpe": round(sharpe, 3),
        "information_ratio": 0.0,
        "deflated_sharpe_probability": round(
            _deflated_sharpe_probability(daily_returns, strategy_trials), 4
        ),
        "expectancy_r": round(statistics.fmean(result_rs), 3) if result_rs else 0.0,
        "profit_factor": round(gains / losses, 3) if losses else (999 if gains else 0),
    }


def _scaled_opening_config(config: OpeningRangeConfig, factor: float) -> OpeningRangeConfig:
    return replace(
        config,
        spread_ceiling_bps=max(1, round(config.spread_ceiling_bps * factor)),
        gap_floor_bps=max(1, round(config.gap_floor_bps * factor)),
        gap_ceiling_bps=max(1, round(config.gap_ceiling_bps * factor)),
        range_floor_bps=max(1, round(config.range_floor_bps * factor)),
        range_ceiling_bps=max(1, round(config.range_ceiling_bps * factor)),
        relative_turnover_floor=max(0.1, config.relative_turnover_floor * factor),
        chase_ceiling_bps=max(1, round(config.chase_ceiling_bps * factor)),
    )


def run_opening_range_oos_archive(
    mission: dict[str, Any],
    snapshot: dict[str, Any],
    config: OpeningRangeConfig | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    config = config or OpeningRangeConfig()
    sessions = sorted(snapshot.get("sessions", []), key=lambda item: item["trade_date"])
    if not sessions:
        raise ValueError("장초 OOS 아카이브에 거래일 세션이 없습니다.")
    if snapshot.get("source") not in {"LICENSED_INTRADAY_VENDOR", "KIWOOM_REALTIME_ARCHIVE"}:
        raise ValueError("승인된 장초 분봉·호가·VI 아카이브가 아닙니다.")
    if any(item.get("source") != snapshot.get("source") for item in sessions):
        raise ValueError("아카이브와 거래일 세션의 출처가 일치하지 않습니다.")

    oos_sessions = sessions[-126:]

    def run_variant(variant: OpeningRangeConfig) -> list[dict[str, Any]]:
        equity = int(mission["equity_krw"])
        trades: list[dict[str, Any]] = []
        for session in oos_sessions:
            session_mission = {**mission, "equity_krw": max(equity, 1)}
            result = run_opening_range_session(session_mission, session, variant)
            completed = [
                item
                for item in result["shadow_trades"]
                if item["state"] == "SHADOW_FILLED_AND_EXITED"
            ]
            trades.extend(completed)
            equity += sum(int(item["net_pnl_krw"]) for item in completed)
        return trades

    base_trades = run_variant(config)
    sensitivity: dict[str, dict[str, Any]] = {}
    for label, factor in (("minus_20pct", 0.8), ("plus_20pct", 1.2)):
        variant_trades = run_variant(_scaled_opening_config(config, factor))
        sensitivity[label] = _opening_performance_metrics(
            variant_trades,
            initial_equity_krw=int(mission["equity_krw"]),
            strategy_trials=3,
        )
    double_slippage_trades = run_variant(
        replace(config, slippage_bps_per_side=config.slippage_bps_per_side * 2)
    )
    double_slippage = _opening_performance_metrics(
        double_slippage_trades,
        initial_equity_krw=int(mission["equity_krw"]),
        strategy_trials=3,
    )
    material = {
        "snapshot_id": snapshot["id"],
        "strategy_id": STRATEGY_ID,
        "config": asdict(config),
        "trade_ids": [
            [item.get("symbol"), item.get("entry_at"), item.get("exit_at")]
            for item in base_trades
        ],
        "sensitivity": sensitivity,
        "double_slippage": double_slippage,
    }
    digest = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    cycle = {
        "id": f"cycle_{digest[:16]}",
        "mission_id": mission["id"],
        "snapshot_id": snapshot["id"],
        "as_of": snapshot["as_of"],
        "source": snapshot["source"],
        "strategy_id": STRATEGY_ID,
        "protocol_version": PROTOCOL_VERSION,
        "status": "COMPLETE",
        "config": asdict(config),
        "initial_equity_krw": int(mission["equity_krw"]),
        "shadow_trades": base_trades,
        "session": {
            "round_trips": len(base_trades),
            "net_pnl_krw": sum(int(item["net_pnl_krw"]) for item in base_trades),
            "broker_order_count": 0,
            "force_flat_verified": all(
                str(item.get("exit_at", ""))[11:16] <= config.force_flat_kst
                for item in base_trades
            ),
        },
        "validation_evidence": {
            "strategy_trials": 3,
            "parameter_sensitivity": sensitivity,
            "double_slippage": double_slippage,
        },
        "decision": {
            "action": "OOS_VALIDATION_COMPLETE",
            "order_allowed": False,
            "reason": "봉인된 최종 126거래일 OOS만 평가했습니다.",
        },
        "broker_submitted": False,
        "trading_enabled": False,
    }
    archive_snapshot = {
        **snapshot,
        "history": {
            **snapshot.get("history", {}),
            "trading_days": len(sessions),
            "start": sessions[0]["trade_date"],
            "end": sessions[-1]["trade_date"],
            "oos_days": len(oos_sessions),
        },
    }
    return cycle, validation_report(archive_snapshot, cycle)


def aggregate_kiwoom_realtime_events(
    events: list[dict[str, Any]], trade_date: str
) -> dict[str, list[dict[str, Any]]]:
    """Build conservative one-minute bars from whitelisted Kiwoom REAL market events."""

    def number(value: Any) -> int:
        try:
            return abs(int(str(value).replace(",", "").strip() or "0"))
        except ValueError:
            return 0

    quotes: dict[str, dict[str, Any]] = {}
    vi_active: set[str] = set()
    minute_rows: dict[tuple[str, str], dict[str, Any]] = {}
    for event in events:
        event_type = str(event.get("type", ""))
        symbol = str(event.get("item", "")).removeprefix("A").split("_", 1)[0]
        values = event.get("values", {})
        if not isinstance(values, dict) or not symbol:
            continue
        if event_type == "0D":
            bid = number(values.get("51") or values.get("28"))
            ask = number(values.get("41") or values.get("27"))
            quotes[symbol] = {
                "bid": bid,
                "ask": ask,
                "bid_depth_krw": number(values.get("125")) * bid,
                "ask_depth_krw": number(values.get("121")) * ask,
            }
            continue
        if event_type == "1h":
            state = str(values.get("9068", ""))
            if "해제" in state or state in {"0", "2"}:
                vi_active.discard(symbol)
            else:
                vi_active.add(symbol)
            continue
        if event_type != "0B":
            continue
        raw_time = str(values.get("20", ""))
        price = number(values.get("10"))
        volume = number(values.get("15"))
        if len(raw_time) < 4 or not raw_time[:4].isdigit() or price <= 0 or volume <= 0:
            continue
        minute = raw_time[:4]
        timestamp = f"{trade_date}T{minute[:2]}:{minute[2:]}:00+09:00"
        key = (symbol, timestamp)
        quote = quotes.get(symbol, {})
        bid = number(values.get("28")) or int(quote.get("bid", 0))
        ask = number(values.get("27")) or int(quote.get("ask", 0))
        row = minute_rows.get(key)
        if row is None:
            row = {
                "timestamp": timestamp,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": 0,
                "turnover_krw": 0,
                "bid": bid,
                "ask": ask,
                "bid_depth_krw": int(quote.get("bid_depth_krw", 0)),
                "ask_depth_krw": int(quote.get("ask_depth_krw", 0)),
                "vi_active": symbol in vi_active,
            }
            minute_rows[key] = row
        row["high"] = max(int(row["high"]), price)
        row["low"] = min(int(row["low"]), price)
        row["close"] = price
        row["volume"] = int(row["volume"]) + volume
        row["turnover_krw"] = int(row["turnover_krw"]) + price * volume
        row["bid"] = bid or row["bid"]
        row["ask"] = ask or row["ask"]
        row["vi_active"] = bool(row["vi_active"] or symbol in vi_active)
    result: dict[str, list[dict[str, Any]]] = {}
    for (symbol, _), row in sorted(minute_rows.items()):
        result.setdefault(symbol, []).append(row)
    return result


def build_fixture_session(trade_date: str = "2026-08-31") -> dict[str, Any]:
    """Reproducible fictional minute session. It can never satisfy promotion gates."""
    start = datetime.fromisoformat(f"{trade_date}T09:00:00+09:00")

    def bars(base: int, *, profile: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        previous = base
        for minute in range(121):
            if profile == "benchmark":
                close = base + min(minute, 20) * 4 + max(minute - 20, 0)
                volume = 20_000 + minute * 30
            elif profile == "breakout":
                first = [10000, 10020, 10010, 10040, 10060, 10050, 10080, 10070, 10090, 10100]
                close = first[minute] if minute < 10 else 10150 + min(max(minute - 10, 0), 6) * 25
                if minute > 16:
                    close = 10300 - min(minute - 16, 50) * 2
                volume = 110_000 if minute < 10 else 35_000
            else:
                close = base + ((minute % 5) - 2) * 5
                volume = 1_000
            high = max(previous, close) + (20 if profile == "breakout" else 5)
            low = min(previous, close) - (20 if profile == "breakout" else 5)
            if profile == "breakout" and minute == 0:
                low = 9_980
            if profile == "breakout" and minute == 9:
                high = 10_120
            if profile == "breakout" and minute == 10:
                high, low = 10_160, 10_100
            if profile == "breakout" and minute == 11:
                high, low = 10_200, 10_130
            timestamp = (start + timedelta(minutes=minute)).isoformat()
            result.append(
                {
                    "timestamp": timestamp,
                    "open": previous,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": volume,
                    "turnover_krw": close * volume,
                    "bid": max(close - 10, 1),
                    "ask": close,
                    "bid_depth_krw": 5_000_000,
                    "ask_depth_krw": 5_000_000,
                    "vi_active": False,
                }
            )
            previous = close
        return result

    benchmark = {
        "symbol": "069500",
        "name": "KODEX 200 (가상세션)",
        "bars": bars(50_000, profile="benchmark"),
    }
    opening_bars = bars(10_000, profile="breakout")
    quiet_bars = bars(15_000, profile="quiet")
    instruments = [
        {
            "symbol": "900001",
            "name": "가온소재(가상)",
            "market": "KOSDAQ",
            "security_type": "COMMON",
            "previous_close": 9_900,
            "median_daily_turnover_krw": 50_000_000_000,
            "median_first10_turnover_krw": 4_500_000_000,
            "designation": {},
            "bars": opening_bars,
        },
        {
            "symbol": "900002",
            "name": "다온로봇(가상)",
            "market": "KOSDAQ",
            "security_type": "COMMON",
            "previous_close": 14_900,
            "median_daily_turnover_krw": 35_000_000_000,
            "median_first10_turnover_krw": 2_000_000_000,
            "designation": {},
            "bars": quiet_bars,
        },
    ]
    payload: dict[str, Any] = {
        "as_of": f"{trade_date}T11:00:00+09:00",
        "trade_date": trade_date,
        "source": "FIXTURE_OPENING_RANGE_REPRODUCIBLE",
        "quality": {"state": "PASS", "issues": []},
        "history": {"trading_days": 1, "final_126_untouched": False},
        "benchmark": benchmark,
        "instruments": instruments,
        "evidence": [
            {
                "id": "ev_opening_range_session",
                "source": "DETERMINISTIC_FIXTURE",
                "title": "재현 가능한 장초 가상 세션",
                "published_at": f"{trade_date}T11:00:00+09:00",
                "verified": True,
            }
        ],
    }
    material = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    checksum = hashlib.sha256(material.encode()).hexdigest()
    payload["checksum"] = checksum
    payload["id"] = f"snap_{checksum[:16]}"
    return payload
