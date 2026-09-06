from __future__ import annotations

import hashlib
import os
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .data_sources import DataSourceError, OpenDartClient
from .kiwoom import KiwoomReadOnlyClient
from .research import finalize_snapshot

# A small, liquid, cross-sector research allowlist. It is not a recommendation list.
DEFAULT_KR_UNIVERSE: tuple[dict[str, str], ...] = (
    {"symbol": "005930", "name": "삼성전자", "sector": "반도체"},
    {"symbol": "000660", "name": "SK하이닉스", "sector": "반도체"},
    {"symbol": "035420", "name": "NAVER", "sector": "인터넷"},
    {"symbol": "035720", "name": "카카오", "sector": "인터넷"},
    {"symbol": "005380", "name": "현대차", "sector": "자동차"},
    {"symbol": "000270", "name": "기아", "sector": "자동차"},
    {"symbol": "068270", "name": "셀트리온", "sector": "바이오"},
    {"symbol": "207940", "name": "삼성바이오로직스", "sector": "바이오"},
)
BENCHMARK = {"symbol": "069500", "name": "KODEX 200", "role": "KOSPI200_PROXY"}


def configured_universe() -> list[dict[str, str]]:
    requested = [
        item.strip() for item in os.getenv("MONEYGUN_KR_UNIVERSE", "").split(",") if item.strip()
    ]
    defaults = {item["symbol"]: dict(item) for item in DEFAULT_KR_UNIVERSE}
    if not requested:
        return list(defaults.values())
    invalid = [symbol for symbol in requested if symbol not in defaults]
    if invalid:
        raise DataSourceError(
            "기본 실사용 유니버스 밖의 종목에는 이름·섹터 검토가 필요합니다: " + ", ".join(invalid)
        )
    return [defaults[symbol] for symbol in requested]


def _number(value: Any) -> int | None:
    text = str(value or "").replace(",", "").strip()
    if not text or text == "-":
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _first_amount(
    rows: list[dict[str, Any]], account_ids: tuple[str, ...], period: str
) -> int | None:
    for row in rows:
        if row.get("account_id") in account_ids:
            result = _number(row.get(period))
            if result is not None:
                return result
    return None


def _fundamental_flags(rows: list[dict[str, Any]]) -> dict[str, bool]:
    revenue_ids = (
        "ifrs-full_Revenue",
        "ifrs-full_RevenueFromContractsWithCustomers",
    )
    revenue = _first_amount(rows, revenue_ids, "thstrm_amount")
    prior_revenue = _first_amount(rows, revenue_ids, "frmtrm_amount")
    operating_profit = _first_amount(rows, ("dart_OperatingIncomeLoss",), "thstrm_amount")
    net_income = _first_amount(rows, ("ifrs-full_ProfitLoss",), "thstrm_amount")
    equity = _first_amount(rows, ("ifrs-full_Equity",), "thstrm_amount")
    operating_cash = _first_amount(
        rows,
        ("ifrs-full_CashFlowsFromUsedInOperatingActivities",),
        "thstrm_amount",
    )
    return {
        "revenue_growth": bool(
            revenue is not None and prior_revenue is not None and revenue > prior_revenue
        ),
        "operating_profit_positive": bool(operating_profit is not None and operating_profit > 0),
        "net_income_positive": bool(net_income is not None and net_income > 0),
        "equity_positive": bool(equity is not None and equity > 0),
        "operating_cashflow_positive": bool(operating_cash is not None and operating_cash > 0),
    }


def _annual_fundamentals(
    dart: OpenDartClient,
    *,
    corp_code: str,
    start_year: int,
    end_year: int,
    cutoff_date: date,
) -> list[dict[str, Any]]:
    history: list[dict[str, Any]] = []
    for business_year in range(start_year, end_year + 1):
        filing_year = business_year + 1
        filing_start = date(filing_year, 1, 1)
        if filing_start > cutoff_date:
            continue
        filing_end = min(date(filing_year, 4, 30), cutoff_date)
        disclosures = dart.search_disclosures(
            corp_code=corp_code,
            begin_date=filing_start.strftime("%Y%m%d"),
            end_date=filing_end.strftime("%Y%m%d"),
        )
        expected = f"사업보고서 ({business_year}.12)"
        filing = next(
            (item for item in disclosures if expected in str(item.get("report_nm", ""))),
            None,
        )
        if filing is None:
            continue
        rows = dart.financial_statements(
            corp_code=corp_code,
            business_year=str(business_year),
            report_code="11011",
        )
        if not rows:
            continue
        receipt_date = str(filing["rcept_dt"])
        history.append(
            {
                "effective_at": (
                    f"{receipt_date[:4]}-{receipt_date[4:6]}-{receipt_date[6:]}T18:00:00+09:00"
                ),
                "business_year": business_year,
                "receipt_no": str(filing.get("rcept_no", "")),
                "values": _fundamental_flags(rows),
            }
        )
        time.sleep(0.04)
    return history


