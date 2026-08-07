"""
Pulls recently-active bills from Congress.gov, asks Claude to classify
industry impact, and writes the result to data/bills.json for the
static dashboard to read.

Bill coverage = your hand-picked WATCHED_BILLS (always included) PLUS
the AUTO_FETCH_LIMIT most recently-updated bills from Congress.gov
(auto-discovered every run) PLUS every bill tracked in a PREVIOUS run
(carried forward automatically). Coverage is CUMULATIVE - once a bill
is discovered, it stays tracked and keeps getting checked for status
changes, even after it's no longer among the most-recently-updated
bills. (Earlier versions of this script only kept whichever bills
happened to be in that run's auto-fetch window, so bills would quietly
disappear from tracking once they stopped being "recent" - fixed here.)

Note: since coverage only grows, the daily Congress.gov call volume
(free, generous limit) and the theoretical Claude re-classification
surface both grow slowly over time too, though caching keeps actual
Claude calls limited to bills whose status has changed. Worth revisiting
with a pruning rule later (e.g. stop tracking bills that became law or
failed months ago) if the tracked count grows large enough to matter.

Classification is CACHED: if a bill was already classified in a
previous run and its latest action hasn't changed since then, we reuse
the cached classification instead of calling Claude again. This keeps
cost roughly proportional to *new* bills and *changed* bills only, not
your total tracked bill count.

Each bill also stores its cosponsors by NAME (not just a count), fetched
from Congress.gov's separate cosponsors endpoint - needed to answer
questions like "what bills is Senator X cosponsoring" or "what bills
has Senator Y sponsored that aren't moving."

Run once daily via .github/workflows/update-bills.yml

Required environment variables (set as GitHub Actions secrets):
  CONGRESS_API_KEY   - from https://api.congress.gov/sign-up/
  ANTHROPIC_API_KEY  - from console.anthropic.com
"""

import os
import json
import datetime
import requests
import anthropic

import re

CONGRESS_API_KEY = os.environ["CONGRESS_API_KEY"]
CONGRESS_BASE = "https://api.congress.gov/v3"
BILLS_JSON_PATH = "data/bills.json"

# Bump this any time the CLASSIFY_PROMPT changes in a way that should
# force every bill to be re-classified (new field, reworded guidance,
# etc.) rather than reusing stale cached values. A cached record only
# gets reused if its own schema_version matches this one.
CLASSIFICATION_SCHEMA_VERSION = 3  # v3: community_category is now derived deterministically from Congress.gov's official policyArea field, not asked of Claude

client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env

# Reserved for bills you specifically want tracked even though
# auto-discovery structurally can't find them -- e.g. a genuinely
# dormant bill (no recent action means it never appears in the
# "recently updated" sort auto-fetch relies on). NOT for hand-picking
# bills because they make a good demo; that undermines the "everything
# here is real, unbiased tracked activity" premise the dashboard relies
# on. Empty at launch -- everything is discovered organically via
# fetch_recent_bill_refs() below.
WATCHED_BILLS = []

# How many additional, auto-discovered "recently updated" bills to pull
# each run, on top of WATCHED_BILLS above. Raise this over time as you
# confirm cost stays reasonable (check the Cost page in the Claude
# Console after a few runs).
AUTO_FETCH_LIMIT = 15

CURRENT_CONGRESS = 119


def fetch_recent_bill_refs(limit):
    """Pulls the `limit` most recently-updated bills across the current
    Congress, regardless of subject. Returns a list of
    {"congress", "type", "number"} refs in the same shape as
    WATCHED_BILLS entries."""
    url = f"{CONGRESS_BASE}/bill/{CURRENT_CONGRESS}"
    params = {
        "api_key": CONGRESS_API_KEY,
        "format": "json",
        "sort": "updateDate+desc",
        "limit": limit,
    }
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    refs = []
    for b in resp.json().get("bills", []):
        refs.append({
            "congress": b["congress"],
            "type": b["type"].lower(),
            "number": int(b["number"]),
        })
    return refs


def fetch_bill(congress, bill_type, number):
    url = f"{CONGRESS_BASE}/bill/{congress}/{bill_type}/{number}"
    params = {"api_key": CONGRESS_API_KEY, "format": "json"}
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()["bill"]


def fetch_bill_cosponsors(congress, bill_type, number):
    """Congress.gov's bill detail response only includes a cosponsor
    COUNT - the actual names live on this separate endpoint. Needed for
    'what bills is person X cosponsoring' style questions, which a
    count alone can't answer."""
    url = f"{CONGRESS_BASE}/bill/{congress}/{bill_type}/{number}/cosponsors"
    params = {"api_key": CONGRESS_API_KEY, "format": "json"}
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    return [c.get("fullName", "Unknown") for c in resp.json().get("cosponsors", [])]


