import gzip
import hashlib
import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from moneygun_api.public_market_data import (
    inspect_kind_sources,
    normalize_collected_market_data,
    read_kind_xls,
)


def _kind_html(headers: list[str], rows: list[list[str]]) -> bytes:
    header = "".join(f"<th>{value}</th>" for value in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{value}</td>" for value in row) + "</tr>"
        for row in rows
    )
    return (f"<html><table><tr>{header}</tr>{body}</table></html>").encode("cp949")


def test_read_kind_html_disguised_as_xls(tmp_path: Path) -> None:
    path = tmp_path / "투자경고.xls"
    path.write_bytes(
        _kind_html(
            ["번호", "종목명", "종목코드", "공시일", "지정일", "해제일"],
            [["1", "테스트", "005930", "2026-08-27", "2026-08-28", "-"]],
        )
    )
    headers, rows = read_kind_xls(path)
    assert headers[-2:] == ["지정일", "해제일"]
    assert rows[0]["종목코드"] == "005930"


def test_kind_inspection_fails_closed_for_static_status_lists(tmp_path: Path) -> None:
    root = tmp_path / "kind"
    quarters = root / "investment-caution-quarterly"
    quarters.mkdir(parents=True)
    for index in range(20):
        day = "2021-08-28" if index == 0 else "2026-08-28"
        (quarters / f"caution-{index:02d}.xls").write_bytes(
            _kind_html(
                ["번호", "종목명", "종목코드", "유형", "공시일", "지정일"],
                [["1", "테스트", "005930", "종가급변", day, day]],
            )
        )
    (root / "관리종목.xls").write_bytes(
        _kind_html(
            ["종목명", "종목코드", "지정일", "지정사유"],
            [["테스트", "005930", "2026-08-28", "사유"]],
        )
    )
    report = inspect_kind_sources(
        root, period_start=date(2021, 8, 28), period_end=date(2026, 8, 28)
    )
    assert report["checks"]["caution_quarter_files"] is True
    assert report["checks"]["managed_intervals_complete"] is False
    assert report["historical_designation_states_complete"] is False


def test_kind_detailed_history_covers_all_six_disqualifying_states(tmp_path: Path) -> None:
    root = tmp_path / "kind"
    actions = root / "market-actions"
    actions.mkdir(parents=True)
    definitions = {
        "caution": "CAUTION",
        "warning": "WARNING",
        "danger": "DANGER",
        "managed": "MANAGED",
        "watchlist": "WATCHLIST",
        "halted": "HALTED",
    }
    files = []
    totals = {}
    for slug, state in definitions.items():
        filename = f"{slug}-20210828-20260828-p0001.xls"
        path = actions / filename
        path.write_bytes(
            _kind_html(
                ["시간", "회사명", "종목코드", "공시제목", "제출인"],
                [["2026-08-28 15:00", "테스트", "005930", state, "한국거래소"]],
            )
        )
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        files.append(
            {
                "file": filename,
                "state": state,
                "coverage_start": "2021-08-28",
                "coverage_end": "2026-08-28",
                "page": 1,
                "rows": 1,
                "sha256": digest,
            }
        )
        totals[state] = 1
    (actions / "manifest.json").write_text(
        json.dumps(
            {
                "period_start": "2021-08-28",
                "period_end": "2026-08-28",
                "complete": True,
                "files": files,
                "totals": totals,
            }
        ),
        encoding="utf-8",
    )
    report = inspect_kind_sources(
        root, period_start=date(2021, 8, 28), period_end=date(2026, 8, 28)
    )
    assert report["historical_designation_states_complete"] is True
    assert report["totals"] == totals


def test_normalize_collected_market_data_keeps_common_stock_and_benchmark(
    tmp_path: Path,
) -> None:
    raw = tmp_path / "raw" / "public-data"
    prices = raw / "prices"
    issuance = raw / "issuance"
    prices.mkdir(parents=True)
    issuance.mkdir(parents=True)
    with gzip.open(issuance / "stock-issuance-0001.json.gz", "wt", encoding="utf-8") as out:
        json.dump(
            {
                "items": [
                    {
                        "scrsItmsKcdNm": "보통주",
                        "itmsShrtnCd": "005930",
                        "isinCd": "KR7005930003",
                        "stckIssuCmpyNm": "삼성전자",
                        "lstgDt": "19750611",
                        "lstgAbolDt": "",
                    }
                ]
            },
            out,
            ensure_ascii=False,
        )
    items = []
    start = date(2025, 1, 1)
    for index in range(200):
        day = (start + timedelta(days=index)).strftime("%Y%m%d")
        for symbol, isin, name in (
            ("005930", "KR7005930003", "삼성전자"),
            ("069500", "KR7069500007", "KODEX 200"),
        ):
            items.append(
                {
                    "srtnCd": symbol,
                    "isinCd": isin,
                    "itmsNm": name,
                    "mrktCtg": "KOSPI",
                    "basDt": day,
                    "mkp": "100",
                    "hipr": "110",
                    "lopr": "90",
                    "clpr": "105",
                    "trqu": "1000",
                }
            )
    with gzip.open(prices / "stock-prices-0001.json.gz", "wt", encoding="utf-8") as out:
        json.dump({"items": items}, out, ensure_ascii=False)
    report = normalize_collected_market_data(
        tmp_path / "raw",
        period_start=start,
        period_end=start + timedelta(days=199),
        output_database=tmp_path / "normalized.sqlite3",
    )
    assert report["instrument_count"] == 1
    assert report["qualified_price_rows"] == 200
    assert report["benchmark_rows"] == 200
    assert report["designation_interval_count"] == 0
    connection = sqlite3.connect(tmp_path / "normalized.sqlite3")
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM status_observations"
        ).fetchone()[0] == 0
    finally:
        connection.close()
