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
in the API response, so this maps by COMMITTEE NAME -> industry using
a small static lookup below, not by cross-referencing bill_id. That
lookup is necessarily incomplete (~200 committees and subcommittees
exist across both chambers) -- unmapped committees fall into "Other /
Cross-Sector" rather than being silently dropped, and the printed
summary reports how many meetings that affects so gaps are visible in
the workflow log.

Writes both aggregate counts AND the actual meeting list per industry
(committee name, chamber, date) -- signals/detail.html shows the real
meetings, not just a number, so "why we flagged this" is backed by
something a reader can actually verify.

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


def main():
    today = datetime.datetime.now(datetime.timezone.utc).date()
    cutoff = (today - datetime.timedelta(days=LOOKBACK_DAYS)).isoformat()

    all_meetings = []
    for chamber in ("house", "senate"):
        try:
            all_meetings.extend(fetch_meetings_for_chamber(chamber, cutoff))
        except requests.HTTPError as e:
            print(f"WARNING: committee-meeting fetch failed for {chamber} ({e}); skipping this chamber this run.")

    industry_counts = {industry: 0 for industry in INDUSTRY_TAXONOMY}
    meetings_by_industry = {industry: [] for industry in INDUSTRY_TAXONOMY}
    unmapped_count = 0

    for m in all_meetings:
        committee_name = (m.get("committees") or [{}])[0].get("name", "")
        industry = committee_to_industry(committee_name)
        meeting_date = m.get("date") or m.get("updateDate")
        entry = {
            "committee": committee_name or "Unknown committee",
            "chamber": m.get("chamber"),
            "date": meeting_date,
            "title": m.get("title"),
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
        "total_meetings": len(all_meetings),
        "unmapped_meetings": unmapped_count,
        "industry_counts": industry_counts,
        "meetings_by_industry": meetings_by_industry,
    }
    os.makedirs("data", exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(output, f, separators=(",", ":"))

    print(f"Wrote {OUTPUT_PATH}: {len(all_meetings)} meetings in the last {LOOKBACK_DAYS} days, "
          f"{unmapped_count} from committees not in COMMITTEE_INDUSTRY_MAP.")


if __name__ == "__main__":
    main()
