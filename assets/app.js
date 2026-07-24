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
  list.innerHTML = data.signals.map(s => {
    const companies = Array.isArray(s.companies) ? s.companies : [];
    const direction = s.direction || 'mixed';
    const probIcon = hasTooltips ? infoIcon(TOOLTIP_TEXT.direction + ' ' + TOOLTIP_TEXT.probability) : '';
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
    ? `<div style="font-size:11px; color:var(--text-muted); margin-bottom:6px;">Exposure score, not price change${infoIcon(TOOLTIP_TEXT.exposure)}</div>`
    : `<div style="font-size:11px; color:var(--text-muted); margin-bottom:6px;">Exposure score, not price change</div>`;
  watchlist.innerHTML = exposureNote + Object.values(tickers).slice(0, 6).map(c => `
    <div class="ticker-row">
      <span>${c.ticker}</span>
      <span class="change ${c.effect || 'mixed'}">${c.exposure}</span>
    </div>
  `).join('');

  if (hasTooltips) initInfoTooltips();
}

loadDashboard().catch(err => {
  console.error('Failed to load dashboard data:', err);
  document.getElementById('signal-list').textContent = 'Could not load signal data.';
});
