from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

KST = timezone(timedelta(hours=9))


def _post(api_url: str, path: str, body: dict[str, object]) -> dict[str, object]:
    request = Request(
        f"{api_url.rstrip('/')}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=180) as response:  # noqa: S310 - loopback URL only
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        return {"action": "BLOCKED", "reason": error.read().decode(errors="replace")}
    except (URLError, TimeoutError) as error:
        return {"action": "API_UNAVAILABLE", "reason": str(error)}


def run_once(api_url: str, now_kst: datetime | None = None) -> list[dict[str, object]]:
    now_kst = (now_kst or datetime.now(KST)).astimezone(KST)
    if now_kst.weekday() >= 5:
        return []
    minute = now_kst.hour * 60 + now_kst.minute
    results: list[dict[str, object]] = []
    if 8 * 60 + 50 <= minute <= 10 * 60 + 30 or 15 * 60 + 35 <= minute <= 18 * 60:
        result = _post(api_url, "/v1/strategies/opening-range/pilot/tick", {})
        results.append({"mode": "OPENING_RANGE", **result})
    if 9 * 60 + 5 <= minute <= 9 * 60 + 10 or 15 * 60 + 35 <= minute <= 18 * 60:
        for mode_code in ("FOCUS", "BALANCED", "LONG_TERM"):
            result = _post(
                api_url,
                "/v1/strategies/l0-modes/pilot/tick",
                {"mode_code": mode_code, "force_scan": False},
            )
            results.append({"mode": mode_code, **result})
    if 9 * 60 + 5 <= minute <= 15 * 60 + 20 and now_kst.second < 20:
        result = _post(api_url, "/v1/l0-pilot/exits/tick", {})
        results.append({"mode": "EXIT_GUARD", **result})
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="MoneyGun L0 multi-mode preparation scheduler")
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--interval", type=int, default=20)
    parser.add_argument("--once", action="store_true")
    arguments = parser.parse_args()
    previous = ""
    while True:
        for result in run_once(arguments.api_url):
            summary = {
                "at": datetime.now(KST).isoformat(timespec="seconds"),
                "mode": result.get("mode"),
                "phase": result.get("phase"),
                "action": result.get("action"),
                "reason": result.get("reason"),
                "automatic_broker_submission": False,
            }
            signature = json.dumps(
                {key: value for key, value in summary.items() if key != "at"},
                ensure_ascii=False,
                sort_keys=True,
            )
            if signature != previous:
                print(json.dumps(summary, ensure_ascii=False, sort_keys=True), flush=True)
                previous = signature
        if arguments.once:
            return
        time.sleep(max(10, arguments.interval))


if __name__ == "__main__":
    main()
