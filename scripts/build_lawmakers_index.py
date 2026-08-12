"""
Aggregates the full member roster (data/members.json, from
fetch_members.py) with the sponsor/cosponsor bioguideIds now recorded
on every tracked bill (data/bills.json, from fetch_bills.py) into two
files for the lawmakers directory page:

  data/lawmakers-index.json - slim, one row per member (name, state,
    party, chamber, sponsored/cosponsored counts) - populates the
    state dropdown and lawmaker list without loading every bill ID.

  data/lawmakers.json - full detail per member, keyed by bioguideId,
    including the actual sponsored/cosponsored bill_id lists. Small
    (roster is ~535 members, not tens of thousands of bills), so no
    chunking needed the way bills.json needed.

Depends on data/members.json already existing (run fetch_members.py
first / separately - membership doesn't change often, so it runs on
its own weekly schedule rather than every daily bill update). If
members.json is missing, this exits cleanly with a warning instead of
failing the workflow, since the two data sources update on different
schedules and shouldn't be able to break each other.

Run after fetch_bills.py, same workflow or a separate one:
  python scripts/build_lawmakers_index.py
"""

import datetime
import json
import os
import sys

from fetch_bills import load_previous_signals

MEMBERS_JSON_PATH = "data/members.json"
LAWMAKERS_JSON_PATH = "data/lawmakers.json"
LAWMAKERS_INDEX_JSON_PATH = "data/lawmakers-index.json"


def load_members():
    try:
        with open(MEMBERS_JSON_PATH) as f:
            return json.load(f).get("members", {})
    except FileNotFoundError:
        return None


def main():
    members = load_members()
    if members is None:
        print(f"WARNING: {MEMBERS_JSON_PATH} not found - run fetch_members.py first. "
              f"Skipping lawmakers index build this run.", file=sys.stderr)
        return

    signals_by_id = load_previous_signals()  # {bill_id: signal_dict}

    # Start every known member with empty bill lists, so members with no
    # sponsor/cosponsor activity yet in your tracked bill set still show
    # up in the directory (just with nothing under them).
    lawmakers = {
        bioguide_id: {**info, "sponsored": [], "cosponsored": []}
        for bioguide_id, info in members.items()
    }

    unmatched_sponsor_ids = set()
    unmatched_cosponsor_ids = set()

    for bill_id, signal in signals_by_id.items():
        sponsor_id = signal.get("sponsor_bioguide_id")
        if sponsor_id:
            if sponsor_id in lawmakers:
                lawmakers[sponsor_id]["sponsored"].append(bill_id)
            else:
                unmatched_sponsor_ids.add(sponsor_id)

        for cosponsor_id in signal.get("cosponsor_ids", []) or []:
            if cosponsor_id in lawmakers:
                lawmakers[cosponsor_id]["cosponsored"].append(bill_id)
            else:
                unmatched_cosponsor_ids.add(cosponsor_id)

    if unmatched_sponsor_ids or unmatched_cosponsor_ids:
        # Expected in small numbers -- e.g. a member who's left/retired
        # mid-Congress won't be in the "currentMember=true" roster
        # anymore, but bills they sponsored earlier still reference
        # their bioguideId. Not an error, just noted for visibility.
        print(f"NOTE: {len(unmatched_sponsor_ids)} sponsor id(s) and "
              f"{len(unmatched_cosponsor_ids)} cosponsor id(s) not found in the current "
              f"roster (likely former members) - their bill counts aren't reflected.")

    index_rows = [
        {
            "bioguide_id": bid,
            "name": info["name"],
            "state": info["state"],
            "state_code": info["state_code"],
            "party": info["party"],
            "party_code": info["party_code"],
            "chamber": info["chamber"],
            "district": info.get("district"),
            "sponsored_count": len(info["sponsored"]),
            "cosponsored_count": len(info["cosponsored"]),
        }
        for bid, info in lawmakers.items()
    ]
    index_rows.sort(key=lambda r: (r["state_code"] or "", r["name"]))

    updated_at = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")

    os.makedirs(os.path.dirname(LAWMAKERS_JSON_PATH) or ".", exist_ok=True)
    with open(LAWMAKERS_JSON_PATH, "w") as f:
        json.dump({"updated_at": updated_at, "lawmakers": lawmakers}, f, separators=(",", ":"))
    with open(LAWMAKERS_INDEX_JSON_PATH, "w") as f:
        json.dump({"updated_at": updated_at, "lawmakers": index_rows}, f, separators=(",", ":"))

    print(f"Wrote {LAWMAKERS_JSON_PATH} and {LAWMAKERS_INDEX_JSON_PATH} "
          f"({len(index_rows)} lawmakers, "
          f"{sum(len(l['sponsored']) for l in lawmakers.values())} sponsor links, "
          f"{sum(len(l['cosponsored']) for l in lawmakers.values())} cosponsor links).")


if __name__ == "__main__":
    main()
