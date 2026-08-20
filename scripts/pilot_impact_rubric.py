"""
Pilot test for the new weighted impact rubric - run this BEFORE touching
fetch_bills.py for real. It classifies a deliberately spread set of 24
already-tracked bills (from data/bills-index.json) against the new
7-factor scoring prompt, prints the breakdown for each, and compares the
new impact_score to what the bill currently has under the old flat 0-100
scoring.

The goal is NOT to check whether the count of "high impact" bills went up
or down against the old 384 - it's to eyeball whether the new scores
actually separate symbolic/routine bills from genuinely structural ones
the way a person would expect. See the "expected" column below for what
each bill is testing.

Requires the same env vars as fetch_bills.py:
  ANTHROPIC_API_KEY  - from console.anthropic.com
  CONGRESS_API_KEY   - from api.congress.gov/sign-up/

Usage:
  python3 scripts/pilot_impact_rubric.py
"""

import os
import re
import json
import requests
import anthropic

CONGRESS_API_KEY = os.environ["CONGRESS_API_KEY"]
CONGRESS_BASE = "https://api.congress.gov/v3"
CONGRESS_NUM = 119  # 2025-2027 session - all pilot bills are from this Congress

client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env

# Copied verbatim from fetch_bills.py - MUST match the real taxonomy or
# results here won't be comparable to production.
INDUSTRY_TAXONOMY = [
    "Healthcare & Pharmaceuticals", "Education", "Defense & Aerospace",
    "Agriculture & Food", "Government & Public Administration",
    "Financial Services & Banking", "Insurance",
    "Telecommunications & Media", "Energy & Utilities",
    "Transportation & Infrastructure", "Real Estate & Housing",
    "Immigration & Legal Services", "Technology",
    "Environmental Protection & Natural Resources",
    "Manufacturing & Industrial", "Retail & Consumer Goods",
    "Tourism, Sports & Entertainment", "Labor & Employment",
    "Criminal Justice & Law Enforcement", "Veterans Affairs",
    "International Affairs & Trade", "Nonprofit & Philanthropy",
    "Social Services & Welfare", "Civil Rights & Social Justice",
    "Science & Research", "Other / Cross-Sector",
]

# (bill_id, old_score, what this bill is stress-testing)
PILOT_BILLS = [
    ("HR6",     0,  "procedural placeholder - should stay near 0"),
    ("SRES2",   1,  "procedural placeholder - should stay near 0"),
    ("HR325",   2,  "symbolic naming - should stay 0-15 despite an official-sounding title"),
    ("SRES542", 5,  "symbolic/commemorative - should stay 0-15"),
    ("HR2258",  12, "symbolic naming - should stay 0-15"),
    ("HR287",   38, "STRESS TEST: narrow operational fix, old score inflated to 38 from title alone - should drop"),
    ("S959",    35, "disclosure/reporting bill, not an actual tariff change - should stay modest on Economic"),
    ("HR5744",  45, "real but industry-narrow - should land Moderate"),
    ("S2943",   55, "real program impact, bounded population - Moderate"),
    ("S1166",   42, "contained, one sector - Moderate"),
    ("HR4479",  52, "regulatory change within one existing agency framework - Moderate"),
    ("HJRES73", 45, "STRESS TEST: national emergency declaration - may be UNDER-scored if it underlies a tariff action"),
    ("HJRES91", 50, "STRESS TEST: same as above, different date"),
    ("HR2802",  55, "real trade/fiscal mechanism - does this outscore old top-tier bills once fiscal is weighted separately?"),
    ("HR1846",  85, "Fed Board abolition - should stay at/near the top (sweeping regulatory change)"),
    ("S869",    85, "Fed Board abolition (Senate companion) - same bill, should score identically to HR1846"),
    ("S1506",   92, "Medicare for All - massive reach+fiscal, should this outrank narrower 'major' bills now?"),
    ("HR3069",  92, "Medicare for All (House companion) - should match S1506"),
    ("HR2032",  85, "STRESS TEST: BITCOIN Act currently scored same as Fed Abolition - does it still belong there?"),
    ("S954",    85, "BITCOIN Act (Senate companion)"),
    ("HJRES93", 85, "WTO withdrawal - broad economy-wide reach, should stay high"),
    ("HR310",   85, "Energy Market Freedom Act - one sector but deep - tests whether narrow+deep can still reach Major"),
    ("HR969",   62, "Taliban rare earth sanctions - narrow but potentially concentrated"),
    ("S3229",   62, "No Tariffs on Groceries Act - real economic mechanism, one sector"),
    ("HRES849", 62, "Ban Crypto Corruption Resolution - resolution, not binding law - should this score lower than actual bills?"),
    ("HR6286",  62, "Indo-Pacific tariff repeal - trade policy, moderate reach"),
]

