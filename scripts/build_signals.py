"""
Rolls up recent activity across all four Alphoptic Signals metrics
(congress trades, new bills, committee activity, upcoming votes) by
industry, and writes two files:

  data/signals.json         -- top 3 industries, summary only. What
                                the homepage fetches.
  data/signals-detail.json  -- every industry that cleared a
                                threshold, WITH the actual underlying
                                records (which trades, which bills,
                                which meetings) that produced each
                                count. What signals/detail.html
                                fetches, so "why we flagged this" can
                                show real facts, not just a number.

Reads three already-written files rather than re-fetching anything:
  data/bills-index.json        -- new/updated bills, and on_calendar flags
  data/companies.json          -- ticker -> industry
  data/congress-trades.json    -- disclosed trades
  data/committee-activity.json -- committee meetings, by industry
                                   (written by fetch_committee_activity.py,
                                   which must run before this script)

Run daily via .github/workflows/update-signals.yml, after update-bills.yml
and update-congress-trades.yml so same-day data is available from both.

IMPORTANT: uses a LOOKBACK_DAYS window, not exact date-string matching
against "today". Two real-world reasons for that:
  1. congress-trades.json's `trade_date` (when the trade happened) can
     lag `filed_date` (when it's disclosed) by weeks -- filed_date is
     the meaningful "this just became known" date, and even that
     rarely lands on today's exact UTC date given reporting/pipeline
     timing, so a same-day-only filter was making this metric
     permanently ~0 in practice.
  2. bills-index.json's last_action_date reflects Congress.gov's own
     action timestamp, which doesn't reliably land on "today" in this
     pipeline's UTC run time either.
A short window (default 3 days) keeps this genuinely "recent" without
being an artifact of exact-match timing.
"""

import json
import datetime
from collections import Counter, defaultdict

BILLS_INDEX_PATH = "data/bills-index.json"
COMPANIES_PATH = "data/companies.json"
TRADES_PATH = "data/congress-trades.json"
COMMITTEE_ACTIVITY_PATH = "data/committee-activity.json"
SIGNALS_PATH = "data/signals.json"
SIGNALS_DETAIL_PATH = "data/signals-detail.json"

LOOKBACK_DAYS = 3

# How many of an industry's combined activity points map to each
# signal level. Combined score = trades + new_bills + committee + votes,
# each unweighted -- deliberately simple to start. Revisit with real
# data once a few weeks of signals.json history exists to tune against.
LEVEL_THRESHOLDS = [
    (10, "HIGH"),
    (4, "MEDIUM"),
    (1, "LOW"),
]

MAX_TICKERS_PER_SIGNAL = 4
MAX_SIGNALS_ON_HOMEPAGE = 3
MAX_EVIDENCE_ITEMS_PER_CATEGORY = 20  # detail page cap, so one noisy industry can't return a huge payload


def signal_level(score):
    for threshold, label in LEVEL_THRESHOLDS:
        if score >= threshold:
            return label
    return None


def within_lookback(date_str, cutoff):
    return bool(date_str) and date_str >= cutoff


def build_ticker_to_industry(companies):
    """A ticker can appear under more than one industry across
    different bills. Uses whichever industry that ticker appears
    under most often -- a simplification, but a reasonable single
    label for a rollup like this."""
    counts = defaultdict(Counter)
    for c in companies:
        if c.get("ticker") and c.get("industry"):
            counts[c["ticker"]][c["industry"]] += 1
    return {ticker: industry_counts.most_common(1)[0][0] for ticker, industry_counts in counts.items()}


