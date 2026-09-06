from __future__ import annotations

import csv
import io
import json
import os
import re
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from xml.etree import ElementTree


class DataSourceError(RuntimeError):
    pass


class OpenDartClient:
    api_root = "https://opendart.fss.or.kr/api"

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or os.getenv("OPENDART_API_KEY", "")

    @property
    def configured(self) -> bool:
        return len(self.api_key) == 40

    def _require_key(self) -> None:
        if not self.configured:
            raise DataSourceError("OPENDART_API_KEY 40자리 인증키가 필요합니다.")

    def _get_json(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        self._require_key()
        query = urlencode({"crtfc_key": self.api_key, **params})
        request = Request(
            f"{self.api_root}/{endpoint}?{query}", headers={"User-Agent": "SignalGuild/0.4"}
        )
        with urlopen(request, timeout=15) as response:  # noqa: S310 - fixed official HTTPS host
            payload = json.loads(response.read().decode("utf-8"))
        if payload.get("status") not in {"000", "013"}:
            raise DataSourceError(f"OpenDART 오류: {payload.get('message', 'unknown')}")
        return payload

    def search_disclosures(
        self,
        *,
        corp_code: str,
        begin_date: str,
        end_date: str,
        last_report_only: bool = True,
    ) -> list[dict[str, Any]]:
        payload = self._get_json(
            "list.json",
            {
                "corp_code": corp_code,
                "bgn_de": begin_date,
                "end_de": end_date,
                "last_reprt_at": "Y" if last_report_only else "N",
                "page_count": 100,
            },
        )
        return payload.get("list", [])

    def financial_statements(
        self, *, corp_code: str, business_year: str, report_code: str, fs_div: str = "CFS"
    ) -> list[dict[str, Any]]:
        payload = self._get_json(
            "fnlttSinglAcntAll.json",
            {
                "corp_code": corp_code,
                "bsns_year": business_year,
                "reprt_code": report_code,
                "fs_div": fs_div,
            },
        )
        return payload.get("list", [])

    def corporate_codes(self) -> list[dict[str, str]]:
        self._require_key()
        query = urlencode({"crtfc_key": self.api_key})
        request = Request(
            f"{self.api_root}/corpCode.xml?{query}", headers={"User-Agent": "SignalGuild/0.4"}
        )
        with urlopen(request, timeout=30) as response:  # noqa: S310 - fixed official HTTPS host
            archive_bytes = response.read()
        try:
            with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
                xml_name = next(
                    name for name in archive.namelist() if name.lower().endswith(".xml")
                )
                root = ElementTree.fromstring(archive.read(xml_name))
        except (zipfile.BadZipFile, StopIteration, ElementTree.ParseError) as error:
            raise DataSourceError("OpenDART 고유번호 파일을 해석할 수 없습니다.") from error
        return [
            {
                "corp_code": item.findtext("corp_code", "").strip(),
                "corp_name": item.findtext("corp_name", "").strip(),
                "stock_code": item.findtext("stock_code", "").strip(),
                "modify_date": item.findtext("modify_date", "").strip(),
            }
            for item in root.findall("list")
            if item.findtext("stock_code", "").strip()
        ]


def market_import_directory() -> Path:
    return Path(os.getenv("MONEYGUN_MARKET_DATA_IMPORT_DIR", "data/imports")).resolve()


def load_market_bundle(file_name: str, root: str | Path | None = None) -> dict[str, Any]:
    """Load an authorized point-in-time JSON bundle from the configured import inbox."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\.json", file_name):
        raise DataSourceError("가져오기 파일명은 중첩 경로가 아닌 .json 파일이어야 합니다.")
    import_root = Path(root).resolve() if root else market_import_directory()
    path = (import_root / file_name).resolve()
    if path.parent != import_root:
        raise DataSourceError("가져오기 디렉터리 밖의 파일은 읽을 수 없습니다.")
    if not path.is_file():
        raise DataSourceError("가져오기 파일을 찾을 수 없습니다.")
    if path.stat().st_size > 50 * 1024 * 1024:
        raise DataSourceError("가져오기 번들은 50MB를 넘을 수 없습니다.")
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as error:
        raise DataSourceError("가져오기 JSON을 해석할 수 없습니다.") from error
    required = {"schema_version", "as_of", "source", "license_basis", "benchmark", "instruments"}
    if not required.issubset(payload):
        raise DataSourceError(f"번들 필수 필드: {', '.join(sorted(required))}")
    if payload["schema_version"] != "1.0":
        raise DataSourceError("지원하는 market bundle schema_version은 1.0입니다.")
    if payload["source"] not in {
        "KRX_AUTHORIZED_EXPORT",
        "LICENSED_VENDOR",
        "KIWOOM_OFFICIAL_REST",
    }:
        raise DataSourceError("실데이터 번들은 승인된 source 식별자를 사용해야 합니다.")
    if payload["license_basis"] not in {
        "USER_AUTHORIZED_INTERNAL_RESEARCH",
        "OFFICIAL_DATA_CONTRACT",
    }:
        raise DataSourceError("내부 연구 이용 권한 또는 공식 데이터 계약 확인이 필요합니다.")
    try:
        as_of = datetime.fromisoformat(payload["as_of"])
    except (TypeError, ValueError) as error:
        raise DataSourceError("as_of는 timezone을 포함한 ISO-8601 시각이어야 합니다.") from error
    if as_of.tzinfo is None:
        raise DataSourceError("as_of에는 timezone이 필요합니다.")
    if not payload["instruments"]:
        raise DataSourceError("가져오기 번들에는 최소 한 종목이 필요합니다.")
    for instrument in payload["instruments"]:
        for record in instrument.get("fundamentals_history", []):
            if datetime.fromisoformat(record["effective_at"]) > as_of:
                raise DataSourceError(f"{instrument.get('symbol', '?')}: 미래 재무정보가 있습니다.")
    for item in payload.get("evidence", []):
        if datetime.fromisoformat(item["published_at"]) > as_of:
            raise DataSourceError(f"{item.get('id', '?')}: 미래 근거가 있습니다.")
    return {
        "as_of": payload["as_of"],
        "source": payload["source"],
        "license_basis": payload["license_basis"],
        "market": "KR",
        "benchmark": payload["benchmark"],
        "instruments": payload["instruments"],
        "evidence": payload.get("evidence", []),
        "import_manifest": {
            "schema_version": payload["schema_version"],
            "file_name": file_name,
        },
    }


def load_krx_export_csv(path: str | Path) -> list[dict[str, int | str]]:
    """Load a user-authorized KRX-style export without relying on undocumented endpoints."""
    required = {"date", "open", "high", "low", "close", "volume"}
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise DataSourceError(f"CSV 필수 열: {', '.join(sorted(required))}")
        bars = [
            {
                "date": row["date"],
                "open": int(row["open"].replace(",", "")),
                "high": int(row["high"].replace(",", "")),
                "low": int(row["low"].replace(",", "")),
                "close": int(row["close"].replace(",", "")),
                "volume": int(row["volume"].replace(",", "")),
            }
            for row in reader
        ]
    if len(bars) < 200:
        raise DataSourceError("전략 계산에는 최소 200거래일이 필요합니다.")
    return bars
