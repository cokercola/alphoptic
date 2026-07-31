"""
Pulls stock trade disclosures for 15 randomly-selected lawmakers who
have a qualifying trade in FMP's daily batch, and writes
data/congress-trades.json for the static site to read.

Uses FMP's senate-latest / house-latest endpoints (confirmed free-tier
accessible - the by-name search endpoints and any explicit page/limit
params both return 402 Payment Required on the free plan, so this makes
a single bare call per chamber and works with whatever batch FMP
returns by default).

There is no pinned/guaranteed lawmaker list anymore - every run picks
15 random lawmakers from whoever has a qualifying trade that day, so
every lawmaker shown always has at least one real trade (no more "no
disclosed trades this period" empty state).

Ticker "linked" status is derived from data/bills.json (this repo's
existing source of truth for company/ticker exposure) rather than a
separate companies dataset - a symbol is linked if it already shows up
somewhere in the tracked bills' company exposure lists, in which case it
points at companies/index.html?ticker=XXX, matching how that page already
filters.

Run once daily via .github/workflows/update-congress-trades.yml

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

# How many lawmakers to show on the site's Trades page each run. This
# is a random sample of whoever has a qualifying trade that day - not
# a fixed/curated list. Raise this later once coverage grows.
RANDOM_LAWMAKER_COUNT = 15

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

    # Every lawmaker who shows up with at least one qualifying trade
    # today, across both chambers.
    all_candidates = {}

    for chamber, raw in (("Senate", senate_raw), ("House", house_raw)):
        for rec in raw:
            full_name = f"{rec.get('firstName', '')} {rec.get('lastName', '')}".strip()
            record_name = full_name or rec.get("office") or rec.get("name")
            if not record_name:
                continue

            entry = build_trade_entry(rec, known_tickers)
            if entry is None:
                continue

            key = (record_name, chamber)
            if key not in all_candidates:
                all_candidates[key] = {
                    "name": record_name,
                    "party": None,
                    "chamber": chamber,
                    "trades": [],
                }
            all_candidates[key]["trades"].append(entry)

    candidate_list = list(all_candidates.values())

    # 15 random lawmakers from whoever has a qualifying trade today.
    # No pinned/guaranteed names, so there's no "no disclosed trades"
    # empty state - everyone shown has at least one real trade.
    selected = random.sample(candidate_list, k=min(RANDOM_LAWMAKER_COUNT, len(candidate_list)))

    for person in selected:
        person["trades"].sort(key=lambda t: t["amount"], reverse=True)
        person["trades"] = person["trades"][:MAX_TRADES_PER_PERSON]

    output = {
        "updated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "lookback_days": LOOKBACK_DAYS,
        "lawmakers": selected,
    }

    os.makedirs("data", exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(output, f, indent=2)

    total = sum(len(p["trades"]) for p in selected)
    print(f"Wrote {total} trades across {len(selected)} randomly-selected lawmakers "
          f"(from {len(candidate_list)} candidates with qualifying trades today) to {OUTPUT_PATH}")

    # Separate, unfiltered output: every qualifying trade from every
    # lawmaker seen this run (not just the 15 shown on the site). This
    # is what the paper trading strategy reads from, so it has a much
    # larger signal pool to act on daily instead of being limited to
    # just the 15 shown on the Trades page.
    all_trade_records = []
    for person in candidate_list:
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
          f"{len(candidate_list)} lawmakers to {ALL_TRADES_OUTPUT_PATH}")


if __name__ == "__main__":
    main()
