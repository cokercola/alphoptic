"""
Resolves a free-text company name (as named by Claude during bill
classification) to a canonical (ticker, name) pair from the SEC's
registry, deterministically -- so Twilio always resolves to TWLO and
never to Trulieve's TCNNF, regardless of which bill or which run named
it.

Why this exists: fetch_bills.py's CLASSIFY_PROMPT asks Claude to name
which companies a bill affects and judge their exposure -- Claude is
good at that. It used to also ask Claude to recall the ticker symbol
from memory, independently, per bill, with nothing to check against.
That's the part this module replaces.

Used by:
  - fetch_bills.py, right after classify() returns, to resolve newly
    classified companies going forward.
  - normalize_companies.py, to fix already-stored (ticker, name) pairs
    in data/bills.json without re-calling Claude.

One shared implementation so the two call sites can't drift apart.
"""

import json
import re

from rapidfuzz import process, fuzz

SEC_TICKERS_PATH = "data/company_tickers_sec.json"

# Confidence floor for accepting a fuzzy match. Tuned by eyeballing a
# sample of real Alphoptic company names against the SEC registry --
# see the docx cleanup plan's "Biggest real risk" section. Revisit
# this if normalize_companies.py's "didn't confidently resolve" report
# turns up too many false negatives (real companies going unmatched)
# or normalize_companies.py output review turns up false positives
# (wrong company matched with high confidence).
CONFIDENCE_THRESHOLD = 90

# Manual overrides for cases fuzzy matching can't bridge on its own --
# mainly companies that renamed since Claude's training data was cut,
# so it names the old company but the SEC registry only carries the
# new name (e.g. 21Vianet Group -> VNET Group, Inc. in Oct 2021).
# Keyed by normalize()'d old/informal name -> current ticker. Grow this
# as normalize_companies.py's "did not resolve" report turns up a real
# miss -- NOT for genuinely private/delisted companies, which should
# stay dropped rather than get force-matched to something wrong.
ALIASES = {
    "21vianet group": "VNET",
}

# Stripped from both the query name and every SEC title before
# matching, so "Twilio Inc." and "Twilio" and "TWILIO INC" all
# normalize to the same string. Longest suffixes first so e.g.
# "holding co" is stripped before a lone trailing "co" rule could
# mangle it.
_SUFFIXES = [
    "holding company", "holdings inc", "holding co", "holdings",
    "incorporated", "corporation", "company", "limited",
    "co inc", "inc", "corp", "co", "ltd", "llc", "plc", "sa", "nv",
    "ag", "se", "lp",
]
_SUFFIX_RE = re.compile(
    r"\b(" + "|".join(re.escape(s) for s in sorted(_SUFFIXES, key=len, reverse=True)) + r")\b\.?\s*$"
)
_PUNCT_RE = re.compile(r"[^\w\s]")
_WHITESPACE_RE = re.compile(r"\s+")

# The SEC registry tags a large fraction of titles with a trailing
# incorporation-state or filing-status marker -- "AGCO CORP /DE",
# "US BANCORP \DE\", "COSTCO WHOLESALE CORP /NEW", "...CO., LTD./ADR".
# Without stripping these, an otherwise-perfect match like "AGCO
# Corporation" vs "AGCO CORP /DE" scores well below threshold on
# nothing but this noise. Handles both /XX and \XX\ delimiter styles,
# with or without a trailing slash/backslash.
_STATE_MARKER_RE = re.compile(r"[/\\]\s*[a-zA-Z]{2,4}[/\\]?\s*$")

# Parenthetical asides ("3M Company (MMM)", "1Life Healthcare (One
# Medical)") are disambiguating notes, not part of the legal name --
# strip the whole parenthetical rather than just the parens, since
# keeping the inner text (a ticker, a brand name, a parent company)
# tends to hurt the match more than help it.
_PARENS_RE = re.compile(r"\([^)]*\)")


def normalize(name):
    """Lowercase, strip parenthetical asides and SEC state/status
    markers, strip punctuation, strip a trailing corporate suffix
    (repeatedly, since e.g. "Foo Holdings, Inc." has two), collapse
    whitespace. Not perfect -- see module docstring's confidence-floor
    comment -- but consistent in both directions is what matters, since
    both the query and every registry entry go through this."""
    n = name.lower()
    n = _PARENS_RE.sub(" ", n)
    n = _STATE_MARKER_RE.sub("", n)
    n = _PUNCT_RE.sub(" ", n)
    n = _WHITESPACE_RE.sub(" ", n).strip()
    while True:
        stripped = _SUFFIX_RE.sub("", n).strip()
        if stripped == n:
            break
        n = stripped
    return n


class CompanyRegistry:
    def __init__(self, path=SEC_TICKERS_PATH):
        with open(path) as f:
            data = json.load(f)
        self._entries = data["companies"]  # [{"cik_str", "ticker", "title"}, ...]
        # normalized_title -> (ticker, canonical_title). A handful of
        # normalized collisions are possible (rare); last one in the
        # SEC file wins, which is an acceptable tradeoff for a fuzzy
        # first pass -- exact-ticker lookups aren't affected.
        self._by_normalized_title = {
            normalize(e["title"]): (e["ticker"], e["title"]) for e in self._entries
        }
        self._by_ticker = {e["ticker"]: e["title"] for e in self._entries}
        self._choices = list(self._by_normalized_title.keys())

    def resolve(self, company_name, threshold=CONFIDENCE_THRESHOLD):
        """Returns (ticker, canonical_name) for a confident match, or
        None. Tries an exact normalized match first (cheap, and avoids
        fuzzy-matching false positives on names that already match
        perfectly); falls back to fuzzy matching against every SEC
        title."""
        if not company_name:
            return None

        query = normalize(company_name)
        if not query:
            return None

        alias_ticker = ALIASES.get(query)
        if alias_ticker and alias_ticker in self._by_ticker:
            return (alias_ticker, self._by_ticker[alias_ticker])

        exact = self._by_normalized_title.get(query)
        if exact:
            return exact

        match = process.extractOne(query, self._choices, scorer=fuzz.token_set_ratio)
        if match is None:
            return None
        matched_choice, score, _ = match
        if score < threshold:
            return None
        return self._by_normalized_title[matched_choice]


_registry = None


def get_registry():
    """Lazy singleton -- the SEC file (~10k entries) only needs to be
    loaded and indexed once per process, not once per bill."""
    global _registry
    if _registry is None:
        _registry = CompanyRegistry()
    return _registry


def resolve_company(company_name, threshold=CONFIDENCE_THRESHOLD):
    """Module-level convenience wrapper -- the common case callers want."""
    return get_registry().resolve(company_name, threshold=threshold)
