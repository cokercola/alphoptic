"""
Pulls stock trade disclosures for a fixed watchlist of lawmakers, plus 2
randomly-selected additional lawmakers who happen to have recent trades,
and writes data/congress-trades.json for the static site to read.

Uses FMP's senate-latest / house-latest endpoints (confirmed free-tier
accessible - the by-name search endpoints and any explicit page/limit
params both return 402 Payment Required on the free plan, so this makes
a single bare call per chamber and works with whatever batch FMP
returns by default).

Because senate-latest/house-latest return the most recent disclosures
across ALL ~535 members of Congress rather than just our 5 tracked ones,
any given run's default batch may contain none, some, or all of them -
that's a real limitation of the free tier, not a bug. To make sure the
page still has something to show even on a quiet day for our 5, we also
grab 2 random other lawmakers from whoever *does* have qualifying trades
in that batch. The 5 tracked lawmakers always appear on the page
(with a "no disclosed trades this period" empty state if applicable);
the 2 random extras only appear if they actually have trades to show.

Ticker "linked" status is derived from data/bills.json (this repo's
existing source of truth for company/ticker exposure) rather than a
separate companies dataset - a symbol is linked if it already shows up
somewhere in the tracked bills' company exposure lists, in which case it
points at companies/index.html?ticker=XXX, matching how that page already
filters.

Run twice daily via .github/workflows/update-congress-trades.yml

Required environment variable (set as a GitHub Actions secret):
  FMP_API_KEY  - from financialmodelingprep.com (free tier)
"""

import os
import json
import random
import datetime
import requests

FMP_API_KEY = os.environ["FMP_API_KEY"]
FMP_BASE = "https://financialmodelingprep.com/stable"

BILLS_JSON_PATH = "data/bills.json"
OUTPUT_PATH = "data/congress-trades.json"
ALL_TRADES_OUTPUT_PATH = "data/congress-trades-all.json"

# The 5 lawmakers we're always tracking. `match` is a list of substrings
# checked against each record's actual firstName + lastName
# (case-insensitive).
WATCHLIST = [
    {"name": "Nancy Pelosi",    "party": "D", "chamber": "House",  "match": ["pelosi"]},
    {"name": "Ro Khanna",       "party": "D", "chamber": "House",  "match": ["khanna"]},
    {"name": "Ted Cruz",        "party": "R", "chamber": "Senate", "match": ["cruz"]},
    {"name": "Michael McCaul",  "party": "R", "chamber": "House",  "match": ["mccaul"]},
    {"name": "Dan Crenshaw",    "party": "R", "chamber": "House",  "match": ["crenshaw"]},
]

RANDOM_EXTRA_COUNT = 2   # how many additional random lawmakers to add

LOOKBACK_DAYS = 30       # only show disclosures within this window
NEW_WITHIN_DAYS = 7      # flag as "new" if filed within this many days
MAX_TRADES_PER_PERSON = 15


def load_known_tickers():
    """Every ticker already referenced anywhere in data/bills.json's
    company exposure lists. A trade symbol is 'linked' if it's in this
    set, since companies/index.html?ticker=XXX only has something to show
    for tickers that actually appear in a tracked bill's exposure list."""
    try:
        with open(BILLS_JSON_PATH) as f:
            bills = json.load(f)
    except FileNotFoundError:
        print(f"WARNING: {BILLS_JSON_PATH} not found; no trade tickers will be linked.")
        return set()

    tickers = set()
    for signal in bills.get("signals", []):
        for company in signal.get("companies", []):
            ticker = company.get("ticker")
            if ticker:
                tickers.add(ticker.upper())
    return tickers


def match_watchlist(record_name, chamber):
    name_lower = (record_name or "").lower()
    for person in WATCHLIST:
        if person["chamber"] != chamber:
            continue
        if any(fragment in name_lower for fragment in person["match"]):
            return person
    return None


def normalize_direction(raw_type):
    """FMP's transaction type field has values like 'Purchase', 'Sale',
    'Sale (Partial)', 'Sale (Full)', 'Exchange', etc. Collapse to buy/sell.
    Returns None for exchanges/gifts - not a directional signal."""
    t = (raw_type or "").lower()
    if "purchase" in t or "buy" in t:
        return "buy"
    if "sale" in t or "sell" in t:
        return "sell"
    return None


def parse_amount(raw_range):
    """FMP reports amounts as a disclosure range string, e.g.
    '$250,001 - $500,000'. We take the midpoint, the standard convention
    other congressional trade trackers use (exact amounts aren't
    legally required in the disclosures)."""
    if not raw_range:
        return None
    parts = raw_range.replace("$", "").replace(",", "").split("-")
    try:
        nums = [int(p.strip()) for p in parts if p.strip()]
        return sum(nums) // len(nums) if nums else None
    except ValueError:
        return None


def within_lookback(date_str, days):
    if not date_str:
        return False
    try:
        d = datetime.datetime.strptime(date_str[:10], "%Y-%m-%d")
    except ValueError:
        return False
    return d >= datetime.datetime.utcnow() - datetime.timedelta(days=days)


