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

def _call_with_retry(fn, description, max_attempts=4):
    """Retries an Anthropic API call after a dropped/reset connection.
    Batch retrieve/results calls are safe to simply re-request from
    scratch -- they return the same completed data each time, no
    resume state needed."""
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except (anthropic.APIConnectionError, anthropic.APITimeoutError) as e:
            if attempt == max_attempts:
                raise
            wait = 30 * attempt
            print(f"WARNING: {description} attempt {attempt} failed ({e}); "
                  f"retrying in {wait}s.")
            time.sleep(wait)


from fetch_bills import (
    CONGRESS_BASE,
    CONGRESS_API_KEY,
    CURRENT_CONGRESS,
    CLASSIFICATION_SCHEMA_VERSION,
    CLASSIFY_PROMPT,
    IMPACT_FACTOR_MAX,
    INDUSTRY_TAXONOMY,
    write_slim_data_files,
    write_bill_chunks,
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
    fetch_bill_cbo_estimate,
    fetch_total_bill_count,
    resolve_companies,
    bill_stage,
    is_on_calendar,
    STAGE_LABELS,
    passage_probability,
    derive_community_category,
    compute_impact_score_and_relevance,
    load_previous_signals,
)

CHECKPOINT_PATH = "data/.backfill-bills-checkpoint.json"
LISTING_PAGE_SIZE = 250          # Congress.gov's max page size for the bill listing endpoint
BATCH_CHUNK_SIZE = 5000          # requests per Batch API submission -- conservative,
                                  # comfortably under any documented cap
MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 700  # was 600 - matches fetch_bills.py's classify(), new schema needs more room

# Rough per-request cost estimate for --debug mode. Re-measured against
# the actual rewritten CLASSIFY_PROMPT (7-factor rubric with band tables)
# on 2026-08-20 - the prompt itself runs ~1,590 tokens filled in, and the
# response now includes impact_breakdown (7 fields) + impact_rationale +
# secondary_industries on top of what used to be just a single
# impact_score integer, so both sides grew substantially from the old
# short-prompt estimate. Still an estimate, not a guarantee - actual
# bill title/status/summary length varies per bill. Batch API is 50% of
# standard $3/$15 per-million-token pricing.
EST_INPUT_TOKENS_PER_BILL = 1600
EST_OUTPUT_TOKENS_PER_BILL = 550
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


