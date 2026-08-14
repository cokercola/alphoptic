"""
Downloads the SEC's company_tickers.json (ticker/CIK/company-name
associations for ~10,000 SEC-registered companies) and commits it to
data/company_tickers_sec.json. This is the deterministic source of
truth that scripts/company_registry.py resolves company names against,
replacing Claude's unreliable free-recall of ticker symbols.

Free, no API key, ~778KB. SEC's only requirement is a descriptive
User-Agent header (see https://www.sec.gov/os/webmaster-faq#developers).

Run this:
  - once, to seed data/company_tickers_sec.json
  - periodically thereafter (see .github/workflows/update-sec-tickers.yml)
    to pick up new IPOs, ticker changes, and name changes -- the SEC
    updates the source file "periodically," not on a fixed schedule,
    so a monthly refresh is a reasonable cadence without being wasteful.

Safe to re-run: always overwrites data/company_tickers_sec.json with a
fresh full download rather than diffing/patching.
"""

import json
import requests

SEC_URL = "https://www.sec.gov/files/company_tickers.json"
OUTPUT_PATH = "data/company_tickers_sec.json"

# SEC blocks requests without a descriptive User-Agent identifying the
# requester -- see https://www.sec.gov/os/webmaster-faq#developers.
# Replace the email if Alphoptic ever needs a different contact on file.
HEADERS = {"User-Agent": "Alphoptic contact@alphoptic.com"}


def main():
    resp = requests.get(SEC_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    data = resp.json()  # {"0": {"cik_str": int, "ticker": "AAPL", "title": "Apple Inc."}, ...}

    # Re-key by ticker for O(1) exact-ticker lookups elsewhere, but keep
    # the original list form too since company_registry.py's fuzzy
    # matcher wants to iterate every (ticker, title) pair regardless.
    companies = list(data.values())

    with open(OUTPUT_PATH, "w") as f:
        json.dump({"companies": companies}, f, separators=(",", ":"))

    print(f"Wrote {OUTPUT_PATH} ({len(companies)} companies).")


if __name__ == "__main__":
    main()
