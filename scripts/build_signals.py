"""
Rolls up today's activity across all four Alphoptic Signals metrics
(congress trades, new bills, committee activity, upcoming votes) by
industry, and writes data/signals.json for the homepage to render.

Reads three already-written files rather than re-fetching anything:
  data/bills-index.json        -- new bills today, and on_calendar flags
  data/companies.json          -- ticker -> industry, for both the
                                   trades rollup and each industry's
                                   affected-tickers list
  data/congress-trades.json    -- today's trades, by ticker
  data/committee-activity.json -- today's committee meetings, by industry
                                   (written by fetch_committee_activity.py,
                                   which must run before this script)

Run daily via .github/workflows/update-signals.yml, after update-bills.yml
and update-congress-trades.yml so same-day data is available from both.
"""

import json
import datetime
from collections import Counter, defaultdict

BILLS_INDEX_PATH = "data/bills-index.json"
COMPANIES_PATH = "data/companies.json"
TRADES_PATH = "data/congress-trades.json"
COMMITTEE_ACTIVITY_PATH = "data/committee-activity.json"
OUTPUT_PATH = "data/signals.json"

# How many of an industry's combined activity points map to each
# signal level. Combined score = trades + new_bills + committee + votes,
# each unweighted -- deliberately simple to start. Revisit with real
# data once a few weeks of signals.json history exists to tune against,
# same as company_registry.py's CONFIDENCE_THRESHOLD was tuned after
# seeing real output rather than guessed upfront.
LEVEL_THRESHOLDS = [
    (10, "HIGH"),
    (4, "MEDIUM"),
    (1, "LOW"),
]

MAX_TICKERS_PER_SIGNAL = 4
MAX_SIGNALS_ON_HOMEPAGE = 3


def signal_level(score):
    for threshold, label in LEVEL_THRESHOLDS:
        if score >= threshold:
            return label
    return None  # below every threshold -- industry omitted entirely


def build_ticker_to_industry(companies):
    """A ticker can appear under more than one industry across
    different bills (e.g. a conglomerate). Uses whichever industry
    that ticker appears under most often, which is a reasonable
    single-label choice for a rollup like this even though it's a
    simplification of the real world."""
    counts = defaultdict(Counter)
    for c in companies:
        if c.get("ticker") and c.get("industry"):
            counts[c["ticker"]][c["industry"]] += 1
    return {ticker: industry_counts.most_common(1)[0][0] for ticker, industry_counts in counts.items()}


def main():
    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()

    with open(BILLS_INDEX_PATH) as f:
        bills_index = json.load(f)
    with open(COMPANIES_PATH) as f:
        companies_data = json.load(f)
    with open(TRADES_PATH) as f:
        trades_data = json.load(f)
    try:
        with open(COMMITTEE_ACTIVITY_PATH) as f:
            committee_data = json.load(f)
    except FileNotFoundError:
        print(f"WARNING: {COMMITTEE_ACTIVITY_PATH} not found -- run "
              f"fetch_committee_activity.py first. Committee counts will be 0.")
        committee_data = {"industry_counts": {}}

    companies = companies_data.get("companies", [])
    ticker_to_industry = build_ticker_to_industry(companies)

    # New bills today, by industry
    new_bills_by_industry = Counter()
    calendar_bills_by_industry = Counter()
    for b in bills_index.get("bills", []):
        if b.get("last_action_date") == today and b.get("industry"):
            new_bills_by_industry[b["industry"]] += 1
        if b.get("on_calendar") and b.get("industry"):
            calendar_bills_by_industry[b["industry"]] += 1

    # Today's trades, by industry (via each trade's ticker -> industry)
    trades_by_industry = Counter()
    tickers_by_industry = defaultdict(set)
    for lawmaker in trades_data.get("lawmakers", []):
        for t in lawmaker.get("trades", []):
            if t.get("trade_date") != today:
                continue
            industry = ticker_to_industry.get(t.get("symbol"))
            if industry:
                trades_by_industry[industry] += 1
                tickers_by_industry[industry].add(t["symbol"])

    # Also pull affected tickers from companies.json itself, not just
    # trades -- an industry can be flagged by bill activity alone with
    # no matching trade today, and the card still needs tickers to show.
    for c in companies:
        if c.get("ticker") and c.get("industry"):
            tickers_by_industry[c["industry"]].add(c["ticker"])

    committee_by_industry = Counter(committee_data.get("industry_counts", {}))

    all_industries = set(new_bills_by_industry) | set(trades_by_industry) | \
        set(committee_by_industry) | set(calendar_bills_by_industry)

    items = []
    for industry in all_industries:
        trades = trades_by_industry.get(industry, 0)
        new_bills = new_bills_by_industry.get(industry, 0)
        committee = committee_by_industry.get(industry, 0)
        votes = calendar_bills_by_industry.get(industry, 0)
        score = trades + new_bills + committee + votes
        level = signal_level(score)
        if level is None:
            continue
        items.append({
            "industry": industry,
            "level": level,
            "score": score,
            "congress_trades": trades,
            "new_bills": new_bills,
            "committee_activity": committee,
            "upcoming_votes": votes,
            "tickers": sorted(tickers_by_industry.get(industry, []))[:MAX_TICKERS_PER_SIGNAL],
        })

    items.sort(key=lambda i: i["score"], reverse=True)

    output = {
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
        "date": today,
        "items": items[:MAX_SIGNALS_ON_HOMEPAGE],
    }
    with open(OUTPUT_PATH, "w") as f:
        json.dump(output, f, separators=(",", ":"))

    print(f"Wrote {OUTPUT_PATH}: {len(items)} industries cleared a signal "
          f"threshold today, top {min(len(items), MAX_SIGNALS_ON_HOMEPAGE)} kept for the homepage.")


if __name__ == "__main__":
    main()
