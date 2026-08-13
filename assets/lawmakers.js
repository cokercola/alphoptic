/**
 * Lawmakers directory page: state dropdown -> lawmaker list -> detail
 * panel with sponsored/cosponsored bills. Loads three JSON files once
 * on page load and does all filtering/rendering client-side:
 *
 *   /data/lawmakers-index.json  - slim, one row per member (~535 rows)
 *   /data/lawmakers.json        - full detail, bill_id lists per member
 *   /data/bills-lookup.json     - title/stage lookup for those bill_ids
 *
 * bills-lookup.json is a purpose-built slim file (~1.5MB vs. the ~6MB
 * bills-index.json would cost) - see write_bills_lookup() in
 * fetch_bills.py. Its records are [title, stage, last_action_date]
 * arrays rather than {title:..., stage:..., ...} objects (skipping
 * repeated key names across 14,000+ bills is most of the size win),
 * so they get normalized into the same {title, stage, last_action_date}
 * shape as before immediately after fetching, below.
 */

// Mirrors STAGE_LABELS in scripts/fetch_bills.py - hardcoded here so this
// page doesn't need to fetch bills-index.json (a much bigger file) just
// to look up these six fixed labels. Keep in sync if that dict changes.
const STAGE_LABELS = {
  introduced: 'Introduced',
  committee: 'Committee action',
  passed_one_chamber: 'Passed one chamber',
  passed_both: 'Passed both chambers',
  became_law: 'Became law',
  failed_vetoed: 'Failed / vetoed',
};

const LAWMAKERS_VIEW_ALL_THRESHOLD = 5;

let lawmakersIndexById = new Map();   // bioguide_id -> slim row
let lawmakersDetailById = null;       // full lawmakers.json "lawmakers" object
let billLookupById = new Map();       // bill_id -> {title, stage, last_action_date}

async function initLawmakersPage() {
  const listEl = document.getElementById('lawmaker-list');
  const detailEl = document.getElementById('lawmaker-detail');
  const stateSelect = document.getElementById('state-select');

  try {
    const [indexRes, detailRes, lookupRes] = await Promise.all([
      fetch('/data/lawmakers-index.json'),
      fetch('/data/lawmakers.json'),
      fetch('/data/bills-lookup.json'),
    ]);
    if (!indexRes.ok) throw new Error('Failed to load lawmakers-index.json');
    if (!detailRes.ok) throw new Error('Failed to load lawmakers.json');
    if (!lookupRes.ok) throw new Error('Failed to load bills-lookup.json');

    const indexData = await indexRes.json();
    const detailData = await detailRes.json();
    const lookupData = await lookupRes.json();

    const updatedEl = document.getElementById('updated-at');
    if (updatedEl) {
      updatedEl.textContent = 'Updated ' + new Date(indexData.updated_at).toLocaleString();
    }

    (indexData.lawmakers || []).forEach(row => lawmakersIndexById.set(row.bioguide_id, row));
    lawmakersDetailById = detailData.lawmakers || {};
    // Each record is [title, stage, last_action_date] - normalize to the
    // same {title, stage, last_action_date} shape the rest of this file
    // already expects, so billRow()/sortByRecent() don't need to change.
    Object.entries(lookupData.bills || {}).forEach(([billId, [title, stage, lastActionDate]]) => {
      billLookupById.set(billId, { title, stage, last_action_date: lastActionDate });
    });

    populateStateDropdown(indexData.lawmakers || []);
  } catch (err) {
    console.error(err);
    listEl.innerHTML = '<p class="empty-note">Could not load lawmakers data.</p>';
    return;
  }

  stateSelect.addEventListener('change', () => {
    renderLawmakerList(stateSelect.value);
    detailEl.innerHTML = '<p class="empty-note">Select a lawmaker to see the bills they\u2019ve sponsored and cosponsored.</p>';
  });

  // Deep-link support: /lawmakers/index.html?state=SC&id=S001234
  // or just ?id=S001234 on its own - state gets looked up from the
  // lawmaker's own record, so a link (e.g. a bill's sponsor name) only
  // needs to know a bioguideId, not which state that person represents.
  const params = new URLSearchParams(window.location.search);
  const preId = params.get('id');
  const preState = params.get('state') || (preId && (lawmakersIndexById.get(preId) || {}).state_code);
  if (preState) {
    stateSelect.value = preState;
    renderLawmakerList(preState);
    if (preId) {
      const item = listEl.querySelector(`.lawmaker-list-item[data-id="${preId}"]`);
      if (item) item.classList.add('active');
      renderLawmakerDetail(preId);
    }
  }
}

function populateStateDropdown(rows) {
  const stateSelect = document.getElementById('state-select');
  const seen = new Map(); // state_code -> state name
  rows.forEach(r => { if (r.state_code) seen.set(r.state_code, r.state); });
  const states = [...seen.entries()].sort((a, b) => a[1].localeCompare(b[1]));
  stateSelect.innerHTML = '<option value="">Select a state\u2026</option>' +
    states.map(([code, name]) => `<option value="${code}">${name}</option>`).join('');
}