def fetch_with_retry(func, *args, retries=3, backoff=5, **kwargs):
    """Retries transient network failures (timeouts, connection resets)
    against Congress.gov's free API, which occasionally has these at
    real scale. Re-raises after exhausting retries, so the caller can
    still decide what to do (skip this one bill vs. abort the run)."""
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            return func(*args, **kwargs)
        except requests.exceptions.RequestException as e:
            last_err = e
            print(f"    request failed (attempt {attempt}/{retries}): {e}", file=sys.stderr)
            if attempt < retries:
                time.sleep(backoff * attempt)
    raise last_err


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
        try:
            page = fetch_with_retry(fetch_bill_listing_page, CURRENT_CONGRESS, offset, LISTING_PAGE_SIZE)
        except requests.exceptions.RequestException as e:
            print(f"\nERROR: bill listing fetch failed repeatedly at offset {offset}: {e}", file=sys.stderr)
            print("Saving progress made so far and stopping cleanly -- just re-run submit, "
                  "it will resume from where this run left off.")
            checkpoint["last_offset"] = offset
            save_checkpoint(checkpoint)
            print(f"\nScanned {total_scanned} bills before the failure.")
            print(f"  Skipped {total_skipped_reserved} 'Reserved for...' placeholder bills.")
            print(f"  Skipped {total_skipped_cached} already cached with unchanged status.")
            print(f"  Queued {total_queued} bills needing fresh classification (not yet submitted).")
            sys.exit(1)

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
            # Same WATCH exception as fetch_bills.py's main loop: a bill
            # with no summary yet can sit at an unchanged status for
            # months while a real summary eventually gets published, so
            # WATCH bills are never skipped here even when nothing else
            # about them changed.
            if (cached and cached.get("status") == status
                    and cached.get("schema_version") == CLASSIFICATION_SCHEMA_VERSION
                    and cached.get("market_relevance") != "WATCH"):
                total_skipped_cached += 1
                continue

            # Needs fresh classification -- fetch the extra detail only
            # for bills that actually need it, same philosophy as the
            # daily job. The listing endpoint doesn't include policyArea
            # or sponsor name, so a detail fetch is required here.
            try:
                detail = fetch_with_retry(fetch_bill, CURRENT_CONGRESS, bill_type, number)
            except requests.exceptions.RequestException as e:
                print(f"WARNING: detail fetch failed for {bill_id} after retries ({e}); "
                      f"skipping this bill this run.", file=sys.stderr)
                continue

            policy_area = detail.get("policyArea", {}).get("name", "")
            sponsor_name = detail.get("sponsors", [{}])[0].get("fullName", "Unknown")
            sponsor_bioguide_id = detail.get("sponsors", [{}])[0].get("bioguideId")

            try:
                cosponsors = fetch_with_retry(fetch_bill_cosponsors, CURRENT_CONGRESS, bill_type, number)
            except requests.exceptions.RequestException as e:
                print(f"WARNING: cosponsor fetch failed for {bill_id} after retries ({e}); "
                      f"continuing with empty cosponsor list.", file=sys.stderr)
                cosponsors = []
            cosponsor_names = [c["name"] for c in cosponsors]
            cosponsor_ids = [c.get("bioguide_id") for c in cosponsors]

            try:
                summary_text = fetch_with_retry(fetch_bill_summary, CURRENT_CONGRESS, bill_type, number)
            except requests.exceptions.RequestException as e:
                print(f"WARNING: summary fetch failed for {bill_id} after retries ({e}); "
                      f"classifying without an official summary.", file=sys.stderr)
                summary_text = None

            try:
                cbo_context = fetch_with_retry(fetch_bill_cbo_estimate, CURRENT_CONGRESS, bill_type, number)
            except requests.exceptions.RequestException as e:
                print(f"WARNING: CBO estimate fetch failed for {bill_id} after retries ({e}); "
                      f"continuing without it.", file=sys.stderr)
                cbo_context = ""

            custom_id = bill_id
            pending_requests.append({
                "custom_id": custom_id,
                "params": {
                    "model": MODEL,
                    "max_tokens": MAX_TOKENS,
                    "messages": [{
                        "role": "user",
                        "content": CLASSIFY_PROMPT.format(
                            title=title, status=status, summary=summary_text or "No summary available.",
                            cbo_context=cbo_context,
                            industry_list=", ".join(f'"{i}"' for i in INDUSTRY_TAXONOMY),
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
                "sponsor_bioguide_id": sponsor_bioguide_id,
                "cosponsors": len(cosponsor_names),
                "cosponsor_names": cosponsor_names,
                "cosponsor_ids": cosponsor_ids,
                "policy_area": policy_area,
                # Needed at merge time (build_signal_from_result) to
                # compute market_relevance - a WATCH bill is defined by
                # having no summary, not by its score, so this has to
                # survive from submit time through to poll time.
                "has_summary": bool(summary_text),
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

    # A bill can genuinely get re-scanned within one run: sorting by
    # updateDate keeps pagination stable in general, but if a bill's
    # status changes WHILE a long scan is still in progress (very
    # possible over a multi-hour run), its updateDate moves forward and
    # can shift it into a later page than where it started, queuing it
    # twice. pending_meta (a dict) already self-dedupes; pending_requests
    # (a list) doesn't, and the Batch API rejects duplicate custom_ids
    # outright -- so dedupe here too, keeping the later (fresher) copy.
    deduped_requests = list({r["custom_id"]: r for r in pending_requests}.values())
    if len(deduped_requests) < len(pending_requests):
        print(f"Deduplicated {len(pending_requests) - len(deduped_requests)} bill(s) "
              f"re-scanned mid-run (likely updated while this scan was in progress).")

    batch = None
    max_attempts = 4
    for attempt in range(1, max_attempts + 1):
        try:
            batch = client.messages.batches.create(requests=deduped_requests)
            break
        except (anthropic.APIConnectionError, anthropic.APITimeoutError) as e:
            if attempt == max_attempts:
                raise
            wait = 30 * attempt
            print(f"WARNING: batch submission attempt {attempt} failed ({e}); "
                  f"retrying in {wait}s (this network drop may still have gone "
                  f"through server-side -- check the Anthropic Console before "
                  f"re-submitting by hand if all retries fail).")
            time.sleep(wait)
    print(f"\nSubmitted batch {batch.id} with {len(deduped_requests)} requests. "
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
    on_calendar = is_on_calendar(meta["status"])
    community_category = derive_community_category(meta.get("policy_area", ""), meta.get("title"))
    bill_like = {"cosponsors": {"count": meta.get("cosponsors", 0)}}

    # has_summary came from submit time (recorded alongside this bill's
    # pending request) - meta.get() rather than direct indexing since
    # reconstruct_meta_from_bill_id() is a separate, older code path
    # that also needs to supply it (see below).
    has_summary = meta.get("has_summary", False)
    impact_score, market_relevance = compute_impact_score_and_relevance(
        classification.get("impact_breakdown", {}), has_summary)

    return {
        "bill_id": meta["bill_id"],
        "congress": meta["congress"],
        "title": meta["title"],
        "status": meta["status"],
        "last_action": meta["status"],
        "last_action_date": meta["status_date"],
        "sponsor": meta.get("sponsor", "Unknown"),
        "sponsor_bioguide_id": meta.get("sponsor_bioguide_id"),
        "cosponsors": meta.get("cosponsors", 0),
        "cosponsor_names": meta.get("cosponsor_names", []),
        "cosponsor_ids": meta.get("cosponsor_ids", []),
        "stage": stage,
        "on_calendar": on_calendar,
        "stage_label": STAGE_LABELS[stage],
        "passage_probability": passage_probability(bill_like, stage),
        "community_category": community_category,
        "community_category_label": COMMUNITY_CATEGORY_LABELS.get(community_category, "None"),
        "schema_version": schema_version,
        **classification,
        "impact_score": impact_score,
        "has_summary": has_summary,
        "market_relevance": market_relevance,
        # fetch_bills.py's daily job always stamps this (currently just a
        # hardcoded "TBD" placeholder, not yet computed dynamically - see
        # that file). This backfill path never set it at all, which left
        # bill.next_event as JS `undefined` - rendered literally as the
        # text "undefined" - on every backfilled bill's detail page.
        "next_event": "TBD",
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
        detail = fetch_with_retry(fetch_bill, CURRENT_CONGRESS, bill_type, number)
    except requests.exceptions.RequestException as e:
        print(f"    WARNING: could not reconstruct metadata for {bill_id} after retries: {e}", file=sys.stderr)
        return None

    try:
        cosponsors = fetch_with_retry(fetch_bill_cosponsors, CURRENT_CONGRESS, bill_type, number)
    except requests.exceptions.RequestException:
        cosponsors = []
    cosponsor_names = [c["name"] for c in cosponsors]
    cosponsor_ids = [c.get("bioguide_id") for c in cosponsors]

    # The original submit-time meta (with its own has_summary flag) is
    # gone in this fallback path - re-fetch fresh rather than assume.
    # On a fetch failure, default to False (WATCH) rather than True:
    # since this determines whether the bill counts toward "High
    # Impact," the safer wrong answer is under-confident, not
    # over-confident.
    try:
        summary_text = fetch_with_retry(fetch_bill_summary, CURRENT_CONGRESS, bill_type, number)
    except requests.exceptions.RequestException:
        summary_text = None

    return {
        "bill_id": bill_id,
        "congress": CURRENT_CONGRESS,
        "title": detail.get("title", ""),
        "status": detail.get("latestAction", {}).get("text", ""),
        "status_date": detail.get("latestAction", {}).get("actionDate", ""),
        "sponsor": detail.get("sponsors", [{}])[0].get("fullName", "Unknown"),
        "sponsor_bioguide_id": detail.get("sponsors", [{}])[0].get("bioguideId"),
        "cosponsors": len(cosponsor_names),
        "cosponsor_names": cosponsor_names,
        "cosponsor_ids": cosponsor_ids,
        "policy_area": detail.get("policyArea", {}).get("name", ""),
        "has_summary": bool(summary_text),
    }


# The same fields fetch_bills.py always writes on a successful
# classification (see its FALLBACK_CLASSIFICATION). A record missing
# any of these was the root cause of the KeyError that once crashed
# fetch_bills.py's daily run -- this backfill script was stamping
# schema_version as valid on classifications that hadn't actually been
# confirmed complete. impact_score/market_relevance are deliberately
# NOT here - those are computed deterministically in
# build_signal_from_result(), never asked of Claude directly.
REQUIRED_CLASSIFICATION_FIELDS = (
    "industry", "secondary_industries", "direction", "confidence",
    "summary", "impact_breakdown", "impact_rationale", "companies",
)


def sanitize_classification(classification):
    """Claude's CLASSIFY_PROMPT schema specifies confidence, every
    impact_breakdown factor, and exposure as integers, but nothing
    enforces that on the way out -- at real scale (thousands of calls)
    some fraction come back as strings instead (e.g. "70" instead of
    70), which crashes any later int comparison/sum on that field.
    Coerce here, once, right after parsing, so a bad type from one bill
    can never take down the whole summary rebuild after a merge has
    already succeeded.

    Also validates that all REQUIRED_CLASSIFICATION_FIELDS are present.
    Claude's response is occasionally syntactically valid JSON that
    just omits a field outright (not a type problem) -- raising here
    routes that case through the same except-and-fall-back-to-
    FALLBACK_CLASSIFICATION path as a JSON parse failure, instead of
    silently shipping an incomplete record stamped as schema-valid."""
    missing = [f for f in REQUIRED_CLASSIFICATION_FIELDS if f not in classification]
    if missing:
        raise ValueError(f"classification missing required field(s): {missing}")
    if "confidence" in classification:
        try:
            classification["confidence"] = int(classification["confidence"])
        except (TypeError, ValueError):
            classification["confidence"] = 0
    breakdown = classification.get("impact_breakdown")
    if isinstance(breakdown, dict):
        for factor in IMPACT_FACTOR_MAX:
            try:
                breakdown[factor] = int(breakdown.get(factor, 0))
            except (TypeError, ValueError):
                breakdown[factor] = 0
    else:
        # Missing/malformed breakdown - treat as fully absent so
        # compute_impact_score_and_relevance() sees all-zero factors
        # rather than crashing on a non-dict.
        classification["impact_breakdown"] = {k: 0 for k in IMPACT_FACTOR_MAX}
    if not isinstance(classification.get("secondary_industries"), list):
        classification["secondary_industries"] = []
    if isinstance(classification.get("companies"), str):
        # Observed at least once in existing data (HRES790): Claude's
        # response had "companies" as the literal JSON string "[]"
        # instead of an actual array. That's valid JSON so it sailed
        # through extract_json_object/json.loads untouched, and every
        # downstream `for c in signal["companies"]` then iterated over
        # individual CHARACTERS ('[', ']') instead of company dicts -
        # crashing normalize_companies.py and write_slim_data_files
        # with "'str' object has no attribute 'get'" the moment it hit
        # that bill. Try to recover a real list from the string; fall
        # back to empty either way rather than let a stray string
        # value poison every script that touches "companies" downstream.
        try:
            parsed = json.loads(classification["companies"])
            classification["companies"] = parsed if isinstance(parsed, list) else []
        except (ValueError, json.JSONDecodeError):
            classification["companies"] = []
    if isinstance(classification.get("companies"), list):
        for c in classification["companies"]:
            if isinstance(c, dict) and "exposure" in c:
                try:
                    c["exposure"] = int(c["exposure"])
                except (TypeError, ValueError):
                    c["exposure"] = 0
        # fetch_bills.py's daily job runs every classification's companies
        # through the SEC-registry resolver before storing (see
        # resolve_companies() / company_registry.py) - this backfill path
        # was skipping that step entirely and storing Claude's raw,
        # free-recalled company names with no ticker at all. That's what
        # was showing up as ticker "undefined" on the dashboard Watchlist
        # and on bill detail pages for any backfilled bill. Companies that
        # don't confidently resolve are dropped here too, same as the
        # daily job - not kept with an unverified ticker.
        classification["companies"] = resolve_companies(classification["companies"])
    else:
        classification["companies"] = []
    if classification.get("industry") not in INDUSTRY_TAXONOMY:
        classification["industry"] = "Other / Cross-Sector"
    return classification


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
        print(f"DEBUG: attempting to retrieve batch_id={batch_id!r} (length {len(batch_id)})")
        batch = _call_with_retry(
            lambda: client.messages.batches.retrieve(batch_id),
            f"retrieve batch {batch_id}",
        )
        print(f"Batch {batch_id}: {batch.processing_status} ({batch.request_counts})")

        if batch.processing_status != "ended":
            still_pending.append(entry)
            continue

        succeeded = 0
        failed = 0
        reconstructed = 0
        results = _call_with_retry(
            lambda: list(client.messages.batches.results(batch_id)),
            f"fetch results for batch {batch_id}",
        )
        for result in results:
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
                    classification = sanitize_classification(json.loads(extract_json_object(text)))
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
        if still_pending:
            print(f"{len(still_pending)} batch(es) still processing -- run poll again later.")
        return

    # Merge: new results overwrite any existing record for the same
    # bill_id, everything else from the previous file is preserved.
    merged = dict(previous)
    merged.update(new_signals)
    signals = list(merged.values())

    rebuild_and_save_bills_json(signals, new_signals_count=len(new_signals))

    print(f"\nMerged {len(new_signals)} newly completed bills into {BILLS_JSON_PATH}. "
          f"Total tracked: {len(signals)}.")
    if still_pending:
        print(f"{len(still_pending)} batch(es) still processing -- run poll again later.")


def safe_int(value, default=0):
    """Defense in depth for rebuild_and_save_bills_json specifically --
    this function is the single point of failure that would lose an
    entire run's already-merged results if it crashes, so it can't
    trust that every signal's numeric fields are actually numeric,
    regardless of what upstream sanitization is supposed to guarantee."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def rebuild_and_save_bills_json(signals, new_signals_count):
    """Shared by poll (after merging fresh batch results) and repair
    (rewriting the summary from existing signals only, no new data) --
    keeping this in one place means they can't drift out of sync with
    each other or with fetch_bills.py's own summary structure."""
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
    except Exception as e:
        # This is a "nice to have" enrichment call, not core to the
        # merge - a timeout/connection error here used to be uncaught
        # (only requests.HTTPError was handled) and would crash the
        # whole run AFTER the checkpoint already marked this batch as
        # no longer pending, permanently losing that batch's
        # already-paid-for classification results since nothing would
        # ever retry them. Catching broadly here means a hiccup on
        # this one lightweight call can never take the merge down.
        print(f"WARNING: couldn't fetch total bill count ({e}); omitting from output.")
        total_bills_this_congress = None

    output = {
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
        "summary": {
            "bills_tracked": len(signals),
            # Matches fetch_bills.py: counts market_relevance == "HIGH",
            # not a raw score threshold - a bill only counts here if
            # it's ALSO backed by a real published summary.
            "high_impact": sum(1 for s in signals if s.get("market_relevance") == "HIGH"),
            "watch_list": sum(1 for s in signals if s.get("market_relevance") == "WATCH"),
            "new_signals_today": new_signals_count,
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
    write_slim_data_files(signals, summary=output["summary"])
    write_bill_chunks(signals)

    return output


def cmd_repair(args):
    """Rewrites data/bills.json's summary from the EXISTING signals
    already on file -- no submission, no classification, no new bills.
    One tiny free Congress.gov metadata call (total bill count), but no
    Anthropic spend at all. Use this to verify a summary-structure fix
    (like the missing community_categories field) took effect on the
    live file without needing to submit/poll any new data first."""
    previous = load_previous_signals()
    signals = list(previous.values())
    if not signals:
        print("No existing signals found in data/bills.json -- nothing to repair.")
        return
    rebuild_and_save_bills_json(signals, new_signals_count=0)
    print(f"Repaired {BILLS_JSON_PATH} summary from {len(signals)} existing signals. "
          f"No new bills were fetched or classified -- this only rebuilds the summary structure.")


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

    repair_parser = sub.add_parser("repair")
    repair_parser.set_defaults(func=cmd_repair)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