# Title/status pulled from data/bills-index.json so this script doesn't
# need to re-fetch those (only the summary text, which isn't cached).
BILL_META = {
    "HR6": ("Reserved for the Speaker.", ""),
    "SRES2": ("A resolution informing the House of Representatives that a quorum of the Senate is assembled.", "Message on Senate action sent to the House."),
    "HR325": ("To designate a peak in the State of Nevada as Maude Frazier Mountain, and for other purposes.", "Referred to the House Committee on Natural Resources."),
    "SRES542": ("A resolution commemorating the 50th anniversary of Southeast Asian refugee resettlement and the many contributions and sacrifices of Southeast Asian Americans to the United States.", "Referred to the Committee on the Judiciary. (text: CR S8676)"),
    "HR2258": ("To designate the Maine Forest and Logging Museum, located in Bradley, Maine, as the National Museum of Forestry and Logging History.", "Referred to the Subcommittee on Forestry and Horticulture."),
    "HR287": ("Mobile Post Office Relief Act", "Referred to the House Committee on Oversight and Government Reform."),
    "S959": ("Tariff Transparency Act of 2025", "Read twice and referred to the Committee on Finance."),
    "HR5744": ("Targeting Online Sales of Fentanyl Act", "Referred to the Committee on Energy and Commerce."),
    "S2943": ("ACE Veterans Act", "Read twice and referred to the Committee on Veterans' Affairs."),
    "S1166": ("Excess Urban Heat Mitigation Act of 2025", "Read twice and referred to the Committee on Banking, Housing, and Urban Affairs."),
    "HR4479": ("To amend the National Housing Act to direct the Secretary of Housing and Urban Development to establish a program to insure certain second liens secured against property for the purpose of financing the construction of an accessory dwelling unit, and for other purposes.", "Referred to the House Committee on Financial Services."),
    "HJRES73": ("Relating to a national emergency by the President on February 1, 2025.", "Referred to the House Committee on Foreign Affairs."),
    "HJRES91": ("Relating to a national emergency by the President on April 2, 2025.", "Sponsor introductory remarks on measure. (CR H1529)"),
    "HR2802": ("Tax Relief from Tariffs and High Costs Act", "Referred to the House Committee on Ways and Means."),
    "HR1846": ("Federal Reserve Board Abolition Act", "Referred to the House Committee on Financial Services."),
    "S869": ("Federal Reserve Board Abolition Act", "Read twice and referred to the Committee on Banking, Housing, and Urban Affairs."),
    "S1506": ("Medicare for All Act", "Read twice and referred to the Committee on Finance."),
    "HR3069": ("Medicare for All Act", "Referred to the Committee on Energy and Commerce."),
    "HR2032": ("BITCOIN Act of 2025", "Referred to the House Committee on Financial Services."),
    "S954": ("BITCOIN Act of 2025", "Read twice and referred to the Committee on Banking, Housing, and Urban Affairs."),
    "HJRES93": ("Withdrawing approval of the Agreement Establishing the World Trade Organization.", "Placed on the Union Calendar, Calendar No. 125."),
    "HR310": ("Restoring Energy Market Freedom Act", "Referred to the House Committee on Ways and Means."),
    "HR969": ("Taliban Rare Earth Minerals Sanctions Act", "Referred to the Committee on Foreign Affairs."),
    "S3229": ("No Tariffs on Groceries Act of 2025", "Read twice and referred to the Committee on Finance."),
    "HRES849": ("Ban Crypto Corruption Resolution", "Referred to the Committee on Financial Services."),
    "HR6286": ("Indo-Pacific Partner and Ally Tariff Repeal Act", "Referred to the House Committee on Ways and Means."),
}