function renderLawmakerList(stateCode) {
  const listEl = document.getElementById('lawmaker-list');
  if (!stateCode) {
    listEl.innerHTML = '<p class="empty-note">Choose a state to see its lawmakers.</p>';
    return;
  }
  const rows = [...lawmakersIndexById.values()]
    .filter(r => r.state_code === stateCode)
    .sort((a, b) => (a.chamber === b.chamber ? 0 : a.chamber === 'Senate' ? -1 : 1) || a.name.localeCompare(b.name));

  if (!rows.length) {
    listEl.innerHTML = '<p class="empty-note">No lawmakers found for this state.</p>';
    return;
  }

  listEl.innerHTML = rows.map(r => `
    <div class="lawmaker-list-item" data-id="${r.bioguide_id}">
      ${r.name}
      <div class="lawmaker-list-meta">${r.party_code} \u00b7 ${r.chamber}${r.district ? ' \u00b7 Dist. ' + r.district : ''}</div>
    </div>
  `).join('');

  listEl.querySelectorAll('.lawmaker-list-item').forEach(el => {
    el.addEventListener('click', () => {
      listEl.querySelectorAll('.lawmaker-list-item').forEach(i => i.classList.remove('active'));
      el.classList.add('active');
      renderLawmakerDetail(el.dataset.id);
    });
  });
}

function billRow(billId) {
  const bill = billLookupById.get(billId);
  if (!bill) {
    return `<tr><td colspan="2">${billId}</td></tr>`;
  }
  const label = STAGE_LABELS[bill.stage] || bill.stage;
  return `
    <tr>
      <td><a href="/bills/detail.html?id=${billId}">${billId}</a> \u2014 ${bill.title}</td>
      <td style="text-align:right">${stagePill(bill.stage, label)}</td>
    </tr>
  `;
}

function billListSection(sectionKey, title, billIds) {
  if (!billIds.length) {
    return `
      <div class="lawmaker-section-heading">${title} (0)</div>
      <p class="empty-note">None on record in tracked bills.</p>
    `;
  }
  const visible = billIds.slice(0, LAWMAKERS_VIEW_ALL_THRESHOLD);
  const rest = billIds.slice(LAWMAKERS_VIEW_ALL_THRESHOLD);

  return `
    <div class="lawmaker-section-heading">${title} (${billIds.length})</div>
    <table class="data-table">
      <tbody id="section-${sectionKey}-visible">${visible.map(billRow).join('')}</tbody>
      <tbody id="section-${sectionKey}-rest" style="display:none">${rest.map(billRow).join('')}</tbody>
    </table>
    ${rest.length ? `<button class="pagination-btn" id="section-${sectionKey}-toggle">View all ${billIds.length} \u2192</button>` : ''}
  `;
}

function renderLawmakerDetail(bioguideId) {
  const detailEl = document.getElementById('lawmaker-detail');
  const info = lawmakersIndexById.get(bioguideId);
  const detail = lawmakersDetailById ? lawmakersDetailById[bioguideId] : null;
  if (!info || !detail) {
    detailEl.innerHTML = '<p class="empty-note">Could not load this lawmaker\u2019s detail.</p>';
    return;
  }

  // Most-recent-first, using bills-index.json's last_action_date so the
  // capped preview shows what's currently moving, not just whatever
  // happened to be sponsored/cosponsored first.
  const sortByRecent = (ids) => [...ids].sort((a, b) => {
    const da = (billLookupById.get(a) || {}).last_action_date || '';
    const db = (billLookupById.get(b) || {}).last_action_date || '';
    return db.localeCompare(da);
  });

  detailEl.innerHTML = `
    <div style="margin-bottom:16px">
      <p style="font-weight:500;font-size:15px;margin:0">${info.name}</p>
      <p style="font-size:13px;color:var(--text-muted);margin:0">${info.state}${info.district ? ' \u00b7 District ' + info.district : ''} \u00b7 ${info.party}</p>
    </div>
    <div class="lawmaker-stats-grid">
      <div class="lawmaker-stat-card"><div class="lawmaker-stat-label">State</div><div class="lawmaker-stat-value">${info.state_code}${info.district ? '-' + info.district : ''}</div></div>
      <div class="lawmaker-stat-card"><div class="lawmaker-stat-label">Party</div><div class="lawmaker-stat-value">${info.party}</div></div>
      <div class="lawmaker-stat-card"><div class="lawmaker-stat-label">Chamber</div><div class="lawmaker-stat-value">${info.chamber}</div></div>
    </div>
    ${billListSection('sponsored', 'Bills sponsored', sortByRecent(detail.sponsored || []))}
    ${billListSection('cosponsored', 'Bills cosponsored', sortByRecent(detail.cosponsored || []))}
  `;

  ['sponsored', 'cosponsored'].forEach(key => {
    const btn = document.getElementById(`section-${key}-toggle`);
    if (!btn) return;
    btn.addEventListener('click', () => {
      document.getElementById(`section-${key}-rest`).style.display = 'table-row-group';
      btn.style.display = 'none';
    });
  });
}
