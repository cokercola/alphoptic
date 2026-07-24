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
  list.innerHTML = data.signals.map(s => `
    <a class="signal-card ${s.direction}" href="bills/detail.html?id=${s.bill_id}">
      <div class="signal-head">
        <span>${s.bill_id} — ${s.title}</span>
        <span class="prob ${s.direction}">${s.direction.toUpperCase()} ${s.passage_probability}%${infoIcon(TOOLTIP_TEXT.direction + ' ' + TOOLTIP_TEXT.probability)}</span>
      </div>
      <div class="signal-sub">${s.industry} · impact ${s.impact_score}/100 · ${s.companies.map(c => c.ticker).join(', ')}</div>
    </a>
  `).join('');

  const tickers = {};
  data.signals.forEach(s => s.companies.forEach(c => { tickers[c.ticker] = c; }));
  const watchlist = document.getElementById('watchlist');
  watchlist.innerHTML = `
    <div style="font-size:11px; color:var(--text-muted); margin-bottom:6px;">
      Exposure score, not price change${infoIcon(TOOLTIP_TEXT.exposure)}
    </div>
  ` + Object.values(tickers).slice(0, 6).map(c => `
    <div class="ticker-row">
      <span>${c.ticker}</span>
      <span class="change ${c.effect}">${c.exposure}</span>
    </div>
  `).join('');

  initInfoTooltips();
}

loadDashboard().catch(err => {
  console.error('Failed to load dashboard data:', err);
  document.getElementById('signal-list').textContent = 'Could not load signal data.';
});