CLASSIFY_PROMPT = """You are a legislative impact analyst. Given a bill's
title, status, and summary, respond with ONLY a JSON object (no markdown
fences, no preamble) matching this schema:

{{
  "industry": "primary industry affected - MUST be exactly one string from
    this fixed list, copied verbatim, no variations: {industry_list}",
  "secondary_industries": "0-3 additional industries from the same list
    that are also meaningfully affected, excluding the primary industry.
    Empty array if nothing else is meaningfully affected - most bills
    should have 0 or 1, not 3.",
  "direction": "positive" | "negative" | "mixed",
  "confidence": integer 0-100,
  "summary": "one sentence, plain language, under 30 words",
  "impact_breakdown": {{
    "economic": integer 0-30,
    "reach": integer 0-20,
    "fiscal": integer 0-15,
    "regulatory": integer 0-15,
    "industry_exposure": integer 0-10,
    "state_local": integer 0-5,
    "duration": integer 0-5
  }},
  "impact_rationale": "one sentence explaining what's actually driving the
    score - name the specific factor(s) that pushed it up or kept it down",
  "companies": [
    {{"name": "Company Name", "effect": "positive"|"negative"|"mixed", "exposure": integer 0-100}}
  ]
}}

Score each of the 7 impact_breakdown factors independently against the
band definitions below. Do not anchor to what a "typical" bill might
score - most bills tracked here are narrow and procedural and should
score low on most factors. A bill that only does one modest thing (funds
a small program, tweaks a filing deadline) should score near 0 on most
factors even if its title sounds significant.

ECONOMIC & MARKET IMPACT (0-30) - effect on companies, industries,
employment, investment, prices, trade, markets:
  0-4   no measurable economic effect
  5-10  small/localized effect (single company or narrow niche)
  11-15 meaningful effect on a niche industry or several companies
  16-21 significant effect on a major industry or broad economic activity
  22-26 broad multi-industry/economy-wide implications
  27-30 major national economic/market restructuring

POPULATION & BUSINESS REACH (0-20) - how many people, businesses, states,
agencies, or sectors are affected:
  0-2   a single entity or tiny group
  3-6   narrow - one locality, a handful of companies
  7-10  moderate - one state, one full industry, or one federal agency's
        user base
  11-14 broad - multiple states, most of a sector, or several agencies
  15-17 very broad - a nationwide industry or large population segment
  18-20 mass reach - most of the U.S. population, economy, or businesses
        generally

FEDERAL FISCAL IMPACT (0-15) - spending, revenue, deficit, taxes, fees,
federal financial exposure. {cbo_context}
  0-2   no meaningful federal spending/revenue effect
  3-5   small appropriation, fee, or minor tax change
  6-8   moderate program funding or tax/fee change (tens of millions to
        low billions)
  9-11  substantial spending/revenue change (billions), real budget
        footprint
  12-13 major deficit/spending/tax impact (tens of billions+)
  14-15 transformative fiscal impact - hundreds of billions or a
        structural entitlement-scale change

REGULATORY & LEGAL CHANGE (0-15) - how substantially the bill changes
regulations, requirements, rights, or restrictions in existing law:
  0-2   no real change to existing law/regulation
  3-5   minor clarification or narrow technical amendment
  6-8   meaningful new requirement, restriction, or right within an
        existing framework
  9-11  substantial rewrite of the rules governing an activity or sector
  12-13 sweeping change to a major regulatory framework or body of law
  14-15 creates or abolishes an entire regulatory regime/agency, or
        fundamentally alters statutory/constitutional rights

INDUSTRY/COMPANY EXPOSURE CONCENTRATION (0-10) - whether particular
industries/companies see unusually large effects relative to the
economy as a whole:
  0-1   effects spread evenly, no concentration
  2-3   slightly concentrated in a handful of firms
  4-5   a recognizable subset of an industry bears most of the effect
  6-7   a few major players absorb most of the impact
  8-10  one or two companies, or a narrow segment, bear the overwhelming
        majority of the effect

STATE & LOCAL IMPACT (0-5) - costs, mandates, funding changes, or
responsibilities imposed on states/local governments:
  0     no state/local involvement
  1     minor reporting or coordination requirement
  2-3   real funding or mandate shift for states/localities
  4     substantial unfunded mandate or major funding change
  5     fundamental change to state/local responsibilities or the
        federal-state balance

DURATION/STRUCTURAL IMPACT (0-5) - temporary program vs. long-term or
permanent structural change:
  0     sunsets immediately / one-time only
  1     short-term (pilot program, temporary authorization)
  2-3   multi-year but not permanent
  4     long-term/quasi-permanent
  5     permanent structural change to law or policy

List at most 4 companies, only ones with real, explainable exposure.

Bill title: {title}
Status: {status}
Summary: {summary}
"""