def fetch_bill_summary(congress, bill_type, number):
    url = f"{CONGRESS_BASE}/bill/{congress}/{bill_type}/{number}/summaries"
    params = {"api_key": CONGRESS_API_KEY, "format": "json"}
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    summaries = resp.json().get("summaries", [])
    return summaries[0]["text"] if summaries else ""


# Fixed taxonomy for community impact categories. This MUST stay a
# closed list (not freely generated by Claude, unlike `industry`),
# because the Community page needs to show every category - including
# ones with zero bills right now - and that only works if the set of
# possible categories is known ahead of time rather than discovered
# from whatever labels happen to show up in the data.
COMMUNITY_CATEGORIES = [
    "healthcare_access",
    "housing",
    "wages_labor",
    "consumer_protection",
    "safety",
    "education",
    "civil_rights",
    "environment",
    "veterans",
    "immigration",
]

COMMUNITY_CATEGORY_LABELS = {
    "healthcare_access": "Healthcare Access",
    "housing": "Housing",
    "wages_labor": "Wages & Labor",
    "consumer_protection": "Consumer Protection",
    "safety": "Safety",
    "education": "Education",
    "civil_rights": "Civil Rights",
    "environment": "Environment",
    "veterans": "Veterans",
    "immigration": "Immigration",
    "none": "None",
}

CLASSIFY_PROMPT = """You are a legislative impact analyst. Given a bill's
title, status, and summary, respond with ONLY a JSON object (no markdown
fences, no preamble) matching this schema:

{{
  "industry": "primary industry affected",
  "direction": "positive" | "negative" | "mixed",
  "impact_score": integer 0-100,
  "confidence": integer 0-100,
  "summary": "one sentence, plain language, under 30 words",
  "companies": [
    {{"ticker": "XXX", "name": "Company Name", "effect": "positive"|"negative"|"mixed", "exposure": integer 0-100}}
  ]
}}

List at most 4 companies, only ones with real, explainable exposure.

Bill title: {title}
Status: {status}
Summary: {summary}
"""


def extract_json_object(text):
    """Finds and returns the first balanced {...} object in text, using
    bracket-depth counting rather than assuming the whole string is
    clean JSON. More robust than a plain json.loads() when Claude's
    response has stray text, an extra trailing comma, or anything else
    slightly off around the actual JSON object."""
    start = text.find("{")
    if start == -1:
        raise ValueError("No '{' found in response text")
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    raise ValueError("No balanced '}' found to close the JSON object")


FALLBACK_CLASSIFICATION = {
    "industry": "Unknown (classification failed)",
    "direction": "mixed",
    "impact_score": 0,
    "confidence": 0,
    "summary": "Classification failed for this bill this run - will retry next run.",
    "companies": [],
    "_classification_failed": True,  # never cache this - see main() loop
}


def classify(title, status, summary, bill_id="unknown"):
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        messages=[{
            "role": "user",
            "content": CLASSIFY_PROMPT.format(
                title=title, status=status, summary=summary or "No summary available."
            ),
        }],
    )
    text = message.content[0].text.strip()
    text = text.replace("```json", "").replace("```", "").strip()

    try:
        json_str = extract_json_object(text)
        return json.loads(json_str)
    except (ValueError, json.JSONDecodeError) as e:
        # Don't let one malformed response take down the entire run -
        # log enough to debug later, fall back to a placeholder for
        # this bill, and let every other bill still get processed.
        # Since this fallback doesn't count as a "real" classification,
        # next run's cache check will naturally retry it (the fallback
        # summary text differs from whatever a real classification
        # would produce, so status-based caching won't accidentally
        # treat this as settled).
        print(f"WARNING: failed to parse classification JSON for {bill_id}: {e}")
        print(f"  Raw response (first 300 chars): {text[:300]!r}")
        return dict(FALLBACK_CLASSIFICATION)


def bill_stage(latest_action_text):
    """Buckets a bill into one of five stages based on its latest
    action text. Order matters here - check more-advanced stages
    first, since a bill that "became law" will also match "Passed
    House" if checked in the wrong order (it passed both chambers on
    its way to becoming law)."""
    text = (latest_action_text or "").lower()

    if "vetoed" in text or "failed" in text or "rejected" in text:
        return "failed_vetoed"
    if "became public law" in text or "signed by president" in text:
        return "became_law"
    if "presented to president" in text or "enrolled" in text:
        return "passed_both"
    if "passed house" in text or "passed senate" in text or "passed/agreed to in senate" in text or "passed/agreed to in house" in text:
        return "passed_one_chamber"
    if "reported" in text or "committee" in text or "markup" in text or "ordered to be reported" in text:
        return "committee"
    return "introduced"


