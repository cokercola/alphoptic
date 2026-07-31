async function fetchSiteData() {
  const res = await fetch('/data/bills.json');
  if (!res.ok) throw new Error('Failed to load /data/bills.json');
  return res.json();
}
function renderSidebar(activePage) {
  const items = [
    { href: '/index.html', label: 'Dashboard', key: 'dashboard' },
    { href: '/bills/index.html', label: 'Bills', key: 'bills' },
    { href: '/industries/index.html', label: 'Industries', key: 'industries' },
    { href: '/companies/index.html', label: 'Companies', key: 'companies' },
    { href: '/signals/index.html', label: 'Signals', key: 'signals' },
    { href: '/trades/index.html', label: 'Trades', key: 'trades' },
    { href: '/watchlist/index.html', label: 'Watchlist', key: 'watchlist' },
  ];
  const nav = document.getElementById('sidebar');
  nav.innerHTML = items.map(i =>
    `<a href="${i.href}" class="${i.key === activePage ? 'active' : ''}">${i.label}</a>`
  ).join('');
}
function directionPill(direction) {
  return `<span class="pill ${direction}">${direction.toUpperCase()}</span>`;
}
function infoIcon(text) {
  return `<span class="info-icon">i<span class="tooltip-popover">${text}</span></span>`;
}

// Dedicated icon for signal cards: a colored, bulleted breakdown of
// direction + passage probability, instead of one run-on paragraph.
// The direction bullet's dot/label color matches the card's own
// direction color (green/red/amber) so it stays visually consistent
// with the rest of the UI.
const DIRECTION_COLOR_VAR = {
  positive: 'var(--green)',
  negative: 'var(--red)',
  mixed: 'var(--amber)',
};

function signalInfoIcon(direction, probability) {
  const color = DIRECTION_COLOR_VAR[direction] || 'var(--amber)';
  const label = direction.charAt(0).toUpperCase() + direction.slice(1);
  const html = `
    <div class="tooltip-label">What this means</div>
    <div class="tooltip-bullet">
      <span class="tooltip-dot" style="background:${color}"></span>
      <span><strong style="color:${color}">${label}</strong> — ${TOOLTIP_TEXT.direction}</span>
    </div>
    <div class="tooltip-bullet">
      <span class="tooltip-dot" style="background:var(--blue)"></span>
      <span><strong style="color:var(--blue)">${probability}% passage probability</strong> — ${TOOLTIP_TEXT.probability}</span>
    </div>
  `;
  return `<span class="info-icon">i<span class="tooltip-popover tooltip-popover-wide">${html}</span></span>`;
}

const TOOLTIP_TEXT = {
  probability: "a rough estimate based on the bill's current stage and cosponsor count. Not a calibrated prediction.",
  direction: "Claude's read of the bill's summary text, judging who it likely helps or hurts. A qualitative AI assessment, not a market signal.",
  exposure: "Exposure score (0-100) reflects how directly this company's business is affected by the bill, as assessed by Claude. It is not a stock price or performance change."
};

function initInfoTooltips() {
  document.querySelectorAll('.info-icon').forEach(icon => {
    if (icon.dataset.wired) return;
    icon.dataset.wired = '1';
    icon.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      const popover = icon.querySelector('.tooltip-popover');
      document.querySelectorAll('.tooltip-popover.open').forEach(p => {
        if (p !== popover) p.classList.remove('open');
      });
      popover.classList.toggle('open');
    });
  });
  document.addEventListener('click', () => {
    document.querySelectorAll('.tooltip-popover.open').forEach(p => p.classList.remove('open'));
  });
}
