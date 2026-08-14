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

from company_registry import resolve_company

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
    COUNT - the actual names (and bioguideIds, for joining against the
    lawmakers directory) live on this separate endpoint. Needed for
    'what bills is person X cosponsoring' style questions, which a
    count alone can't answer.

    Returns a list of {"name", "bioguide_id"} dicts rather than plain
    strings, so callers can derive BOTH cosponsor_names (existing field)
    and cosponsor_ids (new) from one API call instead of two."""
    url = f"{CONGRESS_BASE}/bill/{congress}/{bill_type}/{number}/cosponsors"
    params = {"api_key": CONGRESS_API_KEY, "format": "json"}
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    return [
        {"name": c.get("fullName", "Unknown"), "bioguide_id": c.get("bioguideId")}
        for c in resp.json().get("cosponsors", [])
    ]


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

# Fixed taxonomy for the "industry" field. Same reasoning as
# COMMUNITY_CATEGORIES above - this MUST stay a closed list so the
# Companies and Industries pages can group bills reliably instead of
# splintering into thousands of one-off free-text variants (e.g.
# "Aerospace & Defense" vs "Defense & Aerospace" vs "Defense & National
# Security" all meaning the same thing).
INDUSTRY_TAXONOMY = [
    "Healthcare & Pharmaceuticals",
    "Education",
    "Defense & Aerospace",
    "Agriculture & Food",
    "Government & Public Administration",
    "Financial Services & Banking",
    "Insurance",
    "Telecommunications & Media",
    "Energy & Utilities",
    "Transportation & Infrastructure",
    "Real Estate & Housing",
    "Immigration & Legal Services",
    "Technology",
    "Environmental Protection & Natural Resources",
    "Manufacturing & Industrial",
    "Retail & Consumer Goods",
    "Tourism, Sports & Entertainment",
    "Labor & Employment",
    "Criminal Justice & Law Enforcement",
    "Veterans Affairs",
    "International Affairs & Trade",
    "Nonprofit & Philanthropy",
    "Social Services & Welfare",
    "Civil Rights & Social Justice",
    "Science & Research",
    "Other / Cross-Sector",
]

CLASSIFY_PROMPT = """You are a legislative impact analyst. Given a bill's
title, status, and summary, respond with ONLY a JSON object (no markdown
fences, no preamble) matching this schema:

{{
  "industry": "primary industry affected - MUST be exactly one string from
    this fixed list, copied verbatim, no variations: {industry_list}",
  "direction": "positive" | "negative" | "mixed",
  "impact_score": integer 0-100,
  "confidence": integer 0-100,
  "summary": "one sentence, plain language, under 30 words",
  "companies": [
    {{"name": "Company Name", "effect": "positive"|"negative"|"mixed", "exposure": integer 0-100}}
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


def resolve_companies(companies):
    """Runs each Claude-named company through the deterministic SEC
    resolver, replacing the free-recalled ticker with a canonical one.
    Companies that don't confidently resolve are dropped rather than
    kept with an unverified guess -- see the ticker cleanup plan's
    "Open decision" section; flip this to keep-with-a-flag instead if
    that tradeoff gets revisited."""
    resolved = []
    for c in companies or []:
        match = resolve_company(c.get("name"))
        if match is None:
            continue
        ticker, canonical_name = match
        resolved.append({
            "ticker": ticker,
            "name": canonical_name,
            "effect": c.get("effect"),
            "exposure": c.get("exposure"),
        })
    return resolved


def classify(title, status, summary, bill_id="unknown"):
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        messages=[{
            "role": "user",
            "content": CLASSIFY_PROMPT.format(
                title=title, status=status, summary=summary or "No summary available.",
                industry_list=", ".join(f'"{i}"' for i in INDUSTRY_TAXONOMY),
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


def is_on_calendar(latest_action_text):
    """A bill placed on the House Union Calendar or the Senate
    Legislative Calendar is queued for a floor vote -- Congress.gov
    doesn't expose a clean "scheduled floor votes" API, but this
    calendar-placement language reliably precedes one, and it's free:
    it comes from the same latest-action text already fetched for
    every bill, no new API calls. Used as the "upcoming votes" signal
    on the homepage's Alphoptic Signals panel."""
    text = (latest_action_text or "").lower()
    return "placed on the union calendar" in text or "placed on senate legislative calendar" in text or "placed on the calendar" in text


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


def derive_community_category(policy_area_name, title=None):
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

    Veterans and Consumer Protection are special cases: neither is an
    actual term in Congress.gov's controlled policyArea vocabulary.
    Veterans' issues are folded into the broad "Armed Forces and
    National Security" area; consumer affairs is folded into the
    broad "Commerce" area. policyArea alone can never distinguish
    either one (both were silently broken, stuck at 0 bills, until
    caught by inspection), so a title check is used instead - checked
    first, ahead of and independent from the policyArea checks below.
    """
    title_lower = (title or "").lower()
    if "veteran" in title_lower:
        return "veterans"
    if "consumer" in title_lower:
        return "consumer_protection"

    name = (policy_area_name or "").lower()
    if not name:
        return "none"

    checks = [
        ("healthcare_access", ["health"]),
        ("housing", ["housing", "community development"]),
        ("wages_labor", ["labor", "employment"]),
        ("safety", ["crime and law enforcement", "law enforcement"]),
        ("education", ["education"]),
        ("civil_rights", ["civil rights", "civil liberties"]),
        ("environment", ["environmental protection", "public lands", "natural resources"]),
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
        if derive_community_category(policy_area, bill.get("title")) != "none":
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


# Slim, page-specific exports so browser pages stop needing to fetch and
# parse the full bills.json (23MB+ and growing) just to render a list.
# Called from both the daily update (main(), below) and the backfill
# script's rebuild_and_save_bills_json(), so the two write paths can't
# drift out of sync with each other.
BILLS_INDEX_JSON_PATH = "data/bills-index.json"
COMPANIES_JSON_PATH = "data/companies.json"
BILLS_CHUNKS_DIR = "data/bills-chunks"
NUM_BILL_CHUNKS = 40


def bill_chunk_index(bill_id, num_chunks=NUM_BILL_CHUNKS):
    """Deterministic partition of a bill_id into one of num_chunks buckets.
    Must produce identical results here and in the matching JS function on
    the bill detail page - same simple char-code-sum-mod-N approach in
    both places, so the browser can compute which chunk file to fetch
    without needing a separate lookup index."""
    return sum(ord(c) for c in bill_id) % num_chunks


def write_bill_chunks(signals):
    """Partitions full bill detail into a fixed number of chunk files so
    the bill detail page can fetch ~1/40th of the archive instead of all
    23MB+ just to show one bill. Full signal dicts (every field) go in
    here, unlike the slim exports above - the detail page needs
    everything (summary, companies, cosponsor_names, etc.)."""
    updated_at = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    os.makedirs(BILLS_CHUNKS_DIR, exist_ok=True)

    chunks = [{} for _ in range(NUM_BILL_CHUNKS)]
    for s in signals:
        idx = bill_chunk_index(s["bill_id"])
        chunks[idx][s["bill_id"]] = s

    for i, chunk in enumerate(chunks):
        path = f"{BILLS_CHUNKS_DIR}/chunk-{i}.json"
        with open(path, "w") as f:
            json.dump({"updated_at": updated_at, "bills": chunk}, f, separators=(",", ":"))

    print(f"Wrote {NUM_BILL_CHUNKS} bill detail chunks to {BILLS_CHUNKS_DIR}/.")


BILLS_LOOKUP_JSON_PATH = "data/bills-lookup.json"


def write_bills_lookup(signals):
    """A much smaller companion to bills-index.json, carrying only what
    a page needs to resolve a bare bill_id into something displayable
    (title/stage/date) without loading every other field on every bill.
    First consumer: the lawmakers directory page, which only needs to
    label a list of bill_ids, not analyze them.

    Values are arrays [title, stage, last_action_date] rather than
    {"title": ..., "stage": ..., ...} objects -- skipping the repeated
    key names across 14,000+ bills is most of the size difference from
    bills-index.json. Small tradeoff: any future consumer has to know
    the array's field order (see the comment below) rather than reading
    self-describing keys."""
    updated_at = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    # Array order: [title, stage, last_action_date]
    lookup = {
        s.get("bill_id"): [s.get("title"), s.get("stage"), s.get("last_action_date")]
        for s in signals
    }
    with open(BILLS_LOOKUP_JSON_PATH, "w") as f:
        json.dump({"updated_at": updated_at, "bills": lookup}, f, separators=(",", ":"))
    print(f"Wrote {BILLS_LOOKUP_JSON_PATH} ({len(lookup)} bills).")


def write_slim_data_files(signals, summary=None):
    updated_at = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")

    index_rows = [
        {
            "bill_id": s.get("bill_id"),
            "title": s.get("title"),
            "industry": s.get("industry"),
            "direction": s.get("direction"),
            "stage": s.get("stage"),
            "status": s.get("status"),
            "impact_score": s.get("impact_score"),
            "passage_probability": s.get("passage_probability"),
            "sponsor": s.get("sponsor"),
            "community_category": s.get("community_category"),
            "last_action_date": s.get("last_action_date"),
        }
        for s in signals
    ]
    with open(BILLS_INDEX_JSON_PATH, "w") as f:
        json.dump(
            {"updated_at": updated_at, "summary": summary, "bills": index_rows},
            f, separators=(",", ":"),
        )

    company_rows = []
    for s in signals:
        for c in s.get("companies") or []:
            company_rows.append({
                "ticker": c.get("ticker"),
                "name": c.get("name"),
                "effect": c.get("effect"),
                "exposure": c.get("exposure"),
                "bill_id": s.get("bill_id"),
                "bill_title": s.get("title"),
                "industry": s.get("industry"),
            })
    with open(COMPANIES_JSON_PATH, "w") as f:
        json.dump({"updated_at": updated_at, "companies": company_rows}, f, separators=(",", ":"))

    print(f"Wrote {BILLS_INDEX_JSON_PATH} ({len(index_rows)} rows) and "
          f"{COMPANIES_JSON_PATH} ({len(company_rows)} rows).")


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
        try:
            bill = bill_cache.get(bill_id) or fetch_bill(ref["congress"], ref["type"], ref["number"])
        except requests.exceptions.RequestException as e:
            print(f"WARNING: bill fetch failed for {bill_id} ({e}); skipping this bill this run.")
            continue
        title = bill.get("title", "")
        status = bill.get("latestAction", {}).get("text", "")
        status_date = bill.get("latestAction", {}).get("actionDate", "")

        try:
            cosponsors = fetch_bill_cosponsors(ref["congress"], ref["type"], ref["number"])
        except requests.exceptions.RequestException as e:
            print(f"WARNING: cosponsor fetch failed for {bill_id} ({e}); leaving list empty this run.")
            cosponsors = []
        cosponsor_names = [c["name"] for c in cosponsors]
        cosponsor_ids = [c.get("bioguide_id") for c in cosponsors]

        cached = previous_by_id.get(bill_id)
        # Cache is only reused if the status is unchanged, the schema_version
        # matches CLASSIFICATION_SCHEMA_VERSION, AND the cached record actually
        # has every field we're about to read off it. That last check guards
        # against malformed records (e.g. from a backfill run that stamped a
        # valid schema_version onto an incomplete classification) - instead
        # of crashing the whole run, a bad record just falls through to
        # getting reclassified fresh below, like a normal cache miss.
        REQUIRED_CACHE_FIELDS = ("industry", "direction", "impact_score", "confidence", "summary", "companies")
        if (cached and cached.get("status") == status
                and cached.get("schema_version") == CLASSIFICATION_SCHEMA_VERSION
                and all(field in cached for field in REQUIRED_CACHE_FIELDS)):
            # Nothing has changed since last run - reuse the existing
            # classification instead of calling Claude again.
            classification = {field: cached[field] for field in REQUIRED_CACHE_FIELDS}
            reused += 1
        else:
            summary_text = fetch_bill_summary(ref["congress"], ref["type"], ref["number"])
            classification = classify(title, status, summary_text, bill_id=bill_id)
            classification["companies"] = resolve_companies(classification["companies"])
            classified += 1

        stage = bill_stage(status)
        # Deterministic, not from Claude - derived fresh from this
        # bill's live policyArea every run, regardless of whether the
        # rest of the classification came from cache.
        community_category = derive_community_category(bill.get("policyArea", {}).get("name"), title)

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
            "sponsor_bioguide_id": bill.get("sponsors", [{}])[0].get("bioguideId"),
            "cosponsors": bill.get("cosponsors", {}).get("count", 0),
            "cosponsor_names": cosponsor_names,
            "cosponsor_ids": cosponsor_ids,
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
    write_slim_data_files(signals, summary=output["summary"])
    write_bill_chunks(signals)
    write_bills_lookup(signals)

    print(f"Wrote {len(signals)} signals to {BILLS_JSON_PATH} "
          f"({classified} newly classified, {reused} reused from cache)")


if __name__ == "__main__":
    main()
