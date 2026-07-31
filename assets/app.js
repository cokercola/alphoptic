async function loadDashboard() {
  const res = await fetch('data/bills.json');
  const data = await res.json();
  document.getElementById('updated-at').textContent =
    'Updated ' + new Date(data.updated_at).toLocaleString();
  const stats = data.summary;
  document.getElementById('stat-row').innerHTML = `
    <div class="stat-card"><div class="label">Bills tracked</div><div class="value">${stats.bills_tracked.toLocaleString()}</div></div>
    <div class="stat-card"><div class="label">High impact</div><div class="value" style="color:var(--orange)">${stats.high_impact}</div></div>
    <div class="stat-card"><div class="label">New signals</div><div class="value" style="color:var(--blue)">${stats.new_signals_today}</div></div>
    <div class="stat-card"><div class="label">Industries</div><div class="value">${stats.industries_affected}</div></div>
  `;
  const list = document.getElementById('signal-list');
  const hasTooltips = typeof infoIcon === 'function' && typeof TOOLTIP_TEXT !== 'undefined';

  // Dashboard only teases the top 5 signals; the full list lives on
  // bills/index.html via the "View all bills" link.
  const DASHBOARD_SIGNAL_LIMIT = 5;
  const topSignals = [...data.signals]
    .sort((a, b) => b.impact_score - a.impact_score)
    .slice(0, DASHBOARD_SIGNAL_LIMIT);

  list.innerHTML = topSignals.map(s => {
    const companies = Array.isArray(s.companies) ? s.companies : [];
    const direction = s.direction || 'mixed';
    const probIcon = hasTooltips ? signalInfoIcon(direction, s.passage_probability) : '';
    return `
    <a class="signal-card ${direction}" href="bills/detail.html?id=${s.bill_id}">
      <div class="signal-head">
        <span>${s.bill_id} — ${s.title}</span>
        <span class="prob ${direction}">${direction.toUpperCase()} ${s.passage_probability}%${probIcon}</span>
      </div>
      <div class="signal-sub">${s.industry} · impact ${s.impact_score}/100 · ${companies.map(c => c.ticker).join(', ')}</div>
    </a>
  `;
  }).join('');
  const tickers = {};
  data.signals.forEach(s => (Array.isArray(s.companies) ? s.companies : []).forEach(c => { tickers[c.ticker] = c; }));
  const watchlist = document.getElementById('watchlist');
  const exposureNote = hasTooltips
    ? `<div style="font-size:11px; color:var(--text-muted); margin-bottom:6px;">Exposure score, not price change${exposureInfoIcon()}</div>`
    : `<div style="font-size:11px; color:var(--text-muted); margin-bottom:6px;">Exposure score, not price change</div>`;
  watchlist.innerHTML = exposureNote + Object.values(tickers).slice(0, 6).map(c => `
    <div class="ticker-row">
      <span>${c.ticker}</span>
      <span class="change">${exposureSquare(c.exposure)}<span class="exposure-num">${c.exposure}</span></span>
    </div>
  `).join('');
  if (hasTooltips) initInfoTooltips();
}
loadDashboard().catch(err => {
  console.error('Failed to load dashboard data:', err);
  document.getElementById('signal-list').textContent = 'Could not load signal data.';
});
