#!/usr/bin/env python3
"""
SeatGeek deal watcher for SLC-area events (Jazz, Mammoth, Utes, BYU, and
UFC/concerts at the Delta Center).

This uses SeatGeek's official public Platform API to track each event's
floor price over time and pushes a phone notification when an event looks
genuinely mispriced. It does NOT scrape seatgeek.com and does NOT purchase
anything automatically -- it only reads data through the sanctioned API and
alerts you so you can review and buy manually.

Two independent signals must agree before an alert fires:

  DROP  -- this event's floor price has fallen sharply against its own
           recent baseline (the median of its own observations).
  PEER  -- this event's floor price sits well below the median floor price
           of its sibling events (other Jazz home games, say).

Requiring both is what keeps a permanently cheap game from alerting every
run: DROP alone fires on any dip, PEER alone fires on every low-demand
opponent. Together they describe a game that is BOTH underpriced relative
to its peers AND has just moved -- which is the shape of an actual
mispricing rather than a correctly-priced dud.

Where only one signal has enough data to evaluate (a brand-new event with
no history, a one-off concert with no siblings), that single signal must
fire on its own. See README.md for tuning.
"""

import json
import os
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "state.json"
RESOLVED_PATH = BASE_DIR / "resolved.json"
HISTORY_PATH = BASE_DIR / "history.json"

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


def floor_price(event: dict):
    """The cheapest available listing for an event, or None if unknown."""
    stats = event.get("stats") or {}
    price = stats.get("lowest_price")
    return price if isinstance(price, (int, float)) and price > 0 else None


def record_history(events: list, history: dict, keep_days: int,
                   interval_hours: float = 4.0) -> None:
    """Append this run's floor price for each event, and prune old points.

    Throttled to one point per event per `interval_hours`. The watcher runs
    every 20 minutes; recording every run would store ~1,500 points per event
    over the retention window and commit a large file back to the repo each
    time, for no extra signal. Every 4 hours is plenty to establish a baseline.
    """
    now = datetime.now(timezone.utc)
    stamp = now.isoformat()
    cutoff = now - timedelta(days=keep_days)
    min_gap = timedelta(hours=interval_hours)

    for event in events:
        price = floor_price(event)
        if price is None:
            continue
        key = str(event["id"])
        points = history.setdefault(key, [])

        if points:
            try:
                last = datetime.fromisoformat(points[-1]["t"])
                if now - last < min_gap:
                    continue
            except (ValueError, KeyError, TypeError):
                pass

        points.append({"t": stamp, "p": price})

    # Prune: drop points older than keep_days, and drop events we no longer track.
    live_ids = {str(e["id"]) for e in events}
    for key in list(history.keys()):
        if key not in live_ids:
            del history[key]
            continue
        kept = []
        for point in history[key]:
            try:
                if datetime.fromisoformat(point["t"]) >= cutoff:
                    kept.append(point)
            except (ValueError, KeyError, TypeError):
                continue
        history[key] = kept


def drop_signal(event: dict, history: dict, cfg: dict):
    """How far this event's floor has fallen against its own recent baseline.

    The baseline deliberately excludes the last few hours so that a genuine
    fall is measured against where the price was sitting, not against itself.
    Returns None when there isn't enough history to judge.
    """
    price = floor_price(event)
    if price is None:
        return None

    points = history.get(str(event["id"]), [])
    settle = datetime.now(timezone.utc) - timedelta(
        hours=cfg.get("baseline_lag_hours", 12)
    )

    baseline_prices = []
    for point in points:
        try:
            if datetime.fromisoformat(point["t"]) <= settle:
                baseline_prices.append(point["p"])
        except (ValueError, KeyError, TypeError):
            continue

    if len(baseline_prices) < cfg.get("min_history_points", 3):
        return None

    baseline = statistics.median(baseline_prices)
    if baseline <= 0:
        return None

    return {
        "baseline": round(baseline, 2),
        "pct_below": round(100 * (1 - price / baseline), 1),
        "fired": price <= baseline * (1 - cfg.get("drop_pct", 0.20)),
    }


def peer_signal(event: dict, peer_prices: list, cfg: dict):
    """How far this event's floor sits below its sibling events' median floor.

    Returns None when the peer group is too small to give a meaningful median.
    """
    price = floor_price(event)
    if price is None:
        return None

    others = [p for p in peer_prices if p is not None]
    if len(others) < cfg.get("min_peer_events", 4):
        return None

    median = statistics.median(others)
    if median <= 0:
        return None

    return {
        "median": round(median, 2),
        "pct_below": round(100 * (1 - price / median), 1),
        "fired": price <= median * (1 - cfg.get("peer_pct", 0.25)),
    }