STAGE_LABELS = {
    "introduced": "Introduced",
    "committee": "Committee action",
    "passed_one_chamber": "Passed one chamber",
    "passed_both": "Passed both chambers",
    "became_law": "Became law",
    "failed_vetoed": "Failed / vetoed",
}


def fetch_total_bill_count():
    """A lightweight call (limit=1) purely to read the total bill count
    for the current Congress from the response's pagination info. This
    is the honest denominator for the 'Introduced' bucket - most bills
    never advance past introduced, so this total is a reasonable stand-in
    for 'how many are sitting at introduced' even though a small number
    of them have moved further."""
    url = f"{CONGRESS_BASE}/bill/{CURRENT_CONGRESS}"
    params = {"api_key": CONGRESS_API_KEY, "format": "json", "limit": 1}
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json().get("pagination", {}).get("count")


def derive_community_category(policy_area_name):
    """Maps Congress.gov's own official policyArea term (assigned by
    CRS, one of ~32 controlled terms, returned for free on every bill
    detail response) to our fixed community category taxonomy.

    This is DETERMINISTIC - not a Claude judgment call - since
    policyArea is authoritative government metadata, not a guess. This
    also sidesteps the earlier problem where asking Claude to infer
    community relevance came back overly conservative (everything
    landed on "none").

    The exact wording of all ~32 official policy area terms isn't
    memorized with full confidence here, so this uses substring
    matching on lowercased text rather than exact string equality -
    more forgiving of minor wording variance. Any policyArea that
    doesn't match anything below falls through to "none" and gets
    logged, so real unmapped values show up in the Action logs and the
    mapping can be refined from actual data over time.
    """
    name = (policy_area_name or "").lower()
    if not name:
        return "none"

    checks = [
        ("healthcare_access", ["health"]),
        ("housing", ["housing", "community development"]),
        ("wages_labor", ["labor", "employment"]),
        ("consumer_protection", ["consumer affairs"]),
        ("safety", ["crime and law enforcement", "law enforcement"]),
        ("education", ["education"]),
        ("civil_rights", ["civil rights", "civil liberties"]),
        ("environment", ["environmental protection", "public lands", "natural resources"]),
        ("veterans", ["veteran"]),
        ("immigration", ["immigration"]),
    ]
    for category, keywords in checks:
        if any(kw in name for kw in keywords):
            return category

    print(f"NOTE: policyArea '{policy_area_name}' didn't match any community "
          f"category mapping - defaulting to 'none'. Consider adding a keyword "
          f"for it in derive_community_category() if this looks like it should map somewhere.")
    return "none"


COMMUNITY_SCAN_BATCH = 60   # how many recently-updated bills to scan for policyArea when looking for new community-relevant bills
COMMUNITY_FETCH_LIMIT = 20  # stop once we've found this many NEW community-relevant bills not already tracked


def find_community_candidate_refs(existing_bill_ids, bill_cache):
    """Scans a batch of recently-updated bills (separate from the main
    WATCHED_BILLS/AUTO_FETCH_LIMIT pool, which is built around
    industry/stock relevance) purely to find bills with a
    community-relevant policyArea that aren't already being tracked.

    Populates `bill_cache` with every full bill object fetched during
    the scan, so the main loop in main() can reuse them instead of
    re-fetching the same bill from Congress.gov twice.

    Returns a dict of {bill_id: ref} for newly-found community bills,
    in the same shape as WATCHED_BILLS entries.
    """
    found = {}
    try:
        candidates = fetch_recent_bill_refs(COMMUNITY_SCAN_BATCH)
    except requests.HTTPError as e:
        print(f"WARNING: community candidate scan failed to fetch recent bills ({e}); skipping scan.")
        return found

    for ref in candidates:
        if len(found) >= COMMUNITY_FETCH_LIMIT:
            break
        bill_id = f"{ref['type'].upper()}{ref['number']}"
        if bill_id in existing_bill_ids or bill_id in found:
            continue
        try:
            bill = fetch_bill(ref["congress"], ref["type"], ref["number"])
        except requests.HTTPError:
            continue
        bill_cache[bill_id] = bill
        policy_area = bill.get("policyArea", {}).get("name")
        if derive_community_category(policy_area) != "none":
            found[bill_id] = ref

    return found


