# Congress Trades — setup notes

Built against your actual repo (cokercola/alphoptic) — pulled the real
index.html, site.js, app.js, style.css, fetch_bills.py, and bills.json to
match conventions exactly, rather than guessing.

## What's in this drop
```
.github/workflows/update-congress-trades.yml   # daily cron, mirrors update-bills.yml
scripts/fetch_congress_trades.py                # pulls + filters + writes JSON
data/congress-trades.json                       # sample data (real run overwrites this)
assets/trades.js                                 # renders teaser + full page
assets/trades.css                                # styling, reuses your existing CSS vars
assets/site.js                                   # updated: adds "Trades" to the shared sidebar
trades/index.html                                # new dedicated Trades page
index.html                                       # updated: teaser section + Trades nav link
```

## How ticker linking actually works

There's no separate companies dataset in your repo — companies/index.html
just filters data/bills.json's existing company exposure lists by
`?ticker=`. So a traded symbol only "links" if it already appears
somewhere in a tracked bill's company list.

`fetch_congress_trades.py` reads `data/bills.json` locally at generation
time, builds the set of every ticker mentioned across all signals, and
marks a trade `linked: true` if its symbol is in that set. No extra
config needed — as WATCHED_BILLS in fetch_bills.py grows, more trade
tickers will naturally start linking.

Right now, of the sample trade tickers, only **AAPL** and **LDOS**
actually show up in your live bills.json — so those are the only two
that'll render as clickable links until you track more bills involving
the other companies (NVDA, MU, XOM, etc.).

## Before this goes live

1. **Get an FMP API key** at financialmodelingprep.com (free tier).
2. **Add it as a GitHub secret**: repo Settings → Secrets and variables →
   Actions → New repository secret → name it `FMP_API_KEY`.
3. **Confirm the Senate/House trading endpoints are actually included in
   your free-tier plan.** I couldn't fully verify this from FMP's docs —
   their pricing page doesn't spell out per-dataset access clearly. Test
   locally first:
   ```
   FMP_API_KEY=your_key_here python scripts/fetch_congress_trades.py
   ```
   Run this from the repo root so it can find `data/bills.json`. If you
   get a 401/403, or the response looks like an upgrade prompt instead of
   trade data, you're gated — either upgrade, or swap the two
   `fetch_latest()` calls for Senate Stock Watcher's free JSON instead
   (Senate-only, no key needed).
4. **Replace `assets/site.js`** with the version here — it's the same
   file with one addition (the Trades entry in the sidebar list), so this
   is a safe drop-in, not a rewrite.
5. **Adjust the cron schedule** in the workflow if daily isn't the
   cadence you want. It's set to run at a different hour (13:00 UTC) than
   `update-bills.yml` (12:00 UTC) so the two workflows don't collide when
   pushing commits.

## Testing locally without hitting the API

The sample `data/congress-trades.json` already matches the schema the
script produces. Serve the folder and open it:
```
python3 -m http.server 8000
```
Then visit `http://localhost:8000/index.html` and
`http://localhost:8000/trades/index.html`. For the ticker links to
resolve to anything meaningful, you'll want your real `data/bills.json`
sitting alongside it too (not included here — pull your live copy in).

## Notes on the data itself

- Amounts are the **midpoint** of FMP's disclosed range (e.g.
  "$250,001 - $500,000" becomes $375,000) — the standard convention other
  trackers use, since exact dollar amounts aren't legally required in the
  disclosures.
- "Buy"/"Sell" collapses FMP's more granular transaction types. Exchanges
  and gifts are filtered out entirely since they're not a directional
  signal.
- `is_new` is filed-within-7-days, not traded-within-7-days — filing date
  is when the public actually gets to see it, matching what we discussed.
