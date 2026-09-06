from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

KST = timezone(timedelta(hours=9))


def schedule_period(now_kst: datetime) -> str | None:
    """Return one durable attempt key for the morning fallback or post-close run."""
    now_kst = now_kst.astimezone(KST)
    minute = now_kst.hour * 60 + now_kst.minute
    if 6 * 60 <= minute <= 8 * 60 + 20:
        return f"{now_kst.date().isoformat()}:MORNING_FALLBACK"
    if now_kst.weekday() < 5 and 18 * 60 + 30 <= minute <= 23 * 60 + 55:
        return f"{now_kst.date().isoformat()}:POST_CLOSE"
    return None


def run_once(api_url: str) -> dict[str, object]:
    request = Request(
        f"{api_url.rstrip('/')}/v1/data-qualification/daily-refresh/run",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:  # noqa: S310 - loopback URL only
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        return {
            "state": "API_ERROR",
            "error": f"API {error.code}: {error.read().decode(errors='replace')}",
        }
    except (URLError, TimeoutError) as error:
        return {"state": "API_UNAVAILABLE", "error": str(error)}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Signal Guild official five-year qualification daily refresh scheduler"
    )
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--once", action="store_true")
    arguments = parser.parse_args()
    attempted: set[str] = set()
    while True:
        now_kst = datetime.now(KST)
        period = "MANUAL" if arguments.once else schedule_period(now_kst)
        if period and period not in attempted:
            result = run_once(arguments.api_url)
            if result.get("state") not in {"API_ERROR", "API_UNAVAILABLE"}:
                attempted.add(period)
            print(
                json.dumps(
                    {
                        "at": now_kst.isoformat(timespec="seconds"),
                        "period": period,
                        "accepted": result.get("accepted"),
                        "state": result.get("state"),
                        "stage": result.get("stage"),
                        "error": result.get("error"),
                        "automatic_order_submission": False,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                flush=True,
            )
        if arguments.once:
            return
        today_prefix = now_kst.date().isoformat()
        attempted = {item for item in attempted if item.startswith(today_prefix)}
        time.sleep(max(30, arguments.interval))


if __name__ == "__main__":
    main()
