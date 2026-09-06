from __future__ import annotations

import hashlib
import os
import re
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from .data_sources import OpenDartClient
from .official_research import configured_universe
from .pilot import PILOT_MISSION_ID
from .storage import Database

KST = timezone(timedelta(hours=9))
SOURCE = "OPENDART_OFFICIAL"
CLASSIFIER_VERSION = "NOVA-DISCLOSURE-RISK-v1"
POLL_INTERVAL_SECONDS = 300
ACTIVE_START_MINUTE = 7 * 60 + 30
ACTIVE_END_MINUTE = 18 * 60 + 30

CRITICAL_TERMS = (
    "상장폐지",
    "회생절차",
    "파산",
    "부도",
    "횡령",
    "배임",
    "감사의견거절",
    "영업정지",
    "매매거래정지",
    "관리종목",
    "불성실공시",
)
WARNING_TERMS = (
    "유상증자",
    "전환사채",
    "신주인수권부사채",
    "교환사채",
    "감자",
    "최대주주변경",
    "소송",
    "계약해지",
    "공급계약해지",
    "정정",
)
INFORMATION_TERMS: dict[str, tuple[str, ...]] = {
    "CONTRACT": ("단일판매", "공급계약", "수주"),
    "FINANCIAL": ("잠정실적", "사업보고서", "분기보고서", "반기보고서"),
    "CAPITAL": ("자기주식", "배당", "주식소각"),
    "GOVERNANCE": ("주주총회", "대표이사", "임원"),
}

_corp_code_cache: dict[str, dict[str, str]] = {}
_corp_code_cache_at: datetime | None = None


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value).replace("ㆍ", "")


def classify_disclosure(title: str) -> dict[str, Any]:
    compact = _compact(title)
    critical = [term for term in CRITICAL_TERMS if _compact(term) in compact]
    warning = [term for term in WARNING_TERMS if _compact(term) in compact]
    if critical:
        severity = "CRITICAL"
        category = "CORPORATE_RISK"
        sentiment = "NEGATIVE"
        matched = critical
        rationale = "재무·지배구조·상장 지속성에 중대한 위험이 있어 72시간 신규 매수를 차단합니다."
    elif warning:
        severity = "WARNING"
        category = "CAPITAL_OR_EVENT_RISK"
        sentiment = "MIXED"
        matched = warning
        rationale = (
            "희석·지배구조·계약 변경 가능성을 소유자가 확인할 때까지 "
            "신규 매수를 차단합니다."
        )
    else:
        category = "OTHER"
        matched = []
        for candidate, terms in INFORMATION_TERMS.items():
            found = [term for term in terms if _compact(term) in compact]
            if found:
                category = candidate
                matched = found
                break
        severity = "INFO"
        sentiment = "NEUTRAL"
        rationale = "방향을 추정하지 않고 공식 공시 근거로 기록합니다."
    return {
        "category": category,
        "severity": severity,
        "sentiment": sentiment,
        "review_required": severity in {"CRITICAL", "WARNING"},
        "blocks_new_buy": severity in {"CRITICAL", "WARNING"},
        "analysis": {
            "classifier_version": CLASSIFIER_VERSION,
            "matched_terms": matched,
            "rationale": rationale,
            "llm_used": False,
            "price_or_quantity_inferred": False,
        },
    }


def _corporations(client: OpenDartClient, now: datetime) -> dict[str, dict[str, str]]:
    global _corp_code_cache, _corp_code_cache_at
    if (
        not _corp_code_cache
        or _corp_code_cache_at is None
        or now - _corp_code_cache_at > timedelta(hours=24)
    ):
        _corp_code_cache = {
            row["stock_code"]: row for row in client.corporate_codes() if row["stock_code"]
        }
        _corp_code_cache_at = now
    return _corp_code_cache


def reset_corporation_cache() -> None:
    global _corp_code_cache, _corp_code_cache_at
    _corp_code_cache = {}
    _corp_code_cache_at = None


def _watchlist(database: Database, corporations: dict[str, dict[str, str]]) -> list[dict[str, str]]:
    symbols: dict[str, str] = {
        item["symbol"]: item["name"] for item in configured_universe()
    }
    for mission_id in (
        "mission_focus_001",
        "mission_close_auction_001",
        "mission_opening_range_001",
        "mission_balanced_001",
        "mission_long_term_001",
    ):
        cycle = database.get_latest_cycle(mission_id)
        if cycle and cycle.get("decision", {}).get("symbol"):
            symbols[str(cycle["decision"]["symbol"])] = str(
                cycle["decision"].get("name") or cycle["decision"]["symbol"]
            )
    for intent in database.list_live_order_intents(PILOT_MISSION_ID):
        symbols[str(intent["symbol"])] = str(intent.get("name") or intent["symbol"])
    return [
        {
            "symbol": symbol,
            "name": name,
            "corp_code": corporations[symbol]["corp_code"],
            "corp_name": corporations[symbol]["corp_name"],
        }
        for symbol, name in sorted(symbols.items())
        if symbol in corporations
    ]


def _event(
    disclosure: dict[str, Any], company: dict[str, str], collected_at: datetime
) -> dict[str, Any] | None:
    receipt_no = str(disclosure.get("rcept_no", "")).strip()
    receipt_date = str(disclosure.get("rcept_dt", "")).strip()
    title = str(disclosure.get("report_nm", "공시")).strip()
    if not receipt_no or not re.fullmatch(r"\d{8}", receipt_date):
        return None
    classification = classify_disclosure(title)
    event_id = "news_" + hashlib.sha256(f"{SOURCE}|{receipt_no}".encode()).hexdigest()[:20]
    return {
        "id": event_id,
        "source": SOURCE,
        "source_event_id": receipt_no,
        "symbol": company["symbol"],
        "corp_code": company["corp_code"],
        "company_name": str(disclosure.get("corp_name") or company["corp_name"]),
        "title": title,
        **classification,
        "published_at": (
            f"{receipt_date[:4]}-{receipt_date[4:6]}-{receipt_date[6:]}T00:00:00+09:00"
        ),
        "collected_at": collected_at.astimezone(UTC).isoformat(timespec="seconds"),
        "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={receipt_no}",
    }


