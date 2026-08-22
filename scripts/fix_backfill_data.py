"""One-time cleanup, combining two fixes into a single pass/commit so this
only needs to run once instead of twice:

1. Ticker resolution - backfill_bills_history.py used to skip
   resolve_companies() entirely, so ~10,800 stored companies have no
   ticker at all (renders as literal "undefined" on the dashboard
   Watchlist and bill detail pages). Same fix/logic as
   normalize_companies.py, folded in here.

2. Missing next_event - backfill_bills_history.py never set this field
   (fetch_bills.py's daily job always has, currently just a hardcoded
   "TBD" placeholder). ~10,200 backfilled bills are missing it outright,
   which renders as the literal text "undefined" next to "Next event" on
   that bill's detail page.

Both underlying code paths are already fixed going forward (see the
updated backfill_bills_history.py) - this script only cleans up bills
that were written before that fix landed. No Claude/Anthropic spend:
ticker resolution is a local, deterministic match against the SEC
registry file, not an LLM call.

Safe to re-run: only touches bills actually missing a ticker or missing
next_event; everything else on each signal is left untouched.
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

    # --- Pass 1: resolve every unique (ticker, name) pair once ---
    unique_pairs = set()
    malformed_bills = 0
    for s in signals:
        companies = s.get("companies")
        # Defensive: at least one signal (HRES790) had "companies"
        # stored as the raw string "[]" instead of an actual list -
        # iterating that yields characters ('[', ']') instead of
        # company dicts. Skip rather than let one bad record crash
        # the whole run.
        if not isinstance(companies, list):
            if companies:
                malformed_bills += 1
            continue
        for c in companies:
            if not isinstance(c, dict):
                continue
            unique_pairs.add((c.get("ticker"), c.get("name")))
    if malformed_bills:
        print(f"WARNING: found {malformed_bills} bill(s) with a malformed (non-list) "
              f"companies field - normalizing those to an empty list.")
    print(f"Found {len(unique_pairs)} unique (ticker, name) pairs across {len(signals)} bills.")

    lookup = {}
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

    # --- Pass 2: rewrite companies + stamp missing next_event, together ---
    changed_bills = 0
    dropped_entries = 0
    corrected_entries = 0
    next_event_fixed = 0

    for s in signals:
        bill_changed = False

        companies = s.get("companies") or []
        if not isinstance(companies, list):
            s["companies"] = []
            bill_changed = True
        elif companies:
            new_companies = []
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
            s["companies"] = new_companies

        if "next_event" not in s:
            s["next_event"] = "TBD"
            next_event_fixed += 1
            bill_changed = True

        if bill_changed:
            changed_bills += 1

    print(f"\n{corrected_entries} company entries corrected, {dropped_entries} dropped, "
          f"{next_event_fixed} next_event fields stamped - "
          f"{changed_bills} of {len(signals)} bills touched in total.")
    rebuild_and_save_bills_json(signals, new_signals_count=0)
    print("Saved.")


if __name__ == "__main__":
    main()
