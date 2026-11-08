/* Win / loss: filters -> KPIs -> by competitor -> loss reasons -> deals -> CSV.
 * All data text goes into the DOM via textContent. */
(function () {
  const $ = id => document.getElementById(id);
  const { fmt } = Charts;
  const SALESFORCE = window.LIGHTNING_BASE;
  const DEFAULT_RT = 'Initial Sales Opportunity';   // same scope as the SFDC competitor report
  const RANGES = ['year', 'last_year', '12m', '90d', 'all', 'custom'];

  const url = new URLSearchParams(location.search);
  const state = {
    rt: url.has('rt') ? (url.get('rt') === 'all' ? '' : url.get('rt')) : DEFAULT_RT,
    range: RANGES.includes(url.get('range')) ? url.get('range') : 'year',
    from: url.get('from') || '', to: url.get('to') || '',
    outcome: ['won', 'lost', 'open'].includes(url.get('o')) ? url.get('o') : '',
    owner: url.get('owner') || '', incumbent: url.get('incumbent') || '',
    competitor: url.get('competitor') || '', reason: url.get('reason') || '', q: url.get('q') || '',
    data: null, facetsLoaded: false, sort: { key: 'close_date', dir: -1 },
  };

  function h(tag, cls, text) {
    const el = document.createElement(tag);
    if (cls) el.className = cls;
    if (text != null) el.textContent = text;
    return el;
  }
  const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
  const money = d => d == null ? '—' : fmt.money(d * 100);
  const pct = x => x == null ? '—' : Math.round(x * 100) + '%';
  const iso = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;

  function dates() {
    const now = new Date(), y = now.getFullYear();
    switch (state.range) {
      case 'year': return [`${y}-01-01`, `${y}-12-31`];
      case 'last_year': return [`${y - 1}-01-01`, `${y - 1}-12-31`];
      case '12m': { const d = new Date(now); d.setFullYear(y - 1); return [iso(d), iso(now)]; }
      case '90d': { const d = new Date(now); d.setDate(d.getDate() - 90); return [iso(d), iso(now)]; }
      case 'custom': return [state.from, state.to];
      default: return ['', ''];
    }
  }

  function apiParams() {
    const [from, to] = dates();
    const p = new URLSearchParams();
    if (state.rt) p.set('record_type', state.rt);
    if (from) p.set('date_from', from);
    if (to) p.set('date_to', to);
    if (state.outcome) p.set('outcome', state.outcome);
    ['owner', 'incumbent', 'competitor', 'reason', 'q'].forEach(k => { if (state[k]) p.set(k, state[k]); });
    return p;
  }
  function syncUrl() {
    const p = new URLSearchParams();
    if (state.rt !== DEFAULT_RT) p.set('rt', state.rt || 'all');
    if (state.range !== 'year') p.set('range', state.range);
    if (state.range === 'custom') { if (state.from) p.set('from', state.from); if (state.to) p.set('to', state.to); }
    if (state.outcome) p.set('o', state.outcome);
    ['owner', 'incumbent', 'competitor', 'reason', 'q'].forEach(k => { if (state[k]) p.set(k, state[k]); });
    const qs = p.toString();
    history.replaceState(null, '', qs ? '?' + qs : location.pathname);
    $('f-clear').hidden = !qs;
  }

  /* ---------------------------------------------------------------- data */
  let seq = 0;
  async function load() {
    syncUrl();
    const my = ++seq;
    ['comp-card', 'reason-card', 'deals-card'].forEach(id => $(id).classList.add('loading'));
    try {
      const d = await fetch('/api/deals?' + apiParams()).then(r => r.json());
      if (my !== seq) return;
      state.data = d;
      if (!state.facetsLoaded) { fillFacets(d.facets); state.facetsLoaded = true; }
      renderChips(); renderKpis(); renderCompetitors(); renderReasons(); renderDeals();
    } finally {
      if (my === seq) ['comp-card', 'reason-card', 'deals-card'].forEach(id => $(id).classList.remove('loading'));
    }
  }

  function fillFacets(f) {
    [['f-rt', 'record_type', 'rt'], ['f-owner', 'owner', 'owner'], ['f-incumbent', 'incumbent', 'incumbent']].forEach(([id, k, sk]) => {
      const sel = $(id);
      f[k].forEach(v => { const o = h('option', null, v); o.value = v; sel.appendChild(o); });
      sel.value = state[sk];
    });
  }

  /* ---------------------------------------------------------------- filters */
  function setSeg() {
    $('outcome-seg').querySelectorAll('button').forEach(b => b.setAttribute('aria-pressed', b.dataset.o === state.outcome));
  }
  $('outcome-seg').addEventListener('click', e => {
    const b = e.target.closest('button'); if (!b) return;
    state.outcome = b.dataset.o; setSeg(); load();
  });
  $('f-rt').addEventListener('change', e => { state.rt = e.target.value; load(); });
  $('f-owner').addEventListener('change', e => { state.owner = e.target.value; load(); });
  $('f-incumbent').addEventListener('change', e => { state.incumbent = e.target.value; load(); });
  $('f-range').addEventListener('change', e => {
    state.range = e.target.value;
    $('custom-dates').hidden = state.range !== 'custom';
    load();
  });
  $('f-from').addEventListener('change', e => { state.from = e.target.value; load(); });
  $('f-to').addEventListener('change', e => { state.to = e.target.value; load(); });
  $('f-q').addEventListener('input', debounce(e => { state.q = e.target.value.trim(); load(); }, 300));
  $('f-clear').addEventListener('click', () => {
    Object.assign(state, { rt: DEFAULT_RT, range: 'year', from: '', to: '', outcome: '', owner: '', incumbent: '', competitor: '', reason: '', q: '' });
    boot(); load();
  });

  function renderChips() {
    const el = $('chips');
    el.textContent = '';
    [['competitor', 'Competitor'], ['reason', 'Loss reason']].forEach(([k, label]) => {
      if (!state[k]) return;
      const c = h('span', 'pchip'); c.appendChild(document.createTextNode(`${label}: ${state[k]}`));
      const x = h('button', null, '×'); x.setAttribute('aria-label', `Remove ${label} filter`);
      x.addEventListener('click', () => { state[k] = ''; load(); });
      c.appendChild(x); el.appendChild(c);
    });
  }

  /* ---------------------------------------------------------------- KPIs */
  function renderKpis() {
    const t = state.data.totals, el = $('kpis');
    el.textContent = '';
    const tiles = [
      ['Win rate', pct(t.win_rate), `${fmt.int(t.won)} won · ${fmt.int(t.lost)} lost`],
      ['Won ACV', money(t.won_value), 'Year 1 ACV, won deals'],
      ['Lost ACV', money(t.lost_value), 'Year 1 ACV, lost deals'],
      ['Open pipeline', money(t.open_value), `${fmt.int(t.open)} open deals`],
      ['Competitor recorded', pct(t.competitor_recorded), 'of closed deals', t.competitor_recorded != null && t.competitor_recorded < 0.6],
      ['Loss reason recorded', pct(t.reason_recorded), 'of lost deals', t.reason_recorded != null && t.reason_recorded < 0.6],
    ];
    tiles.forEach(([label, value, note, warn]) => {
      const k = h('div', 'kpi' + (warn ? ' warn' : ''));
      k.append(h('div', 'label', label), h('div', 'value', value), h('div', 'note', note));
      el.appendChild(k);
    });
  }

  /* ---------------------------------------------------------------- breakdowns */
  function meter(frac, label) {
    const wrap = h('div', 'meter-row');
    const m = h('div', 'meter');
    m.setAttribute('role', 'img'); m.setAttribute('aria-label', label);
    if (frac != null) { const f = h('span'); f.style.width = Math.max(2, frac * 100) + '%'; m.appendChild(f); }
    wrap.append(m, h('span', 'meter-val', frac == null ? '—' : pct(frac)));
    return wrap;
  }
  function clickableRow(tr, key, value) {
    const on = state[key] === value;
    if (on) tr.classList.add('selected');
    tr.classList.add('clickable');
    tr.tabIndex = 0;
    tr.setAttribute('aria-pressed', on);
    const go = () => { state[key] = on ? '' : value; load(); };
    tr.addEventListener('click', go);
    tr.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); } });
  }

  function renderCompetitors() {
    const body = $('comp-body');
    body.textContent = '';
    state.data.competitors.forEach(g => {
      const tr = h('tr');
      clickableRow(tr, 'competitor', g.competitor);
      tr.appendChild(h('td', g.competitor === 'Not recorded' ? 'muted' : 'strong', g.competitor));
      tr.appendChild(h('td', 'num', fmt.int(g.won)));
      tr.appendChild(h('td', 'num', fmt.int(g.lost)));
      tr.appendChild(h('td', 'num', g.open ? fmt.int(g.open) : '—'));
      const wr = h('td'); wr.appendChild(meter(g.win_rate, `Win rate against ${g.competitor}: ${pct(g.win_rate)}`)); tr.appendChild(wr);
      tr.appendChild(h('td', 'num', money(g.won_value)));
      tr.appendChild(h('td', 'num', money(g.lost_value)));
      tr.appendChild(h('td', 'small', g.top_reasons.map(r => `${r.reason} (${r.count})`).join(' · ') || '—'));
      tr.appendChild(h('td', 'num', g.secondary ? fmt.int(g.secondary) : '—'));
      body.appendChild(tr);
    });
    if (!state.data.competitors.length) emptyRow(body, 9, 'No deals match these filters.');
  }

  function renderReasons() {
    const body = $('reason-body');
    body.textContent = '';
    state.data.reasons.forEach(g => {
      const tr = h('tr');
      if (g.reason !== 'Not recorded') clickableRow(tr, 'reason', g.reason);
      tr.appendChild(h('td', g.reason === 'Not recorded' ? 'muted' : 'strong', g.reason));
      const sh = h('td'); sh.appendChild(meter(g.share, `${g.reason}: ${pct(g.share)} of losses`)); tr.appendChild(sh);
      tr.appendChild(h('td', 'num', fmt.int(g.lost)));
      tr.appendChild(h('td', 'num', g.regrettable || g.non_regrettable ? `${fmt.int(g.regrettable)} / ${fmt.int(g.non_regrettable)}` : '—'));
      tr.appendChild(h('td', 'num', money(g.lost_value)));
      tr.appendChild(h('td', 'small', g.top_competitor || '—'));
      body.appendChild(tr);
    });
    if (!state.data.reasons.length) emptyRow(body, 6, 'No lost deals in view.');
  }

  function emptyRow(body, cols, text) {
    const tr = h('tr'); const td = h('td', 'empty', text); td.colSpan = cols; tr.appendChild(td); body.appendChild(tr);
  }

  /* ---------------------------------------------------------------- deals table */
  const sortVal = {
    name: r => (r.name || '').toLowerCase(), outcome: r => r.outcome, close_date: r => r.close_date || '',
    value: r => r.value || 0, competitor: r => r.competitor.toLowerCase(), owner: r => (r.owner || '').toLowerCase(),
  };
  document.querySelectorAll('#deals-table th.sortable').forEach(th => th.addEventListener('click', () => {
    const k = th.dataset.sort;
    state.sort = { key: k, dir: state.sort.key === k ? -state.sort.dir : (k === 'value' || k === 'close_date' ? -1 : 1) };
    renderDeals();
  }));

  function renderDeals() {
    const { key, dir } = state.sort, f = sortVal[key];
    const rows = state.data.rows.slice().sort((a, b) => { const x = f(a), y = f(b); return x < y ? -dir : x > y ? dir : 0; });
    document.querySelectorAll('#deals-table th.sortable').forEach(th => {
      th.querySelector('.arrow')?.remove();
      if (th.dataset.sort === key) th.appendChild(h('span', 'arrow', dir > 0 ? ' ▲' : ' ▼'));
    });
    const body = $('deals-body');
    body.textContent = '';
    rows.forEach(r => {
      const tr = h('tr');
      const td = h('td');
      const a = h('a', 'strong', r.name || '(no name)');
      a.href = `${SALESFORCE}/lightning/r/Opportunity/${encodeURIComponent(r.id)}/view`; a.target = '_blank'; a.rel = 'noopener';
      td.appendChild(a);
      if (r.account) {
        const sub = h('div', 'small');
        if (r.customer_uuid) { const ca = h('a', null, r.account); ca.href = '/customer/' + encodeURIComponent(r.customer_uuid); sub.appendChild(ca); }
        else { sub.classList.add('muted'); sub.textContent = r.account; }
        td.appendChild(sub);
      }
      tr.appendChild(td);
      tr.appendChild(h('td', 'small', r.record_type || '—'));
      const oc = h('td');
      oc.appendChild(h('span', 'tag' + (r.outcome === 'won' ? ' ok' : r.outcome === 'lost' ? ' former' : ''),
        r.outcome === 'open' ? (r.stage || 'Open') : r.outcome === 'won' ? 'Won' : 'Lost'));
      tr.appendChild(oc);
      tr.appendChild(h('td', 'small date', r.close_date || '—'));
      tr.appendChild(h('td', 'num', r.value ? money(r.value) : '—'));
      const comp = h('td', 'small' + (r.competitor === 'Not recorded' ? ' muted' : ''), r.competitor === 'Not recorded' ? '—' : r.competitor);
      if (r.secondary) comp.appendChild(h('div', 'muted', 'also ' + r.secondary));
      tr.appendChild(comp);
      tr.appendChild(h('td', 'small', r.incumbent || '—'));
      const rs = h('td', 'small');
      if (r.outcome === 'won' && r.why_won) { rs.textContent = r.why_won; rs.title = 'Why we won'; }
      else if (r.reason) {
        rs.textContent = r.reason;
        if (r.regrettable) rs.appendChild(h('span', 'tag' + (r.regrettable === 'regrettable' ? ' warn' : ' former'), r.regrettable));
        if (r.reason_detail) rs.title = r.reason_detail;
      } else rs.textContent = '—';
      tr.appendChild(rs);
      tr.appendChild(h('td', 'small', r.owner || '—'));
      body.appendChild(tr);
    });
    if (!rows.length) emptyRow(body, 9, 'No deals match these filters.');
    $('deal-count').textContent = fmt.int(state.data.total_rows);
    $('deals-note').textContent = state.data.total_rows > rows.length
      ? `Showing the ${fmt.int(rows.length)} most recent of ${fmt.int(state.data.total_rows)}. The CSV has all of them.` : '';
  }

  /* ---------------------------------------------------------------- export */
  $('export-deals').addEventListener('click', () => {
    const [from, to] = dates();
    $('export-spec').value = JSON.stringify({
      record_type: state.rt, date_from: from, date_to: to, outcome: state.outcome, owner: state.owner,
      competitor: state.competitor, reason: state.reason, incumbent: state.incumbent, q: state.q,
    });
    const f = $('export-form'); f.action = '/export/deals.csv'; f.submit();
  });

  /* ---------------------------------------------------------------- boot */
  function boot() {
    $('f-range').value = state.range; $('custom-dates').hidden = state.range !== 'custom';
    $('f-from').value = state.from; $('f-to').value = state.to; $('f-q').value = state.q;
    if (state.facetsLoaded) { $('f-rt').value = state.rt; $('f-owner').value = state.owner; $('f-incumbent').value = state.incumbent; }
    setSeg();
  }
  boot();
  load();
})();
