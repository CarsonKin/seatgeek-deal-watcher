#!/usr/bin/env python3
"""
SeatGeek deal watcher for SLC-area events (Jazz, Mammoth, Utes, BYU, and
UFC/concerts at the Delta Center).

This uses SeatGeek's official public Platform API to check each event's
listing-price stats and pushes a phone notification when the cheapest
available listing is well below that event's average price. It does NOT
scrape seatgeek.com and does NOT purchase anything automatically -- it only
reads data through the sanctioned API and alerts you so you can review and
buy manually. See README.md for setup and for why it's built this way.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "state.json"
RESOLVED_PATH = BASE_DIR / "resolved.json"

API_BASE = "https://api.seatgeek.com/2"
REQUEST_DELAY_SECONDS = 1.0  # be a polite, low-volume API consumer


def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default


def save_json(path: Path, data):
    path.write_text(json.dumps(data, indent=2, sort_keys=True))


def get_client_id() -> str:
    client_id = os.environ.get("SEATGEEK_CLIENT_ID")
    if not client_id:
        sys.exit(
            "Missing SEATGEEK_CLIENT_ID environment variable.\n"
            "Get a free client ID at https://seatgeek.com/account/develop "
            "and see README.md for how to set it."
        )
    return client_id


def api_get(path: str, params: dict, client_id: str, retries: int = 3) -> dict:
    params = dict(params)
    params["client_id"] = client_id
    last_resp = None
    for attempt in range(retries):
        resp = requests.get(f"{API_BASE}/{path}", params=params, timeout=20)
        last_resp = resp
        if resp.status_code == 429:
            wait = 5 * (attempt + 1)
            print(f"Rate limited by SeatGeek API, waiting {wait}s...")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        time.sleep(REQUEST_DELAY_SECONDS)
        return resp.json()
    last_resp.raise_for_status()
    return {}


def resolve_performer(name: str, client_id: str, cache: dict):
    cache.setdefault("performers", {})
    if name in cache["performers"]:
        return cache["performers"][name]
    data = api_get("performers", {"q": name, "per_page": 5}, client_id)
    results = data.get("performers", [])
    if not results:
        print(f"WARNING: no performer match found for '{name}', skipping")
        return None
    best = results[0]
    cache["performers"][name] = best["slug"]
    print(f"Resolved performer '{name}' -> slug '{best['slug']}' ({best.get('name')})")
    return best["slug"]


def resolve_venue(name: str, client_id: str, cache: dict):
    cache.setdefault("venues", {})
    if name in cache["venues"]:
        return cache["venues"][name]
    data = api_get("venues", {"q": name, "per_page": 5}, client_id)
    results = data.get("venues", [])
    if not results:
        print(f"WARNING: no venue match found for '{name}', skipping")
        return None
    best = results[0]
    cache["venues"][name] = best["slug"]
    print(f"Resolved venue '{name}' -> slug '{best['slug']}' ({best.get('name')})")
    return best["slug"]


def fetch_events(filter_key: str, filter_value: str, client_id: str) -> list:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    data = api_get(
        "events",
        {
            filter_key: filter_value,
            "per_page": 50,
            "datetime_utc.gte": today,
        },
        client_id,
    )
    return data.get("events", [])


def evaluate_deal(event: dict, threshold_pct: float, min_listing_count: int):
    stats = event.get("stats") or {}
    lowest = stats.get("lowest_price")
    average = stats.get("average_price")
    listing_count = stats.get("listing_count") or 0

    if lowest is None or average is None or average <= 0:
        return None
    if listing_count < min_listing_count:
        return None
    if lowest > threshold_pct * average:
        return None

    pct_off = 100 * (1 - lowest / average)
    return {
        "event_id": event["id"],
        "title": event.get("title") or event.get("short_title"),
        "datetime_local": event.get("datetime_local"),
        "url": event.get("url"),
        "lowest_price": lowest,
        "average_price": average,
        "listing_count": listing_count,
        "pct_off": round(pct_off, 1),
    }


def should_alert(deal: dict, state: dict) -> bool:
    """Alert on brand-new deals, and re-alert only if the price drops
    meaningfully further or a while has passed since the last alert --
    avoids spamming you every run for the same $1 fluctuation."""
    key = str(deal["event_id"])
    now = datetime.now(timezone.utc)
    prev = state.get(key)

    if prev is None:
        state[key] = {
            "last_alert_price": deal["lowest_price"],
            "last_alert_time": now.isoformat(),
        }
        return True

    price_dropped_further = deal["lowest_price"] <= prev["last_alert_price"] * 0.9
    last_alert_time = datetime.fromisoformat(prev["last_alert_time"])
    hours_since = (now - last_alert_time).total_seconds() / 3600
    stale = hours_since >= 12

    if price_dropped_further or stale:
        state[key] = {
            "last_alert_price": deal["lowest_price"],
            "last_alert_time": now.isoformat(),
        }
        return True

    return False


def format_deal_message(deal: dict) -> str:
    return (
        f"{deal['title']}\n"
        f"{deal['datetime_local']}\n"
        f"Lowest listing: ${deal['lowest_price']:.2f} "
        f"({deal['pct_off']}% below avg ${deal['average_price']:.2f}, "
        f"{deal['listing_count']} listings)\n"
        f"{deal['url']}"
    )


def send_ntfy(title: str, body: str) -> bool:
    """Push a notification via ntfy.sh. Returns True only on confirmed success."""
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        print("NTFY_TOPIC is not set -- no way to notify you. Set it and re-run.")
        return False
    try:
        resp = requests.post(
            f"https://ntfy.sh/{topic}",
            data=body.encode("utf-8"),
            headers={"Title": title, "Priority": "high"},
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except requests.RequestException as exc:
        print(f"PUSH FAILED: {exc}")
        return False


def run_test_alert() -> int:
    """Send a fake deal through the real push pipeline.

    Lets you verify notification delivery end to end without a SeatGeek API
    key -- useful while your developer account is still pending approval.
    Returns a process exit code: 0 on success, 1 on failure.
    """
    deal = {
        "event_id": 0,
        "title": "TEST ALERT -- Utah Jazz at Delta Center",
        "datetime_local": "2026-11-15T19:00:00",
        "url": "https://seatgeek.com/utah-jazz-tickets",
        "lowest_price": 18.0,
        "average_price": 74.0,
        "listing_count": 42,
        "pct_off": 75.7,
    }
    body = (
        "This is a test alert from your SeatGeek deal watcher.\n"
        "If you're reading this on your phone, notifications work.\n\n"
        + format_deal_message(deal)
    )

    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        print(
            "NTFY_TOPIC is not set. Set it first -- see README.md, Phase 2.\n"
            '  PowerShell:  $env:NTFY_TOPIC="your-topic-name"'
        )
        return 1

    print(f"Sending test push to ntfy topic: {topic}")
    if send_ntfy("SeatGeek deal watcher: test alert", body):
        print("SENT OK -- check your phone.")
        print("If nothing arrived, the topic name here doesn't match the one")
        print("you subscribed to in the ntfy app. Compare them character by character.")
        return 0

    print("Push did not send. Check your internet connection and topic name.")
    return 1


def main():
    if "--test-alert" in sys.argv:
        sys.exit(run_test_alert())

    config = load_json(CONFIG_PATH, None)
    if config is None:
        sys.exit(f"Missing {CONFIG_PATH}. Copy config.example.json to config.json first.")

    client_id = get_client_id()
    resolved = load_json(RESOLVED_PATH, {})
    state = load_json(STATE_PATH, {})

    threshold_pct = config.get("deal_threshold_pct", 0.65)
    min_listing_count = config.get("min_listing_count", 3)

    events_by_id = {}

    for name in config.get("tracked_performers", []):
        slug = resolve_performer(name, client_id, resolved)
        if not slug:
            continue
        for event in fetch_events("performers.slug", slug, client_id):
            events_by_id[event["id"]] = event

    for name in config.get("tracked_venues", []):
        slug = resolve_venue(name, client_id, resolved)
        if not slug:
            continue
        for event in fetch_events("venue.slug", slug, client_id):
            events_by_id[event["id"]] = event

    print(f"Checked {len(events_by_id)} unique upcoming events.")

    deals = []
    for event in events_by_id.values():
        deal = evaluate_deal(event, threshold_pct, min_listing_count)
        if deal and should_alert(deal, state):
            deals.append(deal)

    if deals:
        deals.sort(key=lambda d: d["pct_off"], reverse=True)
        print(f"Found {len(deals)} new/updated deal(s).")
        body = "\n\n".join(format_deal_message(d) for d in deals)
        title = f"SeatGeek deal alert: {len(deals)} event(s) below your threshold"
        send_ntfy(title, body)
        for d in deals:
            print("---")
            print(format_deal_message(d))
    else:
        print("No qualifying deals this run.")

    save_json(RESOLVED_PATH, resolved)
    save_json(STATE_PATH, state)


if __name__ == "__main__":
    main()
