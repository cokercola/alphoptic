"""
Coverage check: what fraction of tracked bills actually have a published
Congress.gov summary at all? This is what the HJRES73/HJRES91 pilot result
surfaced - when there's no summary text, the impact rubric has almost
nothing to work with and scores everything near the floor while saying so
in impact_rationale ("Score is speculative due to no available summary").
That's the right behavior for the model, but if a big chunk of bills have
no summary, they'll all collapse into indistinguishable low scores
regardless of their real importance - worth knowing before the full
14,504-bill reclassification.

This does NOT call Claude at all - just Congress.gov, so it only needs
CONGRESS_API_KEY, not ANTHROPIC_API_KEY.

Samples bills stratified by stage (proportional to how your tracked bills
are actually distributed across stages, with a floor so rare stages like
"passed_one_chamber" aren't skipped entirely) rather than pure random,
since a bill's summary is much more likely to exist once it's moved past
committee - a flat random sample would either overstate or understate
overall coverage depending on which stage dominates it.

Usage: python3 scripts/check_summary_coverage.py
"""

import os
import re
import json
import random
import time
import requests

CONGRESS_API_KEY = os.environ["CONGRESS_API_KEY"]
CONGRESS_BASE = "https://api.congress.gov/v3"
CONGRESS_NUM = 119

# How many bills to sample per stage. Deliberately over-samples the rare
# later-stage buckets relative to their true share, since those are few
# in absolute number and we still want a real read on their coverage
# rather than 1-2 data points.
SAMPLE_SIZE_PER_STAGE = {
    "committee": 25,
    "introduced": 15,
    "became_law": 10,
    "failed_vetoed": 10,
    "passed_one_chamber": 3,  # there are only 3 total right now - take all
}


def bill_type_and_number(bill_id):
    m = re.match(r"^([A-Za-z]+)(\d+)$", bill_id)
    return m.group(1).lower(), int(m.group(2))


def fetch_bill_summary(congress, bill_type, number):
    url = f"{CONGRESS_BASE}/bill/{congress}/{bill_type}/{number}/summaries"
    resp = requests.get(url, params={"api_key": CONGRESS_API_KEY, "format": "json"}, timeout=30)
    resp.raise_for_status()
    summaries = resp.json().get("summaries", [])
    return summaries[0]["text"] if summaries else ""


def build_sample():
    with open("data/bills-index.json") as f:
        bills = json.load(f)["bills"]

    by_stage = {}
    for b in bills:
        by_stage.setdefault(b.get("stage"), []).append(b)

    sample = []
    for stage, n in SAMPLE_SIZE_PER_STAGE.items():
        pool = by_stage.get(stage, [])
        picked = random.sample(pool, min(n, len(pool)))
        sample.extend((b["bill_id"], b["title"], stage) for b in picked)
    return sample


def main():
    sample = build_sample()
    print(f"Sampling {len(sample)} bills across {len(SAMPLE_SIZE_PER_STAGE)} stages\n")

    results = []
    for bill_id, title, stage in sample:
        bill_type, number = bill_type_and_number(bill_id)
        try:
            summary = fetch_bill_summary(CONGRESS_NUM, bill_type, number)
        except requests.HTTPError as e:
            print(f"WARNING: fetch failed for {bill_id}: {e}")
            summary = None  # distinct from "" (confirmed empty) - fetch itself failed

        has_summary = bool(summary)
        results.append({
            "bill_id": bill_id, "title": title, "stage": stage,
            "has_summary": has_summary,
            "summary_length": len(summary) if summary else 0,
        })
        status = "HAS summary" if has_summary else ("FETCH FAILED" if summary is None else "NO summary")
        print(f"{bill_id:<10} [{stage:<20}] {status}")
        time.sleep(0.15)  # polite pacing, nowhere near the 5000/hr limit

    print("\n=== COVERAGE BY STAGE ===")
    by_stage_results = {}
    for r in results:
        by_stage_results.setdefault(r["stage"], []).append(r)

    for stage, rs in by_stage_results.items():
        n = len(rs)
        has = sum(1 for r in rs if r["has_summary"])
        pct = 100 * has / n if n else 0
        print(f"{stage:<20} {has}/{n} have a summary ({pct:.0f}%)")

    total = len(results)
    total_has = sum(1 for r in results if r["has_summary"])
    print(f"\nOVERALL: {total_has}/{total} sampled bills have a summary ({100*total_has/total:.0f}%)")

    with open("coverage_results.json", "w") as f:
        json.dump(results, f, indent=2)
    print("\nFull results written to coverage_results.json")


if __name__ == "__main__":
    main()