def passage_probability(bill, stage):
    # Derived from the SAME stage bucketing as bill_stage(), so the
    # displayed stage and the passage odds can never disagree with each
    # other. (Previously this had its own separate keyword-matching
    # logic that used a different phrase for "became law" than
    # bill_stage() does, so a bill like "Became Public Law No: 119-86"
    # would show the correct stage but the wrong odds - fixed by having
    # one source of truth instead of two.)
    stage_base = {
        "introduced": 15,
        "committee": 30,
        "passed_one_chamber": 65,
        "passed_both": 90,
        "became_law": 100,
        "failed_vetoed": 2,
    }
    if stage == "became_law":
        return 100
    if stage == "failed_vetoed":
        return 2

    cosponsors = bill.get("cosponsors", {}).get("count", 0)
    base = stage_base.get(stage, 25)
    bump = min(cosponsors // 5, 20)
    return min(base + bump, 97)


BILL_ID_RE = re.compile(r"^([A-Z]+)(\d+)$")


def ref_from_previous_signal(signal):
    """Reconstructs a fetchable {congress, type, number} ref from a
    previous run's signal record, so previously-tracked bills can stay
    in the pool going forward instead of dropping out once they're no
    longer 'recently updated'. Uses the signal's own stored `congress`
    field when present; falls back to CURRENT_CONGRESS for older
    records written before that field existed."""
    match = BILL_ID_RE.match(signal["bill_id"])
    if not match:
        return None
    bill_type, number = match.groups()
    return {
        "congress": signal.get("congress", CURRENT_CONGRESS),
        "type": bill_type.lower(),
        "number": int(number),
    }


def load_previous_signals():
    """Returns {bill_id: signal_dict} from the last run's output, or an
    empty dict if there's no previous file yet (first run ever)."""
    try:
        with open(BILLS_JSON_PATH) as f:
            previous = json.load(f)
    except FileNotFoundError:
        return {}
    return {s["bill_id"]: s for s in previous.get("signals", [])}


def main():
    previous_by_id = load_previous_signals()

    # Combine hand-picked + auto-discovered, de-duplicated by bill_id.
    all_refs = {}
    for ref in WATCHED_BILLS:
        bill_id = f"{ref['type'].upper()}{ref['number']}"
        all_refs[bill_id] = ref

    try:
        for ref in fetch_recent_bill_refs(AUTO_FETCH_LIMIT):
            bill_id = f"{ref['type'].upper()}{ref['number']}"
            all_refs.setdefault(bill_id, ref)
    except requests.HTTPError as e:
        print(f"WARNING: auto-fetch of recent bills failed ({e}); "
              f"continuing with WATCHED_BILLS only.")

    # Add back every bill tracked in a previous run, so coverage is
    # CUMULATIVE - once a bill is discovered (via WATCHED_BILLS,
    # auto-fetch, or the community scan), it stays tracked and keeps
    # getting checked for status changes going forward, rather than
    # silently disappearing from output the day it's no longer among
    # the most-recently-updated bills. Without this, a bill discovered
    # a month ago that hasn't moved since would vanish from bills.json
    # entirely - which breaks any AI feature or user expectation that
    # "once tracked, always queryable."
    carried_forward = 0
    for bill_id, prev_signal in previous_by_id.items():
        if bill_id in all_refs:
            continue
        ref = ref_from_previous_signal(prev_signal)
        if ref:
            all_refs[bill_id] = ref
            carried_forward += 1
    if carried_forward:
        print(f"Carried forward {carried_forward} previously-tracked bills not in this run's auto-fetch window.")

    # Separate discovery pass: scans a wider batch of recent bills
    # specifically looking for community-relevant ones (by official
    # policyArea) that aren't already in the industry/stock-focused
    # pool above. bill_cache holds full bill objects fetched during
    # the scan so we don't re-fetch them in the main loop below.
    bill_cache = {}
    community_refs = find_community_candidate_refs(set(all_refs.keys()), bill_cache)
    all_refs.update(community_refs)
    if community_refs:
        print(f"Found {len(community_refs)} new community-relevant bills via policyArea scan: "
              f"{', '.join(community_refs.keys())}")

    signals = []
    reused = 0
    classified = 0

    for bill_id, ref in all_refs.items():
        bill = bill_cache.get(bill_id) or fetch_bill(ref["congress"], ref["type"], ref["number"])
        title = bill.get("title", "")
        status = bill.get("latestAction", {}).get("text", "")
        status_date = bill.get("latestAction", {}).get("actionDate", "")

        try:
            cosponsor_names = fetch_bill_cosponsors(ref["congress"], ref["type"], ref["number"])
        except requests.HTTPError as e:
            print(f"WARNING: cosponsor fetch failed for {bill_id} ({e}); leaving list empty this run.")
            cosponsor_names = []

        cached = previous_by_id.get(bill_id)
        # Cache is only reused if the status is unchanged AND the
        # cached record's schema_version matches CLASSIFICATION_SCHEMA_VERSION
        # above - bump that constant any time CLASSIFY_PROMPT changes in
        # a way that should force fresh classification for everyone.
        if cached and cached.get("status") == status and cached.get("schema_version") == CLASSIFICATION_SCHEMA_VERSION:
            # Nothing has changed since last run - reuse the existing
            # classification instead of calling Claude again.
            classification = {
                "industry": cached["industry"],
                "direction": cached["direction"],
                "impact_score": cached["impact_score"],
                "confidence": cached["confidence"],
                "summary": cached["summary"],
                "companies": cached["companies"],
            }
            reused += 1
        else:
            summary_text = fetch_bill_summary(ref["congress"], ref["type"], ref["number"])
            classification = classify(title, status, summary_text, bill_id=bill_id)
            classified += 1

        stage = bill_stage(status)
        # Deterministic, not from Claude - derived fresh from this
        # bill's live policyArea every run, regardless of whether the
        # rest of the classification came from cache.
        community_category = derive_community_category(bill.get("policyArea", {}).get("name"))

        # If this run's classification failed and fell back to the
        # placeholder, don't stamp a valid schema_version - that keeps
        # next run's cache check from treating this as "already
        # classified," so it automatically retries instead of staying
        # broken forever.
        failed_this_run = classification.get("_classification_failed", False)
        record_schema_version = None if failed_this_run else CLASSIFICATION_SCHEMA_VERSION

        signals.append({
            "bill_id": bill_id,
            "congress": ref["congress"],
            "title": title,
            "industry": classification["industry"],
            "direction": classification["direction"],
            "passage_probability": passage_probability(bill, stage),
            "impact_score": classification["impact_score"],
            "confidence": classification["confidence"],
            "status": status,
            "stage": stage,
            "schema_version": record_schema_version,
            "community_category": community_category,
            "community_category_label": COMMUNITY_CATEGORY_LABELS.get(community_category, "None"),
            "sponsor": bill.get("sponsors", [{}])[0].get("fullName", "Unknown"),
            "cosponsors": bill.get("cosponsors", {}).get("count", 0),
            "cosponsor_names": cosponsor_names,
            "last_action": status,
            "last_action_date": status_date,
            "next_event": "TBD",
            "summary": classification["summary"],
            "companies": classification["companies"],
        })

    try:
        total_bills_this_congress = fetch_total_bill_count()
    except requests.HTTPError as e:
        print(f"WARNING: couldn't fetch total bill count ({e}); omitting from output.")
        total_bills_this_congress = None

    stage_counts = {stage: 0 for stage in STAGE_LABELS}
    for s in signals:
        stage_counts[s["stage"]] = stage_counts.get(s["stage"], 0) + 1

    community_counts = {cat: 0 for cat in COMMUNITY_CATEGORIES}
    for s in signals:
        cat = s["community_category"]
        if cat in community_counts:
            community_counts[cat] += 1

    output = {
        "updated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "summary": {
            "bills_tracked": len(signals),
            "high_impact": sum(1 for s in signals if s["impact_score"] >= 70),
            "new_signals_today": classified,
            "industries_affected": len({s["industry"] for s in signals}),
            "total_bills_this_congress": total_bills_this_congress,
            "stage_breakdown": [
                {
                    "stage": stage,
                    "label": STAGE_LABELS[stage],
                    "count": stage_counts[stage],
                    # Only "introduced" is a sample of a much larger
                    # population - every later stage is small enough
                    # that what we track IS the full picture.
                    "full_coverage": stage != "introduced",
                }
                for stage in STAGE_LABELS
            ],
            # Every category always appears here, even with count 0 -
            # the Community page needs the full fixed list so it can
            # show "no bills tracked yet" for categories that are
            # currently empty, rather than hiding them entirely.
            "community_categories": [
                {
                    "category": cat,
                    "label": COMMUNITY_CATEGORY_LABELS[cat],
                    "count": community_counts[cat],
                }
                for cat in COMMUNITY_CATEGORIES
            ],
        },
        "signals": signals,
    }

    os.makedirs("data", exist_ok=True)
    with open(BILLS_JSON_PATH, "w") as f:
        json.dump(output, f, indent=2)

    print(f"Wrote {len(signals)} signals to {BILLS_JSON_PATH} "
          f"({classified} newly classified, {reused} reused from cache)")


if __name__ == "__main__":
    main()
