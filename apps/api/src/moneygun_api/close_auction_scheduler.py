from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

KST = timezone(timedelta(hours=9))


def _is_work_window(now_kst: datetime) -> bool:
    if now_kst.weekday() >= 5:
        return False
    minute = now_kst.hour * 60 + now_kst.minute
    return (15 * 60 + 5 <= minute <= 15 * 60 + 10) or (15 * 60 + 20 <= minute <= 15 * 60 + 27)


def run_once(api_url: str) -> dict[str, object]:
    request = Request(
        f"{api_url.rstrip('/')}/v1/strategies/close-auction/pilot/tick",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=120) as response:  # noqa: S310 - loopback URL only
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        return {"action": "BLOCKED", "reason": f"API {error.code}: {detail}"}
    except (URLError, TimeoutError) as error:
        return {"action": "API_UNAVAILABLE", "reason": str(error)}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="MoneyGun L0 close-auction candidate preparation scheduler"
    )
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--interval", type=int, default=20)
    parser.add_argument("--once", action="store_true")
    arguments = parser.parse_args()
    previous = ""
    while True:
        now_kst = datetime.now(KST)
        if arguments.once or _is_work_window(now_kst):
            result = run_once(arguments.api_url)
            cycle = result.get("cycle") if isinstance(result.get("cycle"), dict) else {}
            intent = result.get("intent") if isinstance(result.get("intent"), dict) else {}
            summary = {
                "at": now_kst.isoformat(timespec="seconds"),
                "phase": result.get("phase"),
                "action": result.get("action"),
                "reason": result.get("reason"),
                "cycle_id": cycle.get("id"),
                "intent_id": intent.get("id"),
                "intent_state": intent.get("state"),
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
