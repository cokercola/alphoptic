"""
Backfill historical congressional stock trades from the two official
STOCK Act disclosure sources:

  - House Clerk financial disclosure system (disclosures-clerk.house.gov)
    Annual bulk index (XML) + per-filing PDFs. PDFs must be parsed with
    text extraction; asset descriptions are free text, not tickers.

  - Senate electronic financial disclosure system (efdsearch.senate.gov)
    Search API returns filing metadata; electronic PTRs filed since the
    STOCK Act (2012) are itemized HTML pages, which are far easier to
    parse reliably than the House PDFs.

Output: data/congress-trades-history.json
  Same core fields as the existing daily congress-trades.json
  (lawmaker, chamber, symbol, amount, direction, trade_date, filed_date)
  plus fields the Performance page needs:
    - amount_range_raw       the disclosed bracket as filed, e.g. "$1,001 - $15,000"
    - amount_estimated       midpoint of that bracket
    - asset_type             "equity", "bond", "structured_note", "private_placement",
                              or "fund" -- bonds/notes/private stakes stay in the raw
                              data but should be excluded from the Performance page's
                              return-vs-S&P comparison, which only makes sense for
                              equities
    - source                 "house" or "senate"
    - needs_review           true only for equities whose ticker could not be
                              resolved -- bonds/notes/private placements are expected
                              to have no ticker and are not review items
    - source_doc_id          filing identifier, for traceability back to the PDF/page

Unmatched names are also written to data/congress-trades-history-review.csv
so they can be triaged by hand. This is deliberately NOT fuzzy-matched --
see project notes: manual review first, fuzzy matching only if the
review backlog proves too large.

NOTE ON TESTING: this script was written without live network access to
disclosures-clerk.house.gov or efdsearch.senate.gov (outside this
sandbox's allowlist). The endpoint paths and HTML/XML structure below
are based on the publicly documented structure of both systems as of
early 2026, but real-world runs may need small adjustments to selectors
if either site has changed its markup. Run with --limit 20 first and
inspect data/congress-trades-history.json before a full run.
"""

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path

import requests

DATA_DIR = Path("data")
HISTORY_FILE = DATA_DIR / "congress-trades-history.json"
REVIEW_FILE = DATA_DIR / "congress-trades-history-review.csv"
TICKER_MAP_FILE = DATA_DIR / "ticker-name-map.json"
CHECKPOINT_HOUSE = DATA_DIR / ".backfill-checkpoint-house.json"
CHECKPOINT_SENATE = DATA_DIR / ".backfill-checkpoint-senate.json"

HOUSE_BASE = "https://disclosures-clerk.house.gov"
SENATE_SEARCH_URL = "https://efdsearch.senate.gov/search/report/data/"
SENATE_BASE = "https://efdsearch.senate.gov"

REQUEST_TIMEOUT = 30
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 5
POLITE_DELAY_SECONDS = 1.0  # between requests to either source, be a good citizen

STOCK_ACT_START_YEAR = 2012  # electronic disclosure begins here; earlier data is not reliably trade-level

session = requests.Session()
session.headers.update({
    "User-Agent": "AlphopticBackfill/1.0 (contact: your-contact-email-here)"
})


def load_json(path, default):
    if path.exists():
        with open(path, "r") as f:
            return json.load(f)
    return default


def save_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, indent=2, default=str)


def load_ticker_map():
    """
    data/ticker-name-map.json is a manually maintained lookup, e.g.:
      { "apple inc": "AAPL", "microsoft corporation": "MSFT", ... }
    Keys should be lowercased, punctuation-stripped company names.
    Start this file empty; it grows as you triage review rows.
    """
    return load_json(TICKER_MAP_FILE, {})


