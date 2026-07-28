"""
Pulls recent Senate + House stock trade disclosures for a fixed watchlist
of lawmakers and writes data/congress-trades.json for the static site to
read.

Only 2 API calls per run (senate-latest, house-latest), regardless of how
many lawmakers/symbols are tracked - filtering happens client-side here,
which keeps this well within FMP's free-tier daily request cap.

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
import datetime
import requests

FMP_API_KEY = os.environ["FMP_API_KEY"]
FMP_BASE = "https://financialmodelingprep.com/stable"

BILLS_JSON_PATH = "data/bills.json"
OUTPUT_PATH = "data/congress-trades.json"

# The 5 lawmakers we're tracking. `match` is a list of substrings FMP might
# use in its representative/senator name field for this person - FMP's
# naming isn't always consistent (e.g. "Nancy Pelosi" vs "Pelosi, Nancy"),
# so we match loosely and case-insensitively.
WATCHLIST = [
    {"name": "Nancy Pelosi",    "party": "D", "chamber": "House",  "match": ["pelosi"]},
    {"name": "Ro Khanna",       "party": "D", "chamber": "House",  "match": ["khanna"]},
    {"name": "Ted Cruz",        "party": "R", "chamber": "Senate", "match": ["cruz"]},
    {"name": "Michael McCaul",  "party": "R", "chamber": "House",  "match": ["mccaul"]},
    {"name": "Dan Crenshaw",    "party": "R", "chamber": "House",  "match": ["crenshaw"]},
]

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


def match_lawmaker(record_name, chamber):
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
    url = f"{FMP_BASE}/{endpoint}"
    resp = requests.get(url, params={"apikey": FMP_API_KEY}, timeout=30)
    resp.raise_for_status()
    return resp.json()


def main():
    known_tickers = load_known_tickers()

    senate_raw = fetch_latest("senate-latest")
    house_raw = fetch_latest("house-latest")

    by_person = {
        p["name"]: {"name": p["name"], "party": p["party"], "chamber": p["chamber"], "trades": []}
        for p in WATCHLIST
    }

    for chamber, raw in (("Senate", senate_raw), ("House", house_raw)):
        for rec in raw:
            record_name = rec.get("representative") or rec.get("senator") or rec.get("name") or rec.get("office")
            person = match_lawmaker(record_name, chamber)
            if not person:
                continue

            direction = normalize_direction(rec.get("type") or rec.get("transactionType"))
            if direction is None:
                continue

            trade_date = rec.get("transactionDate") or rec.get("dateReceived")
            filed_date = rec.get("disclosureDate") or rec.get("dateReceived")
            if not within_lookback(trade_date or filed_date, LOOKBACK_DAYS):
                continue

            amount = parse_amount(rec.get("amount"))
            if amount is None:
                continue

            symbol = (rec.get("symbol") or rec.get("ticker") or "").upper()
            if not symbol:
                continue

            by_person[person["name"]]["trades"].append({
                "symbol": symbol,
                "amount": amount,
                "direction": direction,
                "trade_date": trade_date,
                "filed_date": filed_date,
                "is_new": within_lookback(filed_date, NEW_WITHIN_DAYS),
                "linked": symbol in known_tickers,
            })

    for person in by_person.values():
        person["trades"].sort(key=lambda t: t["amount"], reverse=True)
        person["trades"] = person["trades"][:MAX_TRADES_PER_PERSON]

    output = {
        "updated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "lookback_days": LOOKBACK_DAYS,
        "lawmakers": list(by_person.values()),
    }

    os.makedirs("data", exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(output, f, indent=2)

    total = sum(len(p["trades"]) for p in by_person.values())
    print(f"Wrote {total} trades across {len(by_person)} lawmakers to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
