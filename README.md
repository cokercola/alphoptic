# Alphoptic

Legislative intelligence dashboard: tracks bills in Congress, classifies
industry impact, and flags likely market-relevant developments. Updates
twice daily via GitHub Actions and reads/writes plain JSON - no database,
no server.

## How it works

- `index.html` / `assets/` — the static dashboard. Reads `data/bills.json`.
- `data/bills.json` — the current signal set. This file is what gets
  overwritten each run. The copy in this repo right now is sample data
  so the site has something to show immediately.
- `scripts/fetch_bills.py` — pulls bill data from Congress.gov, asks Claude
  to classify industry/company impact, writes `data/bills.json`.
- `.github/workflows/update-bills.yml` — runs the script twice daily and
  commits the refreshed data automatically.

## Setup

1. **Create the repo and enable Pages**
   Push this folder to a new GitHub repo, then in Settings → Pages, set
   the source to the `main` branch, root folder. Same pattern as
   OriginRunner.

2. **Get API keys**
   - Congress.gov: sign up at https://api.congress.gov/sign-up/ (free)
   - Anthropic: an API key from https://console.anthropic.com

3. **Add repo secrets**
   In Settings → Secrets and variables → Actions, add:
   - `CONGRESS_API_KEY`
   - `ANTHROPIC_API_KEY`

4. **Pick your watchlist**
   Edit `WATCHED_BILLS` in `scripts/fetch_bills.py`. It starts narrow (3
   bills) on purpose - classifying every active bill in Congress with an
   LLM daily adds up in cost fast. Expand it once you're happy with the
   output quality.

5. **Test it**
   Go to the Actions tab and manually run "Update legislative signals"
   (the `workflow_dispatch` trigger) to confirm it works before waiting
   for the schedule.

## What's built

- Dashboard, Bills, Industries, Companies, Signals, and Watchlist pages —
  all reading live from `data/bills.json`, so nothing needs hand-editing
  as the data refreshes twice daily.
- Bill detail pages are a single template (`bills/detail.html?id=HR1842`)
  rather than one file per bill, since bills change and static
  per-bill files would drift out of sync.

## What's intentionally not built yet

- Watchlist is not personalized per user - that needs accounts and
  sign-in, which this static-site architecture doesn't support. Right
  now it just shows every tracked ticker.
- Passage-probability model is a rough heuristic (cosponsor count +
  latest action stage), not the richer feature set described in the
  original spec (sponsor seniority, companion bills, etc.). Good enough
  to ship, worth revisiting once you have real data to check it against.
- No price-movement backtesting yet - that's the piece that actually
  proves the signal is worth anything, and it needs a market-data source
  wired in separately.
- Search bar in the header is visual only - not wired up to anything yet.
