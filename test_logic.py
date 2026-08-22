#!/usr/bin/env python3
"""Offline tests for the deal logic. No API key or network needed.

Run:  python test_logic.py
"""

from datetime import datetime, timedelta, timezone

import watcher

CFG = {
    "drop_pct": 0.20,
    "peer_pct": 0.25,
    "min_listing_count": 3,
    "min_history_points": 3,
    "min_peer_events": 4,
    "baseline_lag_hours": 12,
    "history_days": 21,
}

PASS, FAIL = [], []


def check(name, got, want):
    (PASS if got == want else FAIL).append((name, got, want))


def event(eid, price, listings=40):
    return {
        "id": eid,
        "title": f"Event {eid}",
        "datetime_local": "2026-11-15T19:00:00",
        "url": "https://seatgeek.com/x",
        "stats": {"lowest_price": price, "listing_count": listings},
    }


def history_at(prices, hours_ago_start=96, step=12):
    """Oldest first, spaced `step` hours apart, ending before the baseline lag."""
    now = datetime.now(timezone.utc)
    return [
        {"t": (now - timedelta(hours=hours_ago_start - i * step)).isoformat(), "p": p}
        for i, p in enumerate(prices)
    ]


# --- DROP signal -----------------------------------------------------------

hist = {"1": history_at([60, 62, 58])}          # baseline median = 60
check("drop fires at -33%", watcher.drop_signal(event(1, 40), hist, CFG)["fired"], True)
check("drop quiet at -8%", watcher.drop_signal(event(1, 55), hist, CFG)["fired"], False)
check("drop exact -20% fires", watcher.drop_signal(event(1, 48), hist, CFG)["fired"], True)

check("drop unavailable with no history", watcher.drop_signal(event(9, 40), {}, CFG), None)
check(
    "drop unavailable with too few points",
    watcher.drop_signal(event(2, 40), {"2": history_at([60, 62])}, CFG),
    None,
)

# Points inside the settling window must not count toward the baseline.
recent_only = {"3": [
    {"t": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(), "p": 60},
    {"t": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(), "p": 61},
    {"t": (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(), "p": 59},
]}
check("recent points excluded from baseline", watcher.drop_signal(event(3, 40), recent_only, CFG), None)

# --- PEER signal -----------------------------------------------------------

peers = [95, 88, 71, 44, 71]                    # median = 71
check("peer fires at -59%", watcher.peer_signal(event(4, 29), peers, CFG)["fired"], True)
check("peer quiet at -14%", watcher.peer_signal(event(4, 61), peers, CFG)["fired"], False)
check("peer unavailable when group too small", watcher.peer_signal(event(4, 29), [95, 88, 71], CFG), None)
check("peer ignores None prices", watcher.peer_signal(event(4, 29), [95, 88, 71, 44, None, 71], CFG)["median"], 71)

# --- Combined ---------------------------------------------------------------

check(
    "both fire -> alert",
    watcher.evaluate_deal(event(1, 40), hist, peers, CFG)["confidence"],
    "both signals",
)
check(
    "drop fires, peer quiet -> no alert",
    watcher.evaluate_deal(event(1, 40), hist, [42, 41, 43, 40], CFG),
    None,
)
check(
    "peer fires, drop quiet -> no alert",
    watcher.evaluate_deal(event(1, 55), {"1": history_at([56, 57, 55])}, peers, CFG),
    None,
)
check(
    "only peer available -> it alone decides",
    watcher.evaluate_deal(event(7, 20), {}, peers, CFG)["confidence"],
    "one signal",
)
check("neither available -> no alert", watcher.evaluate_deal(event(8, 20), {}, [1, 2], CFG), None)
check(
    "thin listings suppressed",
    watcher.evaluate_deal(event(1, 40, listings=1), hist, peers, CFG),
    None,
)
check("missing price handled", watcher.evaluate_deal(event(1, None), hist, peers, CFG), None)

# --- history pruning --------------------------------------------------------

old = datetime.now(timezone.utc) - timedelta(days=40)
h = {"1": [{"t": old.isoformat(), "p": 50}], "999": [{"t": old.isoformat(), "p": 10}]}
watcher.record_history([event(1, 44)], h, keep_days=21)
check("stale points pruned, new point kept", [p["p"] for p in h["1"]], [44])
check("untracked event dropped", "999" in h, False)

# Throttle: a second run minutes later must not add another point.
watcher.record_history([event(1, 43)], h, keep_days=21, interval_hours=4)
check("throttled inside the interval", [p["p"] for p in h["1"]], [44])

# ...but a run after the interval has elapsed does.
h2 = {"1": [{"t": (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat(), "p": 44}]}
watcher.record_history([event(1, 43)], h2, keep_days=21, interval_hours=4)
check("records once the interval passes", [p["p"] for p in h2["1"]], [44, 43])

# --- message renders --------------------------------------------------------

msg = watcher.format_deal_message(watcher.evaluate_deal(event(1, 40), hist, peers, CFG))
check("message names both signals", ("DROP:" in msg and "PEER:" in msg), True)

# --- report -----------------------------------------------------------------

for name, got, want in FAIL:
    print(f"FAIL {name}\n     got  {got}\n     want {want}")
print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
raise SystemExit(1 if FAIL else 0)
