async function fetchSiteData() {
  const res = await fetch('../data/bills.json');
  if (!res.ok) throw new Error('Failed to load data/bills.json');
  return res.json();
}

function renderSidebar(activePage) {
  const items = [
    { href: '../index.html', label: 'Dashboard', key: 'dashboard' },
    { href: '../bills/index.html', label: 'Bills', key: 'bills' },
    { href: '../industries/index.html', label: 'Industries', key: 'industries' },
    { href: '../companies/index.html', label: 'Companies', key: 'companies' },
    { href: '../signals/index.html', label: 'Signals', key: 'signals' },
    { href: '../watchlist/index.html', label: 'Watchlist', key: 'watchlist' },
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

const TOOLTIP_TEXT = {
  probability: "Passage probability is a rough estimate based on the bill's current stage (introduced, passed committee, etc.) and cosponsor count. It is not a calibrated prediction.",
  direction: "Direction (positive/negative/mixed) is Claude's read of the bill's summary text, judging who it likely helps or hurts. It's a qualitative AI assessment, not a market signal.",
  exposure: "Exposure score (0-100) reflects how directly this company's business is affected by the bill, as assessed by Claude. It is not a stock price or performance change."
};

function initInfoTooltips() {
  document.querySelectorAll('.info-icon').forEach(icon => {
    if (icon.dataset.wired) return;
    icon.dataset.wired = '1';
    icon.addEventListener('click', (e) => {
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
