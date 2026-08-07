"""
One-time (or occasional) backfill of ALL bills in the current Congress,
as opposed to fetch_bills.py's daily incremental job which only pulls a
small "most recently updated" slice. Confirmed necessary because a
genuinely stalled bill (no recent action) can never be auto-discovered
by "most recently updated" sorting in the first place - the daily job
structurally cannot build real Stalled Bills coverage on its own.

Uses Anthropic's Message Batches API instead of individual calls -
50% cheaper than standard pricing, and this is exactly the workload
Batch API is built for (large volume, non-interactive, fine with async
results). Tradeoff: results can take up to 24 hours, so this is a
TWO-PHASE process:

  python backfill_bills_history.py submit [--limit N] [--debug]
      Paginates through Congress.gov's full bill listing for the
      current Congress, skips bills already cached with an unchanged
      status (same cache logic as fetch_bills.py), skips "Reserved
      for..." placeholder bills, and submits everything needing fresh
      classification as Batch API request(s). Saves batch ID(s) and
      pending bill metadata to a checkpoint file.

      --debug prints how many requests WOULD be submitted and an
      estimated cost, without actually calling the Batch API or
      spending anything. Run this first.

  python backfill_bills_history.py poll
      Checks any pending batch(es) from a previous submit run. For any
      that have finished, retrieves results and merges them into
      data/bills.json. Leaves still-processing batches for the next
      poll run. Safe to run repeatedly (e.g. on a schedule) until
      everything completes.

Reuses fetch_bill_cosponsors, fetch_bill_summary, bill_stage,
STAGE_LABELS, passage_probability, derive_community_category,
CLASSIFY_PROMPT, FALLBACK_CLASSIFICATION, extract_json_object, and the
schema-version cache logic directly from fetch_bills.py, so this can
never silently drift from how the daily job classifies bills.

Required environment variables (same as fetch_bills.py):
  CONGRESS_API_KEY, ANTHROPIC_API_KEY
"""

import argparse
import datetime
import json
import os
import sys
import time

import anthropic
import requests

from fetch_bills import (
    CONGRESS_BASE,
    CONGRESS_API_KEY,
    CURRENT_CONGRESS,
    CLASSIFICATION_SCHEMA_VERSION,
    CLASSIFY_PROMPT,
    FALLBACK_CLASSIFICATION,
    BILLS_JSON_PATH,
    BILL_ID_RE,
    COMMUNITY_CATEGORIES,
    COMMUNITY_CATEGORY_LABELS,
    client,
    extract_json_object,
    fetch_bill,
    fetch_bill_cosponsors,
    fetch_bill_summary,
    fetch_total_bill_count,
    bill_stage,
    STAGE_LABELS,
    passage_probability,
    derive_community_category,
    load_previous_signals,
)

CHECKPOINT_PATH = "data/.backfill-bills-checkpoint.json"
LISTING_PAGE_SIZE = 250          # Congress.gov's max page size for the bill listing endpoint
BATCH_CHUNK_SIZE = 5000          # requests per Batch API submission -- conservative,
                                  # comfortably under any documented cap
MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 600

# Rough per-request cost estimate for --debug mode. Based on the actual
# CLASSIFY_PROMPT structure (title + status + summary in, short JSON
# out). Batch API is 50% of standard $3/$15 per-million-token pricing.
EST_INPUT_TOKENS_PER_BILL = 600
EST_OUTPUT_TOKENS_PER_BILL = 300
BATCH_INPUT_PRICE_PER_MTOK = 1.50
BATCH_OUTPUT_PRICE_PER_MTOK = 7.50


def load_checkpoint():
    if os.path.exists(CHECKPOINT_PATH):
        with open(CHECKPOINT_PATH) as f:
            return json.load(f)
    return {"last_offset": 0, "pending_batches": []}


def save_checkpoint(checkpoint):
    os.makedirs("data", exist_ok=True)
    with open(CHECKPOINT_PATH, "w") as f:
        json.dump(checkpoint, f, indent=2)


