"""One-time cleanup: remaps every bill's (ticker, name) company pairs to
canonical values from the SEC registry, fixing cases like Twilio Inc.
being tagged with Trulieve's TCNNF ticker.

Same philosophy as normalize_industries.py: operate on the unique
(ticker, name) pairs, not all ~36,000 company rows across every bill,
so this stays cheap. Unlike normalize_industries.py, this needs NO
Claude calls at all -- company_registry.py's resolver is a local,
deterministic fuzzy match against the SEC file, not an LLM judgment
call. That makes this script both cheaper and faster to run than its
industry-label counterpart.

Matches against the company's NAME (ignoring whatever ticker is
currently stored, since that's exactly the unreliable field this
script exists to fix). A pair that doesn't confidently resolve is
DROPPED from that bill's company list rather than kept with an
unverified guess -- see the ticker cleanup plan's "Open decision"
section for the reasoning; flip DROP_UNRESOLVED to False below if
that tradeoff gets revisited.

Safe to re-run: fully re-derives the mapping from whatever pairs are
currently in bills.json and overwrites in place. Everything else on
each signal (industry, direction, impact_score, summary, etc.) is
left untouched.
"""

import json

from fetch_bills import BILLS_JSON_PATH
from backfill_bills_history import rebuild_and_save_bills_json
from company_registry import resolve_company

DROP_UNRESOLVED = True


def main():
    with open(BILLS_JSON_PATH) as f:
        data = json.load(f)
    signals = data["signals"]

    unique_pairs = set()
    malformed_bills = 0
    for s in signals:
        companies = s.get("companies")
        # Defensive: at least one signal (HRES790) had "companies"
        # stored as the raw string "[]" instead of an actual list -
        # iterating that yields individual characters ('[', ']')
        # rather than company dicts, and crashed here on `.get`.
        # Skip anything that isn't a real list rather than let one bad
        # record take the whole normalization run down.
        if not isinstance(companies, list):
            if companies:
                malformed_bills += 1
            continue
        for c in companies:
            if not isinstance(c, dict):
                continue
            unique_pairs.add((c.get("ticker"), c.get("name")))
    if malformed_bills:
        print(f"WARNING: skipped {malformed_bills} bill(s) with a malformed (non-list) "
              f"companies field - these will be normalized to an empty list below.")
    print(f"Found {len(unique_pairs)} unique (ticker, name) pairs across {len(signals)} bills.")

    lookup = {}          # (old_ticker, old_name) -> (new_ticker, new_name) | None
    unresolved = []
    for old_ticker, old_name in unique_pairs:
        match = resolve_company(old_name)
        lookup[(old_ticker, old_name)] = match
        if match is None:
            unresolved.append((old_ticker, old_name))

    print(f"Resolved {len(unique_pairs) - len(unresolved)} of {len(unique_pairs)} pairs confidently.")
    if unresolved:
        print(f"\n{len(unresolved)} pairs did NOT resolve confidently "
              f"({'will be dropped' if DROP_UNRESOLVED else 'will be kept as-is'}):")
        for old_ticker, old_name in sorted(unresolved, key=lambda p: p[1] or ""):
            print(f"  {old_ticker!r:>8}  {old_name!r}")

    changed_bills = 0
    dropped_entries = 0
    corrected_entries = 0
    for s in signals:
        companies = s.get("companies") or []
        if not isinstance(companies, list):
            # Malformed field (see above) - normalize it to an empty
            # list rather than skip, so this run actually fixes it.
            s["companies"] = []
            changed_bills += 1
            continue
        if not companies:
            continue
        new_companies = []
        bill_changed = False
        for c in companies:
            if not isinstance(c, dict):
                bill_changed = True
                continue
            key = (c.get("ticker"), c.get("name"))
            match = lookup.get(key)
            if match is None:
                if DROP_UNRESOLVED:
                    dropped_entries += 1
                    bill_changed = True
                    continue
                new_companies.append(c)
                continue
            new_ticker, new_name = match
            if (new_ticker, new_name) != key:
                corrected_entries += 1
                bill_changed = True
            new_companies.append({
                "ticker": new_ticker,
                "name": new_name,
                "effect": c.get("effect"),
                "exposure": c.get("exposure"),
            })
        if bill_changed:
            changed_bills += 1
        s["companies"] = new_companies

    print(f"\n{corrected_entries} entries corrected, {dropped_entries} entries dropped, "
          f"across {changed_bills} of {len(signals)} bills.")
    rebuild_and_save_bills_json(signals, new_signals_count=0)
    print("Saved.")


if __name__ == "__main__":
    main()
