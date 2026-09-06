import asyncio
import hashlib
import json
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from moneygun_api.kiwoom import KiwoomConfig, KiwoomReadOnlyClient
from moneygun_api.main import create_app
from moneygun_api.opening_range import (
    STRATEGY_ID,
    OpeningRangeConfig,
    aggregate_kiwoom_realtime_events,
    build_fixture_session,
    build_opening_range_signal_plan,
    load_opening_range_archive,
    run_opening_range_session,
    strategy_spec,
    validation_report,
)


def test_kiwoom_ticks_reconstruct_one_minute_bar_without_future_data() -> None:
    events = [
        {
            "type": "0D",
            "item": "005930",
            "values": {"41": "+70100", "51": "+70000", "121": "100", "125": "120"},
        },
        {"type": "0B", "item": "005930", "values": {"20": "091001", "10": "+70000", "15": "+10"}},
        {"type": "0B", "item": "005930", "values": {"20": "091055", "10": "+70100", "15": "+5"}},
        {"type": "0B", "item": "005930", "values": {"20": "091101", "10": "+69900", "15": "+3"}},
    ]

    bars = aggregate_kiwoom_realtime_events(events, "2026-08-31")["005930"]

    assert len(bars) == 2
    assert bars[0]["timestamp"] == "2026-08-31T09:10:00+09:00"
    assert bars[0]["open"] == 70_000
    assert bars[0]["high"] == 70_100
    assert bars[0]["close"] == 70_100
    assert bars[0]["volume"] == 15
    assert bars[0]["bid_depth_krw"] == 8_400_000


def test_opening_range_fixture_is_deterministic_and_flat_by_1100() -> None:
    snapshot = build_fixture_session()
    mission = {"id": "mission_opening_range_001", "equity_krw": 100_000}

    first = run_opening_range_session(mission, snapshot)
    second = run_opening_range_session(mission, snapshot)

    assert first["id"] == second["id"]
    assert first["strategy_id"] == STRATEGY_ID
    assert first["session"]["broker_order_count"] == 0
    assert first["session"]["force_flat_verified"] is True
    assert first["decision"]["order_allowed"] is False
    assert first["shadow_trades"][0]["quantity"] == 5
    trade = first["shadow_trades"][0]
    assert trade["exit_reason"] == "RUNNER_TRAILING_STOP"
    assert trade["runner_activated"] is True
    assert trade["runner_activation_price_krw"] == 10_290
    assert trade["runner_high_watermark_krw"] == 10_300
    assert trade["partial_exits"] == [
        {
            "at": "2026-08-31T09:16:00+09:00",
            "price_krw": 10_290,
            "quantity": 2,
            "reason": "PARTIAL_AT_1_5R",
        }
    ]
    assert first["shadow_trades"][0]["broker_submitted"] is False


def test_one_share_position_keeps_the_whole_quantity_as_runner() -> None:
    cycle = run_opening_range_session(
        {"id": "mission_opening_range_001", "equity_krw": 20_000},
        build_fixture_session(),
    )

    trade = cycle["shadow_trades"][0]
    assert trade["quantity"] == 1
    assert trade["runner_activated"] is True
    assert trade["partial_exits"] == []
    assert trade["exit_legs"][0]["quantity"] == 1


def test_daily_fifteen_percent_profit_locks_the_runner_and_session() -> None:
    snapshot = build_fixture_session()
    runner = snapshot["instruments"][0]["bars"]
    runner[17].update({"open": 20_000, "high": 20_100, "low": 19_900, "close": 20_000})

    cycle = run_opening_range_session(
        {"id": "mission_opening_range_001", "equity_krw": 100_000}, snapshot
    )

    assert cycle["shadow_trades"][0]["exit_reason"] == "DAILY_HARD_PROFIT_LOCK"
    assert cycle["session"]["stop_reason"] == "DAILY_HARD_PROFIT_LOCK"


def test_five_percent_profit_stop_blocks_only_the_next_entry() -> None:
    snapshot = build_fixture_session()
    second = dict(snapshot["instruments"][0])
    second["symbol"] = "900003"
    second["name"] = "누리소재(가상)"
    second["bars"] = [dict(bar) for bar in second["bars"]]
    snapshot["instruments"].append(second)

    cycle = run_opening_range_session(
        {"id": "mission_opening_range_001", "equity_krw": 100_000},
        snapshot,
        OpeningRangeConfig(daily_new_entry_stop_bps=20),
    )

    assert cycle["session"]["round_trips"] == 1
    assert cycle["session"]["stop_reason"] == "DAILY_NEW_ENTRY_PROFIT_STOP"