def fetch_latest(endpoint):
    """A single, unparameterized call to senate-latest / house-latest.
    FMP's free tier only allows the bare call - explicit page/limit
    params (even matching FMP's own documented defaults) trigger a 402.
    So this returns whatever FMP's default batch is and nothing more."""
    url = f"{FMP_BASE}/{endpoint}"
    resp = requests.get(url, params={"apikey": FMP_API_KEY}, timeout=30)
    resp.raise_for_status()
    return resp.json()


def build_trade_entry(rec, known_tickers):
    """Turns one raw FMP record into our trade schema, or returns None if
    it doesn't qualify (no clear direction, too old, unparseable amount,
    missing symbol)."""
    direction = normalize_direction(rec.get("type") or rec.get("transactionType"))
    if direction is None:
        return None

    trade_date = rec.get("transactionDate") or rec.get("dateReceived")
    filed_date = rec.get("disclosureDate") or rec.get("dateReceived")
    if not within_lookback(trade_date or filed_date, LOOKBACK_DAYS):
        return None

    amount = parse_amount(rec.get("amount"))
    if amount is None:
        return None

    symbol = (rec.get("symbol") or rec.get("ticker") or "").upper()
    if not symbol:
        return None

    return {
        "symbol": symbol,
        "amount": amount,
        "direction": direction,
        "trade_date": trade_date,
        "filed_date": filed_date,
        "is_new": within_lookback(filed_date, NEW_WITHIN_DAYS),
        "linked": symbol in known_tickers,
    }


def main():
    known_tickers = load_known_tickers()

    senate_raw = fetch_latest("senate-latest")
    house_raw = fetch_latest("house-latest")
    print(f"Fetched {len(senate_raw)} Senate records, {len(house_raw)} House records "
          f"(FMP's default free-tier batch - no pagination available).")

    # The 5 pinned lawmakers - always present in the output, even empty.
    pinned = {
        p["name"]: {"name": p["name"], "party": p["party"], "chamber": p["chamber"],
                     "pinned": True, "trades": []}
        for p in WATCHLIST
    }

    # Everyone else who shows up with at least one qualifying trade -
    # candidates for the 2 random extras.
    others = {}

    for chamber, raw in (("Senate", senate_raw), ("House", house_raw)):
        for rec in raw:
            full_name = f"{rec.get('firstName', '')} {rec.get('lastName', '')}".strip()
            record_name = full_name or rec.get("office") or rec.get("name")
            if not record_name:
                continue

            entry = build_trade_entry(rec, known_tickers)
            if entry is None:
                continue

            watchlisted = match_watchlist(record_name, chamber)
            if watchlisted:
                pinned[watchlisted["name"]]["trades"].append(entry)
            else:
                key = (record_name, chamber)
                if key not in others:
                    others[key] = {"name": record_name, "party": None, "chamber": chamber,
                                    "pinned": False, "trades": []}
                others[key]["trades"].append(entry)

    # Pick a random 2 (or fewer, if not enough candidates) from everyone
    # else who had qualifying trades this run.
    other_candidates = list(others.values())
    random_extras = random.sample(other_candidates, k=min(RANDOM_EXTRA_COUNT, len(other_candidates)))

    all_lawmakers = list(pinned.values()) + random_extras

    for person in all_lawmakers:
        person["trades"].sort(key=lambda t: t["amount"], reverse=True)
        person["trades"] = person["trades"][:MAX_TRADES_PER_PERSON]

    output = {
        "updated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "lookback_days": LOOKBACK_DAYS,
        "lawmakers": all_lawmakers,
    }

    os.makedirs("data", exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(output, f, indent=2)

    total = sum(len(p["trades"]) for p in all_lawmakers)
    print(f"Wrote {total} trades across {len(pinned)} pinned + {len(random_extras)} random "
          f"lawmakers to {OUTPUT_PATH}")
    if random_extras:
        print("Random extras this run: " + ", ".join(p["name"] for p in random_extras))

    # Separate, unfiltered output: every qualifying trade from every
    # lawmaker seen this run (not just the 5 pinned + 2 random extras
    # shown on the site). This is what the paper trading strategy reads
    # from, so it has a much larger signal pool to act on daily instead
    # of being limited to just 5-7 specific people.
    all_trade_records = []
    for person in list(pinned.values()) + other_candidates:
        for trade in person["trades"]:
            all_trade_records.append({
                "lawmaker": person["name"],
                "chamber": person["chamber"],
                **trade,
            })

    all_trades_output = {
        "updated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "lookback_days": LOOKBACK_DAYS,
        "trades": all_trade_records,
    }

    with open(ALL_TRADES_OUTPUT_PATH, "w") as f:
        json.dump(all_trades_output, f, indent=2)

    print(f"Wrote {len(all_trade_records)} total qualifying trades across "
          f"{len(pinned) + len(other_candidates)} lawmakers to {ALL_TRADES_OUTPUT_PATH}")


if __name__ == "__main__":
    main()
