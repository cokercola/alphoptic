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
  const hasTooltips = typeof infoIcon === 'function' && typeof TOOLTIP_TEXT !== 'undefined';

  renderCommunityHighlight('community-highlight', data, 3);

  const tickers = {};
  data.signals.forEach(s => (Array.isArray(s.companies) ? s.companies : []).forEach(c => {
    if (!tickers[c.ticker]) tickers[c.ticker] = { ...c, industry: s.industry };
  }));
  const watchlist = document.getElementById('watchlist');
  watchlist.innerHTML = Object.values(tickers).slice(0, 6).map(c => `
    <div class="watch-row">
      <div>
        <div class="watch-ticker">${c.ticker}</div>
        <div class="watch-note">${c.industry || ''}</div>
      </div>
      <span class="exposure-pill ${c.effect || 'mixed'}">${c.exposure > 0 ? '+' : ''}${c.exposure} exposure</span>
    </div>
  `).join('') + '<div class="watchlist-footnote">Exposure is how much a bill could move this company, not a price prediction.</div>';
  if (hasTooltips) initInfoTooltips();
}
loadDashboard().catch(err => {
  console.error('Failed to load dashboard data:', err);
});
