async function loadDashboard() {
  const [billsRes, signalsRes] = await Promise.all([
    fetch('data/bills.json'),
    fetch('data/signals.json').catch(() => null),
  ]);
  const data = await billsRes.json();
  const signalsData = signalsRes && signalsRes.ok ? await signalsRes.json() : null;

  document.getElementById('updated-at').textContent =
    'Updated ' + new Date(data.updated_at).toLocaleString();
  const stats = data.summary;
  const wow = stats.week_over_week || {};

  // Industries flagged (30-day distinct-industry count from
  // signals.json) replaces the old bills.json-sourced "new signals
  // today" figure, which was actually counting classified bills this
  // run, not legislative signals. Falls back gracefully if
  // signals.json hasn't been regenerated with the new fields yet.
  const industriesFlagged = signalsData && signalsData.industries_flagged_month != null
    ? signalsData.industries_flagged_month
    : null;
  const industriesFlaggedDelta = signalsData && signalsData.industries_flagged_month_delta != null
    ? signalsData.industries_flagged_month_delta
    : null;

  document.getElementById('stat-row').innerHTML = `
    ${statCardHTML({
      href: '/bills/index.html',
      label: 'Bills tracked',
      value: stats.bills_tracked.toLocaleString(),
      delta: wow.bills_tracked,
      caption: 'this week',
    })}
    ${statCardHTML({
      href: '/bills/index.html?impact=high',
      label: 'High impact bills',
      value: stats.high_impact,
      valueColor: 'var(--orange)',
      delta: wow.high_impact,
      caption: 'this week',
    })}
    ${statCardHTML({
      href: '/signals/index.html',
      label: 'Industries flagged',
      value: industriesFlagged != null ? industriesFlagged : '—',
      delta: industriesFlaggedDelta,
      caption: 'this month',
    })}
    ${statCardHTML({
      href: '/industries/index.html',
      label: 'Industries tracked',
      value: stats.industries_affected,
      delta: wow.industries_affected,
      caption: 'this week',
    })}
  `;
  const hasTooltips = typeof infoIcon === 'function' && typeof TOOLTIP_TEXT !== 'undefined';

  renderCommunityHighlight('community-highlight', data, 3);

  const tickers = {};
  data.signals.forEach(s => (Array.isArray(s.companies) ? s.companies : []).forEach(c => {
    if (!tickers[c.ticker]) tickers[c.ticker] = { ...c, industry: s.industry };
  }));
  const watchlist = document.getElementById('watchlist');
  const signFor = (effect) => effect === 'negative' ? '-' : effect === 'positive' ? '+' : '±';
  watchlist.innerHTML = Object.values(tickers).slice(0, 6).map(c => `
    <div class="watch-row">
      <div>
        <div class="watch-ticker">${c.ticker}</div>
        <div class="watch-note">${c.industry || ''}</div>
      </div>
      <span class="exposure-pill ${c.effect || 'mixed'}">${signFor(c.effect)}${Math.abs(Number(c.exposure) || 0)} exposure</span>
    </div>
  `).join('') + '<div class="watchlist-footnote">Exposure scores how much a tracked bill could affect this company if passed — green means the bill would likely help it, red means it would likely hurt it, amber means the effect could cut either way. It\'s a legislative-impact estimate, not a stock price prediction.</div>';
  if (hasTooltips) initInfoTooltips();
}

function statCardHTML({ href, label, value, valueColor, delta, caption }) {
  const deltaHTML = (delta === null || delta === undefined)
    ? ''
    : `<span class="stat-delta ${delta > 0 ? 'up' : delta < 0 ? 'down' : 'flat'}">${delta > 0 ? '+' : delta < 0 ? '' : '±'}${delta}</span>`;
  return `
    <a class="stat-card" href="${href}">
      <div class="label">${label}</div>
      <div class="value-row">
        <span class="value"${valueColor ? ` style="color:${valueColor}"` : ''}>${value}</span>
        ${deltaHTML}
      </div>
      <div class="caption">${caption}</div>
    </a>
  `;
}

loadDashboard().catch(err => {
  console.error('Failed to load dashboard data:', err);
});
