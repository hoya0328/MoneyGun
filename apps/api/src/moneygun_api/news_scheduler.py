from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .news import POLL_INTERVAL_SECONDS, should_poll

KST = timezone(timedelta(hours=9))


def run_once(api_url: str) -> dict[str, object]:
    request = Request(
        f"{api_url.rstrip('/')}/v1/news/poll",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=180) as response:  # noqa: S310 - loopback URL only
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        return {
            "state": "API_ERROR",
            "error": f"API {error.code}: {error.read().decode(errors='replace')}",
        }
    except (URLError, TimeoutError) as error:
        return {"state": "API_UNAVAILABLE", "error": str(error)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Signal Guild official disclosure monitor")
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--interval", type=int, default=POLL_INTERVAL_SECONDS)
    parser.add_argument("--once", action="store_true")
    arguments = parser.parse_args()
    while True:
        now_kst = datetime.now(KST)
        if arguments.once or should_poll(now_kst):
            result = run_once(arguments.api_url)
            print(
                json.dumps(
                    {
                        "at": now_kst.isoformat(timespec="seconds"),
                        "state": result.get("state"),
                        "watched_symbols": result.get("watched_symbols"),
                        "inserted_count": result.get("inserted_count"),
                        "blocking_count": result.get("blocking_count"),
                        "error": result.get("error"),
                        "automatic_broker_submission": False,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                flush=True,
            )
        if arguments.once:
            return
        time.sleep(max(60, arguments.interval))


if __name__ == "__main__":
    main()
