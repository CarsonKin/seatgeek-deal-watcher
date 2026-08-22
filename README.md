# SeatGeek Deal Watcher (SLC area)

Watches SeatGeek's official public API for Jazz, Mammoth, Utah Utes (football
and basketball), BYU (football and basketball), and anything at the Delta
Center (UFC, concerts). Pushes a notification to your phone when an event's
cheapest available listing is well below that event's average listing price.
**It does not buy anything for you** — you get an alert, you decide, you
check out manually on SeatGeek.

## Why it's built this way

SeatGeek's Terms of Use prohibit ticket-buying bots and scraping the site
directly. The federal BOTS Act also makes it illegal to use software that
circumvents a ticket site's purchase limits or security checks. This tool
stays on the right side of both: it only reads data from SeatGeek's
sanctioned public Platform API, polls infrequently (every 20 minutes, a
handful of small requests — not a scraping load), and never automates
checkout.

## Resale viability by target (read this before buying anything)

This tool flags price anomalies. It cannot tell you whether a given ticket
is actually resellable — and that varies a lot across the targets you picked.
Here's what the published policies say as of August 2026. Verify against the
specific event before putting money in; none of this is legal advice.

| Target | Resale outlook | What the policy says |
|---|---|---|
| **Utah Jazz** | Good | Mobile tickets delivered via the Jazz or SeatGeek app; SeatGeek documents both transfer and resale from your account. |
| **Utah Mammoth** | Likely similar to Jazz | Same venue/mobile-ticketing stack; confirm on a specific game before relying on it. |
| **Delta Center concerts** | Varies per show | Depends entirely on the promoter/artist. Some tours restrict resale or cap it at face value. Check per event. |
| **Utah Utes** | Caution | Individual-game resale is permitted, but Utah Athletics "reserves the right to cancel and refund tickets" from accounts showing "activity consistent with... purchasing tickets with the primary intent of resale for profit," and to block those accounts from presales. |
| **BYU** | Caution | Resale allowed through your BYU account, but BYU prohibits buying "for the primary purpose of reselling" and may cancel and refund tickets if it determines that's your intent. BYU explicitly disclaims responsibility for tickets bought through third-party services. |
| **UFC at Delta Center** | Avoid for flipping | UFC's event terms state a ticket "may not be resold or offered for resale on any platform other than a platform expressly authorized by the Company," and that transfers to non-compliant buyers "may be voided by the Company and the Ticket cancelled." |

The practical read: **Jazz, Mammoth, and some Delta Center concerts are the
realistic targets.** The college programs both have explicit anti-flipping
language aimed at buyer accounts, so volume there risks account cancellation
rather than just a bad trade. UFC is the one to skip — unauthorized resale
can get the ticket voided, which means your buyer gets turned away at the
door and you eat the chargeback.

Also: Utah doesn't currently ban ticket resale outright, but there's active
legislative interest in adding resale price caps. Worth re-checking if you
scale up.

## Setup

Do steps 1–3 first and confirm it works on your own machine before touching
GitHub. That way, if SeatGeek's API isn't returning price stats for your
events, you find out in two minutes instead of after a full deploy.

### Phase 1 — get it working locally

1. **Get a free SeatGeek API client ID**
   Sign up at https://seatgeek.com/account/develop, create an app, copy the
   Client ID.

2. **Install and configure**
   ```
   pip install -r requirements.txt
   cp config.example.json config.json
   export SEATGEEK_CLIENT_ID=your_client_id_here
   ```
   (On Windows PowerShell use `$env:SEATGEEK_CLIENT_ID="your_client_id_here"`.)

3. **Run it once with no alerting configured**
   ```
   python watcher.py
   ```
   You should see each team/venue resolve to a slug, a count of events
   checked, and either deals printed to the console or "No qualifying deals
   this run."

   **This run is the real test.** If you see events checked but never any
   deals, temporarily set `deal_threshold_pct` to `0.99` in `config.json` and
   re-run — that flags almost everything. If you still get nothing, SeatGeek
   isn't returning `stats.lowest_price` / `average_price` for your events on
   your API tier, and the tool can't work as-is. Tell me and we'll rework the
   data source. Set the threshold back to `0.65` once you've confirmed.

