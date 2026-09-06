from typing import Any

from fastapi.testclient import TestClient

from moneygun_api.kiwoom import JsonPage, KiwoomConfig, KiwoomReadOnlyClient
from moneygun_api.main import create_app


class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str], dict[str, Any]]] = []

    def post(self, url: str, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((url, headers, payload))
        if url.endswith("/oauth2/token"):
            return {"return_code": 0, "token": "test-access-token"}
        return {
            "return_code": 0,
            "account_no": "1234567890",
            "tot_evlt_amt": "100000",
            "acnt_evlt_remn_indv_tot": [],
        }


def test_kiwoom_read_only_client_has_narrow_capabilities_and_redacts_account() -> None:
    transport = FakeTransport()
    client = KiwoomReadOnlyClient(
        KiwoomConfig("app-key", "secret-key", "mock", "테스트 계좌"), transport
    )

    result = client.fetch_domestic_account_evaluation()

    assert result["account_no"] == "***7890"
    assert [call[0] for call in transport.calls] == [
        "https://mockapi.kiwoom.com/oauth2/token",
        "https://mockapi.kiwoom.com/api/dostk/acnt",
    ]
    assert transport.calls[1][1]["api-id"] == "kt00018"
    assert transport.calls[1][2] == {"qry_tp": "1", "dmst_stex_tp": "KRX"}
    status = client.status()
    assert status["connection_mode"] == "READ_ONLY"
    assert "ORDER_CREATE" in status["blocked_capabilities"]
    assert status["trading_enabled"] is False
    assert not hasattr(client, "create_order")


def test_unconfigured_kiwoom_sync_fails_closed(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("KIWOOM_APP_KEY", raising=False)
    monkeypatch.delenv("KIWOOM_SECRET_KEY", raising=False)
    client = TestClient(create_app(tmp_path / "kiwoom-unconfigured.sqlite3"))

    status = client.get("/v1/brokers/kiwoom/status")
    sync = client.post("/v1/brokers/kiwoom/account-snapshots/sync")

    assert status.status_code == 200
    assert status.json()["configured"] is False
    assert status.json()["trading_enabled"] is False
    assert sync.status_code == 503


class PaginatedChartTransport(FakeTransport):
    def post_page(self, url: str, headers: dict[str, str], payload: dict[str, Any]) -> JsonPage:
        self.calls.append((url, headers, payload))
        if url.endswith("/oauth2/token"):
            return JsonPage({"return_code": 0, "token": "test-access-token"})
        if headers["cont-yn"] == "N":
            return JsonPage(
                {
                    "return_code": 0,
                    "stk_dt_pole_chart_qry": [
                        {
                            "dt": "20260831",
                            "open_pric": "-70000",
                            "high_pric": "71000",
                            "low_pric": "69000",
                            "cur_prc": "70500",
                            "trde_qty": "1000",
                        },
                    ],
                },
                cont_yn="Y",
                next_key="page-2",
            )
        return JsonPage(
            {
                "return_code": 0,
                "stk_dt_pole_chart_qry": [
                    {
                        "dt": "20260830",
                        "open_pric": "68000",
                        "high_pric": "70000",
                        "low_pric": "67500",
                        "cur_prc": "69500",
                        "trde_qty": "900",
                    },
                    {
                        "dt": "20260829",
                        "open_pric": "68000",
                        "high_pric": "68000",
                        "low_pric": "68000",
                        "cur_prc": "68000",
                        "trde_qty": "0",
                    },
                ],
            }
        )


def test_official_daily_chart_paginates_and_normalizes() -> None:
    transport = PaginatedChartTransport()
    client = KiwoomReadOnlyClient(
        KiwoomConfig("app-key", "secret-key", "production", "테스트 계좌"), transport
    )

    bars = client.fetch_daily_bars("005930", base_date="20260831", max_pages=2)

    assert [bar["date"] for bar in bars] == ["2026-08-30", "2026-08-31"]
    assert bars[-1]["open"] == 70000
    chart_calls = [call for call in transport.calls if call[0].endswith("/api/dostk/chart")]
    assert chart_calls[0][1]["api-id"] == "ka10081"
    assert chart_calls[1][1]["cont-yn"] == "Y"
    assert chart_calls[1][1]["next-key"] == "page-2"
    assert chart_calls[0][2] == {"stk_cd": "005930", "base_dt": "20260831", "upd_stkpc_tp": "1"}


class PaginatedRankingTransport(FakeTransport):
    def post_page(self, url: str, headers: dict[str, str], payload: dict[str, Any]) -> JsonPage:
        self.calls.append((url, headers, payload))
        if url.endswith("/oauth2/token"):
            return JsonPage({"return_code": 0, "token": "test-access-token"})
        row = {
            "stk_cd": "A123456" if headers["cont-yn"] == "N" else "234567",
            "stk_nm": "작은회사A" if headers["cont-yn"] == "N" else "작은회사B",
            "cur_prc": "-12500" if headers["cont-yn"] == "N" else "8000",
            "flu_rt": "2.50",
            "trde_qty": "1,000,000",
        }
        return JsonPage(
            {"return_code": 0, "tdy_trde_qty_upper": [row]},
            cont_yn="Y" if headers["cont-yn"] == "N" else "N",
            next_key="page-2" if headers["cont-yn"] == "N" else "",
        )


def test_market_ranking_is_read_only_paginated_and_affordable() -> None:
    transport = PaginatedRankingTransport()
    client = KiwoomReadOnlyClient(
        KiwoomConfig("app-key", "secret-key", "production", "테스트 계좌"), transport
    )

    rows = client.fetch_today_volume_top(market="KOSDAQ", price_filter="10", max_pages=2)

    assert [row["symbol"] for row in rows] == ["123456", "234567"]
    assert rows[0]["current_price_krw"] == 12_500
    rank_calls = [call for call in transport.calls if call[0].endswith("/api/dostk/rkinfo")]
    assert rank_calls[0][1]["api-id"] == "ka10030"
    assert rank_calls[1][1]["next-key"] == "page-2"
    assert rank_calls[0][2]["mrkt_tp"] == "101"
    assert rank_calls[0][2]["pric_tp"] == "10"
    assert rank_calls[0][2]["mang_stk_incls"] == "4"


def test_account_snapshot_is_sanitized_and_immutable(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("KIWOOM_APP_KEY", raising=False)
    monkeypatch.delenv("KIWOOM_SECRET_KEY", raising=False)
    app = create_app(tmp_path / "kiwoom-snapshot.sqlite3")
    transport = FakeTransport()
    app.state.kiwoom = KiwoomReadOnlyClient(
        KiwoomConfig("app-key", "secret-key", "mock", "테스트 계좌"), transport
    )
    client = TestClient(app)

    first = client.post("/v1/brokers/kiwoom/account-snapshots/sync")
    second = client.post("/v1/brokers/kiwoom/account-snapshots/sync")

    assert first.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["payload"]["account_no"] == "***7890"
    assert "app-key" not in first.text
    assert "secret-key" not in first.text
    assert first.json()["trading_enabled"] is False


def _run_cycle(client: TestClient) -> None:
    response = client.post(
        "/v1/research/cycles/run",
        json={
            "mission_id": "mission_focus_001",
            "data_source": "FIXTURE_KR_REPRODUCIBLE",
        },
        headers={"Idempotency-Key": "shadow-cycle-test-001"},
    )
    assert response.status_code == 200


def test_shadow_order_is_idempotent_and_never_reaches_broker(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "shadow-order.sqlite3"))
    _run_cycle(client)
    headers = {"Idempotency-Key": "shadow-order-test-001"}

    first = client.post(
        "/v1/shadow/orders",
        json={"mission_id": "mission_focus_001"},
        headers=headers,
    )
    second = client.post(
        "/v1/shadow/orders",
        json={"mission_id": "mission_focus_001"},
        headers=headers,
    )

    assert first.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["state"] == "READY"
    assert [event["state"] for event in first.json()["events"]] == [
        "CREATED",
        "PRECHECKED",
        "READY",
    ]
    assert first.json()["broker_submitted"] is False

    simulated = client.post(
        f"/v1/shadow/orders/{first.json()['id']}/simulate",
        json={"scenario": "AUTO"},
    )
    assert simulated.status_code == 200
    assert simulated.json()["state"] == "SHADOW_FILLED"
    assert simulated.json()["fill"]["quantity"] > 0
    assert simulated.json()["broker_submitted"] is False
    assert simulated.json()["trading_enabled"] is False

    replay = client.post(
        f"/v1/shadow/orders/{first.json()['id']}/simulate",
        json={"scenario": "AUTO"},
    )
    assert replay.json() == simulated.json()
    assert len(client.get("/v1/shadow/orders").json()) == 1


def test_shadow_gap_guard_cancels_without_fill(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "shadow-gap.sqlite3"))
    _run_cycle(client)
    order = client.post(
        "/v1/shadow/orders",
        json={"mission_id": "mission_focus_001"},
        headers={"Idempotency-Key": "shadow-order-gap-test-001"},
    ).json()

    cancelled = client.post(
        f"/v1/shadow/orders/{order['id']}/simulate",
        json={"scenario": "GAP_UP_CANCEL"},
    )

    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "SHADOW_CANCELLED"
    assert cancelled.json()["fill"] is None
    assert cancelled.json()["broker_submitted"] is False
