from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen

from .data_sources import DataSourceError

PRICE_ENDPOINT = (
    "https://apis.data.go.kr/1160100/service/GetStockSecuritiesInfoService/getStockPriceInfo"
)
ISSUANCE_ENDPOINT = "https://apis.data.go.kr/1160100/GetStocIssuInfoService_V3/getItemBasiInfo_V3"
PRICE_SOURCE_URL = "https://www.data.go.kr/data/15094808/openapi.do"
ISSUANCE_SOURCE_URL = "https://www.data.go.kr/data/15043423/openapi.do"
KIND_SOURCE_URL = (
    "https://kind.krx.co.kr/investwarn/investattentwarnrisky.do?method=investattentwarnriskyMain"
)
KIND_DETAIL_SOURCE_URL = "https://kind.krx.co.kr/disclosure/details.do?method=searchDetailsMain"
KIND_DETAIL_ENDPOINT = "https://kind.krx.co.kr/disclosure/details.do"
KIND_MARKET_ACTION_TYPES = {
    "caution": ("0341", "CAUTION"),
    "warning": ("0342", "WARNING"),
    "danger": ("0343", "DANGER"),
    "managed": ("0350", "MANAGED"),
    "watchlist": ("0356", "WATCHLIST"),
    "halted": ("0311", "HALTED"),
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_gzip_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(temporary, "wt", encoding="utf-8", compresslevel=6) as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
    temporary.replace(path)


def _read_gzip_json(path: Path) -> dict[str, Any]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise DataSourceError(f"공공데이터 원본 객체가 아닙니다: {path.name}")
    return payload


@dataclass(frozen=True)
class PageResult:
    page: int
    total_count: int
    items: list[dict[str, Any]]


class PublicDataPortalClient:
    """Read-only client for the two approved Financial Services Commission APIs."""

    def __init__(self, service_key: str | None = None) -> None:
        configured = (service_key or os.getenv("DATA_GO_KR_SERVICE_KEY", "")).strip()
        self.service_key = unquote(configured)

    @property
    def configured(self) -> bool:
        return len(self.service_key) >= 20

    def _get_page(
        self,
        endpoint: str,
        *,
        page: int,
        page_size: int,
        params: dict[str, str],
    ) -> PageResult:
        if not self.configured:
            raise DataSourceError("DATA_GO_KR_SERVICE_KEY 인증키가 필요합니다.")
        query = urlencode(
            {
                "serviceKey": self.service_key,
                "resultType": "json",
                "pageNo": page,
                "numOfRows": page_size,
                **params,
            }
        )
        request = Request(f"{endpoint}?{query}", headers={"User-Agent": "SignalGuild/0.5"})
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                with urlopen(request, timeout=60) as response:  # noqa: S310 - fixed HTTPS hosts
                    payload = json.loads(response.read().decode("utf-8"))
                header = payload.get("response", {}).get("header", {})
                if str(header.get("resultCode")) != "00":
                    raise DataSourceError(
                        f"공공데이터 API 오류: {header.get('resultMsg', 'unknown')}"
                    )
                body = payload["response"]["body"]
                item_payload = body.get("items", {}).get("item", [])
                if isinstance(item_payload, dict):
                    items = [item_payload]
                else:
                    items = list(item_payload or [])
                return PageResult(
                    page=page,
                    total_count=int(body.get("totalCount", 0)),
                    items=items,
                )
            except (OSError, TimeoutError, json.JSONDecodeError, KeyError, TypeError) as error:
                last_error = error
                if attempt < 2:
                    time.sleep(0.5 * (2**attempt))
        raise DataSourceError(f"공공데이터 API 호출 실패(page={page})") from last_error

    def price_page(
        self, *, start: date, end: date, page: int, page_size: int = 10_000
    ) -> PageResult:
        return self._get_page(
            PRICE_ENDPOINT,
            page=page,
            page_size=page_size,
            params={
                "beginBasDt": start.strftime("%Y%m%d"),
                "endBasDt": end.strftime("%Y%m%d"),
            },
        )

    def issuance_page(self, *, as_of: date, page: int, page_size: int = 1_000) -> PageResult:
        return self._get_page(
            ISSUANCE_ENDPOINT,
            page=page,
            page_size=page_size,
            params={"basDt": as_of.strftime("%Y%m%d")},
        )


def _collect_pages(
    *,
    first: PageResult,
    page_size: int,
    output_dir: Path,
    prefix: str,
    fetch: Any,
    workers: int,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    page_count = max(1, math.ceil(first.total_count / page_size))

    def save(result: PageResult) -> dict[str, Any]:
        path = output_dir / f"{prefix}-{result.page:04d}.json.gz"
        if not path.exists():
            _atomic_gzip_json(
                path,
                {
                    "page": result.page,
                    "total_count": result.total_count,
                    "items": result.items,
                },
            )
        return {"page": result.page, "file": path.name, "sha256": _sha256(path)}

    saved: dict[int, dict[str, Any]] = {first.page: save(first)}
    missing = [
        page
        for page in range(2, page_count + 1)
        if not (output_dir / f"{prefix}-{page:04d}.json.gz").exists()
    ]
    with ThreadPoolExecutor(max_workers=max(1, min(workers, 6))) as pool:
        futures = {pool.submit(fetch, page): page for page in missing}
        for future in as_completed(futures):
            result = future.result()
            saved[result.page] = save(result)
    for page in range(1, page_count + 1):
        if page not in saved:
            path = output_dir / f"{prefix}-{page:04d}.json.gz"
            payload = _read_gzip_json(path)
            if int(payload.get("page", 0)) != page:
                raise DataSourceError(f"체크포인트 페이지 불일치: {path.name}")
            saved[page] = {"page": page, "file": path.name, "sha256": _sha256(path)}
    pages = [saved[index] for index in sorted(saved)]
    manifest_hash = hashlib.sha256("".join(item["sha256"] for item in pages).encode()).hexdigest()
    return {
        "total_count": first.total_count,
        "page_count": page_count,
        "page_size": page_size,
        "pages": pages,
        "combined_sha256": manifest_hash,
    }


def collect_price_history(
    client: PublicDataPortalClient,
    *,
    start: date,
    end: date,
    output_dir: Path,
    workers: int = 4,
) -> dict[str, Any]:
    page_size = 10_000
    first = client.price_page(start=start, end=end, page=1, page_size=page_size)
    result = _collect_pages(
        first=first,
        page_size=page_size,
        output_dir=output_dir,
        prefix="stock-prices",
        fetch=lambda page: client.price_page(start=start, end=end, page=page, page_size=page_size),
        workers=workers,
    )
    return {**result, "coverage_start": start.isoformat(), "coverage_end": end.isoformat()}


def collect_issuance_history(
    client: PublicDataPortalClient,
    *,
    as_of: date,
    output_dir: Path,
    workers: int = 4,
) -> dict[str, Any]:
    page_size = 1_000
    first = client.issuance_page(as_of=as_of, page=1, page_size=page_size)
    result = _collect_pages(
        first=first,
        page_size=page_size,
        output_dir=output_dir,
        prefix="stock-issuance",
        fetch=lambda page: client.issuance_page(as_of=as_of, page=page, page_size=page_size),
        workers=workers,
    )
    return {**result, "as_of": as_of.isoformat()}


class _HtmlTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag.lower() == "tr":
            self._row = []
        elif tag.lower() in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered in {"td", "th"} and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif lowered == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None


def read_kind_xls(path: str | Path) -> tuple[list[str], list[dict[str, str]]]:
    source = Path(path)
    try:
        payload = source.read_bytes()
    except OSError as error:
        raise DataSourceError(f"KIND XLS를 읽을 수 없습니다: {source.name}") from error
    html: str | None = None
    for encoding in ("utf-8-sig", "cp949"):
        try:
            html = payload.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if html is None:
        raise DataSourceError(f"KIND XLS 문자 인코딩을 판별할 수 없습니다: {source.name}")
    parser = _HtmlTableParser()
    parser.feed(html)
    if len(parser.rows) < 2:
        raise DataSourceError(f"KIND XLS 표가 비어 있습니다: {source.name}")
    headers = parser.rows[0]
    rows = [dict(zip(headers, row, strict=False)) for row in parser.rows[1:]]
    return headers, rows


def _kind_detail_windows(start: date, end: date) -> list[tuple[date, date]]:
    windows: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        try:
            next_year = cursor.replace(year=cursor.year + 1)
        except ValueError:
            next_year = cursor.replace(year=cursor.year + 1, day=28)
        window_end = min(end, next_year - date.resolution)
        windows.append((cursor, window_end))
        cursor = window_end + date.resolution
    return windows


def collect_kind_market_action_history(
    *,
    start: date,
    end: date,
    output_dir: str | Path,
    pause_seconds: float = 0.15,
) -> dict[str, Any]:
    """Download the public KIND detail-search Excel pages with audit hashes.

    KIND limits an unqualified detailed search to one year and its visible Excel
    export to the selected result page. We therefore checkpoint every 100-row
    page in adjacent one-year windows and stop only after a short final page.
    """
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    opener = build_opener(HTTPCookieProcessor(CookieJar()))
    opener.open(
        Request(KIND_DETAIL_SOURCE_URL, headers={"User-Agent": "SignalGuild/0.6"}), timeout=30
    ).read()
    inventory: list[dict[str, Any]] = []
    totals: dict[str, int] = {}
    for slug, (type_code, state) in KIND_MARKET_ACTION_TYPES.items():
        state_total = 0
        for window_start, window_end in _kind_detail_windows(start, end):
            page = 1
            while True:
                filename = f"{slug}-{window_start:%Y%m%d}-{window_end:%Y%m%d}-p{page:04d}.xls"
                path = root / filename
                if not path.exists():
                    body = urlencode(
                        {
                            "method": "searchDetailsSub",
                            "currentPageSize": "100",
                            "pageIndex": str(page),
                            "orderMode": "1",
                            "orderStat": "D",
                            "forward": "details_down",
                            "disclosureType02": f"{type_code}|",
                            "pDisclosureType02": f"{type_code}|",
                            "fromDate": window_start.isoformat(),
                            "toDate": window_end.isoformat(),
                            "marketType": "",
                            "business": "",
                            "settlementMonth": "",
                            "securities": "",
                            "submitOblgNm": "",
                            "enterprise": "",
                            "searchCorpName": "",
                            "repIsuSrtCd": "",
                            "allRepIsuSrtCd": "",
                            "reportNm": "",
                            "reportCd": "",
                            "bfrDsclsType": "on",
                        }
                    ).encode()
                    request = Request(
                        KIND_DETAIL_ENDPOINT,
                        data=body,
                        headers={
                            "User-Agent": "SignalGuild/0.6",
                            "Referer": KIND_DETAIL_SOURCE_URL,
                            "Content-Type": "application/x-www-form-urlencoded",
                        },
                    )
                    last_error: Exception | None = None
                    for attempt in range(3):
                        try:
                            payload = opener.open(request, timeout=45).read()
                            if b"<table" not in payload.lower():
                                raise DataSourceError("KIND Excel 응답에 표가 없습니다.")
                            temporary = path.with_suffix(path.suffix + ".tmp")
                            temporary.write_bytes(payload)
                            temporary.replace(path)
                            break
                        except (OSError, TimeoutError, DataSourceError) as error:
                            last_error = error
                            if attempt < 2:
                                time.sleep(0.5 * (2**attempt))
                    else:
                        raise DataSourceError(
                            f"KIND {state} 이력 수집 실패: {window_start} page {page}"
                        ) from last_error
                    time.sleep(max(0.0, pause_seconds))
                headers, rows = read_kind_xls(path)
                expected = {"시간", "회사명", "종목코드", "공시제목", "제출인"}
                if not expected.issubset(headers):
                    raise DataSourceError(f"KIND 상세검색 열 형식 오류: {filename}")
                count = len(rows)
                state_total += count
                inventory.append(
                    {
                        "file": filename,
                        "state": state,
                        "coverage_start": window_start.isoformat(),
                        "coverage_end": window_end.isoformat(),
                        "page": page,
                        "rows": count,
                        "sha256": _sha256(path),
                    }
                )
                if count < 100:
                    break
                page += 1
        totals[state] = state_total
    combined = hashlib.sha256("".join(item["sha256"] for item in inventory).encode()).hexdigest()
    manifest = {
        "schema_version": "1.0",
        "source": "KRX_KIND_DETAIL_SEARCH_PUBLIC_EXCEL",
        "source_url": KIND_DETAIL_SOURCE_URL,
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "page_size": 100,
        "files": inventory,
        "totals": totals,
        "combined_sha256": combined,
        "complete": all(totals.get(state, 0) > 0 for _, state in KIND_MARKET_ACTION_TYPES.values()),
    }
    manifest_path = root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def inspect_kind_sources(
    root: str | Path, *, period_start: date, period_end: date
) -> dict[str, Any]:
    source_root = Path(root)
    files = (
        sorted(source_root.glob("*.xls"))
        + sorted((source_root / "investment-caution-quarterly").glob("*.xls"))
        + sorted((source_root / "market-actions").glob("*-p*.xls"))
    )
    if not files:
        raise DataSourceError("KIND 원본 XLS 파일이 없습니다.")
    inventory: list[dict[str, Any]] = []
    caution_dates: list[date] = []
    caution_coverage: list[tuple[date, date]] = []
    totals: dict[str, int] = {}
    for path in files:
        headers, rows = read_kind_xls(path)
        kind = "OTHER"
        if {"시간", "종목코드", "공시제목"}.issubset(headers):
            slug = path.name.split("-", 1)[0]
            kind = KIND_MARKET_ACTION_TYPES.get(slug, ("", "OTHER"))[1]
        elif "유형" in headers and "지정일" in headers:
            kind = "CAUTION"
            match = re.search(r"(\d{8})-(\d{8})", path.name)
            if match:
                caution_coverage.append(
                    (
                        date.fromisoformat(f"{match[1][:4]}-{match[1][4:6]}-{match[1][6:]}"),
                        date.fromisoformat(f"{match[2][:4]}-{match[2][4:6]}-{match[2][6:]}"),
                    )
                )
            for row in rows:
                try:
                    caution_dates.append(date.fromisoformat(row["지정일"]))
                except (KeyError, ValueError):
                    continue
        elif "해제일" in headers and "지정일" in headers:
            kind = "DANGER" if "위험" in path.name else "WARNING"
        elif "지정사유" in headers and "지정일" in headers:
            kind = "WATCHLIST" if "환기" in path.name else "MANAGED"
        elif "사유" in headers and "지정일" not in headers:
            kind = "HALTED_STATIC"
        totals[kind] = totals.get(kind, 0) + len(rows)
        inventory.append(
            {
                "file": str(path.relative_to(source_root)).replace("\\", "/"),
                "kind": kind,
                "rows": len(rows),
                "headers": headers,
                "sha256": _sha256(path),
            }
        )
    quarterly = [item for item in inventory if "investment-caution-quarterly/" in item["file"]]
    action_manifest_path = source_root / "market-actions" / "manifest.json"
    action_complete = False
    if action_manifest_path.is_file():
        try:
            action_manifest = json.loads(action_manifest_path.read_text(encoding="utf-8"))
            action_files = action_manifest.get("files", [])
            action_complete = (
                action_manifest.get("period_start") == period_start.isoformat()
                and action_manifest.get("period_end") == period_end.isoformat()
                and action_manifest.get("complete") is True
                and bool(action_files)
                and all(
                    (source_root / "market-actions" / item["file"]).is_file()
                    and _sha256(source_root / "market-actions" / item["file"]) == item["sha256"]
                    for item in action_files
                )
            )
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            action_complete = False
    detailed_caution_complete = action_complete and totals.get("CAUTION", 0) > 0
    checks = {
        # Twenty quarter-sized exports cover five years. A small leading/trailing
        # boundary export is allowed, so completeness must not fail at 21 files.
        "caution_quarter_files": detailed_caution_complete or len(quarterly) >= 20,
        "caution_period_start_covered": (
            detailed_caution_complete
            or (
                bool(caution_coverage)
                and min(item[0] for item in caution_coverage) <= period_start
            )
        )
        or (bool(caution_dates) and min(caution_dates) <= period_start),
        "caution_period_end_covered": (
            detailed_caution_complete
            or (
                bool(caution_coverage)
                and max(item[1] for item in caution_coverage) >= period_end
            )
        )
        or (bool(caution_dates) and max(caution_dates) >= period_end),
        "warning_intervals_present": totals.get("WARNING", 0) > 0,
        "danger_intervals_present": totals.get("DANGER", 0) > 0,
        "managed_intervals_complete": action_complete and totals.get("MANAGED", 0) > 0,
        "watchlist_intervals_complete": action_complete and totals.get("WATCHLIST", 0) > 0,
        "halted_intervals_complete": action_complete and totals.get("HALTED", 0) > 0,
    }
    return {
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "files": inventory,
        "totals": totals,
        "checks": checks,
        "historical_designation_states_complete": all(checks.values()),
        "combined_sha256": hashlib.sha256(
            "".join(item["sha256"] for item in inventory).encode()
        ).hexdigest(),
        "source_url": KIND_SOURCE_URL,
    }


def _optional_api_date(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:8])).isoformat()
    except (ValueError, IndexError) as error:
        raise DataSourceError(f"공공데이터 날짜 형식 오류: {text}") from error