def fetch_bill_listing_page(congress, offset, limit):
    """Congress.gov's plain bill listing endpoint - includes title and
    latestAction directly in the response, so we can determine whether
    a bill needs fresh classification WITHOUT a separate per-bill fetch
    for the common case (already cached, status unchanged). This is
    the main reason a full-Congress backfill is feasible without
    18,000+ extra API calls."""
    url = f"{CONGRESS_BASE}/bill/{congress}"
    params = {
        "api_key": CONGRESS_API_KEY,
        "format": "json",
        "offset": offset,
        "limit": limit,
        "sort": "updateDate+asc",  # stable ordering so pagination doesn't
                                    # skip/repeat bills as new ones get added mid-backfill
    }
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json().get("bills", [])


def cmd_submit(args):
    checkpoint = load_checkpoint()
    previous = load_previous_signals()  # {bill_id: prior signal record}

    pending_requests = []   # Batch API request objects for this chunk
    pending_meta = {}       # custom_id -> bill metadata, to reassemble after poll
    offset = checkpoint["last_offset"]
    total_scanned = 0
    total_skipped_reserved = 0
    total_skipped_cached = 0
    total_queued = 0

    while True:
        page = fetch_bill_listing_page(CURRENT_CONGRESS, offset, LISTING_PAGE_SIZE)
        if not page:
            print(f"Reached end of bill listing at offset {offset}.")
            break

        hit_limit_mid_page = False
        items_consumed_this_page = 0

        for b in page:
            items_consumed_this_page += 1
            total_scanned += 1
            bill_type = b.get("type", "").lower()
            number = b.get("number")
            bill_id = f"{bill_type.upper()}{number}"
            title = b.get("title", "")

            if title.strip().lower().startswith("reserved for"):
                total_skipped_reserved += 1
                continue

            status = b.get("latestAction", {}).get("text", "")
            status_date = b.get("latestAction", {}).get("actionDate", "")

            cached = previous.get(bill_id)
            if cached and cached.get("status") == status and cached.get("schema_version") == CLASSIFICATION_SCHEMA_VERSION:
                total_skipped_cached += 1
                continue

            # Needs fresh classification -- fetch the extra detail only
            # for bills that actually need it, same philosophy as the
            # daily job. The listing endpoint doesn't include policyArea
            # or sponsor name, so a detail fetch is required here.
            try:
                detail = fetch_bill(CURRENT_CONGRESS, bill_type, number)
            except requests.HTTPError as e:
                print(f"WARNING: detail fetch failed for {bill_id} ({e}); skipping this run.", file=sys.stderr)
                continue

            policy_area = detail.get("policyArea", {}).get("name", "")
            sponsor_name = detail.get("sponsors", [{}])[0].get("fullName", "Unknown")

            try:
                cosponsor_names = fetch_bill_cosponsors(CURRENT_CONGRESS, bill_type, number)
            except requests.HTTPError:
                cosponsor_names = []
            summary_text = fetch_bill_summary(CURRENT_CONGRESS, bill_type, number)

            custom_id = bill_id
            pending_requests.append({
                "custom_id": custom_id,
                "params": {
                    "model": MODEL,
                    "max_tokens": MAX_TOKENS,
                    "messages": [{
                        "role": "user",
                        "content": CLASSIFY_PROMPT.format(
                            title=title, status=status, summary=summary_text or "No summary available."
                        ),
                    }],
                },
            })
            pending_meta[custom_id] = {
                "bill_id": bill_id,
                "congress": CURRENT_CONGRESS,
                "type": bill_type,
                "number": number,
                "title": title,
                "status": status,
                "status_date": status_date,
                "sponsor": sponsor_name,
                "cosponsors": len(cosponsor_names),
                "cosponsor_names": cosponsor_names,
                "policy_area": policy_area,
            }
            total_queued += 1

            if args.limit and total_queued >= args.limit:
                hit_limit_mid_page = True
                break

        if hit_limit_mid_page:
            # Only advance past the items we actually scanned this page --
            # NOT a full page jump, which would silently skip everything
            # between here and the end of the page on the next run.
            checkpoint["last_offset"] = offset + items_consumed_this_page
        else:
            offset += LISTING_PAGE_SIZE
            checkpoint["last_offset"] = offset

        if args.limit and total_queued >= args.limit:
            break
        if len(pending_requests) >= BATCH_CHUNK_SIZE:
            # Same reasoning -- checkpoint should reflect exactly what
            # was scanned, not a full extra page.
            checkpoint["last_offset"] = offset if not hit_limit_mid_page else offset + items_consumed_this_page
            break  # submit this chunk, continue from here on the next submit run

    print(f"\nScanned {total_scanned} bills this run.")
    print(f"  Skipped {total_skipped_reserved} 'Reserved for...' placeholder bills.")
    print(f"  Skipped {total_skipped_cached} already cached with unchanged status.")
    print(f"  Queued {total_queued} bills needing fresh classification.")

    if args.debug:
        est_input_cost = (total_queued * EST_INPUT_TOKENS_PER_BILL / 1_000_000) * BATCH_INPUT_PRICE_PER_MTOK
        est_output_cost = (total_queued * EST_OUTPUT_TOKENS_PER_BILL / 1_000_000) * BATCH_OUTPUT_PRICE_PER_MTOK
        print(f"\nDEBUG: estimated Batch API cost for these {total_queued} requests: "
              f"${est_input_cost + est_output_cost:.2f} "
              f"(${est_input_cost:.2f} input + ${est_output_cost:.2f} output, at batch pricing)")
        print("DEBUG: not submitting anything. Re-run without --debug to actually submit this batch.")
        return

    if not pending_requests:
        print("Nothing to submit this run.")
        save_checkpoint(checkpoint)
        return

    batch = client.messages.batches.create(requests=pending_requests)
    print(f"\nSubmitted batch {batch.id} with {len(pending_requests)} requests. "
          f"Status: {batch.processing_status}")

    checkpoint["pending_batches"].append({
        "batch_id": batch.id,
        "submitted_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "meta": pending_meta,
    })
    save_checkpoint(checkpoint)
    print(f"Checkpoint saved. Run 'poll' later to check status and merge results "
          f"once this batch finishes (can take up to 24 hours, often much less).")