def evaluate_deal(event: dict, history: dict, peer_prices: list, cfg: dict):
    """Alert only when every signal with enough data to judge agrees.

    Both available -> both must fire. Only one available -> it must fire.
    Neither available -> no alert (we have nothing to go on yet).
    """
    price = floor_price(event)
    if price is None:
        return None

    listing_count = (event.get("stats") or {}).get("listing_count") or 0
    if listing_count < cfg.get("min_listing_count", 3):
        return None

    drop = drop_signal(event, history, cfg)
    peer = peer_signal(event, peer_prices, cfg)

    available = [s for s in (drop, peer) if s is not None]
    if not available:
        return None
    if not all(s["fired"] for s in available):
        return None

    return {
        "event_id": event["id"],
        "title": event.get("title") or event.get("short_title"),
        "datetime_local": event.get("datetime_local"),
        "url": event.get("url"),
        "lowest_price": price,
        "listing_count": listing_count,
        "drop": drop,
        "peer": peer,
        "confidence": "both signals" if len(available) == 2 else "one signal",
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
    lines = [
        deal["title"],
        deal["datetime_local"],
        f"Floor price: ${deal['lowest_price']:.2f} ({deal['listing_count']} listings)",
    ]

    drop = deal.get("drop")
    if drop:
        lines.append(
            f"DROP: {drop['pct_below']}% below its own baseline of ${drop['baseline']:.2f}"
        )

    peer = deal.get("peer")
    if peer:
        lines.append(
            f"PEER: {peer['pct_below']}% below the ${peer['median']:.2f} median for similar games"
        )

    lines.append(f"Confirmed by {deal['confidence']}.")
    lines.append("Check Deal Scores on SeatGeek before buying.")
    lines.append(deal["url"])
    return "\n".join(lines)


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
        "lowest_price": 38.0,
        "listing_count": 42,
        "drop": {"baseline": 58.0, "pct_below": 34.5, "fired": True},
        "peer": {"median": 71.0, "pct_below": 46.5, "fired": True},
        "confidence": "both signals",
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
    history = load_json(HISTORY_PATH, {})

    events_by_id = {}
    # peer_group_of[event_id] = the config entry it was fetched under, so a
    # Jazz game is compared against other Jazz games rather than every event.
    peer_group_of = {}

    def collect(group_name: str, filter_key: str, slug: str):
        for event in fetch_events(filter_key, slug, client_id):
            events_by_id[event["id"]] = event
            peer_group_of.setdefault(event["id"], group_name)

    for name in config.get("tracked_performers", []):
        slug = resolve_performer(name, client_id, resolved)
        if slug:
            collect(name, "performers.slug", slug)

    for name in config.get("tracked_venues", []):
        slug = resolve_venue(name, client_id, resolved)
        if slug:
            collect(name, "venue.slug", slug)

    events = list(events_by_id.values())
    print(f"Checked {len(events)} unique upcoming events.")

    # Peer prices, grouped. Built before recording history so an event is never
    # compared against a median that already includes this run's own point.
    peers_by_group = {}
    for event in events:
        group = peer_group_of.get(event["id"], "ungrouped")
        peers_by_group.setdefault(group, []).append(floor_price(event))

    record_history(
        events,
        history,
        config.get("history_days", 21),
        config.get("history_interval_hours", 4),
    )

    deals = []
    for event in events:
        group = peer_group_of.get(event["id"], "ungrouped")
        deal = evaluate_deal(event, history, peers_by_group.get(group, []), config)
        if deal and should_alert(deal, state):
            deals.append(deal)

    if deals:
        deals.sort(key=lambda d: _deal_rank(d), reverse=True)
        print(f"Found {len(deals)} new/updated deal(s).")
        body = "\n\n".join(format_deal_message(d) for d in deals)
        title = f"SeatGeek: {len(deals)} possible mispricing(s)"
        send_ntfy(title, body)
        for d in deals:
            print("---")
            print(format_deal_message(d))
    else:
        print("No qualifying deals this run.")
        _report_readiness(events, history, peers_by_group, peer_group_of, config)

    save_json(RESOLVED_PATH, resolved)
    save_json(STATE_PATH, state)
    save_json(HISTORY_PATH, history)


def _deal_rank(deal: dict) -> float:
    """Rank by the strongest discount either signal reports."""
    scores = [s["pct_below"] for s in (deal.get("drop"), deal.get("peer")) if s]
    return max(scores) if scores else 0.0


def _report_readiness(events, history, peers_by_group, peer_group_of, cfg) -> None:
    """Explain why nothing fired, so silence is never ambiguous.

    On a fresh install both signals are unavailable and the tool cannot
    alert at all -- without this line that looks identical to "no deals".
    """
    drop_ready = sum(1 for e in events if drop_signal(e, history, cfg) is not None)
    peer_ready = sum(
        1
        for e in events
        if peer_signal(e, peers_by_group.get(peer_group_of.get(e["id"]), []), cfg)
        is not None
    )
    print(
        f"  Signal coverage: DROP can judge {drop_ready}/{len(events)} events, "
        f"PEER can judge {peer_ready}/{len(events)}."
    )
    if drop_ready == 0 and events:
        need = cfg.get("min_history_points", 3)
        lag = cfg.get("baseline_lag_hours", 12)
        print(
            f"  DROP needs {need} observations older than {lag}h per event. "
            "Still building history -- give it a day or two."
        )


if __name__ == "__main__":
    main()
