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