def build_signal_from_result(meta, classification, schema_version):
    stage = bill_stage(meta["status"])
    community_category = derive_community_category(meta.get("policy_area", ""))
    bill_like = {"cosponsors": {"count": meta.get("cosponsors", 0)}}
    return {
        "bill_id": meta["bill_id"],
        "congress": meta["congress"],
        "title": meta["title"],
        "status": meta["status"],
        "last_action": meta["status"],
        "last_action_date": meta["status_date"],
        "sponsor": meta.get("sponsor", "Unknown"),
        "cosponsors": meta.get("cosponsors", 0),
        "cosponsor_names": meta.get("cosponsor_names", []),
        "stage": stage,
        "stage_label": STAGE_LABELS[stage],
        "passage_probability": passage_probability(bill_like, stage),
        "community_category": community_category,
        "community_category_label": COMMUNITY_CATEGORY_LABELS.get(community_category, "None"),
        "schema_version": schema_version,
        **classification,
    }


def reconstruct_meta_from_bill_id(bill_id):
    """Fallback for when a batch's local metadata is missing or lost
    (e.g. checkpoint file deleted while a batch was still in flight).
    Since custom_id IS the bill_id, we can always rebuild everything
    needed by re-fetching fresh from Congress.gov -- the local
    checkpoint is a convenience cache, not the only source of truth."""
    match = BILL_ID_RE.match(bill_id)
    if not match:
        return None
    bill_type, number = match.groups()
    bill_type = bill_type.lower()
    number = int(number)

    try:
        detail = fetch_bill(CURRENT_CONGRESS, bill_type, number)
    except requests.HTTPError as e:
        print(f"    WARNING: could not reconstruct metadata for {bill_id}: {e}", file=sys.stderr)
        return None

    try:
        cosponsor_names = fetch_bill_cosponsors(CURRENT_CONGRESS, bill_type, number)
    except requests.HTTPError:
        cosponsor_names = []

    return {
        "bill_id": bill_id,
        "congress": CURRENT_CONGRESS,
        "title": detail.get("title", ""),
        "status": detail.get("latestAction", {}).get("text", ""),
        "status_date": detail.get("latestAction", {}).get("actionDate", ""),
        "sponsor": detail.get("sponsors", [{}])[0].get("fullName", "Unknown"),
        "cosponsors": len(cosponsor_names),
        "cosponsor_names": cosponsor_names,
        "policy_area": detail.get("policyArea", {}).get("name", ""),
    }


