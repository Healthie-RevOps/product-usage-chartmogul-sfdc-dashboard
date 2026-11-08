/* Enterprise add-ons: filters -> per-add-on adoption summary -> account x add-on matrix
 * -> CSVs. All data text goes into the DOM via textContent. */
(function () {
  const $ = id => document.getElementById(id);
  const { fmt } = Charts;
  const ADDONS = JSON.parse($('addons-data').textContent);
  const ADDON_LABEL = Object.fromEntries(ADDONS.map(a => [a.key, a.label]));
  const STATES = ['has', 'pipeline', 'lost', 'none'];
  const STATE_LABEL = { has: 'Has it', pipeline: 'In pipeline', lost: 'Lost', none: 'Not pitched', not_has: "Doesn't have it" };
  const SALESFORCE = window.LIGHTNING_BASE;

  const url = new URLSearchParams(location.search);
  const state = {
    paying: url.get('paying') !== '0',
    status: url.get('status') || '', csm: url.get('csm') || '', segment: url.get('segment') || '',
    impl: url.get('impl') || '', q: url.get('q') || '',
    addons: Object.fromEntries(url.getAll('addon').map(s => s.split(':')).filter(([k, v]) => ADDON_LABEL[k] && STATE_LABEL[v])),
    rows: [], summary: [], totals: null, facetsLoaded: false,
    sort: { key: 'mrr', dir: -1 }, selected: new Set(),
  };

  function h(tag, cls, text) {
    const el = document.createElement(tag);
    if (cls) el.className = cls;
    if (text != null) el.textContent = text;
    return el;
  }
  const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
  const money = dollars => dollars == null ? '—' : fmt.money(dollars * 100);

  function params() {
    const p = new URLSearchParams();
    if (!state.paying) p.set('paying', '0');
    ['status', 'csm', 'segment', 'impl', 'q'].forEach(k => { if (state[k]) p.set(k, state[k]); });
    Object.entries(state.addons).forEach(([k, v]) => p.append('addon', `${k}:${v}`));
    return p;
  }
  function syncUrl() {
    const qs = params().toString();
    history.replaceState(null, '', qs ? '?' + qs : location.pathname);
    const any = !state.paying || state.status || state.csm || state.segment || state.impl || state.q || Object.keys(state.addons).length;
    $('f-clear').hidden = !any;
  }

  /* ---------------------------------------------------------------- data */
  let seq = 0;
  async function load() {
    syncUrl();
    const my = ++seq;
    const p = params(); p.set('paying', state.paying ? 'true' : 'false');
    $('matrix-card').classList.add('loading'); $('summary-card').classList.add('loading');
    try {
      const r = await fetch('/api/enterprise?' + p).then(r => r.json());
      if (my !== seq) return;
      if (!App.dataState(r, 'Enterprise data')) return;
      state.rows = r.rows; state.summary = r.summary; state.totals = r.totals;
      const ids = new Set(r.rows.map(x => x.id));
      [...state.selected].forEach(id => { if (!ids.has(id)) state.selected.delete(id); });
      if (!state.facetsLoaded) { fillFacets(r.facets); state.facetsLoaded = true; }
      renderChips(); renderKpis(); renderSummary(); renderMatrix();
    } finally {
      if (my === seq) { $('matrix-card').classList.remove('loading'); $('summary-card').classList.remove('loading'); }
    }
  }

  function fillFacets(f) {
    [['f-status', 'status'], ['f-csm', 'csm'], ['f-segment', 'segment'], ['f-impl', 'impl']].forEach(([id, k]) => {
      const sel = $(id);
      f[k].forEach(v => { const o = h('option', null, v); o.value = v; sel.appendChild(o); });
      sel.value = state[k];
    });
  }

  /* ---------------------------------------------------------------- filters */
  function setSeg() {
    $('paying-seg').querySelectorAll('button').forEach(b => b.setAttribute('aria-pressed', (b.dataset.paying === '1') === state.paying));
  }
  $('paying-seg').addEventListener('click', e => {
    const b = e.target.closest('button'); if (!b) return;
    state.paying = b.dataset.paying === '1'; setSeg(); load();
  });
  [['f-status', 'status'], ['f-csm', 'csm'], ['f-segment', 'segment'], ['f-impl', 'impl']].forEach(([id, k]) =>
    $(id).addEventListener('change', e => { state[k] = e.target.value; load(); }));
  $('f-q').addEventListener('input', debounce(e => { state.q = e.target.value.trim(); load(); }, 300));
  $('f-clear').addEventListener('click', () => {
    Object.assign(state, { paying: true, status: '', csm: '', segment: '', impl: '', q: '', addons: {} });
    ['f-status', 'f-csm', 'f-segment', 'f-impl', 'f-q'].forEach(id => { $(id).value = ''; });
    setSeg(); load();
  });
  function toggleAddonFilter(key, st) {
    if (state.addons[key] === st) delete state.addons[key]; else state.addons[key] = st;
    load();
  }
  function renderChips() {
    const el = $('addon-chips');
    el.textContent = '';
    Object.entries(state.addons).forEach(([k, v]) => {
      const c = h('span', 'pchip');
      c.appendChild(document.createTextNode(`${ADDON_LABEL[k]}: ${STATE_LABEL[v]}`));
      const x = h('button', null, '×'); x.setAttribute('aria-label', `Remove filter ${ADDON_LABEL[k]}`);
      x.addEventListener('click', () => { delete state.addons[k]; load(); });
      c.appendChild(x); el.appendChild(c);
    });
  }

  /* ---------------------------------------------------------------- KPIs */
  function renderKpis() {
    const t = state.totals, el = $('kpis');
    el.textContent = '';
    [
      ['Accounts', fmt.int(t.accounts), state.paying ? 'paying Enterprise accounts' : 'Enterprise accounts', true],
      ['MRR', money(t.mrr), 'Salesforce MRR'],
      ['Open add-on deals', fmt.int(t.pipeline), money(t.pipeline_amount) + ' in pipeline'],
      ['Lost add-on deals', fmt.int(t.lost), 'most recent deal was lost'],
    ].forEach(([label, value, note, primary]) => {
      const k = h('div', 'kpi' + (primary ? ' primary' : ''));
      k.append(h('div', 'label', label), h('div', 'value', value), h('div', 'note', note));
      el.appendChild(k);
    });
  }

  /* ---------------------------------------------------------------- summary */
  (function legend() {
    const el = $('state-legend');
    STATES.forEach(s => {
      const span = h('span'); const i = h('i', 'box st-swatch st-' + s);
      span.append(i, document.createTextNode(STATE_LABEL[s])); el.appendChild(span);
    });
  })();

  function renderSummary() {
    const body = $('summary-body');
    body.textContent = '';
    state.summary.forEach(s => {
      const tr = h('tr');
      const name = h('td'); name.appendChild(h('span', 'strong', s.label));
      if (!s.has_flag) { const t = h('div', 'small muted', 'no Salesforce flag; billing from ChartMogul'); name.appendChild(t); }
      tr.appendChild(name);
      const total = STATES.reduce((n, st) => n + s.counts[st], 0);
      const barTd = h('td');
      const bar = h('div', 'segbar');
      bar.setAttribute('role', 'img');
      bar.setAttribute('aria-label', `${s.label}: ` + STATES.map(st => `${STATE_LABEL[st]} ${s.counts[st]}`).join(', '));
      STATES.forEach(st => {
        if (!s.counts[st]) return;
        const seg = h('span', 'st-' + st);
        seg.style.flexGrow = s.counts[st];
        seg.title = `${STATE_LABEL[st]}: ${fmt.int(s.counts[st])} of ${fmt.int(total)} (${Math.round(100 * s.counts[st] / total)}%)`;
        bar.appendChild(seg);
      });
      barTd.appendChild(bar); tr.appendChild(barTd);
      STATES.forEach(st => {
        const td = h('td', 'num');
        const b = h('button', 'countbtn', fmt.int(s.counts[st]));
        const on = state.addons[s.key] === st;
        b.setAttribute('aria-pressed', on);
        b.title = on ? 'Clear this filter' : `Show accounts where ${s.label} is “${STATE_LABEL[st]}”`;
        b.disabled = !s.counts[st] && !on;
        b.addEventListener('click', () => toggleAddonFilter(s.key, st));
        td.appendChild(b); tr.appendChild(td);
      });
      tr.appendChild(h('td', 'num', s.pipeline_amount ? money(s.pipeline_amount) : '—'));
      body.appendChild(tr);
    });
  }

  /* ---------------------------------------------------------------- matrix */
  const sortVal = {
    name: r => (r.name || '').toLowerCase(), mrr: r => r.mrr || 0,
    renewal: r => r.renewal_date || '9999', providers: r => r.providers == null ? -1 : r.providers,
  };
  ADDONS.forEach(a => { sortVal['a:' + a.key] = r => STATES.indexOf(r.addons[a.key].state); });

  (function head() {
    const tr = $('matrix-head');
    const chk = h('th', 'check'); const all = h('input'); all.type = 'checkbox'; all.id = 'check-all';
    all.setAttribute('aria-label', 'Select all shown accounts'); chk.appendChild(all); tr.appendChild(chk);
    [['name', 'Account', ''], ['mrr', 'MRR', 'num'], ['renewal', 'Renewal', ''], ['providers', 'Providers', 'num']].forEach(([k, label, cls]) => {
      const th = h('th', 'sortable ' + cls, label); th.dataset.sort = k; tr.appendChild(th);
    });
    ADDONS.forEach(a => { const th = h('th', 'sortable addon', a.label); th.dataset.sort = 'a:' + a.key; tr.appendChild(th); });
    tr.querySelectorAll('th.sortable').forEach(th => th.addEventListener('click', () => {
      const k = th.dataset.sort;
      state.sort = { key: k, dir: state.sort.key === k ? -state.sort.dir : (k === 'name' || k === 'renewal' || k.startsWith('a:') ? 1 : -1) };
      renderMatrix();
    }));
    all.addEventListener('change', e => {
      if (e.target.checked) state.rows.forEach(r => state.selected.add(r.id)); else state.selected.clear();
      renderMatrix();
    });
  })();

  function cellTitle(label, c) {
    const lines = [`${label}: ${STATE_LABEL[c.state]}`];
    if (c.source) lines.push(`Source: ${c.source}`);
    if (c.opp_name) lines.push(`Deal: ${c.opp_name}`);
    if (c.stage && c.state !== 'lost') lines.push(`Stage: ${c.stage}${c.probability != null ? ` (${Math.round(c.probability)}%)` : ''}`);
    if (c.close_date) lines.push(`${c.state === 'pipeline' ? 'Expected close' : 'Closed'}: ${c.close_date}`);
    if (c.amount) lines.push(`Amount: ${money(c.amount)}`);
    if (c.arr_delta) lines.push(`ARR change: ${money(c.arr_delta)}`);
    if (c.reason) lines.push(`Loss reason: ${c.reason}`);
    if (c.competitor) lines.push(`Competitor: ${c.competitor}`);
    if (c.target_price) lines.push(`Customer target price: ${money(c.target_price)}`);
    if (c.value_gap) lines.push(`Missing: ${c.value_gap}`);
    if (c.revisit_date) lines.push(`Revisit: ${c.revisit_date}`);
    if (c.owner) lines.push(`Owner: ${c.owner}`);
    if (c.note) lines.push(c.note);
    if (c.deals > 1) lines.push(`${c.deals} deals for this add-on`);
    return lines.join('\n');
  }

  function cellEl(label, c) {
    const text = c.state === 'has' ? 'Has it' : c.state === 'pipeline' ? (c.stage || 'Open')
      : c.state === 'lost' ? 'Lost' + (c.reason ? ' · ' + c.reason : '') : '—';
    const el = c.opp_id && c.state !== 'has' ? h('a', 'st st-' + c.state) : h('span', 'st st-' + c.state);
    if (el.tagName === 'A') {
      el.href = `${SALESFORCE}/lightning/r/Opportunity/${encodeURIComponent(c.opp_id)}/view`;
      el.target = '_blank'; el.rel = 'noopener';
    }
    if (c.state !== 'none') el.appendChild(h('i'));
    el.appendChild(h('span', 'st-text', text));
    if (c.state === 'lost' && c.revisit_date) el.appendChild(h('span', 'st-flag', '↻'));
    el.title = cellTitle(label, c);
    return el;
  }

  function renderMatrix() {
    const { key, dir } = state.sort, f = sortVal[key];
    const rows = state.rows.slice().sort((a, b) => { const x = f(a), y = f(b); return x < y ? -dir : x > y ? dir : (b.mrr || 0) - (a.mrr || 0); });
    document.querySelectorAll('#matrix-head th.sortable').forEach(th => {
      th.querySelector('.arrow')?.remove();
      if (th.dataset.sort === key) th.appendChild(h('span', 'arrow', dir > 0 ? ' ▲' : ' ▼'));
    });
    const body = $('matrix-body');
    body.textContent = '';
    rows.forEach(r => {
      const tr = h('tr');
      const sel = state.selected.has(r.id);
      if (sel) tr.classList.add('selected');
      const cb = h('input'); cb.type = 'checkbox'; cb.checked = sel; cb.setAttribute('aria-label', 'Select ' + r.name);
      cb.addEventListener('change', () => {
        cb.checked ? state.selected.add(r.id) : state.selected.delete(r.id);
        tr.classList.toggle('selected', cb.checked); selectionChanged();
      });
      const tdc = h('td', 'check'); tdc.appendChild(cb); tr.appendChild(tdc);

      const name = h('td', 'acct');
      const a = h('a', 'strong', r.name || r.sfdc_name);
      if (r.customer_uuid) a.href = '/customer/' + encodeURIComponent(r.customer_uuid);
      else { a.href = `${SALESFORCE}/lightning/r/Account/${encodeURIComponent(r.id)}/view`; a.target = '_blank'; a.rel = 'noopener'; }
      name.appendChild(a);
      const subText = [r.csm && `CSM ${r.csm}`, r.segment, r.implementation_status].filter(Boolean).join(' · ');
      const sub = h('div', 'small muted');
      if (r.customer_status && r.customer_status !== 'Customer') sub.appendChild(h('span', 'tag former', r.customer_status));
      if (subText) sub.appendChild(document.createTextNode(subText));
      if (sub.childNodes.length) name.appendChild(sub);
      tr.appendChild(name);
      tr.appendChild(h('td', 'num', money(r.mrr)));
      tr.appendChild(h('td', 'small date', r.renewal_date || '—'));
      const prov = h('td', 'num', r.providers == null ? '—' : fmt.int(r.providers));
      if (r.providers == null) prov.title = r.org_id ? 'No Healthie product data for this org' : 'No Healthie Org ID on the Salesforce account';
      tr.appendChild(prov);
      ADDONS.forEach(ad => { const td = h('td', 'cell'); td.appendChild(cellEl(ad.label, r.addons[ad.key])); tr.appendChild(td); });
      body.appendChild(tr);
    });
    if (!rows.length) {
      const tr = h('tr'); const td = h('td', 'empty', 'No accounts match these filters.'); td.colSpan = 5 + ADDONS.length;
      tr.appendChild(td); body.appendChild(tr);
    }
    $('acct-count').textContent = fmt.int(rows.length);
    selectionChanged();
  }

  function selectionChanged() {
    const n = state.selected.size;
    const all = state.rows.length && state.rows.every(r => state.selected.has(r.id));
    $('check-all').checked = !!all;
    $('check-all').indeterminate = !all && n > 0;
    $('selbar').hidden = n === 0;
    $('selbar-text').textContent = `${fmt.int(n)} account${n === 1 ? '' : 's'} selected`;
    renderScope();
  }
  function renderScope() {
    const el = $('use-scope');
    el.textContent = '';
    const n = state.selected.size;
    const filters = Object.entries(state.addons).map(([k, v]) => `${ADDON_LABEL[k]}: ${STATE_LABEL[v]}`);
    ['csm', 'segment', 'impl', 'status'].forEach(k => { if (state[k]) filters.push(state[k]); });
    if (state.q) filters.push(`“${state.q}”`);
    el.append('Downloads will cover ');
    el.appendChild(h('b', null, n ? `the ${fmt.int(n)} account${n === 1 ? '' : 's'} you ticked` : `all ${fmt.int(state.rows.length)} accounts in step 3`));
    el.append(filters.length && !n ? ` (${filters.join(' · ')}).` : '.');
    if (!n) el.append(' Tick accounts in step 3 to narrow it.');
  }
  $('selbar-clear').addEventListener('click', () => { state.selected.clear(); renderMatrix(); });

  /* ---------------------------------------------------------------- exports */
  function spec(useSelection) {
    return {
      paying: state.paying, status: state.status, csm: state.csm, segment: state.segment,
      impl: state.impl, q: state.q, addons: Object.entries(state.addons).map(([k, v]) => `${k}:${v}`),
      account_ids: useSelection && state.selected.size ? [...state.selected] : null,
    };
  }
  function exportCsv(path, s) {
    $('export-spec').value = JSON.stringify(s);
    const f = $('export-form'); f.action = path; f.submit();
  }
  $('export-accounts').addEventListener('click', () => exportCsv('/export/enterprise.csv', spec(true)));
  $('export-contacts').addEventListener('click', () => exportCsv('/export/enterprise-contacts.csv', spec(true)));
  $('selbar-accounts').addEventListener('click', () => exportCsv('/export/enterprise.csv', spec(true)));
  $('selbar-contacts').addEventListener('click', () => exportCsv('/export/enterprise-contacts.csv', spec(true)));

  /* ---------------------------------------------------------------- boot */
  $('f-q').value = state.q;
  setSeg();
  load();
})();