def bill_type_and_number(bill_id):
    """'HJRES73' -> ('hjres', 73), 'HR6' -> ('hr', 6), 'S869' -> ('s', 869)"""
    m = re.match(r"^([A-Za-z]+)(\d+)$", bill_id)
    return m.group(1).lower(), int(m.group(2))


def fetch_bill_summary(congress, bill_type, number):
    url = f"{CONGRESS_BASE}/bill/{congress}/{bill_type}/{number}/summaries"
    resp = requests.get(url, params={"api_key": CONGRESS_API_KEY, "format": "json"}, timeout=30)
    resp.raise_for_status()
    summaries = resp.json().get("summaries", [])
    return summaries[0]["text"] if summaries else ""


def fetch_cbo_context(congress, bill_type, number):
    """Returns a prompt-ready sentence if a CBO cost estimate exists,
    otherwise an empty string. Mirrors the real fetch_bill_cbo_estimate()
    this pilot is validating the need for."""
    url = f"{CONGRESS_BASE}/bill/{congress}/{bill_type}/{number}"
    resp = requests.get(url, params={"api_key": CONGRESS_API_KEY, "format": "json"}, timeout=30)
    resp.raise_for_status()
    bill = resp.json().get("bill", {})
    cbo = bill.get("cboCostEstimates", [])
    if not cbo:
        return ""
    # cboCostEstimates is a list of {title, url, pubDate, description}
    desc = cbo[0].get("description", "")
    if not desc:
        return ""
    return f"CBO estimate: {desc} Weigh this directly in your fiscal score."


def classify(title, status, summary, cbo_context, bill_id):
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=700,
        messages=[{
            "role": "user",
            "content": CLASSIFY_PROMPT.format(
                title=title, status=status, summary=summary or "No summary available.",
                cbo_context=cbo_context,
                industry_list=", ".join(f'"{i}"' for i in INDUSTRY_TAXONOMY),
            ),
        }],
    )
    text = message.content[0].text.strip().replace("```json", "").replace("```", "").strip()
    start = text.find("{")
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError(f"couldn't parse response for {bill_id}: {text[:300]!r}")


def main():
    results = []
    for bill_id, old_score, note in PILOT_BILLS:
        title, status = BILL_META[bill_id]
        bill_type, number = bill_type_and_number(bill_id)

        try:
            summary = fetch_bill_summary(CONGRESS_NUM, bill_type, number)
        except requests.HTTPError as e:
            print(f"WARNING: summary fetch failed for {bill_id}: {e}")
            summary = ""

        try:
            cbo_context = fetch_cbo_context(CONGRESS_NUM, bill_type, number)
        except requests.HTTPError as e:
            print(f"WARNING: CBO fetch failed for {bill_id}: {e}")
            cbo_context = ""

        classification = classify(title, status, summary, cbo_context, bill_id)
        breakdown = classification["impact_breakdown"]
        new_score = sum(breakdown.values())
        relevance = "HIGH" if new_score >= 70 else ("MEDIUM" if new_score >= 40 else "LOW")

        results.append({
            "bill_id": bill_id, "title": title, "old_score": old_score,
            "new_score": new_score, "relevance": relevance,
            "breakdown": breakdown, "rationale": classification["impact_rationale"],
            "had_cbo": bool(cbo_context), "note": note,
        })

        print(f"\n{bill_id} - {title[:70]}")
        print(f"  old: {old_score}  ->  new: {new_score}  [{relevance}]"
              f"{'  (CBO estimate used)' if cbo_context else ''}")
        print(f"  breakdown: {breakdown}")
        print(f"  rationale: {classification['impact_rationale']}")
        print(f"  testing: {note}")

    results.sort(key=lambda r: -r["new_score"])
    print("\n\n=== SUMMARY (sorted by new score) ===")
    print(f"{'bill_id':<9} {'old':>4} {'new':>4}  {'relevance':<8} title")
    for r in results:
        print(f"{r['bill_id']:<9} {r['old_score']:>4} {r['new_score']:>4}  {r['relevance']:<8} {r['title'][:60]}")

    with open("pilot_results.json", "w") as f:
        json.dump(results, f, indent=2)
    print("\nFull results written to pilot_results.json")


if __name__ == "__main__":
    main()