def normalize_name(raw_name):
    if not raw_name:
        return ""
    n = raw_name.lower()
    n = re.sub(r"[^\w\s]", "", n)
    n = re.sub(r"\b(inc|corp|corporation|co|ltd|llc|plc|common stock|class a|class b)\b", "", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


def resolve_ticker(asset_name, ticker_map):
    key = normalize_name(asset_name)
    return ticker_map.get(key)


def request_with_retry(method, url, **kwargs):
    last_err = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            resp = session.request(method, url, timeout=REQUEST_TIMEOUT, **kwargs)
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            body_snippet = ""
            if getattr(e, "response", None) is not None:
                body_snippet = f" | response body: {e.response.text[:500]!r}"
            last_err = e
            print(f"  request failed (attempt {attempt}/{RETRY_ATTEMPTS}): {e}{body_snippet}", file=sys.stderr)
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    raise RuntimeError(f"giving up on {url}: {last_err}")


# ---------------------------------------------------------------------------
# House Clerk
# ---------------------------------------------------------------------------

def fetch_house_year_index(year, debug=False):
    """
    House Clerk publishes an annual ZIP with an XML index of every filer
    and filing for that year: /public_disc/financial-pdfs/{year}FD.zip
    The XML lists DocID, filer name, filing type (P = Periodic Transaction
    Report is what we want), and state/district.
    Returns a list of dicts: {doc_id, filer_name, filing_type, filing_date}
    """
    url = f"{HOUSE_BASE}/public_disc/financial-pdfs/{year}FD.zip"
    print(f"House: fetching index for {year}")
    resp = request_with_retry("GET", url)

    import io
    import zipfile
    import xml.etree.ElementTree as ET

    filings = []
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        xml_names = [n for n in zf.namelist() if n.lower().endswith(".xml")]
        if debug:
            print(f"  DEBUG: files in zip: {zf.namelist()}")
        if not xml_names:
            print(f"  no XML index found in {year}FD.zip", file=sys.stderr)
            return filings
        with zf.open(xml_names[0]) as f:
            raw = f.read()
            if debug:
                print(f"  DEBUG: raw XML first 3000 chars:\n{raw[:3000].decode('utf-8', errors='replace')}")
            root = ET.fromstring(raw)
            if debug:
                child_tags = {}
                for elem in root.iter():
                    child_tags[elem.tag] = child_tags.get(elem.tag, 0) + 1
                print(f"  DEBUG: root tag = {root.tag}")
                print(f"  DEBUG: all tag names found and counts: {child_tags}")
                first_records = list(root)[:3]
                for i, rec in enumerate(first_records):
                    print(f"  DEBUG: sample record {i} tag={rec.tag}, children={[c.tag for c in rec]}")
                    for c in rec:
                        print(f"    {c.tag} = {c.text!r}")
            for member in root.findall(".//Member"):
                filing_type = (member.findtext("FilingType") or "").strip()
                if filing_type != "P":  # P = Periodic Transaction Report
                    continue
                filings.append({
                    "doc_id": (member.findtext("DocID") or "").strip(),
                    "filer_name": f"{(member.findtext('First') or '').strip()} {(member.findtext('Last') or '').strip()}".strip(),
                    "filing_date": (member.findtext("FilingDate") or "").strip(),
                    "year": year,
                })
    print(f"  found {len(filings)} PTR filings in {year}")
    return filings


def fetch_house_pdf_text(doc_id, year):
    """
    Individual PTR PDFs live at a predictable path once you have the DocID.
    Uses pdftotext (poppler-utils) for extraction, matching the approach
    your existing scripts likely already use for other PDF-based sources.
    """
    url = f"{HOUSE_BASE}/public_disc/ptr-pdfs/{year}/{doc_id}.pdf"
    resp = request_with_retry("GET", url)

    import subprocess
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
        tmp.write(resp.content)
        tmp.flush()
        result = subprocess.run(
            ["pdftotext", "-layout", tmp.name, "-"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            print(f"  pdftotext failed for {doc_id}: {result.stderr}", file=sys.stderr)
            return ""
        return result.stdout


TRANSACTION_LINE_RE = re.compile(
    r'^\s*(?:(?P<owner>SP|DC|JT)\s+)?'
    r'(?P<asset>[^()]+?)\s*'
    r'\(\s*(?P<ticker>[A-Za-z.]{1,6})\s*\)\s+'
    r'(?P<type>[A-Za-z])\s+'
    r'(?P<trade_date>\d{1,2}/\d{1,2}/\d{4})\s+'
    r'(?P<notify_date>\d{1,2}/\d{1,2}/\d{4})\s+'
    r'\$(?P<low>[\d,]+)\s*-\s*\$(?P<high>[\d,]+)',
    re.IGNORECASE
)

TYPE_MAP = {"p": "buy", "s": "sell", "e": "exchange"}

# Asset type classification -- keeps bonds/notes/private placements in the
# raw data (useful for future features) while flagging them as non-equity
# so they can be excluded from the Performance page's return-vs-S&P
# comparison, which only makes sense for equities. Order matters: check
# the most specific/reliable pattern first.
BOND_PATTERN = re.compile(r"Rate/Coupon", re.IGNORECASE)
STRUCTURED_NOTE_PATTERN = re.compile(r"Structured Note|Autocallable|Contingent Yield|Linked Note", re.IGNORECASE)
PRIVATE_PATTERN = re.compile(r"\bLLC\b|\bL\.?P\.?\b|Limited Partnership|Company:", re.IGNORECASE)
FUND_PATTERN = re.compile(r"\bETF\b|Exchange-Traded|Index Fund|Money Market", re.IGNORECASE)


def classify_asset_type(asset_name, ticker):
    """
    Returns one of: bond, structured_note, private_placement, fund, equity.
    Defaults to "equity" when nothing else matches, since that's the
    overwhelming majority case for both House and Senate filings, and
    keeps needs_review meaningful (only equities without a resolved
    ticker are flagged for review; bonds/notes/private stakes are
    expected to have no ticker and aren't review items).
    """
    if BOND_PATTERN.search(asset_name):
        return "bond"
    if STRUCTURED_NOTE_PATTERN.search(asset_name):
        return "structured_note"
    if PRIVATE_PATTERN.search(asset_name):
        return "private_placement"
    if FUND_PATTERN.search(asset_name):
        return "fund"
    return "equity"


def parse_house_pdf_transactions(pdf_text, filer_name, doc_id, filing_date, ticker_map):
    """
    House PTR PDFs list one transaction per line once the header/labels are
    stripped away, in the order: [id] [owner] asset (ticker) type date
    notification_date $low - $high. Matched line by line rather than as one
    block regex, since the amount/notification fields are separated by
    variable whitespace that a whole-text regex would otherwise treat as
    crossing into the next line.

    The PDF text extraction renders some fields (owner code, ticker letters)
    with inconsistent case -- uppercasing on the way out fixes this, since
    real tickers and owner codes are always uppercase in the actual filing.
    """
    records = []
    for line in pdf_text.splitlines():
        match = TRANSACTION_LINE_RE.match(line)
        if not match:
            continue

        asset_name = match.group("asset").strip().rstrip(",")
        ticker = match.group("ticker").strip().upper()
        # a handful of PTR lines put a fund/plan abbreviation in parens
        # instead of a real ticker (e.g. "(TSP)" for Thrift Savings Plan) --
        # treat anything that isn't 1-5 letters as not a real ticker
        if not re.fullmatch(r"[A-Z]{1,5}", ticker):
            ticker = None

        direction = TYPE_MAP.get(match.group("type").lower(), "unknown")

        low = int(match.group("low").replace(",", ""))
        high = int(match.group("high").replace(",", ""))
        midpoint = (low + high) // 2

        try:
            trade_date = datetime.strptime(match.group("trade_date"), "%m/%d/%Y").date().isoformat()
        except ValueError:
            trade_date = None

        owner = (match.group("owner") or "self").upper()

        # fall back to the name map only if no ticker was found in parens
        if ticker is None:
            ticker = resolve_ticker(asset_name, ticker_map)

        asset_type = classify_asset_type(asset_name, ticker)

        records.append({
            "lawmaker": filer_name,
            "chamber": "House",
            "owner": owner,
            "symbol": ticker,
            "asset_name_raw": asset_name,
            "asset_type": asset_type,
            "amount_range_raw": f"${low:,} - ${high:,}",
            "amount_estimated": midpoint,
            "direction": direction,
            "trade_date": trade_date,
            "filed_date": filing_date,
            "source": "house",
            "source_doc_id": doc_id,
            "needs_review": asset_type == "equity" and ticker is None,
        })
    return records


# ---------------------------------------------------------------------------
# Senate PTR
# ---------------------------------------------------------------------------

def init_senate_session():
    """
    efdsearch.senate.gov requires accepting a terms-of-use agreement in
    session before the search API will respond -- hitting the search
    endpoint cold returns 403. This replicates the handshake a browser
    does automatically on first visit: load the search page to get a CSRF
    cookie, then POST acceptance of the agreement using that token.
    Returns the CSRF token to use on subsequent search requests.
    """
    print("Senate: establishing session (loading search page, accepting terms)")
    session.get(f"{SENATE_BASE}/search/", timeout=REQUEST_TIMEOUT)
    csrf_token = session.cookies.get("csrftoken")
    if not csrf_token:
        raise RuntimeError("Senate: no csrftoken cookie returned from /search/ -- site behavior may have changed")

    resp = request_with_retry(
        "POST",
        f"{SENATE_BASE}/search/home/",
        data={"csrfmiddlewaretoken": csrf_token, "prohibition_agreement": "1"},
        headers={"Referer": f"{SENATE_BASE}/search/"},
    )
    # the token sometimes rotates after the agreement POST; use whatever is current
    return session.cookies.get("csrftoken") or csrf_token


def fetch_senate_filings_page(start_date, end_date, offset, csrf_token, page_size=100):
    """
    Senate eFD search returns paginated JSON. Filing type 11 = Periodic
    Transaction Report in their internal type system as of early 2026 --
    verify this against a live response and adjust if their type codes
    have changed.

    This mirrors the exact form the site's own search page submits
    (a DataTables-backed endpoint), including fields that are empty by
    default but still expected to be present, and the X-Requested-With
    header that marks this as a legitimate AJAX call rather than a bare
    POST -- omitting either has been observed to produce a 503 rather
    than a clean error.
    """
    start_fmt = datetime.strptime(start_date, "%Y-%m-%d").strftime("%m/%d/%Y")
    end_fmt = datetime.strptime(end_date, "%Y-%m-%d").strftime("%m/%d/%Y")

    payload = {
        "start": str(offset),
        "length": str(page_size),
        "report_types": "[11]",
        "filer_types": "[]",
        "submitted_start_date": f"{start_fmt} 00:00:00",
        "submitted_end_date": f"{end_fmt} 00:00:00",
        "candidate_state": "",
        "senator_state": "",
        "office_id": "",
        "first_name": "",
        "last_name": "",
        "csrfmiddlewaretoken": csrf_token,
    }
    resp = request_with_retry(
        "POST",
        SENATE_SEARCH_URL,
        data=payload,
        headers={
            "Referer": f"{SENATE_BASE}/search/",
            "X-CSRFToken": csrf_token,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        },
    )
    return resp.json()


SUFFIX_TICKER_RE = re.compile(r"\(([A-Z]{1,5})\)\s*$")
PREFIX_TICKER_RE = re.compile(r"^([A-Z]{1,5})\s*-\s*\S")


def extract_ticker_from_text(asset_name):
    """
    The Senate table's dedicated ticker column is frequently '--' even for
    plain stock trades -- real data shows the ticker is often embedded in
    the asset description instead, in one of two styles:
      "ACN - Accenture plc Class A Ordinary Shares"   (prefix)
      "Recruit Holdings Co Ltd Unsponsored ADR (RCRUY)"  (suffix, same
        style House PTR filings use)
    Tries suffix first since it's less likely to false-positive on
    unrelated capitalized abbreviations at the start of a description.
    """
    suffix_match = SUFFIX_TICKER_RE.search(asset_name)
    if suffix_match:
        return suffix_match.group(1)
    prefix_match = PREFIX_TICKER_RE.match(asset_name)
    if prefix_match:
        return prefix_match.group(1)
    return None


def parse_senate_ptr_page(report_url, filer_name, filing_date, ticker_map):
    """
    Electronic Senate PTRs render as an HTML table with one transaction per
    row, in this column order (confirmed against a real filing):
      [0] row id, [1] transaction date, [2] owner, [3] ticker column
      (often "--" even for real stocks -- see extract_ticker_from_text),
      [4] asset description, [5] asset type, [6] transaction type
      (e.g. "Purchase", "Sale (Full)", "Sale (Partial)", "Exchange"),
      [7] amount range, [8] comment
    """
    resp = request_with_retry("GET", report_url)

    from bs4 import BeautifulSoup
    soup = BeautifulSoup(resp.text, "html.parser")
    records = []

    rows = soup.select("table tbody tr")
    for row in rows:
        cells = [c.get_text(strip=True) for c in row.find_all("td")]
        if len(cells) < 8:
            continue

        trade_date_raw = cells[1]
        owner = cells[2].upper() if cells[2] else "SELF"
        ticker_cell = cells[3].strip()
        asset_name = cells[4]
        transaction_type_raw = cells[6].lower()
        amount_raw = cells[7]

        ticker = None if ticker_cell in ("--", "") else ticker_cell.upper()
        if ticker is None:
            ticker = extract_ticker_from_text(asset_name)

        if "purchase" in transaction_type_raw:
            direction = "buy"
        elif "sale" in transaction_type_raw:
            direction = "sell"
        elif "exchange" in transaction_type_raw:
            direction = "exchange"
        else:
            direction = "unknown"

        trade_date = None
        date_match = re.search(r"\d{1,2}/\d{1,2}/\d{4}", trade_date_raw)
        if date_match:
            trade_date = datetime.strptime(date_match.group(), "%m/%d/%Y").date().isoformat()

        amount_match = re.search(r"\$([\d,]+)\s*-\s*\$([\d,]+)", amount_raw)
        amount_range_raw = None
        midpoint = None
        if amount_match:
            low = int(amount_match.group(1).replace(",", ""))
            high = int(amount_match.group(2).replace(",", ""))
            amount_range_raw = f"${low:,} - ${high:,}"
            midpoint = (low + high) // 2

        if ticker is None:
            ticker = resolve_ticker(asset_name, ticker_map)

        asset_type = classify_asset_type(asset_name, ticker)

        records.append({
            "lawmaker": filer_name,
            "chamber": "Senate",
            "owner": owner,
            "symbol": ticker,
            "asset_name_raw": asset_name,
            "asset_type": asset_type,
            "amount_range_raw": amount_range_raw,
            "amount_estimated": midpoint,
            "direction": direction,
            "trade_date": trade_date,
            "filed_date": filing_date,
            "source": "senate",
            "source_doc_id": report_url.rstrip("/").split("/")[-1],
            "needs_review": asset_type == "equity" and ticker is None,
        })
    return records


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run_house_backfill(start_year, end_year, ticker_map, limit=None, debug=False, dump_pdf_text=False):
    checkpoint = load_json(CHECKPOINT_HOUSE, {"last_completed_year": start_year - 1})
    all_records = []

    for year in range(max(start_year, checkpoint["last_completed_year"] + 1), end_year + 1):
        try:
            filings = fetch_house_year_index(year, debug=debug)
        except Exception as e:
            print(f"House: failed to fetch index for {year}, stopping here: {e}", file=sys.stderr)
            break

        if debug:
            print("  DEBUG: stopping after first year's index dump, no PDFs fetched")
            checkpoint["last_completed_year"] = year - 1  # do not advance checkpoint on a debug run
            return all_records

        if limit:
            filings = filings[:limit]

        if dump_pdf_text:
            if not filings:
                print(f"  DEBUG: no filings in {year}, moving to next year")
                continue
            filing = filings[0]
            print(f"  DEBUG: dumping raw pdftotext output for {filing['filer_name']} ({filing['doc_id']})")
            text = fetch_house_pdf_text(filing["doc_id"], year)
            print("  ----- BEGIN RAW PDF TEXT -----")
            print(text[:5000])
            print("  ----- END RAW PDF TEXT (first 5000 chars) -----")
            match_count = sum(1 for line in text.splitlines() if TRANSACTION_LINE_RE.match(line))
            print(f"  DEBUG: regex found {match_count} matching lines in this text")
            checkpoint["last_completed_year"] = year - 1  # do not advance checkpoint on a debug run
            return all_records

        for i, filing in enumerate(filings):
            print(f"  [{i + 1}/{len(filings)}] {filing['filer_name']} ({filing['doc_id']})")
            try:
                text = fetch_house_pdf_text(filing["doc_id"], year)
                records = parse_house_pdf_transactions(
                    text, filing["filer_name"], filing["doc_id"], filing["filing_date"], ticker_map
                )
                all_records.extend(records)
            except Exception as e:
                print(f"    skipping {filing['doc_id']}: {e}", file=sys.stderr)
            time.sleep(POLITE_DELAY_SECONDS)

        checkpoint["last_completed_year"] = year
        save_json(CHECKPOINT_HOUSE, checkpoint)

        if limit:
            break  # test mode: just do one partial year

    return all_records


def run_senate_backfill(start_date, end_date, ticker_map, limit=None, debug=False, dump_page=False):
    checkpoint = load_json(CHECKPOINT_SENATE, {"last_offset": 0})
    all_records = []
    offset = checkpoint["last_offset"]
    page_size = 100

    try:
        csrf_token = init_senate_session()
    except Exception as e:
        print(f"Senate: failed to establish session, stopping here: {e}", file=sys.stderr)
        return all_records

    while True:
        print(f"Senate: fetching filings at offset {offset}")
        try:
            page = fetch_senate_filings_page(start_date, end_date, offset, csrf_token, page_size)
        except Exception as e:
            print(f"Senate: failed at offset {offset}, stopping here: {e}", file=sys.stderr)
            break

        if debug:
            print(f"  DEBUG: raw response keys: {list(page.keys()) if isinstance(page, dict) else type(page)}")
            print(f"  DEBUG: raw response (first 3000 chars): {json.dumps(page, indent=2)[:3000]}")
            return all_records

        rows = page.get("data", [])
        if not rows:
            print("Senate: no more filings, backfill complete for this date range")
            break

        if limit:
            rows = rows[:limit]

        href_re = re.compile(r'href="([^"]+)"')
        paper_skipped = 0

        for row in rows:
            if len(row) < 5:
                continue
            first_name, last_name, display_name, link_html, filed_date = row[:5]
            filer_name = f"{first_name} {last_name}".strip()

            href_match = href_re.search(link_html)
            if not href_match:
                continue
            relative_url = href_match.group(1)

            if "/paper/" in relative_url:
                # scanned paper filing, not an electronic HTML report -- can't
                # be table-parsed the same way. Skipped for now; these are a
                # small minority. Revisit later if the skip count is significant.
                paper_skipped += 1
                continue

            report_url = SENATE_BASE + relative_url

            if dump_page:
                print(f"  DEBUG: dumping raw HTML for {filer_name} -> {report_url}")
                resp = request_with_retry("GET", report_url)
                print("  ----- BEGIN RAW HTML (first 5000 chars) -----")
                print(resp.text[:5000])
                print("  ----- END RAW HTML -----")
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(resp.text, "html.parser")
                tables = soup.find_all("table")
                print(f"  DEBUG: found {len(tables)} <table> elements on page")
                if tables:
                    rows_found = tables[0].select("tbody tr")
                    print(f"  DEBUG: first table has {len(rows_found)} tbody rows")
                    if rows_found:
                        cells = [c.get_text(strip=True) for c in rows_found[0].find_all("td")]
                        print(f"  DEBUG: first row cells: {cells}")
                return all_records

            try:
                records = parse_senate_ptr_page(report_url, filer_name, filed_date, ticker_map)
                all_records.extend(records)
            except Exception as e:
                print(f"    skipping {report_url}: {e}", file=sys.stderr)
            time.sleep(POLITE_DELAY_SECONDS)

        if paper_skipped:
            print(f"  skipped {paper_skipped} paper (non-electronic) filings this page")

        offset += page_size
        checkpoint["last_offset"] = offset
        save_json(CHECKPOINT_SENATE, checkpoint)

        if limit and offset >= limit:
            break

    return all_records


def write_review_csv(records):
    review_rows = [r for r in records if r["needs_review"]]
    REVIEW_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(REVIEW_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "asset_name_raw", "asset_type", "lawmaker", "chamber", "trade_date", "source", "source_doc_id"
        ])
        writer.writeheader()
        for r in review_rows:
            writer.writerow({k: r.get(k, "") for k in writer.fieldnames})

    type_counts = {}
    for r in records:
        t = r.get("asset_type", "unknown")
        type_counts[t] = type_counts.get(t, 0) + 1
    equity_count = type_counts.get("equity", 0)
    equity_review_pct = (len(review_rows) / equity_count * 100) if equity_count else 0

    print(f"\nAsset type breakdown: {dict(sorted(type_counts.items(), key=lambda kv: -kv[1]))}")
    print(f"{len(review_rows)} equities need manual ticker review out of {equity_count} total equities "
          f"({equity_review_pct:.1f}%) -> {REVIEW_FILE}")
    if len(review_rows) > 100:
        print("That's over 100 -- worth considering fuzzy matching for the bulk of these,")
        print("with a clear methodology note on the Performance chart if you do.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-year", type=int, default=STOCK_ACT_START_YEAR)
    parser.add_argument("--end-year", type=int, default=date.today().year)
    parser.add_argument("--limit", type=int, default=None,
                         help="cap filings per source, for a small test run")
    parser.add_argument("--source", choices=["house", "senate", "both"], default="both")
    parser.add_argument("--debug", action="store_true",
                         help="dump raw XML/HTML structure from the first fetch instead of guessing field names")
    parser.add_argument("--dump-pdf-text", action="store_true",
                         help="dump raw pdftotext output for the first House filing instead of parsing it")
    parser.add_argument("--dump-senate-page", action="store_true",
                         help="dump raw HTML for the first electronic Senate PTR page instead of parsing it")
    args = parser.parse_args()

    ticker_map = load_ticker_map()
    existing = load_json(HISTORY_FILE, {"records": []})
    new_records = []

    if args.source in ("house", "both"):
        new_records.extend(run_house_backfill(args.start_year, args.end_year, ticker_map, args.limit, args.debug, args.dump_pdf_text))

    if args.source in ("senate", "both"):
        start_date = f"{args.start_year}-01-01"
        end_date = f"{args.end_year}-12-31"
        new_records.extend(run_senate_backfill(start_date, end_date, ticker_map, args.limit, args.debug, args.dump_senate_page))

    combined = new_records + existing["records"]  # new records first, so fixes to parsing logic win on dedup
    seen = set()
    deduped = []
    for r in combined:
        key = (r["source"], r["source_doc_id"], r.get("asset_name_raw"), r.get("trade_date"))
        if key not in seen:
            seen.add(key)
            deduped.append(r)

    output = {
        "updated_at": datetime.utcnow().isoformat() + "Z",
        "record_count": len(deduped),
        "needs_review_count": sum(1 for r in deduped if r["needs_review"]),
        "records": deduped,
    }
    save_json(HISTORY_FILE, output)
    write_review_csv(deduped)

    print(f"\nDone. {len(new_records)} new records this run, {len(deduped)} total in history file.")


if __name__ == "__main__":
    main()