### Phase 2 — wire up push notifications

Alerts go to your phone via [ntfy.sh](https://ntfy.sh) — free, no account, no
API key. It's the only notification channel; there's nothing else to set up.

4. **Pick a hard-to-guess topic name.** Something like
   `carson-slc-tickets-u5wj8rmq`. Anyone who knows the string can read your
   alerts, so make it random rather than a plain word.

5. **Install the ntfy app** (iOS or Android), tap **+**, and subscribe to that
   exact topic name. Allow notifications when prompted — denying that leaves
   pushes silent inside the app. To confirm the phone side works on its own,
   open `ntfy.sh/your-topic-name` in a browser and send yourself a message.

6. **Send a test alert.** This needs no API key, so you can do it while your
   SeatGeek developer account is still pending:
   ```
   python watcher.py --test-alert
   ```
   It pushes a fake deal through the real notification path. Success prints
   `SENT OK -- check your phone` and exits 0; any failure prints `PUSH FAILED`
   with the reason and exits 1.

7. **Re-run a real check locally** to confirm alerts fire from live data:
   ```
   export NTFY_TOPIC=your_topic
   python watcher.py
   ```
   Temporarily raise `deal_threshold_pct` to force an alert if nothing
   currently qualifies. Delete `state.json` between test runs — otherwise the
   dedupe logic will suppress repeat alerts for the same event.

### Phase 3 — put it on a schedule

8. **Create a GitHub repo and push this folder to it.**
   Make it **public** unless you have a reason not to. Public repos get
   unlimited free Actions minutes; private repos on the free plan get 2,000
   minutes/month, and running every 20 minutes will exceed that. Your secrets
   stay encrypted and hidden either way — nothing sensitive lives in these
   files. If you'd rather keep it private, change the cron in
   `.github/workflows/watch.yml` to `"0 * * * *"` (hourly) to stay under the
   limit.

9. **Add two repository secrets** (Settings -> Secrets and variables -> Actions -> New repository secret):
   - `SEATGEEK_CLIENT_ID` — your API key
   - `NTFY_TOPIC` — your notification topic name

10. **Commit `config.json`** with whatever threshold and teams/venues you
   settled on. (`config.example.json` is just the template.)

11. **Trigger a manual run** from the Actions tab -> "SeatGeek Deal Watcher"
    -> "Run workflow", and check the log. Once that run is green, the
    every-20-minute schedule takes over on its own.

    Note: GitHub disables scheduled workflows in repos with no activity for
    60 days. This workflow commits `state.json` on each run, which counts as
    activity, so it should stay alive — but if alerts ever go quiet for a
    long stretch, check the Actions tab first.

## Tuning the deal threshold

`deal_threshold_pct` in `config.json` (default `0.65`) means: flag an event
when its lowest listing price is at or below 65% of that event's average
listing price — i.e. a 35%+ discount. Lower the number for stricter/rarer
alerts, raise it for more (weaker) alerts.

`min_listing_count` (default `3`) avoids flagging events with too few
listings for "average price" to mean much.

## Running locally instead of GitHub Actions

Windows PowerShell:

```
python -m pip install -r requirements.txt
copy config.example.json config.json
$env:SEATGEEK_CLIENT_ID="your_client_id"
$env:NTFY_TOPIC="your_topic"
python watcher.py
```

macOS / Linux:

```
pip install -r requirements.txt
cp config.example.json config.json
export SEATGEEK_CLIENT_ID=your_client_id
export NTFY_TOPIC=your_topic
python watcher.py
```

Run it on a schedule yourself with cron, Task Scheduler, or similar if you'd
rather not use GitHub Actions.

## Files

- `watcher.py` — the whole thing: fetch events, compute deal scores, alert.
- `config.example.json` — copy to `config.json`, holds tracked teams/venues and thresholds (not secret).
- `resolved.json` / `state.json` — auto-created caches (performer/venue slug lookups, and dedupe state so you're not re-alerted every 20 minutes for the same deal).
- `.github/workflows/watch.yml` — the schedule.
