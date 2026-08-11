async function fetchSiteData() {
  const res = await fetch('/data/bills.json');
  if (!res.ok) throw new Error('Failed to load /data/bills.json');
  return res.json();
}
function renderSidebar(activePage) {
  const items = [
    { href: '/index.html', label: 'Dashboard', key: 'dashboard' },
    { href: '/bills/index.html', label: 'Bills', key: 'bills' },
    { href: '/bills/index.html?stalled=1', label: 'Stalled', key: 'stalled' },
    { href: '/industries/index.html', label: 'Industries', key: 'industries' },
    { href: '/companies/index.html', label: 'Companies', key: 'companies' },
    { href: '/signals/index.html', label: 'Signals', key: 'signals' },
    { href: '/trades/index.html', label: 'Trades', key: 'trades' },
    { href: '/watchlist/index.html', label: 'Watchlist', key: 'watchlist' },
    { href: '/community/index.html', label: 'Community', key: 'community' },
  ];
  const nav = document.getElementById('sidebar');
  nav.innerHTML = items.map(i =>
    `<a href="${i.href}" class="${i.key === activePage ? 'active' : ''}">${i.label}</a>`
  ).join('');
}
function directionPill(direction) {
  const d = direction || 'unknown';
  return `<span class="pill ${d}">${d.toUpperCase()}</span>`;
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

// Stage colors for the Bills page breakdown bar/tabs. Deliberately
// distinct from the direction colors (green/red/amber) and from the
// exposure blue-intensity scale, to avoid a fourth color language
// clashing with the other three already in use across the site.
const STAGE_COLOR = {
  introduced: 'var(--border)',
  committee: 'rgba(90,155,255,0.5)',
  passed_one_chamber: 'var(--blue)',
  passed_both: 'var(--blue)',
  became_law: 'var(--green)',
  failed_vetoed: 'var(--red)',
};

function stagePill(stage, label) {
  const color = STAGE_COLOR[stage] || 'var(--border)';
  const bg = stage === 'introduced' ? 'rgba(39,66,100,0.6)'
    : stage === 'became_law' ? 'rgba(76,181,107,0.15)'
    : stage === 'failed_vetoed' ? 'rgba(224,82,82,0.15)'
    : 'rgba(90,155,255,0.15)';
  return `<span class="stage-pill" style="color:${color};background:${bg};">${label}</span>`;
}

function renderStageBreakdown(containerId, summary, onFilterChange) {
  const el = document.getElementById(containerId);
  if (!el || !summary || !Array.isArray(summary.stage_breakdown)) return;

  const bar = summary.stage_breakdown.map(s => {
    const total = summary.bills_tracked || 1;
    const pct = Math.max((s.count / total) * 100, s.count > 0 ? 2 : 0);
    return `<div style="background:${STAGE_COLOR[s.stage]};width:${pct}%;"></div>`;
  }).join('');

  const legend = summary.stage_breakdown.map(s => {
    const sub = s.full_coverage
      ? 'full coverage'
      : (summary.total_bills_this_congress
          ? `of ~${summary.total_bills_this_congress.toLocaleString()} total this Congress`
          : 'sampled');
    return `
      <div class="stage-legend-row">
        <div class="stage-legend-label">
          <span class="stage-dot" style="background:${STAGE_COLOR[s.stage]}"></span>
          ${s.label}
        </div>
        <div class="stage-legend-count">
          <div class="stage-legend-num">${s.count} tracked</div>
          <div class="stage-legend-sub">${sub}</div>
        </div>
      </div>`;
  }).join('');

  const tabs = ['<button class="stage-tab active" data-stage="all">All (' + summary.bills_tracked + ')</button>']
    .concat(summary.stage_breakdown
      .filter(s => s.count > 0)
      .map(s => `<button class="stage-tab" data-stage="${s.stage}">${s.label} (${s.count})</button>`))
    .join('');

  el.innerHTML = `
    <div class="stage-bar">${bar}</div>
    <div class="stage-legend">${legend}</div>
    <div class="stage-tabs">${tabs}</div>
  `;

  if (typeof onFilterChange === 'function') {
    el.querySelectorAll('.stage-tab').forEach(btn => {
      btn.addEventListener('click', () => {
        el.querySelectorAll('.stage-tab').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        onFilterChange(btn.dataset.stage);
      });
    });
  }
}
const TOOLTIP_TEXT = {
  probability: "a rough estimate based on the bill's current stage and cosponsor count. Not a calibrated prediction.",
  direction: "Claude's read of the bill's summary text, judging who it likely helps or hurts. A qualitative AI assessment, not a market signal.",
  exposure: "how directly this company's business is affected by the bill, as assessed by Claude. Not a stock price or performance change."
};

// Single-hue intensity scale for exposure score, deliberately NOT using
// green/red/amber (those are reserved for bill direction elsewhere on
// the site) since a company can have high exposure to a positive OR
// negative bill - reusing direction colors here would be misleading.
function exposureColor(score) {
  if (score >= 67) return 'var(--blue)';
  if (score >= 34) return 'rgba(90,155,255,0.55)';
  return 'var(--border)';
}

function exposureSquare(score) {
  return `<span class="exposure-square" style="background:${exposureColor(score)}"></span>`;
}

function exposureInfoIcon() {
  const html = `
    <div class="tooltip-label">What this means</div>
    <div class="tooltip-bullet">
      <span class="tooltip-dot" style="background:var(--blue)"></span>
      <span><strong style="color:var(--blue)">Exposure score (0–100)</strong> — ${TOOLTIP_TEXT.exposure}</span>
    </div>
    <div class="tooltip-legend">
      <span class="legend-item"><span class="legend-swatch" style="background:var(--border)"></span>0–33 low</span>
      <span class="legend-item"><span class="legend-swatch" style="background:rgba(90,155,255,0.55)"></span>34–66 medium</span>
      <span class="legend-item"><span class="legend-swatch" style="background:var(--blue)"></span>67–100 high</span>
    </div>
  `;
  return `<span class="info-icon">i<span class="tooltip-popover tooltip-popover-wide">${html}</span></span>`;
}

// Renders the full Community page: every category from
// summary.community_categories gets its own card, INCLUDING categories
// with zero bills right now (deliberate - see EMPTY_CATEGORY_TEXT).
const EMPTY_CATEGORY_TEXT = "No bills currently tracked in this category — coverage is still growing, check back soon.";

function renderCommunityPage(containerId, data) {
  const el = document.getElementById(containerId);
  if (!el) return;
  const categories = [...((data.summary && data.summary.community_categories) || [])]
    .sort((a, b) => a.label.localeCompare(b.label));
  const byCategory = {};
  data.bills.forEach(s => {
    if (s.community_category === 'none') return;
    (byCategory[s.community_category] = byCategory[s.community_category] || []).push(s);
  });

  const CAP = 10;
  el.innerHTML = categories.map(cat => {
    const bills = byCategory[cat.category] || [];
    const shown = bills.slice(0, CAP);
    const remaining = bills.length - shown.length;
    const body = shown.length
      ? shown.map(s => `
          <div class="community-bill-row">
            <div><a href="/bills/detail.html?id=${s.bill_id}">${s.bill_id}</a> — ${s.title}</div>
            <div class="community-bill-meta">${s.status || 'No status available'}</div>
          </div>
        `).join('')
      : `<div class="community-empty">${EMPTY_CATEGORY_TEXT}</div>`;
    const viewAll = remaining > 0
      ? `<div style="padding-top:8px;"><a href="/bills/index.html?category=${cat.category}" style="color:var(--blue); text-decoration:none; font-size:13px;">View all ${bills.length} →</a></div>`
      : '';

    return `
      <div class="industry-card">
        <div class="community-card-head">
          <h3>${cat.label}</h3>
          <span class="community-count">${cat.count} bill${cat.count === 1 ? '' : 's'}</span>
        </div>
        ${body}
        ${viewAll}
      </div>
    `;
  }).join('');
}

// Renders the small "Community impact" highlight box on the dashboard:
// up to `limit` bills with a non-"none" category. Since we're not
// tracking a real "date introduced" yet, this just takes the first
// `limit` qualifying bills in signals order (roughly recency-ordered,
// since auto-discovered bills are pulled sorted by updateDate desc).
function renderCommunityHighlight(containerId, data, limit) {
  const el = document.getElementById(containerId);
  if (!el) return;
  const qualifying = data.signals.filter(s => s.community_category && s.community_category !== 'none');
  if (!qualifying.length) {
    el.innerHTML = '';
    return;
  }
  const shown = qualifying.slice(0, limit || 3);

  el.innerHTML = `
    <div class="community-highlight">
      <div class="community-highlight-head">
        <span>Community impact</span>
        <a href="/community/index.html">View all →</a>
      </div>
      ${shown.map(s => `
        <div class="community-highlight-row">
          <div class="community-highlight-title"><a href="/bills/detail.html?id=${s.bill_id}" style="color:inherit; text-decoration:none;">${s.bill_id} — ${s.title}</a></div>
          <div class="community-highlight-meta"><span class="community-highlight-cat">${s.community_category_label}</span> · ${s.status || 'No status available'}</div>
        </div>
      `).join('')}
    </div>
  `;
}

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