def _current_opening_signal_snapshot() -> dict[str, object]:
    snapshot = build_fixture_session()
    snapshot["source"] = "KIWOOM_REALTIME_ARCHIVE"
    snapshot["as_of"] = "2026-08-31T09:10:30+09:00"
    snapshot["benchmark"]["bars"] = snapshot["benchmark"]["bars"][:11]
    for instrument in snapshot["instruments"]:
        instrument["bars"] = instrument["bars"][:11]
    return snapshot


def test_opening_range_live_signal_uses_only_the_current_completed_bar() -> None:
    mission = {
        "id": "mission_opening_range_001",
        "equity_krw": 100_000,
        "available_krw": 100_000,
    }
    snapshot = _current_opening_signal_snapshot()

    plan = build_opening_range_signal_plan(
        mission,
        snapshot,
        now=datetime.fromisoformat("2026-08-31T09:10:30+09:00"),
    )

    assert plan["strategy_id"] == "OPEN-RANGE-KR-v2"
    assert plan["decision"]["symbol"] == "900001"
    assert plan["decision"]["quantity"] == 5
    assert plan["decision"]["limit_price_krw"] == 10_140
    assert plan["decision"]["order_allowed"] is False


def test_opening_range_live_signal_rejects_raw_and_stale_archives() -> None:
    mission = {
        "id": "mission_opening_range_001",
        "equity_krw": 100_000,
        "available_krw": 100_000,
    }
    raw = _current_opening_signal_snapshot()
    raw["source"] = "KIWOOM_REALTIME_RAW_ARCHIVE"
    with pytest.raises(ValueError, match="정규화된"):
        build_opening_range_signal_plan(
            mission,
            raw,
            now=datetime.fromisoformat("2026-08-31T09:10:30+09:00"),
        )
    normalized = _current_opening_signal_snapshot()
    with pytest.raises(ValueError, match="과거 신호"):
        build_opening_range_signal_plan(
            mission,
            normalized,
            now=datetime.fromisoformat("2026-08-31T09:12:30+09:00"),
        )


def test_opening_range_fixture_can_never_promote() -> None:
    snapshot = build_fixture_session()
    cycle = run_opening_range_session(
        {"id": "mission_opening_range_001", "equity_krw": 100_000}, snapshot
    )
    report = validation_report(snapshot, cycle)

    assert report["promotion_eligible"] is False
    assert report["status"] == "NOT_ELIGIBLE"
    assert "authorized_non_fixture_source" in report["failed_gates"]
    assert "minimum_300_oos_trades" in report["failed_gates"]
    assert report["gates"]["zero_broker_orders_during_research"] is True


def test_opening_range_oos_metrics_are_computed_before_separate_r1_gate() -> None:
    trades = []
    first_day = date(2025, 1, 2)
    for index in range(300):
        profitable = index % 5 != 0
        pnl = 100 if profitable else -30
        result_r = 0.3 if profitable else -0.1
        exit_day = first_day + timedelta(days=index)
        trades.append(
            {
                "state": "SHADOW_FILLED_AND_EXITED",
                "symbol": f"{index:06d}",
                "entry_at": f"{exit_day.isoformat()}T09:11:00+09:00",
                "exit_at": f"{exit_day.isoformat()}T09:30:00+09:00",
                "net_pnl_krw": pnl,
                "result_r": result_r,
            }
        )
    snapshot = {
        "id": "snap_0123456789abcdef",
        "source": "LICENSED_INTRADAY_VENDOR",
        "as_of": "2026-08-31T11:00:00+09:00",
        "history": {
            "trading_days": 504,
            "start": "2024-08-01",
            "end": "2026-08-31",
            "oos_days": 126,
            "final_126_untouched": True,
        },
    }
    robust_variant = {"trade_count": 300, "expectancy_r": 0.1}
    cycle = {
        "initial_equity_krw": 100_000,
        "shadow_trades": trades,
        "session": {"broker_order_count": 0, "force_flat_verified": True},
        "validation_evidence": {
            "strategy_trials": 3,
            "parameter_sensitivity": {
                "minus_20pct": robust_variant,
                "plus_20pct": robust_variant,
            },
            "double_slippage": {"expectancy_r": 0.05},
        },
    }

    report = validation_report(snapshot, cycle)

    assert report["promotion_eligible"] is True
    assert report["metrics"]["trade_count"] == 300
    assert report["metrics"]["sharpe"] >= 1.2
    assert report["metrics"]["deflated_sharpe_probability"] >= 0.95
    assert "shadow_60_days_300_signals" not in report["gates"]


