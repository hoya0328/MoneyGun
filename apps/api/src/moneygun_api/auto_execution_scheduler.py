from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

KST = timezone(timedelta(hours=9))


def _active(now_kst: datetime) -> bool:
    minute = now_kst.hour * 60 + now_kst.minute
    return now_kst.weekday() < 5 and 8 * 60 + 45 <= minute <= 18 * 60 + 5


def run_once(api_url: str) -> dict[str, object]:
    request = Request(
        f"{api_url.rstrip('/')}/v1/execution/automation/tick"
        "?mission_id=mission_l0_pilot_001",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=180) as response:  # noqa: S310 - loopback URL only
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        return {
            "action": "BLOCKED",
            "reason": f"API {error.code}: {error.read().decode(errors='replace')}",
        }
    except (URLError, TimeoutError) as error:
        return {"action": "API_UNAVAILABLE", "reason": str(error)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Signal Guild L0 delegated auto execution")
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--interval", type=int, default=10)
    parser.add_argument("--once", action="store_true")
    arguments = parser.parse_args()
    previous = ""
    while True:
        now_kst = datetime.now(KST)
        if arguments.once or _active(now_kst):
            result = run_once(arguments.api_url)
            intent = result.get("intent") if isinstance(result.get("intent"), dict) else {}
            summary = {
                "at": now_kst.isoformat(timespec="seconds"),
                "action": result.get("action"),
                "intent_id": intent.get("id"),
                "intent_state": intent.get("state"),
                "submitted": result.get("submitted", False),
                "automatic_broker_submission": result.get(
                    "automatic_broker_submission", True
                ),
                "reason": result.get("reason"),
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
        time.sleep(max(5, arguments.interval))


if __name__ == "__main__":
    main()
