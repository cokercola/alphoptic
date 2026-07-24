"""
Pulls recently-active bills from Congress.gov, asks Claude to classify
industry impact, and writes the result to data/bills.json for the
static dashboard to read.

Run twice daily via .github/workflows/update-bills.yml

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

client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env

# Bills to track this run. Start narrow (a hand-picked watchlist of bill
# numbers you care about) rather than "all active bills" - Congress.gov
# rate limits are generous but classifying every bill with an LLM daily
# gets expensive fast. Expand this list over time.
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


def main():
    signals = []
    for ref in WATCHED_BILLS:
        bill = fetch_bill(ref["congress"], ref["type"], ref["number"])
        summary_text = fetch_bill_summary(ref["congress"], ref["type"], ref["number"])
        title = bill.get("title", "")
        status = bill.get("latestAction", {}).get("text", "")

        classification = classify(title, status, summary_text)

        bill_id = f"{ref['type'].upper()}{ref['number']}"
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
            "bills_tracked": len(WATCHED_BILLS),
            "high_impact": sum(1 for s in signals if s["impact_score"] >= 70),
            "new_signals_today": len(signals),
            "industries_affected": len({s["industry"] for s in signals}),
        },
        "signals": signals,
    }

    os.makedirs("data", exist_ok=True)
    with open("data/bills.json", "w") as f:
        json.dump(output, f, indent=2)

    print(f"Wrote {len(signals)} signals to data/bills.json")


if __name__ == "__main__":
    main()