def _kind_date(value: Any) -> str | None:
    text = str(value or "").strip().replace(".", "-")
    if not text or text in {"-", "--"}:
        return None
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError as error:
        raise DataSourceError(f"KIND 날짜 형식 오류: {text}") from error


def _normalize_kind_rows(
    connection: sqlite3.Connection,
    kind_root: Path,
    *,
    period_start: date,
    period_end: date,
) -> dict[str, int]:
    """Store complete intervals separately from incomplete current observations."""
    connection.executescript(
        """
        CREATE TABLE designation_intervals (
            symbol TEXT NOT NULL,
            state TEXT NOT NULL,
            effective_from TEXT NOT NULL,
            effective_to TEXT NOT NULL,
            source_file TEXT NOT NULL,
            PRIMARY KEY (symbol, state, effective_from, effective_to)
        );
        CREATE INDEX idx_designation_interval_date
            ON designation_intervals(effective_from, effective_to);
        CREATE TABLE status_observations (
            symbol TEXT NOT NULL,
            state TEXT NOT NULL,
            observed_on TEXT NOT NULL,
            designated_on TEXT,
            reason TEXT NOT NULL,
            source_file TEXT NOT NULL,
            interval_complete INTEGER NOT NULL CHECK (interval_complete IN (0, 1)),
            PRIMARY KEY (symbol, state, observed_on, source_file)
        );
        """
    )
    universe = {row[0] for row in connection.execute("SELECT symbol FROM lifecycle").fetchall()}
    files = (
        sorted(kind_root.glob("*.xls"))
        + sorted((kind_root / "investment-caution-quarterly").glob("*.xls"))
        + sorted((kind_root / "market-actions").glob("*-p*.xls"))
    )
    intervals: list[tuple[str, str, str, str, str]] = []
    observations: list[tuple[str, str, str, str | None, str, str, int]] = []
    action_events: dict[tuple[str, str], list[tuple[str, str, str]]] = {}
    available_action_states = {
        KIND_MARKET_ACTION_TYPES[path.name.split("-", 1)[0]][1]
        for path in files
        if path.parent.name == "market-actions"
        and path.name.split("-", 1)[0] in KIND_MARKET_ACTION_TYPES
    }
    for path in files:
        headers, rows = read_kind_xls(path)
        relative = str(path.relative_to(kind_root)).replace("\\", "/")
        if {"시간", "종목코드", "공시제목"}.issubset(headers):
            slug = path.name.split("-", 1)[0]
            state = KIND_MARKET_ACTION_TYPES.get(slug, ("", "OTHER"))[1]
            if state == "OTHER":
                continue
            for row in rows:
                symbol = str(row.get("종목코드", "")).strip()
                timestamp = str(row.get("시간", "")).strip().replace(".", "-")
                title = str(row.get("공시제목", "")).strip()
                if symbol not in universe or len(timestamp) < 10:
                    continue
                try:
                    event_day = date.fromisoformat(timestamp[:10]).isoformat()
                except ValueError:
                    continue
                if period_start.isoformat() <= event_day <= period_end.isoformat():
                    action_events.setdefault((state, symbol), []).append(
                        (timestamp, event_day, title)
                    )
            continue
        if "유형" in headers and "지정일" in headers:
            state = "CAUTION"
        elif "해제일" in headers and "지정일" in headers:
            state = "DANGER" if "위험" in path.name else "WARNING"
        elif "지정사유" in headers and "지정일" in headers:
            state = "WATCHLIST" if "환기" in path.name else "MANAGED"
        elif "사유" in headers and "지정일" not in headers:
            state = "HALTED"
        else:
            continue
        if state in available_action_states:
            continue
        for row in rows:
            symbol = str(row.get("종목코드", "")).strip()
            if symbol not in universe:
                continue
            designated_on = _kind_date(row.get("지정일"))
            reason = str(row.get("유형") or row.get("지정사유") or row.get("사유") or "").strip()
            if (
                state == "CAUTION"
                and designated_on
                and period_start.isoformat() <= designated_on <= period_end.isoformat()
            ):
                intervals.append((symbol, state, designated_on, designated_on, relative))
            elif state in {"WARNING", "DANGER"} and designated_on:
                released_on = _kind_date(row.get("해제일")) or period_end.isoformat()
                intervals.append(
                    (
                        symbol,
                        state,
                        max(designated_on, period_start.isoformat()),
                        min(released_on, period_end.isoformat()),
                        relative,
                    )
                )
            elif (
                state in {"MANAGED", "WATCHLIST", "HALTED"} and state not in available_action_states
            ):
                observations.append(
                    (
                        symbol,
                        state,
                        period_end.isoformat(),
                        designated_on,
                        reason,
                        relative,
                        0,
                    )
                )
    for (state, symbol), events in action_events.items():
        active_from: str | None = None
        for _, event_day, title in sorted(set(events)):
            compact = re.sub(r"\s+", "", title)
            partial_release = "일부해제" in compact
            if state == "CAUTION":
                if "투자주의종목" in compact:
                    intervals.append(
                        (
                            symbol,
                            state,
                            event_day,
                            event_day,
                            "market-actions/manifest.json",
                        )
                    )
                continue
            if state == "MANAGED":
                full_release = "관리종목해제" in compact and not partial_release
                designation = "관리종목지정" in compact
            elif state == "WATCHLIST":
                full_release = "투자주의환기종목해제" in compact and not partial_release
                designation = "투자주의환기종목지정" in compact
            elif state == "WARNING":
                full_release = (
                    "투자경고종목지정해제" in compact or "투자경고종목해제" in compact
                ) and not partial_release
                designation = "투자경고종목지정" in compact and not full_release
            elif state == "DANGER":
                full_release = (
                    "투자위험종목지정해제" in compact or "투자위험종목해제" in compact
                ) and not partial_release
                designation = "투자위험종목지정" in compact and not full_release
            else:
                full_release = (
                    "매매거래정지해제" in compact or "매매거래재개" in compact
                ) and not partial_release
                designation = "매매거래정지" in compact and not full_release
            if full_release:
                start_day = active_from or period_start.isoformat()
                intervals.append(
                    (
                        symbol,
                        state,
                        start_day,
                        min(event_day, period_end.isoformat()),
                        "market-actions/manifest.json",
                    )
                )
                active_from = None
            elif designation or partial_release:
                if active_from is None:
                    active_from = max(event_day, period_start.isoformat())
        if active_from is not None:
            intervals.append(
                (
                    symbol,
                    state,
                    active_from,
                    period_end.isoformat(),
                    "market-actions/manifest.json",
                )
            )

    benchmark_days = [
        row[0]
        for row in connection.execute(
            """
            SELECT trading_date FROM benchmark_bars
            WHERE trading_date BETWEEN ? AND ? ORDER BY trading_date
            """,
            (period_start.isoformat(), period_end.isoformat()),
        ).fetchall()
    ]
    if action_events and benchmark_days:
        benchmark_index = {day: index for index, day in enumerate(benchmark_days)}
        for symbol, listed_on, delisted_on in connection.execute(
            "SELECT symbol, listed_on, delisted_on FROM lifecycle"
        ).fetchall():
            lower = max(str(listed_on), period_start.isoformat())
            upper = min(str(delisted_on or period_end.isoformat()), period_end.isoformat())
            eligible_days = [day for day in benchmark_days if lower <= day <= upper]
            present = {
                row[0]
                for row in connection.execute(
                    """
                    SELECT trading_date FROM price_bars
                    WHERE symbol = ? AND trading_date BETWEEN ? AND ?
                    """,
                    (symbol, lower, upper),
                ).fetchall()
            }
            missing = [day for day in eligible_days if day not in present]
            if not missing:
                continue
            start_day = previous = missing[0]
            for day in missing[1:]:
                if benchmark_index[day] != benchmark_index[previous] + 1:
                    intervals.append(
                        (
                            symbol,
                            "HALTED",
                            start_day,
                            previous,
                            "market-actions/manifest.json+public-data/prices",
                        )
                    )
                    start_day = day
                previous = day
            intervals.append(
                (
                    symbol,
                    "HALTED",
                    start_day,
                    previous,
                    "market-actions/manifest.json+public-data/prices",
                )
            )
    connection.executemany(
        """
        INSERT OR IGNORE INTO designation_intervals (
            symbol, state, effective_from, effective_to, source_file
        ) VALUES (?, ?, ?, ?, ?)
        """,
        [item for item in intervals if item[2] <= item[3]],
    )
    connection.executemany(
        """
        INSERT OR IGNORE INTO status_observations (
            symbol, state, observed_on, designated_on, reason,
            source_file, interval_complete
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        observations,
    )
    return {
        "designation_interval_count": connection.execute(
            "SELECT COUNT(*) FROM designation_intervals"
        ).fetchone()[0],
        "incomplete_status_observation_count": connection.execute(
            "SELECT COUNT(*) FROM status_observations WHERE interval_complete = 0"
        ).fetchone()[0],
    }


def normalize_collected_market_data(
    raw_root: str | Path,
    *,
    period_start: date,
    period_end: date,
    output_database: str | Path,
) -> dict[str, Any]:
    """Normalize page checkpoints into an auditable SQLite staging database."""
    root = Path(raw_root)
    issuance_files = sorted((root / "public-data" / "issuance").glob("*.json.gz"))
    price_files = sorted((root / "public-data" / "prices").glob("*.json.gz"))
    if not issuance_files or not price_files:
        raise DataSourceError("공공데이터 가격 또는 발행정보 체크포인트가 없습니다.")

    basics: dict[str, dict[str, Any]] = {}
    symbols_to_isins: dict[str, set[str]] = {}
    for path in issuance_files:
        for item in _read_gzip_json(path).get("items", []):
            if str(item.get("scrsItmsKcdNm", "")).strip() != "보통주":
                continue
            symbol = str(item.get("itmsShrtnCd", "")).strip()
            isin = str(item.get("isinCd", "")).strip()
            if len(symbol) != 6 or not symbol.isdigit() or not isin:
                continue
            listed_on = _optional_api_date(item.get("lstgDt"))
            if not listed_on:
                continue
            candidate = {
                "symbol": symbol,
                "isin": isin,
                "name": str(item.get("stckIssuCmpyNm", "")).strip(),
                "listed_on": listed_on,
                "delisted_on": _optional_api_date(item.get("lstgAbolDt")),
            }
            existing = basics.get(isin)
            if existing is None or candidate["listed_on"] < existing["listed_on"]:
                basics[isin] = candidate
            symbols_to_isins.setdefault(symbol, set()).add(isin)
    reused_symbols = {symbol for symbol, isins in symbols_to_isins.items() if len(isins) > 1}

    target = Path(output_database).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    if temporary.exists():
        temporary.unlink()
    connection = sqlite3.connect(temporary)
    try:
        connection.executescript(
            """
            PRAGMA journal_mode = WAL;
            PRAGMA synchronous = NORMAL;
            CREATE TABLE price_bars (
                symbol TEXT NOT NULL,
                isin TEXT NOT NULL,
                market TEXT NOT NULL,
                name TEXT NOT NULL,
                trading_date TEXT NOT NULL,
                open_price INTEGER NOT NULL,
                high_price INTEGER NOT NULL,
                low_price INTEGER NOT NULL,
                close_price INTEGER NOT NULL,
                volume INTEGER NOT NULL,
                PRIMARY KEY (symbol, isin, trading_date)
            );
            CREATE INDEX idx_price_bars_date ON price_bars(trading_date);
            CREATE TABLE benchmark_bars (
                trading_date TEXT PRIMARY KEY,
                open_price INTEGER NOT NULL,
                high_price INTEGER NOT NULL,
                low_price INTEGER NOT NULL,
                close_price INTEGER NOT NULL,
                volume INTEGER NOT NULL
            );
            CREATE TABLE lifecycle (
                symbol TEXT NOT NULL,
                isin TEXT NOT NULL,
                name TEXT NOT NULL,
                market TEXT NOT NULL,
                listed_on TEXT NOT NULL,
                delisted_on TEXT,
                bar_count INTEGER NOT NULL,
                first_bar TEXT NOT NULL,
                last_bar TEXT NOT NULL,
                PRIMARY KEY (symbol, isin)
            );
            """
        )
        inserted = 0
        skipped_invalid = 0
        for path in price_files:
            rows: list[tuple[Any, ...]] = []
            benchmarks: list[tuple[Any, ...]] = []
            for item in _read_gzip_json(path).get("items", []):
                symbol = str(item.get("srtnCd", "")).strip()
                isin = str(item.get("isinCd", "")).strip()
                market = str(item.get("mrktCtg", "")).strip()
                raw_day = str(item.get("basDt", "")).strip()
                try:
                    day = date(int(raw_day[:4]), int(raw_day[4:6]), int(raw_day[6:8])).isoformat()
                    values = tuple(
                        int(str(item.get(field, "0")).replace(",", ""))
                        for field in ("mkp", "hipr", "lopr", "clpr", "trqu")
                    )
                except (ValueError, IndexError):
                    skipped_invalid += 1
                    continue
                if not period_start.isoformat() <= day <= period_end.isoformat():
                    continue
                if (
                    min(values) <= 0
                    or values[2] > min(values[0], values[3])
                    or values[1] < max(values[0], values[3])
                ):
                    skipped_invalid += 1
                    continue
                if symbol == "069500":
                    benchmarks.append((day, *values))
                basic = basics.get(isin)
                if (
                    market not in {"KOSPI", "KOSDAQ"}
                    or basic is None
                    or symbol in reused_symbols
                    or basic["symbol"] != symbol
                ):
                    continue
                if day < basic["listed_on"] or (
                    basic["delisted_on"] and day > basic["delisted_on"]
                ):
                    skipped_invalid += 1
                    continue
                rows.append(
                    (
                        symbol,
                        isin,
                        market,
                        str(item.get("itmsNm", basic["name"])).strip(),
                        day,
                        *values,
                    )
                )
            connection.executemany(
                """
                INSERT OR IGNORE INTO price_bars (
                    symbol, isin, market, name, trading_date, open_price,
                    high_price, low_price, close_price, volume
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            connection.executemany(
                """
                INSERT OR IGNORE INTO benchmark_bars (
                    trading_date, open_price, high_price, low_price, close_price, volume
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                benchmarks,
            )
            inserted += len(rows)
            connection.commit()

        aggregates = connection.execute(
            """
            SELECT symbol, isin, MIN(name), MIN(market), COUNT(*),
                   MIN(trading_date), MAX(trading_date)
            FROM price_bars
            GROUP BY symbol, isin
            HAVING COUNT(*) >= 200
            """
        ).fetchall()
        lifecycle_rows: list[tuple[Any, ...]] = []
        for symbol, isin, name, market, count, first_bar, last_bar in aggregates:
            basic = basics[isin]
            lifecycle_rows.append(
                (
                    symbol,
                    isin,
                    name or basic["name"],
                    market,
                    basic["listed_on"],
                    basic["delisted_on"],
                    count,
                    first_bar,
                    last_bar,
                )
            )
        connection.executemany(
            """
            INSERT INTO lifecycle (
                symbol, isin, name, market, listed_on, delisted_on,
                bar_count, first_bar, last_bar
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            lifecycle_rows,
        )
        connection.execute(
            """
            DELETE FROM price_bars
            WHERE NOT EXISTS (
                SELECT 1 FROM lifecycle
                WHERE lifecycle.symbol = price_bars.symbol
                  AND lifecycle.isin = price_bars.isin
            )
            """
        )
        kind_metrics = _normalize_kind_rows(
            connection,
            root / "kind",
            period_start=period_start,
            period_end=period_end,
        )
        connection.commit()
        metrics = {
            "raw_price_rows_inserted": inserted,
            "qualified_price_rows": connection.execute(
                "SELECT COUNT(*) FROM price_bars"
            ).fetchone()[0],
            "benchmark_rows": connection.execute("SELECT COUNT(*) FROM benchmark_bars").fetchone()[
                0
            ],
            "instrument_count": connection.execute("SELECT COUNT(*) FROM lifecycle").fetchone()[0],
            "historical_delisted_count": connection.execute(
                """
                SELECT COUNT(*) FROM lifecycle
                WHERE delisted_on BETWEEN ? AND ?
                """,
                (period_start.isoformat(), period_end.isoformat()),
            ).fetchone()[0],
            "reused_symbol_count_excluded": len(reused_symbols),
            "invalid_or_nontrading_rows_skipped": skipped_invalid,
            **kind_metrics,
        }
    finally:
        connection.close()
    temporary.replace(target)
    return {
        "database": str(target),
        "database_sha256": _sha256(target),
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        **metrics,
    }


def refresh_normalized_kind_data(
    raw_root: str | Path,
    *,
    period_start: date,
    period_end: date,
    output_database: str | Path,
) -> dict[str, Any]:
    """Atomically refresh KIND tables while preserving normalized price history."""
    root = Path(raw_root)
    target = Path(output_database).resolve()
    if not target.is_file():
        return normalize_collected_market_data(
            root,
            period_start=period_start,
            period_end=period_end,
            output_database=target,
        )
    temporary = target.with_suffix(target.suffix + ".refresh.tmp")
    if temporary.exists():
        temporary.unlink()
    source = sqlite3.connect(target)
    destination = sqlite3.connect(temporary)
    try:
        source.backup(destination)
    finally:
        source.close()
        destination.close()
    connection = sqlite3.connect(temporary)
    try:
        connection.executescript(
            """
            DROP TABLE IF EXISTS designation_intervals;
            DROP TABLE IF EXISTS status_observations;
            """
        )
        kind_metrics = _normalize_kind_rows(
            connection,
            root / "kind",
            period_start=period_start,
            period_end=period_end,
        )
        connection.commit()
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise DataSourceError("정규화 DB 무결성 검사에 실패했습니다.")
        metrics = {
            "qualified_price_rows": connection.execute(
                "SELECT COUNT(*) FROM price_bars"
            ).fetchone()[0],
            "benchmark_rows": connection.execute("SELECT COUNT(*) FROM benchmark_bars").fetchone()[
                0
            ],
            "instrument_count": connection.execute("SELECT COUNT(*) FROM lifecycle").fetchone()[0],
            "historical_delisted_count": connection.execute(
                "SELECT COUNT(*) FROM lifecycle WHERE delisted_on BETWEEN ? AND ?",
                (period_start.isoformat(), period_end.isoformat()),
            ).fetchone()[0],
            **kind_metrics,
        }
    finally:
        connection.close()
    temporary.replace(target)
    return {
        "database": str(target),
        "database_sha256": _sha256(target),
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "refreshed_existing_prices": True,
        **metrics,
    }


def merge_kiwoom_benchmark_history(
    output_database: str | Path,
    snapshot_database: str | Path,
    *,
    period_start: date,
    period_end: date,
) -> dict[str, Any]:
    """Merge an already audited Kiwoom KODEX 200 snapshot as benchmark history."""
    target = Path(output_database).resolve()
    source_path = Path(snapshot_database).resolve()
    if not target.is_file() or not source_path.is_file():
        raise DataSourceError("정규화 DB 또는 Signal Guild 스냅샷 DB가 없습니다.")
    source = sqlite3.connect(source_path)
    try:
        row = source.execute(
            """
            SELECT id, checksum, payload_json
            FROM market_snapshots
            WHERE source = 'KIWOOM_OFFICIAL_REST' AND quality_state = 'PASS'
            ORDER BY as_of DESC, created_at DESC
            LIMIT 1
            """
        ).fetchone()
    finally:
        source.close()
    if row is None:
        raise DataSourceError("품질 PASS인 키움 공식 시장 스냅샷이 없습니다.")
    snapshot_id, snapshot_checksum, payload_json = row
    try:
        payload = json.loads(payload_json)
        benchmark = payload["benchmark"]
        if benchmark.get("symbol") != "069500":
            raise KeyError("benchmark symbol")
        bars = benchmark["bars"]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise DataSourceError("키움 벤치마크 스냅샷 형식이 올바르지 않습니다.") from error
    normalized: list[tuple[Any, ...]] = []
    for item in bars:
        day = _kind_date(item.get("date"))
        if day is None or not period_start.isoformat() <= day <= period_end.isoformat():
            continue
        try:
            values = tuple(int(item[field]) for field in ("open", "high", "low", "close", "volume"))
        except (KeyError, TypeError, ValueError) as error:
            raise DataSourceError("키움 벤치마크 가격 형식이 올바르지 않습니다.") from error
        if (
            min(values) <= 0
            or values[2] > min(values[0], values[3])
            or values[1] < max(values[0], values[3])
        ):
            raise DataSourceError(f"키움 벤치마크 OHLCV 오류: {day}")
        normalized.append((day, *values))
    if len(normalized) < 1_200:
        raise DataSourceError("키움 벤치마크가 5년 검증 구간을 충분히 덮지 않습니다.")
    connection = sqlite3.connect(target)
    try:
        connection.execute("DELETE FROM benchmark_bars")
        connection.executemany(
            """
            INSERT INTO benchmark_bars (
                trading_date, open_price, high_price, low_price, close_price, volume
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            normalized,
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS source_provenance (
                kind TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                reference TEXT NOT NULL,
                sha256 TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT OR REPLACE INTO source_provenance(kind, source, reference, sha256)
            VALUES ('BENCHMARK', 'KIWOOM_OFFICIAL_REST', ?, ?)
            """,
            (str(snapshot_id), str(snapshot_checksum)),
        )
        connection.commit()
    finally:
        connection.close()
    return {
        "benchmark_rows": len(normalized),
        "benchmark_symbol": "069500",
        "benchmark_source": "KIWOOM_OFFICIAL_REST",
        "benchmark_snapshot_id": str(snapshot_id),
        "benchmark_snapshot_sha256": str(snapshot_checksum),
    }


def _canonical_json_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def build_qualified_bundle_from_normalized_database(
    database_path: str | Path,
    collection_manifest_path: str | Path,
    output_path: str | Path,
) -> dict[str, Any]:
    """Build the schema 2.0 artifact consumed by the fail-closed validator."""
    database = Path(database_path).resolve()
    manifest_path = Path(collection_manifest_path).resolve()
    target = Path(output_path).resolve()
    if not database.is_file() or not manifest_path.is_file():
        raise DataSourceError("정규화 DB 또는 수집 manifest가 없습니다.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    kind = manifest.get("kind", {})
    if kind.get("historical_designation_states_complete") is not True:
        raise DataSourceError("지정상태 이력이 완전하지 않아 자격 번들을 만들 수 없습니다.")
    period_start = str(kind["period_start"])
    period_end = str(kind["period_end"])
    connection = sqlite3.connect(database)
    try:
        benchmark_bars = [
            {
                "date": row[0],
                "open": row[1],
                "high": row[2],
                "low": row[3],
                "close": row[4],
                "volume": row[5],
            }
            for row in connection.execute(
                """
                SELECT trading_date, open_price, high_price, low_price,
                       close_price, volume
                FROM benchmark_bars ORDER BY trading_date
                """
            ).fetchall()
        ]
        lifecycle_rows = connection.execute(
            """
            SELECT symbol, name, market, listed_on, delisted_on
            FROM lifecycle ORDER BY symbol
            """
        ).fetchall()
        universe = [
            {
                "symbol": row[0],
                "name": row[1],
                "market": row[2],
                "security_type": "COMMON_STOCK",
                "listed_on": row[3],
                "delisted_on": row[4],
            }
            for row in lifecycle_rows
        ]
        metadata = {row[0]: row for row in lifecycle_rows}
        instruments: list[dict[str, Any]] = []
        current_symbol: str | None = None
        current_bars: list[dict[str, Any]] = []

        def append_instrument() -> None:
            if current_symbol is None:
                return
            row = metadata[current_symbol]
            instruments.append(
                {
                    "symbol": current_symbol,
                    "name": row[1],
                    "sector": row[2],
                    "bars": list(current_bars),
                    "fundamentals": {
                        "revenue_growth": False,
                        "operating_profit_positive": False,
                        "operating_cashflow_positive": False,
                        "debt_ratio_ok": False,
                    },
                    "fundamentals_history": [],
                    "catalyst_evidence_ids": [],
                    "customer_concentration": "UNKNOWN",
                }
            )

        for row in connection.execute(
            """
            SELECT symbol, trading_date, open_price, high_price, low_price,
                   close_price, volume
            FROM price_bars ORDER BY symbol, trading_date
            """
        ):
            if row[0] != current_symbol:
                append_instrument()
                current_symbol = row[0]
                current_bars = []
            current_bars.append(
                {
                    "date": row[1],
                    "open": row[2],
                    "high": row[3],
                    "low": row[4],
                    "close": row[5],
                    "volume": row[6],
                }
            )
        append_instrument()
        designations = [
            {
                "symbol": row[0],
                "state": row[1],
                "effective_from": row[2],
                "effective_to": row[3],
            }
            for row in connection.execute(
                """
                SELECT symbol, state, effective_from, effective_to
                FROM designation_intervals
                ORDER BY symbol, state, effective_from, effective_to
                """
            ).fetchall()
        ]
    finally:
        connection.close()
    benchmark = {
        "symbol": "069500",
        "name": "KODEX 200",
        "role": "KOSPI_200_PROXY",
        "bars": benchmark_bars,
    }
    normalized = {
        "KRX_DAILY_PRICES": _canonical_json_hash(
            {
                "benchmark": benchmark,
                "instruments": [
                    {"symbol": item["symbol"], "bars": item["bars"]} for item in instruments
                ],
            }
        ),
        "KRX_LISTING_LIFECYCLE": _canonical_json_hash(universe),
        "KRX_DESIGNATION_HISTORY": _canonical_json_hash(designations),
    }
    artifacts = [
        {
            "kind": "KRX_DAILY_PRICES",
            "source_url": PRICE_SOURCE_URL,
            "sha256": manifest["prices"]["combined_sha256"],
            "normalized_sha256": normalized["KRX_DAILY_PRICES"],
            "coverage_start": period_start,
            "coverage_end": period_end,
        },
        {
            "kind": "KRX_LISTING_LIFECYCLE",
            "source_url": ISSUANCE_SOURCE_URL,
            "sha256": manifest["issuance"]["combined_sha256"],
            "normalized_sha256": normalized["KRX_LISTING_LIFECYCLE"],
            "coverage_start": period_start,
            "coverage_end": period_end,
        },
        {
            "kind": "KRX_DESIGNATION_HISTORY",
            "source_url": KIND_DETAIL_SOURCE_URL,
            "sha256": kind["combined_sha256"],
            "normalized_sha256": normalized["KRX_DESIGNATION_HISTORY"],
            "coverage_start": period_start,
            "coverage_end": period_end,
        },
    ]
    payload = {
        "schema_version": "2.0",
        "as_of": f"{period_end}T15:40:00+09:00",
        "source": "OFFICIAL_PUBLIC_DATA_API_KIND_AND_KIWOOM",
        "license_basis": "USER_AUTHORIZED_INTERNAL_RESEARCH",
        "benchmark": benchmark,
        "instruments": instruments,
        "evidence": [
            {
                "id": "ev_qualified_market_archive",
                "source": "FSC_PUBLIC_DATA_KIND_KIWOOM",
                "title": "공식 가격·상장생애·시장조치 시점 데이터",
                "published_at": f"{period_end}T15:40:00+09:00",
                "url": KIND_DETAIL_SOURCE_URL,
                "verified": True,
            }
        ],
        "qualification": {
            "period_start": period_start,
            "period_end": period_end,
            "universe_history": universe,
            "designation_history": designations,
            "source_artifacts": artifacts,
        },
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    with gzip.open(temporary, "wt", encoding="utf-8", compresslevel=6) as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
    temporary.replace(target)
    return {
        "file": str(target),
        "sha256": _sha256(target),
        "compressed_bytes": target.stat().st_size,
        "instrument_count": len(instruments),
        "price_bar_count": sum(len(item["bars"]) for item in instruments),
        "designation_interval_count": len(designations),
    }
