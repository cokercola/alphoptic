"""One-time cleanup: upgrades community_category to "veterans" or
"consumer_protection" for existing bills whose title now matches, per the
title-based fix added to derive_community_category() in fetch_bills.py.

Why this is safe without re-fetching anything: veterans and
consumer_protection are checked purely from each bill's title (already
stored in bills.json), and are checked FIRST in derive_community_category
-- ahead of, and independent from, every policy-area-based check. So this
script only needs to look at each bill's title locally; every other
category (healthcare_access, housing, etc., which DO depend on the raw
policyArea string we never persisted) is left completely untouched.

This intentionally does NOT call derive_community_category() itself --
that function needs a policy_area argument we don't have for historical
bills. Instead it duplicates just the two title checks directly, so it's
obviously correct by inspection rather than relying on optional-argument
behavior to skip the parts we can't safely redo.

Safe to re-run: only ever moves a bill INTO veterans/consumer_protection
when its title newly qualifies, never out of it, and never touches a
bill that doesn't match either keyword.
"""

import json

from fetch_bills import BILLS_JSON_PATH, COMMUNITY_CATEGORY_LABELS
from backfill_bills_history import rebuild_and_save_bills_json


def main():
    with open(BILLS_JSON_PATH) as f:
        data = json.load(f)
    signals = data["signals"]

    changed = 0
    upgraded_to = {"veterans": 0, "consumer_protection": 0}

    for s in signals:
        title_lower = (s.get("title") or "").lower()
        if "veteran" in title_lower:
            new_category = "veterans"
        elif "consumer" in title_lower:
            new_category = "consumer_protection"
        else:
            continue  # leave every other bill exactly as it was

        if s.get("community_category") != new_category:
            s["community_category"] = new_category
            s["community_category_label"] = COMMUNITY_CATEGORY_LABELS[new_category]
            changed += 1
            upgraded_to[new_category] += 1

    print(f"Upgraded {changed} bills: "
          f"{upgraded_to['veterans']} to veterans, "
          f"{upgraded_to['consumer_protection']} to consumer_protection.")
    rebuild_and_save_bills_json(signals, new_signals_count=0)
    print("Saved.")


if __name__ == "__main__":
    main()
