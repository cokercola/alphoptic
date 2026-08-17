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
BILLS_FULL_PATH = "data/bills.json"
COMPANIES_PATH = "data/companies.json"
TRADES_PATH = "data/congress-trades.json"
COMMITTEE_ACTIVITY_PATH = "data/committee-activity.json"
SIGNALS_PATH = "data/signals.json"
SIGNALS_DETAIL_PATH = "data/signals-detail.json"

# "Other / Cross-Sector" is a catch-all for committee meetings that
# fetch_committee_activity.py couldn't map to a real industry (see
# COMMITTEE_INDUSTRY_MAP there) - it's a data-quality bucket, not an
# actual industry, so it shouldn't show up as a legislative signal a
# user could act on. Still tracked in committee-activity.json's
# unmapped_meetings count for internal visibility; just excluded here.
EXCLUDED_INDUSTRIES = {"Other / Cross-Sector"}

# update-congress-trades.yml's schedule is paused (see that workflow's
# comment) while we look for a free replacement for the FMP data
# source. congress-trades.json is now a frozen snapshot that stops
# getting fresher - but its trades don't stop being "recent" by this
# script's lookback-window math just because the file stopped
# updating. Left un-excluded, a signal's LOW/MEDIUM/HIGH level could
# keep being partly determined by stale trade data that no longer
# appears anywhere in the UI for a reader to see or verify, which
# defeats the entire point of the "why we flagged this" page. So:
# while trades stay paused, they're read but never counted toward
# score or evidence. Flip this back to False (and re-enable that
# workflow's schedule) once trades are back on a live data source.
TRADES_DATA_PAUSED = True

LOOKBACK_DAYS = 3

