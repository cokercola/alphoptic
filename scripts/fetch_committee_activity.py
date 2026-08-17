"""
Pulls the last few days of House and Senate committee meetings from
Congress.gov and rolls them up by industry, for the "Committee
activity" metric on the homepage's Alphoptic Signals panel and its
detail page.

Congress.gov has a real committee-meeting endpoint
(/v3/committee-meeting/{congress}/{chamber}) with date filters, unlike
"upcoming floor votes" which has no clean API (see is_on_calendar() in
fetch_bills.py for how that one's handled instead).

Committee meetings aren't reliably linked to a specific tracked bill
in the LIST response, so this maps by COMMITTEE NAME -> industry using
a small static lookup below, not by cross-referencing bill_id. That
lookup is necessarily incomplete (~200 committees and subcommittees
exist across both chambers) -- unmapped committees fall into "Other /
Cross-Sector" rather than being silently dropped, and the printed
summary reports how many meetings that affects so gaps are visible in
the workflow log.

The DETAIL response for a meeting (fetched separately, one call per
meeting) is richer: it can include relatedItems.bills, the actual
legislation on the agenda for markup/business meetings (hearings
without markup often have none - that's a real gap in what Congress
publishes, not a bug here). This script fetches that detail for every
meeting and records any related bill numbers, so build_signals.py can
cross-reference them against tracked bills for real titles, sponsors,
and company evidence on the "why we flagged this" page - instead of
that page only ever saying "there was a meeting."

Writes both aggregate counts AND the actual meeting list per industry
(committee name, chamber, date, related bill numbers) -- signals/
detail.html shows the real meetings, not just a number, so "why we
flagged this" is backed by something a reader can actually verify.

LOOKBACK_DAYS must match scripts/build_signals.py's constant of the
same name -- both need to agree on what "recent" means, or the
committee count here won't line up with the other three metrics there.

Run daily via .github/workflows/update-signals.yml, before build_signals.py.

Required environment variables:
  CONGRESS_API_KEY - from https://api.congress.gov/sign-up/
"""

import os
import json
import datetime

import requests

from fetch_bills import INDUSTRY_TAXONOMY

CONGRESS_API_KEY = os.environ["CONGRESS_API_KEY"]
CONGRESS_BASE = "https://api.congress.gov/v3"
OUTPUT_PATH = "data/committee-activity.json"
CURRENT_CONGRESS = 119
LOOKBACK_DAYS = 3  # keep in sync with build_signals.py's LOOKBACK_DAYS

# Maps a committee's name (as returned by the committee-meeting API,
# case-insensitive substring match) to one of the fixed industries in
# INDUSTRY_TAXONOMY (see fetch_bills.py). Deliberately substring-based
# rather than exact-match, since the API mixes full-committee and
# subcommittee names ("Armed Services" vs "House Armed Services
# Subcommittee on Readiness") and both should map the same way.
# Extend this as unmapped committees show up in the workflow log.
COMMITTEE_INDUSTRY_MAP = {
    "armed services": "Defense & Aerospace",
    "financial services": "Financial Services & Banking",
    "banking": "Financial Services & Banking",
    "energy and commerce": "Energy & Utilities",
    "energy and natural resources": "Energy & Utilities",
    "health": "Healthcare & Pharmaceuticals",
    "veterans": "Veterans Affairs",
    "agriculture": "Agriculture & Food",
    "transportation": "Transportation & Infrastructure",
    "infrastructure": "Transportation & Infrastructure",
    "science, space": "Science & Research",
    "education": "Education",
    "labor": "Labor & Employment",
    "judiciary": "Criminal Justice & Law Enforcement",
    "homeland security": "Criminal Justice & Law Enforcement",
    "foreign affairs": "International Affairs & Trade",
    "foreign relations": "International Affairs & Trade",
    "ways and means": "Financial Services & Banking",
    "small business": "Retail & Consumer Goods",
    "natural resources": "Environmental Protection & Natural Resources",
    "environment": "Environmental Protection & Natural Resources",
    "commerce, science": "Technology",
    "oversight": "Government & Public Administration",
    "appropriations": "Government & Public Administration",
    "rules": "Government & Public Administration",
    "budget": "Government & Public Administration",
    "housing": "Real Estate & Housing",
    "intelligence": "Defense & Aerospace",
}


def committee_to_industry(committee_name):
    name = (committee_name or "").lower()
    for keyword, industry in COMMITTEE_INDUSTRY_MAP.items():
        if keyword in name:
            return industry
    return None  # unmapped -- counted separately, not silently dropped


def fetch_meetings_for_chamber(chamber, since_iso):
    """Congress.gov's committee-meeting list endpoint isn't
    date-filterable server-side in a way that's reliable across
    congresses, so this pages through the most recent meetings and
    stops once it reaches items older than `since_iso` -- cheap in
    practice since meeting lists are naturally sorted newest-first."""
    meetings = []
    offset = 0
    limit = 100
    while True:
        resp = requests.get(
            f"{CONGRESS_BASE}/committee-meeting/{CURRENT_CONGRESS}/{chamber}",
            params={"api_key": CONGRESS_API_KEY, "format": "json", "limit": limit, "offset": offset},
            timeout=30,
        )
        resp.raise_for_status()
        batch = resp.json().get("committeeMeetings", [])
        if not batch:
            break
        stop = False
        for m in batch:
            meeting_date = m.get("date") or m.get("updateDate")
            if meeting_date and meeting_date < since_iso:
                stop = True
                break
            meetings.append(m)
        if stop or len(batch) < limit:
            break
        offset += limit
    return meetings


