from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def datetime_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class KiwoomError(RuntimeError):
    """A safe-to-display Kiwoom read-only connection error."""


class JsonTransport(Protocol):
    def post(
        self, url: str, headers: dict[str, str], payload: dict[str, Any]
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class JsonPage:
    payload: dict[str, Any]
    cont_yn: str = "N"
    next_key: str = ""


class UrllibJsonTransport:
    def post(self, url: str, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
        return self.post_page(url, headers, payload).payload

    def post_page(self, url: str, headers: dict[str, str], payload: dict[str, Any]) -> JsonPage:
        request = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=12) as response:  # noqa: S310 - fixed broker hosts only
                return JsonPage(
                    payload=json.loads(response.read().decode("utf-8")),
                    cont_yn=response.headers.get("cont-yn", "N").upper(),
                    next_key=response.headers.get("next-key", ""),
                )
        except HTTPError as error:
            raise KiwoomError(f"키움 조회가 HTTP {error.code}로 거절되었습니다.") from error
        except (URLError, TimeoutError, json.JSONDecodeError) as error:
            raise KiwoomError("키움 조회 서버에 안전하게 연결하지 못했습니다.") from error


@dataclass(frozen=True)
class KiwoomConfig:
    app_key: str
    secret_key: str
    environment: str = "mock"
    account_alias: str = "키움 주계좌"

    @classmethod
    def from_env(cls) -> KiwoomConfig:
        environment = os.getenv("KIWOOM_ENVIRONMENT", "mock").strip().lower()
        if environment not in {"mock", "production"}:
            environment = "mock"
        return cls(
            app_key=os.getenv("KIWOOM_APP_KEY", "").strip(),
            secret_key=os.getenv("KIWOOM_SECRET_KEY", "").strip(),
            environment=environment,
            account_alias=os.getenv("KIWOOM_ACCOUNT_ALIAS", "키움 주계좌").strip() or "키움 주계좌",
        )

    @property
    def configured(self) -> bool:
        return bool(self.app_key and self.secret_key)

    @property
    def base_url(self) -> str:
        return (
            "https://api.kiwoom.com"
            if self.environment == "production"
            else "https://mockapi.kiwoom.com"
        )


def _sanitize_account_payload(value: Any) -> Any:
    """Remove account identifiers before persistence or API responses."""
    if isinstance(value, list):
        return [_sanitize_account_payload(item) for item in value]
    if not isinstance(value, dict):
        return value
    result: dict[str, Any] = {}
    for key, item in value.items():
        normalized = key.lower().replace("-", "_")
        if normalized in {"account_no", "account_number", "acct_no", "acnt_no"}:
            text = str(item)
            result[key] = f"***{text[-4:]}" if len(text) >= 4 else "***"
        else:
            result[key] = _sanitize_account_payload(item)
    return result


class KiwoomReadOnlyClient:
    """Capability-limited client: OAuth plus domestic account evaluation only.

    There is intentionally no order method and no generic request escape hatch.
    """

    TOKEN_PATH = "/oauth2/token"
    ACCOUNT_PATH = "/api/dostk/acnt"
    ACCOUNT_API_ID = "kt00018"
    QUOTE_PATH = "/api/dostk/stkinfo"
    QUOTE_API_ID = "ka10001"
    OPEN_ORDERS_API_ID = "ka10075"
    FILLS_API_ID = "ka10076"
    RANK_PATH = "/api/dostk/rkinfo"
    TODAY_VOLUME_TOP_API_ID = "ka10030"
    CHART_PATH = "/api/dostk/chart"
    DAILY_CHART_API_ID = "ka10081"

    def __init__(
        self,
        config: KiwoomConfig | None = None,
        transport: JsonTransport | None = None,
    ) -> None:
        self.config = config or KiwoomConfig.from_env()
        self._transport = transport or UrllibJsonTransport()
        self._token: str | None = None
        self._token_cached_at = 0.0

    def status(self) -> dict[str, Any]:
        blockers: list[str] = []
        if not self.config.app_key:
            blockers.append("KIWOOM_APP_KEY가 설정되지 않았습니다.")
        if not self.config.secret_key:
            blockers.append("KIWOOM_SECRET_KEY가 설정되지 않았습니다.")
        return {
            "broker": "KIWOOM",
            "connection_mode": "READ_ONLY",
            "configured": self.config.configured,
            "environment": self.config.environment,
            "account_alias": self.config.account_alias,
            "credentials_present": self.config.configured,
            "token_cached": self._token is not None,
            "allowed_capabilities": [
                "OAUTH_TOKEN",
                "DOMESTIC_ACCOUNT_EVALUATION",
                "DOMESTIC_QUOTE",
                "OPEN_ORDER_QUERY",
                "FILL_QUERY",
                "DOMESTIC_DAILY_CHART",
                "DOMESTIC_MARKET_RANKING",
                "DOMESTIC_REALTIME_READ_ONLY",
            ],
            "blocked_capabilities": ["ORDER_CREATE", "ORDER_AMEND", "ORDER_CANCEL"],
            "trading_enabled": False,
            "blockers": blockers,
        }

    async def collect_realtime_market_events(
        self,
        symbols: list[str],
        *,
        max_messages: int = 100,
        timeout_seconds: int = 10,
        connect_factory: Callable[..., Awaitable[Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Collect public market frames without subscribing to account or order types."""
        if not 1 <= len(symbols) <= 20:
            raise KiwoomError("실시간 감시 종목은 1~20개만 허용합니다.")
        if any(not symbol.isdigit() or len(symbol) != 6 for symbol in symbols):
            raise KiwoomError("실시간 감시 종목코드는 숫자 6자리여야 합니다.")
        if not 1 <= max_messages <= 500:
            raise KiwoomError("실시간 수집 건수는 1~500건만 허용합니다.")
        if not 1 <= timeout_seconds <= 30:
            raise KiwoomError("실시간 수집 제한시간은 1~30초만 허용합니다.")
        try:
            if connect_factory is None:
                from websockets.asyncio.client import connect as connect_factory
        except ImportError as error:  # pragma: no cover - dependency is installed with uvicorn
            raise KiwoomError("실시간 수집용 WebSocket 패키지가 없습니다.") from error

        base = (
            "wss://api.kiwoom.com:10000"
            if self.config.environment == "production"
            else "wss://mockapi.kiwoom.com:10000"
        )
        uri = f"{base}/api/dostk/websocket"
        token = self._access_token()
        events: list[dict[str, Any]] = []
        try:
            async with connect_factory(
                uri, open_timeout=timeout_seconds, ping_interval=None
            ) as socket:
                await socket.send(json.dumps({"trnm": "LOGIN", "token": token}))
                login = json.loads(await asyncio.wait_for(socket.recv(), timeout_seconds))
                if str(login.get("trnm", "")).upper() != "LOGIN" or login.get(
                    "return_code"
                ) not in {None, 0}:
                    raise KiwoomError("키움 실시간 로그인 승인이 거절되었습니다.")
                await socket.send(
                    json.dumps(
                        {
                            "trnm": "REG",
                            "grp_no": "1",
                            "refresh": "0",
                            "data": [
                                {"item": symbols, "type": ["0B", "0D"]},
                                {"item": [], "type": ["1h", "0s"]},
                            ],
                        }
                    )
                )
                deadline = time.monotonic() + timeout_seconds
                while len(events) < max_messages:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    try:
                        raw = await asyncio.wait_for(socket.recv(), remaining)
                    except TimeoutError:
                        break
                    message = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
                    if str(message.get("trnm", "")).upper() == "PING":
                        await socket.send(json.dumps(message))
                        continue
                    if str(message.get("trnm", "")).upper() != "REAL":
                        continue
                    for item in message.get("data", []):
                        if not isinstance(item, dict) or item.get("type") not in {
                            "0B",
                            "0D",
                            "1h",
                            "0s",
                        }:
                            continue
                        values = item.get("values")
                        if not isinstance(values, dict):
                            continue
                        events.append(
                            {
                                "received_at": datetime_now_iso(),
                                "type": str(item.get("type", "")),
                                "item": str(item.get("item", "")),
                                "values": {str(key): str(value) for key, value in values.items()},
                            }
                        )
                        if len(events) >= max_messages:
                            break
        except KiwoomError:
            raise
        except (OSError, TimeoutError, json.JSONDecodeError) as error:
            raise KiwoomError("키움 실시간 시세를 안전하게 수집하지 못했습니다.") from error
        except Exception as error:  # WebSocket implementations expose version-specific errors.
            raise KiwoomError("키움 실시간 시세를 안전하게 수집하지 못했습니다.") from error
        return events

    def _access_token(self) -> str:
        if not self.config.configured:
            raise KiwoomError("키움 앱 키와 시크릿 키를 먼저 로컬 환경변수에 설정하세요.")
        if self._token and time.monotonic() - self._token_cached_at < 23 * 60 * 60:
            return self._token
        response = self._transport.post(
            f"{self.config.base_url}{self.TOKEN_PATH}",
            {"Content-Type": "application/json;charset=UTF-8"},
            {
                "grant_type": "client_credentials",
                "appkey": self.config.app_key,
                "secretkey": self.config.secret_key,
            },
        )
        token = response.get("token")
        if response.get("return_code") not in {None, 0} or not isinstance(token, str) or not token:
            raise KiwoomError("키움 접근 토큰 발급이 거절되었습니다. 앱 키 권한을 확인하세요.")
        self._token = token
        self._token_cached_at = time.monotonic()
        return token

    def fetch_domestic_account_evaluation(self) -> dict[str, Any]:
        return self._authorized_query(
            path=self.ACCOUNT_PATH,
            api_id=self.ACCOUNT_API_ID,
            payload={"qry_tp": "1", "dmst_stex_tp": "KRX"},
            error_message="키움 계좌평가잔고 조회가 거절되었습니다.",
        )

    def fetch_domestic_quote(self, symbol: str) -> dict[str, Any]:
        if not symbol.isdigit() or len(symbol) != 6:
            raise KiwoomError("국내 주식 종목코드는 숫자 6자리여야 합니다.")
        return self._authorized_query(
            path=self.QUOTE_PATH,
            api_id=self.QUOTE_API_ID,
            payload={"stk_cd": symbol},
            error_message="키움 국내주식 시세 조회가 거절되었습니다.",
        )

    def fetch_open_orders(self, symbol: str = "") -> dict[str, Any]:
        if symbol and (not symbol.isdigit() or len(symbol) != 6):
            raise KiwoomError("국내 주식 종목코드는 숫자 6자리여야 합니다.")
        return self._authorized_query(
            path=self.ACCOUNT_PATH,
            api_id=self.OPEN_ORDERS_API_ID,
            payload={
                "all_stk_tp": "1" if symbol else "0",
                "trde_tp": "0",
                "stk_cd": symbol,
                "stex_tp": "0",
            },
            error_message="키움 미체결 조회가 거절되었습니다.",
        )

    def fetch_fills(self, symbol: str = "") -> dict[str, Any]:
        if symbol and (not symbol.isdigit() or len(symbol) != 6):
            raise KiwoomError("국내 주식 종목코드는 숫자 6자리여야 합니다.")
        return self._authorized_query(
            path=self.ACCOUNT_PATH,
            api_id=self.FILLS_API_ID,
            payload={
                "stk_cd": symbol,
                "qry_tp": "1" if symbol else "0",
                "sell_tp": "0",
                "ord_no": "",
                "stex_tp": "0",
            },
            error_message="키움 체결 조회가 거절되었습니다.",
        )

    def fetch_today_volume_top(
        self,
        *,
        market: str,
        price_filter: str = "0",
        max_pages: int = 2,
    ) -> list[dict[str, Any]]:
        """Return KRX-wide same-day liquidity leaders from official ka10030.

        This is a discovery-only capability. It cannot place orders and deliberately
        excludes management issues and preferred shares at the broker query boundary.
        """
        market_codes = {"KOSPI": "001", "KOSDAQ": "101"}
        if market not in market_codes:
            raise KiwoomError("시장 순위는 KOSPI 또는 KOSDAQ만 조회할 수 있습니다.")
        if price_filter not in {"0", "10"}:
            raise KiwoomError("시장 순위 가격 구분은 전체 또는 1만원 미만만 허용합니다.")
        if not 1 <= max_pages <= 5:
            raise KiwoomError("시장 순위 연속조회는 1~5페이지만 허용합니다.")

        rows: list[dict[str, Any]] = []
        cont_yn, next_key = "N", ""
        for page_index in range(max_pages):
            page = self._authorized_query_page(
                path=self.RANK_PATH,
                api_id=self.TODAY_VOLUME_TOP_API_ID,
                payload={
                    "mrkt_tp": market_codes[market],
                    "sort_tp": "3",
                    "mang_stk_incls": "4",
                    "crd_tp": "0",
                    "trde_qty_tp": "0",
                    "pric_tp": price_filter,
                    "trde_prica_tp": "300",
                    "mrkt_open_tp": "1",
                    "stex_tp": "1",
                },
                cont_yn=cont_yn,
                next_key=next_key,
                error_message="키움 당일 거래대금 순위 조회가 거절되었습니다.",
            )
            raw_rows = page.payload.get("tdy_trde_qty_upper")
            if not isinstance(raw_rows, list):
                raise KiwoomError("키움 당일 시장 순위 응답 형식이 예상과 다릅니다.")
            for raw in raw_rows:
                if not isinstance(raw, dict):
                    continue
                symbol = str(raw.get("stk_cd", "")).strip().removeprefix("A")
                if not symbol.isdigit() or len(symbol) != 6:
                    continue
                try:
                    current_price = abs(int(float(str(raw.get("cur_prc", "0")).replace(",", ""))))
                    volume = abs(int(float(str(raw.get("trde_qty", "0")).replace(",", ""))))
                    change_rate = float(str(raw.get("flu_rt", "0")).replace(",", ""))
                except ValueError:
                    continue
                if current_price <= 0 or volume <= 0:
                    continue
                rows.append(
                    {
                        "symbol": symbol,
                        "name": str(raw.get("stk_nm", symbol)).strip() or symbol,
                        "market": market,
                        "current_price_krw": current_price,
                        "volume": volume,
                        "change_rate_pct": change_rate,
                        "estimated_turnover_krw": current_price * volume,
                        "source": "KIWOOM_KA10030",
                    }
                )
            if page.cont_yn != "Y" or not page.next_key:
                break
            cont_yn, next_key = "Y", page.next_key
            if page_index + 1 < max_pages:
                time.sleep(0.22)

        deduplicated = {item["symbol"]: item for item in rows}
        return sorted(
            deduplicated.values(),
            key=lambda item: item["estimated_turnover_krw"],
            reverse=True,
        )

    def fetch_daily_chart_page(
        self,
        symbol: str,
        *,
        base_date: str,
        cont_yn: str = "N",
        next_key: str = "",
    ) -> JsonPage:
        """Fetch one official ka10081 page without exposing a generic API escape hatch."""
        if not symbol.isdigit() or len(symbol) != 6:
            raise KiwoomError("국내 주식 종목코드는 숫자 6자리여야 합니다.")
        if not base_date.isdigit() or len(base_date) != 8:
            raise KiwoomError("기준일자는 YYYYMMDD 형식이어야 합니다.")
        if cont_yn not in {"N", "Y"}:
            raise KiwoomError("연속조회 값은 N 또는 Y여야 합니다.")
        return self._authorized_query_page(
            path=self.CHART_PATH,
            api_id=self.DAILY_CHART_API_ID,
            payload={"stk_cd": symbol, "base_dt": base_date, "upd_stkpc_tp": "1"},
            cont_yn=cont_yn,
            next_key=next_key,
            error_message="키움 국내주식 일봉 조회가 거절되었습니다.",
        )

    def fetch_daily_bars(
        self, symbol: str, *, base_date: str, max_pages: int = 8
    ) -> list[dict[str, int | str]]:
        """Return normalized, ascending adjusted daily bars from official Kiwoom pages."""
        if not 1 <= max_pages <= 20:
            raise KiwoomError("일봉 연속조회는 1~20페이지만 허용합니다.")
        rows: list[dict[str, Any]] = []
        cont_yn, next_key = "N", ""
        for page_index in range(max_pages):
            page = self.fetch_daily_chart_page(
                symbol, base_date=base_date, cont_yn=cont_yn, next_key=next_key
            )
            raw_rows = page.payload.get("stk_dt_pole_chart_qry")
            if not isinstance(raw_rows, list):
                raise KiwoomError("키움 일봉 응답 형식이 예상과 다릅니다.")
            rows.extend(item for item in raw_rows if isinstance(item, dict))
            if page.cont_yn != "Y" or not page.next_key:
                break
            cont_yn, next_key = "Y", page.next_key
            if page_index + 1 < max_pages:
                time.sleep(0.22)

        normalized: dict[str, dict[str, int | str]] = {}
        aliases = {
            "date": ("dt", "date"),
            "open": ("open_pric", "open"),
            "high": ("high_pric", "high"),
            "low": ("low_pric", "low"),
            "close": ("cur_prc", "close_pric", "close"),
            "volume": ("trde_qty", "volume"),
        }
        for row in rows:
            values: dict[str, str] = {}
            for target, candidates in aliases.items():
                value = next(
                    (row.get(key) for key in candidates if row.get(key) not in {None, ""}),
                    None,
                )
                if value is None:
                    raise KiwoomError(f"키움 일봉 응답에 {target} 값이 없습니다.")
                values[target] = str(value).replace(",", "").strip()
            raw_date = values["date"]
            if len(raw_date) != 8 or not raw_date.isdigit():
                raise KiwoomError("키움 일봉 날짜 형식이 예상과 다릅니다.")
            try:
                bar = {
                    "date": f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}",
                    "open": abs(int(values["open"])),
                    "high": abs(int(values["high"])),
                    "low": abs(int(values["low"])),
                    "close": abs(int(values["close"])),
                    "volume": abs(int(values["volume"])),
                }
            except ValueError as error:
                raise KiwoomError("키움 일봉 숫자 값을 해석할 수 없습니다.") from error
            if (
                bar["volume"] == 0
                and len({bar["open"], bar["high"], bar["low"], bar["close"]}) == 1
            ):
                # Kiwoom emits flat zero-volume rows for formal trading suspensions.
                # They are not executable trading days, so exclude them from signal history.
                continue
            normalized[bar["date"]] = bar
        return [normalized[key] for key in sorted(normalized)]

    def _authorized_query(
        self,
        *,
        path: str,
        api_id: str,
        payload: dict[str, Any],
        error_message: str,
    ) -> dict[str, Any]:
        return self._authorized_query_page(
            path=path,
            api_id=api_id,
            payload=payload,
            cont_yn="N",
            next_key="",
            error_message=error_message,
        ).payload

    def _authorized_query_page(
        self,
        *,
        path: str,
        api_id: str,
        payload: dict[str, Any],
        cont_yn: str,
        next_key: str,
        error_message: str,
    ) -> JsonPage:
        token = self._access_token()
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "authorization": f"Bearer {token}",
            "api-id": api_id,
            "cont-yn": cont_yn,
            "next-key": next_key,
        }
        post_page = getattr(self._transport, "post_page", None)
        page = (
            post_page(
                f"{self.config.base_url}{path}",
                headers,
                payload,
            )
            if callable(post_page)
            else JsonPage(self._transport.post(f"{self.config.base_url}{path}", headers, payload))
        )
        if page.payload.get("return_code") not in {None, 0}:
            raise KiwoomError(error_message)
        return JsonPage(
            payload=_sanitize_account_payload(page.payload),
            cont_yn=page.cont_yn,
            next_key=page.next_key,
        )