def build_official_kr_snapshot(
    kiwoom: KiwoomReadOnlyClient,
    dart: OpenDartClient,
    *,
    as_of_date: date | None = None,
    max_chart_pages: int = 4,
    fundamental_start_year: int = 2016,
) -> dict[str, Any]:
    """Build an immutable internal-research snapshot from two official sources."""
    if not kiwoom.config.configured:
        raise DataSourceError("키움 조회 전용 키가 필요합니다.")
    if not dart.configured:
        raise DataSourceError("OpenDART 인증키가 필요합니다.")
    target_date = as_of_date or datetime.now(timezone(timedelta(hours=9))).date()
    base_date = target_date.strftime("%Y%m%d")
    as_of = f"{target_date.isoformat()}T15:40:00+09:00"
    universe = configured_universe()
    codes = {item["stock_code"]: item for item in dart.corporate_codes()}

    benchmark_bars = kiwoom.fetch_daily_bars(
        BENCHMARK["symbol"], base_date=base_date, max_pages=max_chart_pages
    )
    instruments: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = [
        {
            "id": "ev_snapshot",
            "source": "KIWOOM_OFFICIAL_REST",
            "title": "키움 REST API 수정주가 일봉 스냅샷",
            "published_at": as_of,
            "url": "https://openapi.kiwoom.com/guide/apiguide",
        }
    ]
    evidence_seen = {"ev_snapshot"}
    for meta in universe:
        symbol = meta["symbol"]
        corp = codes.get(symbol)
        if corp is None:
            raise DataSourceError(f"OpenDART 상장사 코드에서 {symbol}을 찾지 못했습니다.")
        bars = kiwoom.fetch_daily_bars(symbol, base_date=base_date, max_pages=max_chart_pages)
        history = _annual_fundamentals(
            dart,
            corp_code=corp["corp_code"],
            start_year=fundamental_start_year,
            end_year=target_date.year - 1,
            cutoff_date=target_date,
        )
        recent = dart.search_disclosures(
            corp_code=corp["corp_code"],
            begin_date=(target_date - timedelta(days=120)).strftime("%Y%m%d"),
            end_date=base_date,
        )
        evidence_ids: list[str] = []
        for disclosure in recent[:2]:
            receipt_no = str(disclosure.get("rcept_no", ""))
            if not receipt_no:
                continue
            evidence_id = f"dart_{hashlib.sha256(receipt_no.encode()).hexdigest()[:16]}"
            evidence_ids.append(evidence_id)
            if evidence_id not in evidence_seen:
                receipt_date = str(disclosure["rcept_dt"])
                evidence.append(
                    {
                        "id": evidence_id,
                        "source": "OPENDART_OFFICIAL",
                        "title": str(disclosure.get("report_nm", "공시")),
                        "published_at": (
                            f"{receipt_date[:4]}-{receipt_date[4:6]}-{receipt_date[6:]}"
                            "T18:00:00+09:00"
                        ),
                        "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={receipt_no}",
                    }
                )
                evidence_seen.add(evidence_id)
        latest_fundamentals = (
            history[-1]["values"]
            if history
            else {
                "revenue_growth": False,
                "operating_profit_positive": False,
                "net_income_positive": False,
                "equity_positive": False,
                "operating_cashflow_positive": False,
            }
        )
        instruments.append(
            {
                **meta,
                "bars": bars,
                "fundamentals": latest_fundamentals,
                "fundamentals_history": history,
                "catalyst_evidence_ids": evidence_ids,
                "customer_concentration": "공식 자료에서 정량 확인 필요",
            }
        )
        time.sleep(0.22)

    return finalize_snapshot(
        {
            "as_of": as_of,
            "source": "KIWOOM_OFFICIAL_REST",
            "license_basis": "USER_AUTHORIZED_INTERNAL_RESEARCH",
            "market": "KR",
            "benchmark": {**BENCHMARK, "bars": benchmark_bars},
            "instruments": instruments,
            "evidence": evidence,
            "collection_manifest": {
                "kiwoom_api_id": "ka10081",
                "adjusted_price": True,
                "chart_pages_per_symbol": max_chart_pages,
                "fundamental_source": "OPENDART_OFFICIAL",
                "point_in_time_rule": "annual filing receipt date 18:00 KST",
                "fundamental_revision_safe": False,
                "survivorship_bias_controlled": False,
                "known_limitations": [
                    "OpenDART 단일계정 API는 과거 정정 전 원문 값을 고정하지 않습니다.",
                    "현재 상장 종목 고정 유니버스라 상장폐지 종목 생존편향이 남습니다.",
                ],
                "redistribution_allowed": False,
            },
        }
    )