def fetch_meeting_detail(meeting_url):
    """One request per meeting to the detail endpoint. Returns
    (committee_name, related_bills, real_date).

    real_date is the actual meeting date from the detail response.
    This matters because the LIST endpoint's date field is frequently
    empty, and the code used to fall back to updateDate - when
    Congress.gov last touched the record (e.g. posting a witness
    statement or hearing video weeks or months after the fact), not
    when the meeting happened. That silently misclassified old
    meetings as "recent committee activity" any time their listing
    got edited, and made the same meeting able to look "new" more
    than once if it got edited more than once - very likely the cause
    of an apparent duplicate meeting seen in testing. The detail
    endpoint's date field is what Congress.gov's own event pages
    display, so it's authoritative.

    related_bills is a list of bill_id strings like "HR1842" - our own
    format, matching what fetch_bills.py uses, so build_signals.py can
    cross-reference them against tracked bills directly by string
    equality.

    The list endpoint's committee-meeting entries often omit the
    committees sub-object entirely, and never include relatedItems or
    a reliable date - all three only show up in the detail response.
    This is the one place that pays for that with an extra request per
    meeting; capped in main() so a busy week doesn't run away with API
    calls."""
    if not meeting_url:
        return None, [], None
    try:
        resp = requests.get(meeting_url, params={"api_key": CONGRESS_API_KEY, "format": "json"}, timeout=15)
        resp.raise_for_status()
        detail = resp.json().get("committeeMeeting", {})
    except requests.RequestException:
        return None, [], None

    committees = detail.get("committees", [])
    committee_name = committees[0].get("name") if committees else None

    related_bills = []
    for b in (detail.get("relatedItems", {}) or {}).get("bills", []) or []:
        bill_type = (b.get("type") or "").upper()
        bill_number = b.get("number")
        if bill_type and bill_number:
            related_bills.append(f"{bill_type}{bill_number}")

    real_date = detail.get("date")

    return committee_name, related_bills, real_date


def main():
    today = datetime.datetime.now(datetime.timezone.utc).date()
    cutoff = (today - datetime.timedelta(days=LOOKBACK_DAYS)).isoformat()

    all_meetings = []
    for chamber in ("house", "senate"):
        try:
            all_meetings.extend(fetch_meetings_for_chamber(chamber, cutoff))
        except requests.HTTPError as e:
            print(f"WARNING: committee-meeting fetch failed for {chamber} ({e}); skipping this chamber this run.")

    # Dedup by the meeting's own detail URL, which is unique per
    # meeting/event id. Without this, the same meeting can appear
    # more than once in all_meetings (e.g. Congress.gov's list
    # pagination isn't guaranteed stable, or a meeting shows up under
    # slightly different sort positions across paged requests), and
    # each copy would separately count toward "committee activity" -
    # inflating a signal's evidence with what's really one meeting.
    seen_urls = set()
    deduped_meetings = []
    for m in all_meetings:
        url = m.get("url")
        if url and url in seen_urls:
            continue
        if url:
            seen_urls.add(url)
        deduped_meetings.append(m)

    industry_counts = {industry: 0 for industry in INDUSTRY_TAXONOMY}
    meetings_by_industry = {industry: [] for industry in INDUSTRY_TAXONOMY}
    unmapped_count = 0
    stale_date_dropped = 0
    detail_lookups = 0
    MAX_DETAIL_LOOKUPS = 80  # keep a busy week's worth of meetings from running away with API calls

    for m in deduped_meetings:
        committee_name = (m.get("committees") or [{}])[0].get("name", "")
        related_bills = []
        real_date = None
        if detail_lookups < MAX_DETAIL_LOOKUPS:
            detail_name, related_bills, real_date = fetch_meeting_detail(m.get("url"))
            detail_lookups += 1
            if not committee_name and detail_name:
                committee_name = detail_name

        # Prefer the verified detail-endpoint date. Only fall back to
        # the list endpoint's own (non-updateDate) date field if detail
        # wasn't fetched for this one (past the cap). Deliberately does
        # NOT fall back to updateDate - that's when the record was
        # last edited, not when the meeting happened, and using it as
        # a stand-in silently misclassifies old meetings as recent
        # whenever their listing gets touched (a witness statement or
        # video posted well after the fact, for example).
        meeting_date = real_date or m.get("date")
        if not meeting_date or meeting_date < cutoff:
            stale_date_dropped += 1
            continue

        industry = committee_to_industry(committee_name)
        entry = {
            "committee": committee_name or "Unknown committee",
            "chamber": m.get("chamber"),
            "date": meeting_date,
            "title": m.get("title"),
            "related_bills": related_bills,
        }
        if industry:
            industry_counts[industry] += 1
            meetings_by_industry[industry].append(entry)
        else:
            industry_counts["Other / Cross-Sector"] += 1
            meetings_by_industry["Other / Cross-Sector"].append(entry)
            unmapped_count += 1

    output = {
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
        "lookback_days": LOOKBACK_DAYS,
        "total_meetings": sum(len(v) for v in meetings_by_industry.values()),
        "unmapped_meetings": unmapped_count,
        "industry_counts": industry_counts,
        "meetings_by_industry": meetings_by_industry,
    }
    os.makedirs("data", exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(output, f, separators=(",", ":"))

    print(f"Wrote {OUTPUT_PATH}: {sum(len(v) for v in meetings_by_industry.values())} meetings confirmed "
          f"within the last {LOOKBACK_DAYS} days (of {len(all_meetings)} fetched, "
          f"{len(all_meetings) - len(deduped_meetings)} deduped, "
          f"{stale_date_dropped} dropped for an unverifiable or stale date), "
          f"{unmapped_count} from committees not in COMMITTEE_INDUSTRY_MAP, "
          f"{detail_lookups} detail lookups performed"
          f"{' (capped)' if detail_lookups >= MAX_DETAIL_LOOKUPS else ''}.")


if __name__ == "__main__":
    main()
