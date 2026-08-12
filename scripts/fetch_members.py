"""
Pulls the full current-Congress member roster (all ~535 senators and
representatives, not just the ones who happen to sponsor/cosponsor a
bill you're already tracking) from Congress.gov and writes it to
data/members.json, keyed by bioguideId.

This is the source-of-truth roster for the lawmakers directory page.
fetch_bills.py's signals only know about members who show up as a
sponsor/cosponsor on a tracked bill -- this pulls everyone, so the
state dropdown can list a member even before they've sponsored
anything in your tracked bill set.

Membership barely changes between elections (occasional special
elections/appointments aside), so this doesn't need to run daily --
weekly is plenty. Runs via .github/workflows/update-members.yml on a
weekly cron, or manually after a special election.

Required environment variables (same as fetch_bills.py):
  CONGRESS_API_KEY
"""

import datetime
import json
import os

import requests

from fetch_bills import CONGRESS_API_KEY, CONGRESS_BASE, CURRENT_CONGRESS

MEMBERS_JSON_PATH = "data/members.json"
LISTING_PAGE_SIZE = 250  # Congress.gov's max page size for this endpoint

CHAMBER_LABELS = {
    "Senate": "Senate",
    "House of Representatives": "House",
}

PARTY_CODES = {
    "Democratic": "D",
    "Republican": "R",
    "Independent": "I",
    "Independent Democrat": "ID",
    "Libertarian": "L",
}

STATE_NAME_TO_CODE = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE",
    "Florida": "FL", "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID",
    "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
    "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD",
    "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS",
    "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV",
    "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY",
    "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK",
    "Oregon": "OR", "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC",
    "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX", "Utah": "UT",
    "Vermont": "VT", "Virginia": "VA", "Washington": "WA", "West Virginia": "WV",
    "Wisconsin": "WI", "Wyoming": "WY",
    "District of Columbia": "DC", "Puerto Rico": "PR", "Guam": "GU",
    "American Samoa": "AS", "United States Virgin Islands": "VI",
    "Northern Mariana Islands": "MP",
}


def _terms_list(terms_field):
    """Congress.gov's JSON responses don't always flatten XML container
    elements the same way across endpoints -- some come back as a plain
    list, others as {"item": [...]}. Handle both instead of assuming."""
    if isinstance(terms_field, list):
        return terms_field
    if isinstance(terms_field, dict):
        return terms_field.get("item", [])
    return []


def reformat_name(last_first_name):
    """Congress.gov's member list returns 'Last, First Middle' (e.g.
    'Leahy, Patrick J.'). Flip to 'First Middle Last' to match how
    sponsor/cosponsor names are already displayed elsewhere on the site."""
    if not last_first_name or "," not in last_first_name:
        return last_first_name or "Unknown"
    last, _, rest = last_first_name.partition(",")
    return f"{rest.strip()} {last.strip()}".strip()


def current_chamber(terms, congress):
    """terms covers a member's whole career across multiple Congresses --
    find the entry for the Congress we're pulling and report which chamber
    they're serving in right now. Falls back to the most recent term if an
    exact match isn't present."""
    for term in terms:
        if term.get("congress") == congress:
            chamber = term.get("chamber")
            return CHAMBER_LABELS.get(chamber, chamber or "Unknown")
    if terms:
        chamber = terms[-1].get("chamber")
        return CHAMBER_LABELS.get(chamber, chamber or "Unknown")
    return "Unknown"


def fetch_members_page(congress, offset, limit=LISTING_PAGE_SIZE):
    """One page of the current Congress's member roster. currentMember=true
    keeps this to active members only -- former members who've since left
    are out of scope for a 'who represents me right now' directory."""
    url = f"{CONGRESS_BASE}/member/congress/{congress}"
    params = {
        "api_key": CONGRESS_API_KEY,
        "format": "json",
        "currentMember": "true",
        "offset": offset,
        "limit": limit,
    }
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json().get("members", [])


def fetch_all_current_members(congress):
    """Pages through the full roster. ~535 members / 250-per-page is only
    2-3 requests total, so no retry/backoff machinery here -- worth adding
    if this ever needs to pull historical Congresses too."""
    members = {}
    offset = 0
    while True:
        page = fetch_members_page(congress, offset)
        if not page:
            break
        for m in page:
            bioguide_id = m.get("bioguideId")
            if not bioguide_id:
                continue
            state_name = m.get("state", "Unknown")
            party_name = m.get("partyName", "Unknown")
            members[bioguide_id] = {
                "bioguide_id": bioguide_id,
                "name": reformat_name(m.get("name")),
                "state": state_name,
                "state_code": STATE_NAME_TO_CODE.get(state_name, state_name),
                "party": party_name,
                "party_code": PARTY_CODES.get(party_name, "?"),
                "district": m.get("district"),
                "chamber": current_chamber(_terms_list(m.get("terms")), congress),
            }
        if len(page) < LISTING_PAGE_SIZE:
            break
        offset += LISTING_PAGE_SIZE
    return members


def main():
    print(f"Fetching current member roster for Congress {CURRENT_CONGRESS}...")
    members = fetch_all_current_members(CURRENT_CONGRESS)

    house = sum(1 for m in members.values() if m["chamber"] == "House")
    senate = sum(1 for m in members.values() if m["chamber"] == "Senate")
    unknown = len(members) - house - senate
    print(f"Fetched {len(members)} current members: {house} House, {senate} Senate"
          + (f", {unknown} unknown chamber (check parsing)" if unknown else "."))

    os.makedirs(os.path.dirname(MEMBERS_JSON_PATH), exist_ok=True)
    updated_at = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    with open(MEMBERS_JSON_PATH, "w") as f:
        json.dump({
            "updated_at": updated_at,
            "congress": CURRENT_CONGRESS,
            "members": members,
        }, f, separators=(",", ":"))
    print(f"Wrote {MEMBERS_JSON_PATH}.")


if __name__ == "__main__":
    main()