def cmd_poll(args):
    checkpoint = load_checkpoint()
    if not checkpoint["pending_batches"]:
        print("No pending batches to check.")
        return

    previous = load_previous_signals()
    still_pending = []
    new_signals = {}

    for entry in checkpoint["pending_batches"]:
        batch_id = entry["batch_id"]
        batch = client.messages.batches.retrieve(batch_id)
        print(f"Batch {batch_id}: {batch.processing_status} ({batch.request_counts})")

        if batch.processing_status != "ended":
            still_pending.append(entry)
            continue

        succeeded = 0
        failed = 0
        reconstructed = 0
        for result in client.messages.batches.results(batch_id):
            meta = entry.get("meta", {}).get(result.custom_id)
            if not meta:
                meta = reconstruct_meta_from_bill_id(result.custom_id)
                if not meta:
                    continue
                reconstructed += 1

            if result.result.type == "succeeded":
                text = result.result.message.content[0].text.strip()
                text = text.replace("```json", "").replace("```", "").strip()
                try:
                    classification = json.loads(extract_json_object(text))
                    succeeded += 1
                except (ValueError, json.JSONDecodeError):
                    classification = dict(FALLBACK_CLASSIFICATION)
                    failed += 1
            else:
                classification = dict(FALLBACK_CLASSIFICATION)
                failed += 1

            signal = build_signal_from_result(meta, classification, CLASSIFICATION_SCHEMA_VERSION)
            new_signals[signal["bill_id"]] = signal

        print(f"  Merged {succeeded} succeeded, {failed} failed/fell back to placeholder classification"
              f"{f', {reconstructed} with metadata reconstructed live (checkpoint data was missing)' if reconstructed else ''}.")

    checkpoint["pending_batches"] = still_pending
    save_checkpoint(checkpoint)

    if not new_signals:
        print("No newly completed results to merge this run.")
        return

    # Merge: new results overwrite any existing record for the same
    # bill_id, everything else from the previous file is preserved.
    merged = dict(previous)
    merged.update(new_signals)
    signals = list(merged.values())

    stage_counts = {stage: 0 for stage in STAGE_LABELS}
    for s in signals:
        stage_counts[s.get("stage", "introduced")] = stage_counts.get(s.get("stage", "introduced"), 0) + 1

    community_counts = {cat: 0 for cat in COMMUNITY_CATEGORIES}
    for s in signals:
        cat = s.get("community_category")
        if cat in community_counts:
            community_counts[cat] += 1

    try:
        total_bills_this_congress = fetch_total_bill_count()
    except requests.HTTPError as e:
        print(f"WARNING: couldn't fetch total bill count ({e}); omitting from output.")
        total_bills_this_congress = None

    output = {
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
        "summary": {
            "bills_tracked": len(signals),
            "high_impact": sum(1 for s in signals if s.get("impact_score", 0) >= 70),
            "new_signals_today": len(new_signals),
            "industries_affected": len({s.get("industry") for s in signals if s.get("industry")}),
            "total_bills_this_congress": total_bills_this_congress,
            "stage_breakdown": [
                {
                    "stage": stage,
                    "label": STAGE_LABELS[stage],
                    "count": stage_counts[stage],
                    "full_coverage": stage != "introduced",
                }
                for stage in STAGE_LABELS
            ],
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

    print(f"\nMerged {len(new_signals)} newly completed bills into {BILLS_JSON_PATH}. "
          f"Total tracked: {len(signals)}.")
    if still_pending:
        print(f"{len(still_pending)} batch(es) still processing -- run poll again later.")


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    submit_parser = sub.add_parser("submit")
    submit_parser.add_argument("--limit", type=int, default=None,
                                help="cap how many bills get queued this run, for a small test")
    submit_parser.add_argument("--debug", action="store_true",
                                help="print counts and estimated cost without submitting anything")
    submit_parser.set_defaults(func=cmd_submit)

    poll_parser = sub.add_parser("poll")
    poll_parser.set_defaults(func=cmd_poll)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