def main():
    today = datetime.datetime.now(datetime.timezone.utc).date()
    cutoff = (today - datetime.timedelta(days=LOOKBACK_DAYS)).isoformat()

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
        committee_data = {"industry_counts": {}, "meetings_by_industry": {}}

    companies = companies_data.get("companies", [])
    ticker_to_industry = build_ticker_to_industry(companies)

    bills_evidence = defaultdict(list)
    calendar_evidence = defaultdict(list)
    trades_evidence = defaultdict(list)

    for b in bills_index.get("bills", []):
        industry = b.get("industry")
        last_action_date = b.get("last_action_date")
        if not industry or not within_lookback(last_action_date, cutoff):
            continue
        entry = {
            "bill_id": b.get("bill_id"),
            "title": b.get("title"),
            "last_action": b.get("last_action") or b.get("status"),
            "last_action_date": last_action_date,
            "stage_label": b.get("stage_label"),
        }
        bills_evidence[industry].append(entry)
        if b.get("on_calendar"):
            calendar_evidence[industry].append(entry)

    for lawmaker in trades_data.get("lawmakers", []):
        for t in lawmaker.get("trades", []):
            if not within_lookback(t.get("filed_date"), cutoff):
                continue
            industry = ticker_to_industry.get(t.get("symbol"))
            if not industry:
                continue
            trades_evidence[industry].append({
                "lawmaker": lawmaker.get("name"),
                "party": lawmaker.get("party"),
                "chamber": lawmaker.get("chamber"),
                "ticker": t.get("symbol"),
                "amount": t.get("amount"),
                "direction": t.get("direction"),
                "filed_date": t.get("filed_date"),
            })

    tickers_by_industry = defaultdict(set)
    for c in companies:
        if c.get("ticker") and c.get("industry"):
            tickers_by_industry[c["industry"]].add(c["ticker"])

    committee_meetings_by_industry = committee_data.get("meetings_by_industry", {})
    committee_counts = Counter(committee_data.get("industry_counts", {}))

    all_industries = set(bills_evidence) | set(trades_evidence) | \
        set(committee_counts) | set(calendar_evidence)

    detail_items = []
    for industry in all_industries:
        trades = trades_evidence.get(industry, [])
        new_bills = bills_evidence.get(industry, [])
        committee_meetings = committee_meetings_by_industry.get(industry, [])
        calendar_bills = calendar_evidence.get(industry, [])

        score = len(trades) + len(new_bills) + len(committee_meetings) + len(calendar_bills)
        level = signal_level(score)
        if level is None:
            continue

        detail_items.append({
            "industry": industry,
            "level": level,
            "score": score,
            "lookback_days": LOOKBACK_DAYS,
            "congress_trades": len(trades),
            "new_bills": len(new_bills),
            "committee_activity": len(committee_meetings),
            "upcoming_votes": len(calendar_bills),
            "tickers": sorted(tickers_by_industry.get(industry, []))[:MAX_TICKERS_PER_SIGNAL],
            "evidence": {
                "trades": trades[:MAX_EVIDENCE_ITEMS_PER_CATEGORY],
                "new_bills": new_bills[:MAX_EVIDENCE_ITEMS_PER_CATEGORY],
                "committee_meetings": committee_meetings[:MAX_EVIDENCE_ITEMS_PER_CATEGORY],
                "calendar_bills": calendar_bills[:MAX_EVIDENCE_ITEMS_PER_CATEGORY],
            },
        })

    detail_items.sort(key=lambda i: i["score"], reverse=True)

    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")

    with open(SIGNALS_DETAIL_PATH, "w") as f:
        json.dump({"updated_at": now_iso, "date": today.isoformat(),
                    "lookback_days": LOOKBACK_DAYS, "items": detail_items}, f, separators=(",", ":"))

    summary_items = [
        {k: v for k, v in item.items() if k != "evidence"}
        for item in detail_items[:MAX_SIGNALS_ON_HOMEPAGE]
    ]
    with open(SIGNALS_PATH, "w") as f:
        json.dump({"updated_at": now_iso, "date": today.isoformat(), "items": summary_items}, f, separators=(",", ":"))

    print(f"Wrote {SIGNALS_DETAIL_PATH}: {len(detail_items)} industries cleared a signal threshold "
          f"(lookback: {LOOKBACK_DAYS} days).")
    print(f"Wrote {SIGNALS_PATH}: top {len(summary_items)} kept for the homepage.")


if __name__ == "__main__":
    main()
