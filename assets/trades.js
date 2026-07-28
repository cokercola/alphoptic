/**
 * Renders congress trade data (from data/congress-trades.json) into either:
 *   - a compact "teaser" list on the Dashboard (top N trades overall), or
 *   - the full per-lawmaker breakdown on the dedicated Trades page.
 *
 * Matches the rest of the site's plain-JS, no-build-step pattern
 * (see assets/site.js / assets/app.js).
 */

function formatAmount(amount) {
  return "$" + Math.round(amount / 1000) + "k";
}

function formatDate(isoDateStr) {
  if (!isoDateStr) return "-";
  const d = new Date(isoDateStr);
  if (isNaN(d)) return isoDateStr;
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

function barColor(direction) {
  return direction === "buy" ? "var(--green)" : "var(--red)";
}

function directionLabel(direction) {
  return direction === "buy" ? "Buy" : "Sell";
}

/**
 * A single trade row: ticker, bar, amount, dates.
 * `pathPrefix` accounts for page depth - "" on the dashboard (root),
 * "../" from trades/index.html - so the companies link resolves correctly
 * from either page.
 */
function tradeRowHTML(trade, opts) {
  opts = opts || {};
  const maxAmount = opts.maxAmount || trade.amount;
  const pathPrefix = opts.pathPrefix || "";
  const pct = Math.max(14, Math.round((trade.amount / maxAmount) * 100));
  const color = barColor(trade.direction);
  const label = directionLabel(trade.direction);

  const symbolHTML = trade.linked
    ? `<a href="${pathPrefix}companies/index.html?ticker=${trade.symbol}" class="trade-symbol trade-symbol--linked">${trade.symbol}</a>`
    : `<span class="trade-symbol">${trade.symbol}</span>`;

  const dateLabel = opts.showBothDates
    ? `Traded ${formatDate(trade.trade_date)} &middot; Filed ${formatDate(trade.filed_date)}`
    : `Filed ${formatDate(trade.filed_date)}`;

  return `
    <div class="trade-row">
      ${symbolHTML}
      <div class="trade-bar-track">
        <div class="trade-bar-fill" style="width:${pct}%; background:${color};">
          <span class="trade-bar-label">${label}</span>
        </div>
      </div>
      <span class="trade-amount">${formatAmount(trade.amount)}</span>
      <span class="trade-date">
        ${trade.is_new ? '<span class="trade-new-dot"></span>' : ""}${dateLabel}
      </span>
    </div>
  `;
}

/**
 * Dashboard teaser: flattens all lawmakers' trades, takes the N most
 * recently filed, each row labeled with the lawmaker's name.
 * Call from index.html, where pathPrefix should be "" (root page).
 */
function renderTeaser(containerId, data, count, pathPrefix) {
  const el = document.getElementById(containerId);
  if (!el) return;

  count = count || 3;
  pathPrefix = pathPrefix || "";

  const flattened = [];
  data.lawmakers.forEach((person) => {
    const metaParts = [person.party, person.chamber].filter(Boolean);
    person.trades.forEach((trade) => {
      flattened.push({ ...trade, personName: person.name, personMeta: metaParts.join(" · ") });
    });
  });

  flattened.sort((a, b) => new Date(b.filed_date) - new Date(a.filed_date));
  const top = flattened.slice(0, count);

  if (top.length === 0) {
    el.innerHTML = `<div class="empty-note">No recent trades to show.</div>`;
    return;
  }

  const maxAmount = Math.max(...top.map((t) => t.amount));

  el.innerHTML = top
    .map((trade) => {
      const row = tradeRowHTML(trade, { maxAmount, showBothDates: false, pathPrefix });
      return `
        <div class="trade-teaser-row">
          <div class="trade-teaser-person">
            <div class="trade-teaser-name">${trade.personName}</div>
            <div class="trade-teaser-meta">${trade.personMeta}</div>
          </div>
          ${row}
        </div>
      `;
    })
    .join("");
}

/**
 * Full trades page: pinned lawmaker cards first, then a divider, then any
 * randomly-selected extra lawmakers who have trades this run (there may
 * be 0-2 of these, since they're only included when they have data).
 * Call from trades/index.html, where pathPrefix should be "../".
 */
function renderFullTrades(containerId, data, pathPrefix) {
  const el = document.getElementById(containerId);
  if (!el) return;

  pathPrefix = pathPrefix || "";

  const cardHTML = (person) => {
    const metaParts = [person.party, person.chamber].filter(Boolean);
    const meta = metaParts.join(" · ");

    if (person.trades.length === 0) {
      return `
        <div class="trade-card">
          <div class="trade-card-head">
            <span class="trade-card-name">${person.name}</span>
            <span class="trade-card-meta">${meta}</span>
          </div>
          <div class="empty-note" style="padding-top:8px; background:none;">No disclosed trades this period</div>
        </div>
      `;
    }

    const maxAmount = Math.max(...person.trades.map((t) => t.amount));
    const rows = person.trades.map((t) => tradeRowHTML(t, { maxAmount, showBothDates: true, pathPrefix })).join("");

    return `
      <div class="trade-card">
        <div class="trade-card-head">
          <span class="trade-card-name">${person.name}</span>
          <span class="trade-card-meta">${meta}</span>
        </div>
        ${rows}
      </div>
    `;
  };

  const pinned = data.lawmakers.filter((p) => p.pinned !== false);
  const extras = data.lawmakers.filter((p) => p.pinned === false);

  let html = pinned.map(cardHTML).join("");

  if (extras.length > 0) {
    html += `<div class="trades-extra-divider">Also trading recently</div>`;
    html += extras.map(cardHTML).join("");
  }

  el.innerHTML = html;
}

/**
 * Fetches data/congress-trades.json once. `dataPath` should be
 * "data/congress-trades.json" from the dashboard or
 * "../data/congress-trades.json" from a subpage.
 */
function loadTradesData(dataPath, callback) {
  fetch(dataPath)
    .then((res) => {
      if (!res.ok) throw new Error(`Failed to load ${dataPath}`);
      return res.json();
    })
    .then(callback)
    .catch((err) => {
      console.error(err);
      document.querySelectorAll("[data-trades-target]").forEach((elm) => {
        elm.innerHTML = `<div class="empty-note">Couldn't load trade data right now.</div>`;
      });
    });
}
