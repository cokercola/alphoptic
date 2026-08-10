"""One-time cleanup: remaps every bill's free-text `industry` label to the
fixed INDUSTRY_TAXONOMY introduced alongside this script.

Why this is cheap despite 14,000+ bills: we don't reclassify each bill.
We classify each *unique label string* once (a few thousand short strings,
not full bill titles/summaries), build an old-label -> canonical-label
lookup table, then apply that table locally to every signal already in
data/bills.json. No bill detail needs to be re-fetched or re-summarized.

Safe to re-run: fully re-derives the mapping from whatever labels are
currently in bills.json and overwrites the `industry` field in place.
Everything else on each signal (companies, direction, impact_score, etc.)
is left untouched.
"""

import json
import sys
import time
from collections import Counter

import anthropic

from fetch_bills import BILLS_JSON_PATH, INDUSTRY_TAXONOMY, client
from backfill_bills_history import rebuild_and_save_bills_json

MODEL = "claude-sonnet-4-6"
CHUNK_SIZE = 300  # unique labels per API call - keeps each response small
                  # and means a single bad/truncated response only loses
                  # one chunk's worth of labels, not the whole run.

MAP_PROMPT = """You are cleaning up a messy list of free-text industry
labels by mapping each one to the single closest match from a fixed
canonical list.

Canonical list (choose ONLY from these, copied verbatim):
{taxonomy}

Messy labels to map (one per line, each prefixed with its index):
{labels}

Respond with ONLY a JSON object (no markdown fences, no preamble) mapping
each index (as a string) to its chosen canonical label, e.g.:
{{"0": "Healthcare & Pharmaceuticals", "1": "Defense & Aerospace"}}
"""


def extract_json_object(text):
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
    raise ValueError("No balanced '}' found in response text")


def call_with_retry(fn, description, max_attempts=4):
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except (anthropic.APIConnectionError, anthropic.APITimeoutError) as e:
            if attempt == max_attempts:
                raise
            wait = 15 * attempt
            print(f"WARNING: {description} attempt {attempt} failed ({e}); "
                  f"retrying in {wait}s.", file=sys.stderr)
            time.sleep(wait)


def map_chunk(labels_chunk):
    """labels_chunk: list of label strings. Returns {label: canonical_label}."""
    numbered = "\n".join(f"{i}: {label}" for i, label in enumerate(labels_chunk))
    prompt = MAP_PROMPT.format(
        taxonomy="\n".join(f"- {t}" for t in INDUSTRY_TAXONOMY),
        labels=numbered,
    )
    message = call_with_retry(
        lambda: client.messages.create(
            model=MODEL,
            max_tokens=4000,
            messages=[{"role": "user", "content": prompt}],
        ),
        "map_chunk",
    )
    text = message.content[0].text.strip()
    text = text.replace("```json", "").replace("```", "").strip()
    try:
        index_map = json.loads(extract_json_object(text))
    except (ValueError, json.JSONDecodeError) as e:
        print(f"WARNING: couldn't parse chunk response ({e}); "
              f"labels in this chunk fall back to 'Other / Cross-Sector'.",
              file=sys.stderr)
        return {}

    result = {}
    for i, label in enumerate(labels_chunk):
        canonical = index_map.get(str(i))
        if canonical not in INDUSTRY_TAXONOMY:
            canonical = "Other / Cross-Sector"
        result[label] = canonical
    return result


def main():
    with open(BILLS_JSON_PATH) as f:
        data = json.load(f)
    signals = data["signals"]

    label_counts = Counter(s.get("industry", "") for s in signals if s.get("industry"))
    unique_labels = list(label_counts.keys())
    print(f"Found {len(unique_labels)} unique industry labels across {len(signals)} bills.")

    lookup = {}
    for i in range(0, len(unique_labels), CHUNK_SIZE):
        chunk = unique_labels[i:i + CHUNK_SIZE]
        print(f"  Mapping labels {i}-{i + len(chunk)} of {len(unique_labels)}...")
        lookup.update(map_chunk(chunk))

    changed = 0
    for s in signals:
        old = s.get("industry", "")
        new = lookup.get(old, "Other / Cross-Sector")
        if new != old:
            changed += 1
        s["industry"] = new

    print(f"Remapped {changed} of {len(signals)} bills to canonical industries.")
    rebuild_and_save_bills_json(signals, new_signals_count=0)
    print("Saved.")


if __name__ == "__main__":
    main()