# How many of an industry's combined activity points map to each
# signal level. Combined score = new_bills + committee + votes, plus
# trades whenever TRADES_DATA_PAUSED is False -- each unweighted,
# deliberately simple to start. Revisit with real data once a few
# weeks of signals.json history exists to tune against.
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
    with open(BILLS_FULL_PATH) as f:
        bills_full = json.load(f)
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

    # bill_id -> its companies (ticker/name/effect/exposure), from the
    # full bills.json signals list. bills-index.json (used above for
    # the lightweight per-industry rollup) doesn't carry this, so each
    # evidence bill can show the specific companies actually tied to
    # it, rather than every company loosely tagged under the industry.
    companies_by_bill_id = {
        s.get("bill_id"): s.get("companies", [])
        for s in bills_full.get("signals", [])
        if s.get("bill_id")
    }

    # bill_id -> {title, sponsor}, so a committee meeting's related_bills
    # (just bill numbers from Congress.gov's relatedItems) can be resolved
    # into something a reader can actually recognize. Only covers bills
    # Alphoptic tracks - a meeting can reference a bill outside that set,
    # in which case it's shown as a bare bill number with no extra detail.
    tracked_bill_lookup = {
        b.get("bill_id"): {"title": b.get("title"), "sponsor": b.get("sponsor")}
        for b in bills_index.get("bills", [])
        if b.get("bill_id")
    }

    def resolve_related_bills(bill_ids):
        resolved = []
        for bid in (bill_ids or [])[:MAX_TICKERS_PER_SIGNAL]:
            tracked = tracked_bill_lookup.get(bid)
            resolved.append({
                "bill_id": bid,
                "title": tracked["title"] if tracked else None,
                "sponsor": tracked["sponsor"] if tracked else None,
                "companies": companies_by_bill_id.get(bid, [])[:MAX_TICKERS_PER_SIGNAL] if tracked else [],
                "tracked": tracked is not None,
            })
        return resolved

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
            "sponsor": b.get("sponsor"),
            "companies": companies_by_bill_id.get(b.get("bill_id"), [])[:MAX_TICKERS_PER_SIGNAL],
        }
        bills_evidence[industry].append(entry)
        if b.get("on_calendar"):
            calendar_evidence[industry].append(entry)

    if not TRADES_DATA_PAUSED:
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

    def evidence_companies(new_bills, committee_meetings, calendar_bills):
        """Real companies tied to this industry's actual evidence this
        run - the bills discussed, not the industry's full static
        roster. Dedupes by ticker, first occurrence wins. Deliberately
        has no static-roster fallback when this is empty - an empty
        result here means "no company evidence," and showing a generic
        roster instead would misrepresent that."""
        seen = {}
        for b in new_bills:
            for c in b.get("companies", []):
                if c.get("ticker") and c["ticker"] not in seen:
                    seen[c["ticker"]] = c
        for m in committee_meetings:
            for rb in m.get("related_bills", []):
                for c in rb.get("companies", []):
                    if c.get("ticker") and c["ticker"] not in seen:
                        seen[c["ticker"]] = c
        for b in calendar_bills:
            for c in b.get("companies", []):
                if c.get("ticker") and c["ticker"] not in seen:
                    seen[c["ticker"]] = c
        return list(seen.values())[:MAX_TICKERS_PER_SIGNAL]

    def most_recent_date(new_bills, committee_meetings, calendar_bills):
        dates = [b.get("last_action_date") for b in new_bills if b.get("last_action_date")]
        dates += [b.get("last_action_date") for b in calendar_bills if b.get("last_action_date")]
        dates += [(m.get("date") or "")[:10] for m in committee_meetings if m.get("date")]
        return max(dates) if dates else None

    def build_subject(new_bills, committee_meetings, calendar_bills):
        """A specific, real sentence fragment naming what actually
        happened, preferring the most concrete evidence available.
        Returns None when there's genuinely nothing specific to name -
        the caller falls back to a generic-but-honest tier phrase."""
        for m in committee_meetings:
            title = m.get("title")
            if title and title.strip().lower() not in ("committee meeting", ""):
                short = title if len(title) <= 110 else title[:107] + "..."
                return f"a {m.get('chamber', '').lower()} committee meeting covered {short}".replace("  ", " ")
        for m in committee_meetings:
            for rb in m.get("related_bills", []):
                if rb.get("tracked") and rb.get("title"):
                    return f"a committee meeting discussed {rb['bill_id']} — {rb['title']}"
        for b in new_bills:
            if b.get("title"):
                return f"{b['bill_id']} — {b['title']}"
        for b in calendar_bills:
            if b.get("title"):
                return f"{b['bill_id']} is now queued for a floor vote"
        return None

    committee_meetings_by_industry = {
        industry: [
            {**meeting, "related_bills": resolve_related_bills(meeting.get("related_bills"))}
            for meeting in meetings
        ]
        for industry, meetings in committee_data.get("meetings_by_industry", {}).items()
    }
    committee_counts = Counter(committee_data.get("industry_counts", {}))

    all_industries = set(bills_evidence) | set(trades_evidence) | \
        set(committee_counts) | set(calendar_evidence)

    detail_items = []
    for industry in all_industries:
        if industry in EXCLUDED_INDUSTRIES:
            continue
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
            "companies": evidence_companies(new_bills, committee_meetings, calendar_bills),
            "subject": build_subject(new_bills, committee_meetings, calendar_bills),
            "most_recent_date": most_recent_date(new_bills, committee_meetings, calendar_bills),
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

    # Real totals across every tracked industry (not just ones that
    # cleared threshold), excluding the catch-all bucket - these back
    # the homepage's empty state ("0 new bill actions, 0 committee
    # meetings... across all N tracked industries") with the same
    # verifiable-evidence standard as everything else on the site,
    # instead of a vague "check back soon."
    totals = {
        "new_bills": sum(len(v) for k, v in bills_evidence.items() if k not in EXCLUDED_INDUSTRIES),
        "committee_meetings": sum(len(v) for k, v in committee_meetings_by_industry.items() if k not in EXCLUDED_INDUSTRIES),
        "calendar_bills": sum(len(v) for k, v in calendar_evidence.items() if k not in EXCLUDED_INDUSTRIES),
        # committee_data's industry_counts already has every tracked
        # industry as a key (fetch_committee_activity.py initializes
        # it that way, even industries with 0 meetings this run) - a
        # free, already-loaded source for this count that avoids
        # importing fetch_bills.py just for its INDUSTRY_TAXONOMY
        # constant, which would also pull in that script's top-level
        # CONGRESS_API_KEY read (not set in this step of the workflow).
        "industries_tracked": len([
            i for i in committee_data.get("industry_counts", {}) if i not in EXCLUDED_INDUSTRIES
        ]),
    }

    # Upcoming scheduled meetings, resolved the same way as past ones -
    # real bill titles/sponsors/companies where the related bill is
    # tracked, a bare bill number otherwise. Meetings whose committee
    # didn't map to a real industry are excluded here too, same as
    # Other/Cross-Sector everywhere else - "unknown which industry"
    # isn't useful "what's coming up" information.
    MAX_UPCOMING_ON_HOMEPAGE = 6
    upcoming = [
        {
            "date": m["date"],
            "industry": m["industry"],
            "committee": m["committee"],
            "chamber": m.get("chamber"),
            "related_bills": resolve_related_bills(m.get("related_bills")),
        }
        for m in committee_data.get("upcoming_meetings", [])
        if m.get("industry") and m["industry"] not in EXCLUDED_INDUSTRIES
    ][:MAX_UPCOMING_ON_HOMEPAGE]

    with open(SIGNALS_PATH, "w") as f:
        json.dump({
            "updated_at": now_iso,
            "date": today.isoformat(),
            "lookback_days": LOOKBACK_DAYS,
            "total_cleared": len(detail_items),
            "totals": totals,
            "upcoming": upcoming,
            "items": summary_items,
        }, f, separators=(",", ":"))

    print(f"Wrote {SIGNALS_DETAIL_PATH}: {len(detail_items)} industries cleared a signal threshold "
          f"(lookback: {LOOKBACK_DAYS} days).")
    print(f"Wrote {SIGNALS_PATH}: top {len(summary_items)} kept for the homepage, "
          f"{len(upcoming)} upcoming meetings included "
          f"(total_cleared={len(detail_items)}, totals={totals}).")


if __name__ == "__main__":
    main()