def test_opening_range_archive_import_requires_manifest_and_checksum(tmp_path) -> None:
    session = build_fixture_session()
    session["source"] = "LICENSED_INTRADAY_VENDOR"
    payload = {
        "source": "LICENSED_INTRADAY_VENDOR",
        "as_of": "2026-08-31T11:00:00+09:00",
        "quality": {"state": "PASS", "issues": []},
        "collection_manifest": {
            "schema_version": "OPENING_RANGE_ARCHIVE-v1",
            "point_in_time": True,
            "includes_quotes": True,
            "includes_vi": True,
        },
        "history": {"final_126_untouched": True},
        "sessions": [session],
    }
    payload["checksum"] = hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    archive = tmp_path / "valid.intraday.json"
    archive.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    loaded = load_opening_range_archive(archive.name, tmp_path)

    assert loaded["id"].startswith("snap_")
    assert loaded["sessions"][0]["source"] == "LICENSED_INTRADAY_VENDOR"

    payload["sessions"][0]["trade_date"] = "2026-09-01"
    archive.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="SHA-256"):
        load_opening_range_archive(archive.name, tmp_path)


def test_opening_range_api_seeds_mode_and_creates_shadow_only(tmp_path) -> None:
    app = create_app(tmp_path / "opening.sqlite3")
    with TestClient(app) as client:
        mission = client.get("/v1/missions/mission_opening_range_001")
        result = client.post(
            "/v1/strategies/opening-range/run",
            json={},
            headers={"Idempotency-Key": "opening-range-test-001"},
        )

        assert mission.status_code == 200
        assert mission.json()["mode_display_name"] == "장초 단타"
        assert mission.json()["halt_equity_krw"] == 90_000
        assert result.status_code == 200
        assert result.json()["trading_enabled"] is False
        assert result.json()["broker_submitted"] is False
        assert result.json()["validation"]["promotion_eligible"] is False
        assert result.json()["shadow_orders"][0]["state"] == "SHADOW_FILLED"
        assert result.json()["shadow_orders"][0]["broker_submitted"] is False
        assert client.get("/v1/strategies/opening-range/latest").status_code == 200


class _TokenTransport:
    def post(
        self, url: str, headers: dict[str, str], payload: dict[str, object]
    ) -> dict[str, object]:
        del url, headers, payload
        return {"return_code": 0, "token": "safe-test-token"}


class _FakeSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []
        self.frames = [
            {"trnm": "LOGIN", "return_code": 0, "return_msg": ""},
            {
                "trnm": "REAL",
                "data": [
                    {
                        "type": "0B",
                        "item": "005930",
                        "values": {"20": "091001", "10": "+70000", "15": "+10"},
                    }
                ],
            },
        ]

    async def __aenter__(self) -> "_FakeSocket":
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    async def recv(self) -> str:
        return json.dumps(self.frames.pop(0))


def test_kiwoom_realtime_collector_subscribes_to_market_data_only() -> None:
    socket = _FakeSocket()
    client = KiwoomReadOnlyClient(
        KiwoomConfig("app", "secret", environment="production"), _TokenTransport()
    )

    events = asyncio.run(
        client.collect_realtime_market_events(
            ["005930"], max_messages=1, connect_factory=lambda *args, **kwargs: socket
        )
    )

    assert events[0]["type"] == "0B"
    assert socket.sent[0] == {"trnm": "LOGIN", "token": "safe-test-token"}
    assert socket.sent[1]["data"][0]["type"] == ["0B", "0D"]
    assert all("00" not in item["type"] for item in socket.sent[1]["data"])


def test_opening_range_spec_forbids_overnight_and_market_orders() -> None:
    spec = strategy_spec()

    assert spec["entry"]["order_type"] == "LIMIT_ONLY"
    assert spec["risk_limits"]["overnight_position"] is False
    assert spec["risk_limits"]["max_round_trips"] == 3
    assert spec["exit"]["runner_activation_r"] == 1.5
    assert spec["exit"]["runner_trail_pct"] == 0.8
    assert spec["exit"]["runner_tight_trail_pct"] == 0.5
    assert spec["risk_limits"]["daily_new_entry_stop_pct"] == 5.0
    assert spec["risk_limits"]["daily_trail_tighten_pct"] == 10.0
    assert spec["risk_limits"]["daily_hard_profit_lock_pct"] == 15.0
    assert spec["trading_enabled"] is False