def poll_official_news(
    database: Database,
    client: OpenDartClient | None = None,
    *,
    now_kst: datetime | None = None,
) -> dict[str, Any]:
    client = client or OpenDartClient()
    now_kst = (now_kst or datetime.now(KST)).astimezone(KST)
    started_at = datetime.now(UTC).isoformat(timespec="seconds")
    run_id = f"newspoll_{uuid4().hex[:20]}"
    watched = 0
    fetched = 0
    inserted = 0
    blocking = 0
    try:
        corporations = _corporations(client, datetime.now(UTC))
        watchlist = _watchlist(database, corporations)
        watched = len(watchlist)
        latest = database.latest_news_poll_run(SOURCE)
        lookback_days = 2 if latest and latest["state"] == "PASS" else 7
        begin_date = (now_kst.date() - timedelta(days=lookback_days)).strftime("%Y%m%d")
        end_date = now_kst.strftime("%Y%m%d")
        for company in watchlist:
            disclosures = client.search_disclosures(
                corp_code=company["corp_code"],
                begin_date=begin_date,
                end_date=end_date,
                last_report_only=False,
            )
            fetched += len(disclosures)
            for disclosure in disclosures:
                candidate = _event(disclosure, company, now_kst)
                if candidate is None:
                    continue
                saved = database.save_news_event(candidate)
                if not saved["inserted"]:
                    continue
                inserted += 1
                if saved["blocks_new_buy"]:
                    blocking += 1
                    database.enqueue_notification(
                        PILOT_MISSION_ID,
                        severity=saved["severity"],
                        category="NEWS_RISK",
                        title=f"김뉴스: {saved['company_name']} 신규 공시 확인 필요",
                        body=f"{saved['title']} · 신규 매수는 확인 전 차단됩니다.",
                        dedupe_key=f"news-risk-{saved['id']}",
                    )
        state = "PASS"
        error = None
    except Exception as exc:  # one poll failure must be persisted and fail closed
        state = "FAIL"
        error = f"{type(exc).__name__}: {str(exc)[:400]}"
    completed_at = datetime.now(UTC).isoformat(timespec="seconds")
    return database.save_news_poll_run(
        {
            "id": run_id,
            "source": SOURCE,
            "state": state,
            "watched_symbols": watched,
            "fetched_count": fetched,
            "inserted_count": inserted,
            "blocking_count": blocking,
            "error": error,
            "started_at": started_at,
            "completed_at": completed_at,
        }
    )


def _in_active_window(now_kst: datetime) -> bool:
    minute = now_kst.hour * 60 + now_kst.minute
    return now_kst.weekday() < 5 and ACTIVE_START_MINUTE <= minute <= ACTIVE_END_MINUTE


def news_status(database: Database, *, now_kst: datetime | None = None) -> dict[str, Any]:
    now_kst = (now_kst or datetime.now(KST)).astimezone(KST)
    latest = database.latest_news_poll_run(SOURCE)
    events = database.list_news_events(limit=100)
    unresolved = [
        event
        for event in events
        if event["review_required"] and not event["acknowledged_at"]
    ]
    active_ids = {
        block["id"]
        for symbol in {event["symbol"] for event in events}
        for block in database.active_news_blocks(symbol, now_utc=now_kst.astimezone(UTC))
    }
    active_blocks = [event for event in events if event["id"] in active_ids]
    if latest is None:
        state = "NEVER_RUN"
    elif latest["state"] != "PASS":
        state = "ERROR"
    else:
        age = datetime.now(UTC) - datetime.fromisoformat(latest["completed_at"])
        state = "STALE" if _in_active_window(now_kst) and age > timedelta(minutes=15) else "READY"
    return {
        "agent": {
            "code": "NOVA",
            "name": "김뉴스",
            "state": "WORKING" if state == "READY" and _in_active_window(now_kst) else state,
            "summary": (
                f"공식 공시 {len(events)}건 · 미확인 {len(unresolved)}건 · "
                f"매수 차단 {len(active_blocks)}건"
            ),
        },
        "state": state,
        "configured": OpenDartClient().configured,
        "scheduler_enabled": os.getenv("MONEYGUN_NEWS_SCHEDULER_ENABLED", "").lower()
        in {"1", "true", "yes", "enabled"},
        "poll_interval_seconds": POLL_INTERVAL_SECONDS,
        "active_window_kst": "평일 07:30~18:30",
        "latest_poll": latest,
        "counts": {
            "total": len(events),
            "unresolved": len(unresolved),
            "active_buy_blocks": len(active_blocks),
        },
        "sources": [
            {
                "code": SOURCE,
                "name": "금융감독원 OpenDART 공식 공시",
                "state": "ACTIVE" if OpenDartClient().configured else "NOT_CONFIGURED",
            },
            {
                "code": "GENERAL_NEWS_PROVIDER",
                "name": "라이선스 뉴스 공급자",
                "state": "NOT_CONFIGURED",
            },
        ],
        "events": events,
        "automatic_broker_submission": False,
    }


def should_poll(now_kst: datetime | None = None) -> bool:
    return _in_active_window((now_kst or datetime.now(KST)).astimezone(KST))
