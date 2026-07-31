"""
Pulls recently-active bills from Congress.gov, asks Claude to classify
industry impact, and writes the result to data/bills.json for the
static dashboard to read.

Bill coverage = your hand-picked WATCHED_BILLS (always included) PLUS
the AUTO_FETCH_LIMIT most recently-updated bills from Congress.gov
(auto-discovered every run). This lets coverage grow over time without
manually adding every bill number.

Classification is CACHED: if a bill was already classified in a
previous run and its latest action hasn't changed since then, we reuse
the cached classification instead of calling Claude again. This keeps
cost roughly proportional to *new* bills and *changed* bills only, not
your total tracked bill count.

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

CONGRESS_API_KEY = os.environ["CONGRESS_API_KEY"]
CONGRESS_BASE = "https://api.congress.gov/v3"
BILLS_JSON_PATH = "data/bills.json"

client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env

# Hand-picked bills you always want tracked, regardless of whether
# they're currently "recently updated." Keep adding to this list for
# anything specific you care about.
WATCHED_BILLS = [
    {"congress": 119, "type": "hr", "number": 1842},
    {"congress": 119, "type": "s", "number": 622},
    {"congress": 119, "type": "hr", "number": 3101},
    {"congress": 119, "type": "hr", "number": 6179},   # Clean Cloud Act - data centers/crypto mining energy use
    {"congress": 119, "type": "hr", "number": 6983},   # PRICE Act - data center electricity generation requirements
    {"congress": 119, "type": "hr", "number": 2152},   # AI PLAN Act - AI-enabled financial crime/fraud strategy
    {"congress": 119, "type": "hr", "number": 7147},   # DHS Appropriations Act, 2026
    {"congress": 119, "type": "hr", "number": 7006},   # Financial Services & General Government Appropriations Act, 2026
    {"congress": 119, "type": "hr", "number": 9040},   # Regulate the Price of All Drugs Act
    {"congress": 119, "type": "hr", "number": 9393},   # Lower Costs, More Transparency Act of 2026
]

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


def fetch_bill_summary(congress, bill_type, number):
    url = f"{CONGRESS_BASE}/bill/{congress}/{bill_type}/{number}/summaries"
    params = {"api_key": CONGRESS_API_KEY, "format": "json"}
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    summaries = resp.json().get("summaries", [])
    return summaries[0]["text"] if summaries else ""


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


def classify(title, status, summary):
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
    return json.loads(text)


def passage_probability(bill):
    # Placeholder heuristic - swap in a real model later.
    # For now: bump probability based on cosponsor count and latest action stage.
    cosponsors = bill.get("cosponsors", {}).get("count", 0)
    stage_scores = {
        "Introduced": 15,
        "Reported": 40,
        "Passed House": 65,
        "Passed Senate": 65,
        "Presented to President": 90,
        "Became Law": 100,
    }
    latest_action = bill.get("latestAction", {}).get("text", "")
    base = 25
    for stage, score in stage_scores.items():
        if stage.lower() in latest_action.lower():
            base = score
    bump = min(cosponsors // 5, 20)
    return min(base + bump, 97)


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

    signals = []
    reused = 0
    classified = 0

    for bill_id, ref in all_refs.items():
        bill = fetch_bill(ref["congress"], ref["type"], ref["number"])
        title = bill.get("title", "")
        status = bill.get("latestAction", {}).get("text", "")

        cached = previous_by_id.get(bill_id)
        if cached and cached.get("status") == status:
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
            classification = classify(title, status, summary_text)
            classified += 1

        signals.append({
            "bill_id": bill_id,
            "title": title,
            "industry": classification["industry"],
            "direction": classification["direction"],
            "passage_probability": passage_probability(bill),
            "impact_score": classification["impact_score"],
            "confidence": classification["confidence"],
            "status": status,
            "sponsor": bill.get("sponsors", [{}])[0].get("fullName", "Unknown"),
            "cosponsors": bill.get("cosponsors", {}).get("count", 0),
            "last_action": status,
            "next_event": "TBD",
            "summary": classification["summary"],
            "companies": classification["companies"],
        })

    output = {
        "updated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "summary": {
            "bills_tracked": len(signals),
            "high_impact": sum(1 for s in signals if s["impact_score"] >= 70),
            "new_signals_today": len(signals),
            "industries_affected": len({s["industry"] for s in signals}),
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
