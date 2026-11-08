/* Enterprise accounts: completeness by field -> account table with inline Salesforce
 * edits (allowlisted people, allowlisted fields) -> CSV + change log.
 * All data text goes into the DOM via textContent. */
(function () {
  const $ = id => document.getElementById(id);
  const { fmt } = Charts;
  const FIELDS = JSON.parse($('fields-data').textContent);
  const FIELD = Object.fromEntries(FIELDS.map(f => [f.key, f]));
  const SALESFORCE = window.LIGHTNING_BASE;
  const IS_LABEL = { missing: 'missing', filled: 'filled in', yes: 'Yes', no: 'No' };

  const url = new URLSearchParams(location.search);
  const state = {
    paying: url.get('paying') !== '0', plan: url.get('plan') || 'Enterprise', status: url.get('status') || '', csm: url.get('csm') || '',
    segment: url.get('segment') || '', impl: url.get('impl') || '', q: url.get('q') || '',
    field: FIELD[url.get('field')] ? url.get('field') : '', is: IS_LABEL[url.get('is')] ? url.get('is') : '',
    rows: [], canEdit: !!window.CAN_EDIT, facetsLoaded: false, sort: { key: 'mrr', dir: -1 },
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

  function params() {
    const p = new URLSearchParams();
    if (!state.paying) p.set('paying', '0');
    if (state.plan !== 'Enterprise') p.set('plan', state.plan);
    ['status', 'csm', 'segment', 'impl', 'q', 'field', 'is'].forEach(k => { if (state[k]) p.set(k, state[k]); });
    return p;
  }
  function syncUrl() {
    const qs = params().toString();
    history.replaceState(null, '', qs ? '?' + qs : location.pathname);
    $('f-clear').hidden = !qs;
  }

  let seq = 0;
  async function load() {
    syncUrl();
    const my = ++seq;
    const p = params(); p.set('paying', state.paying ? 'true' : 'false'); p.set('plan', state.plan);
    $('table-card').classList.add('loading'); $('summary-card').classList.add('loading');
    try {
      const d = await fetch('/api/accounts?' + p).then(r => r.json());
      if (my !== seq) return;
      if (!App.dataState(d, 'Enterprise account data')) return;
      state.rows = d.rows; state.canEdit = d.can_edit; state.totalRows = d.total_rows;
      if (!state.facetsLoaded) {
        [['f-status', 'status'], ['f-csm', 'csm'], ['f-segment', 'segment'], ['f-impl', 'impl']].forEach(([id, k]) => {
          d.facets[k].forEach(v => { const o = h('option', null, v); o.value = v; $(id).appendChild(o); });
          $(id).value = state[k];
        });
        state.facetsLoaded = true;
      }
      renderChips(); renderSummary(d.summary); renderTable();
    } finally {
      if (my === seq) { $('table-card').classList.remove('loading'); $('summary-card').classList.remove('loading'); }
    }
  }

  /* ---------------------------------------------------------------- filters */
  function setSeg() {
    $('paying-seg').querySelectorAll('button').forEach(b => b.setAttribute('aria-pressed', (b.dataset.paying === '1') === state.paying));
  }
  $('paying-seg').addEventListener('click', e => { const b = e.target.closest('button'); if (!b) return; state.paying = b.dataset.paying === '1'; setSeg(); load(); });
  [['f-status', 'status'], ['f-csm', 'csm'], ['f-segment', 'segment'], ['f-impl', 'impl']].forEach(([id, k]) =>
    $(id).addEventListener('change', e => { state[k] = e.target.value; load(); }));
  $('f-q').addEventListener('input', debounce(e => { state.q = e.target.value.trim(); load(); }, 300));
  $('f-plan').addEventListener('change', e => { state.plan = e.target.value; load(); });
  $('f-clear').addEventListener('click', () => {
    Object.assign(state, { paying: true, plan: 'Enterprise', status: '', csm: '', segment: '', impl: '', q: '', field: '', is: '' });
    $('f-plan').value = 'Enterprise';
    ['f-status', 'f-csm', 'f-segment', 'f-impl', 'f-q'].forEach(id => { $(id).value = ''; });
    setSeg(); load();
  });
  function setFieldFilter(field, is) {
    if (state.field === field && state.is === is) { state.field = ''; state.is = ''; } else { state.field = field; state.is = is; }
    load();
  }
  function renderChips() {
    const el = $('chips'); el.textContent = '';
    if (!state.field) return;
    const c = h('span', 'pchip'); c.appendChild(document.createTextNode(`${FIELD[state.field].label}: ${IS_LABEL[state.is]}`));
    const x = h('button', null, '×'); x.setAttribute('aria-label', 'Remove field filter');
    x.addEventListener('click', () => { state.field = ''; state.is = ''; load(); });
    c.appendChild(x); el.appendChild(c);
  }

  /* ---------------------------------------------------------------- summary */
  function countButton(n, field, is, title) {
    const b = h('button', 'countbtn', fmt.int(n));
    const on = state.field === field && state.is === is;
    b.setAttribute('aria-pressed', on); b.title = on ? 'Clear this filter' : title;
    b.disabled = !n && !on;
    b.addEventListener('click', () => setFieldFilter(field, is));
    return b;
  }
  function renderSummary(summary) {
    const body = $('summary-body'); body.textContent = '';
    let group = null;
    summary.forEach(s => {
      if (s.group !== group) {
        group = s.group;
        const tr = h('tr', 'group-row'); const td = h('td', null, group === 'Account' ? 'Account basics' : 'References & marketing');
        td.colSpan = 5; tr.appendChild(td); body.appendChild(tr);
      }
      const tr = h('tr');
      tr.appendChild(h('td', 'strong', s.label));
      const m = h('td'); const row = h('div', 'meter-row'); const bar = h('div', 'meter');
      bar.setAttribute('role', 'img'); bar.setAttribute('aria-label', `${s.label}: ${pct(s.share)} filled in`);
      if (s.share != null) { const f = h('span'); f.style.width = Math.max(2, s.share * 100) + '%'; bar.appendChild(f); }
      row.append(bar, h('span', 'meter-val', pct(s.share))); m.appendChild(row); tr.appendChild(m);
      const miss = h('td', 'num'); miss.appendChild(countButton(s.missing, s.key, 'missing', `Show accounts missing ${s.label}`)); tr.appendChild(miss);
      const yes = h('td', 'num');
      if (s.kind === 'yesno' || s.kind === 'bool') yes.appendChild(countButton(s.yes, s.key, 'yes', `Show accounts where ${s.label} is Yes`));
      else yes.textContent = '—';
      tr.appendChild(yes);
      tr.appendChild(h('td', 'small', s.editable ? (state.canEdit ? '✎ Yes' : 'Yes (editors only)') : 'No, edit in Salesforce'));
      body.appendChild(tr);
    });
  }

  /* ---------------------------------------------------------------- table */
  const sortVal = { name: r => (r.name || '').toLowerCase(), mrr: r => r.mrr || 0, missing: r => r.missing };
  FIELDS.forEach(f => { sortVal['f:' + f.key] = r => (r.values[f.key] || '￿').toLowerCase(); });

  (function head() {
    const tr = $('acct-head');
    [['name', 'Account', ''], ['mrr', 'MRR', 'num'], ['missing', 'Missing', 'num']].forEach(([k, label, cls]) => {
      const th = h('th', 'sortable ' + cls, label); th.dataset.sort = k; tr.appendChild(th);
    });
    FIELDS.forEach(f => {
      const th = h('th', 'sortable addon', f.label + (f.editable && state.canEdit ? ' ✎' : ''));
      th.dataset.sort = 'f:' + f.key; tr.appendChild(th);
    });
    tr.querySelectorAll('th.sortable').forEach(th => th.addEventListener('click', () => {
      const k = th.dataset.sort;
      state.sort = { key: k, dir: state.sort.key === k ? -state.sort.dir : (k === 'mrr' || k === 'missing' ? -1 : 1) };
      renderTable();
    }));
  })();

  function editor(r, f) {
    const cur = r.values[f.key];
    const wrap = h('span', 'edit-cell');
    let input;
    if (f.kind === 'yesno') {
      input = h('select');
      [['', '—'], ['Yes', 'Yes'], ['No', 'No']].forEach(([v, t]) => { const o = h('option', null, t); o.value = v; input.appendChild(o); });
      input.value = cur || '';
    } else {
      input = h('input'); input.type = 'date'; input.value = cur || '';
    }
    input.setAttribute('aria-label', `${f.label} for ${r.name}`);
    const status = h('span', 'edit-status');
    input.addEventListener('change', async () => {
      const value = input.value || null;
      input.disabled = true; status.className = 'edit-status saving'; status.textContent = 'Saving…';
      try {
        const res = await fetch(`/api/accounts/${encodeURIComponent(r.id)}/fields`, {
          method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'fetch' },
          body: JSON.stringify({ field: f.key, value, expected: r.values[f.key] }),
        });
        const body = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(body.detail || `Error ${res.status}`);
        r.values[f.key] = body.value;
        input.value = body.value || '';
        if (body.status === 'conflict') {
          status.className = 'edit-status warn';
          status.textContent = 'Changed in Salesforce since the last sync; showing the current value. Nothing was saved.';
        } else {
          status.className = 'edit-status ok'; status.textContent = 'Saved';
          setTimeout(() => { if (status.textContent === 'Saved') status.textContent = ''; }, 2500);
        }
        cell(r, f, wrap.parentElement);
      } catch (err) {
        input.value = r.values[f.key] || '';
        status.className = 'edit-status err'; status.textContent = err.message;
      } finally { input.disabled = false; }
    });
    wrap.append(input, status);
    return wrap;
  }

  function cell(r, f, td) {
    td.classList.toggle('is-missing', r.values[f.key] == null);
    if (f.editable && state.canEdit) {
      if (!td.querySelector('.edit-cell')) { td.textContent = ''; td.appendChild(editor(r, f)); }
      return;
    }
    td.textContent = '';
    const v = r.values[f.key];
    td.appendChild(v == null ? h('span', 'missing', 'missing') : h('span', null, v));
  }

  function renderTable() {
    const { key, dir } = state.sort, fn = sortVal[key];
    const rows = state.rows.slice().sort((a, b) => { const x = fn(a), y = fn(b); return x < y ? -dir : x > y ? dir : (b.mrr || 0) - (a.mrr || 0); });
    document.querySelectorAll('#acct-head th.sortable').forEach(th => {
      th.querySelector('.arrow')?.remove();
      if (th.dataset.sort === key) th.appendChild(h('span', 'arrow', dir > 0 ? ' ▲' : ' ▼'));
    });
    const body = $('acct-body'); body.textContent = '';
    rows.forEach(r => {
      const tr = h('tr');
      const name = h('td', 'acct');
      const a = h('a', 'strong', r.name || r.sfdc_name);
      a.href = `${SALESFORCE}/lightning/r/Account/${encodeURIComponent(r.id)}/view`; a.target = '_blank'; a.rel = 'noopener';
      a.title = 'Open in Salesforce';
      name.appendChild(a);
      if (r.customer_uuid) { const c = h('a', 'small', 'customer page'); c.href = '/customer/' + encodeURIComponent(r.customer_uuid); const d = h('div'); d.appendChild(c); name.appendChild(d); }
      tr.appendChild(name);
      tr.appendChild(h('td', 'num', money(r.mrr)));
      tr.appendChild(h('td', 'num', fmt.int(r.missing)));
      FIELDS.forEach(f => { const td = h('td', 'cell small'); cell(r, f, td); tr.appendChild(td); });
      body.appendChild(tr);
    });
    if (!rows.length) { const tr = h('tr'); const td = h('td', 'empty', 'No accounts match these filters.'); td.colSpan = 3 + FIELDS.length; tr.appendChild(td); body.appendChild(tr); }
    $('acct-count').textContent = fmt.int(state.totalRows ?? rows.length);
    $('table-note').textContent = (state.totalRows || 0) > rows.length ? `Showing the top ${fmt.int(rows.length)} of ${fmt.int(state.totalRows)} by MRR. Narrow the filters, or download the CSV for all of them.` : '';
  }

  /* ---------------------------------------------------------------- export + log */
  $('export-accounts').addEventListener('click', () => {
    $('export-spec').value = JSON.stringify({ paying: state.paying, plan: state.plan, status: state.status, csm: state.csm,
      segment: state.segment, impl: state.impl, q: state.q, field: state.field, is_: state.is });
    const f = $('export-form'); f.action = '/export/accounts.csv'; f.submit();
  });
  $('show-log').addEventListener('click', async () => {
    const wrap = $('log-wrap');
    wrap.hidden = false;
    const d = await fetch('/api/accounts/edits').then(r => r.json());
    const body = $('log-body'); body.textContent = '';
    d.edits.forEach(e => {
      const tr = h('tr');
      tr.appendChild(h('td', 'small date', (e.at || '').replace('T', ' ').slice(0, 16)));
      tr.appendChild(h('td', 'small', e.user));
      tr.appendChild(h('td', 'small', e.account));
      tr.appendChild(h('td', 'small', e.field));
      tr.appendChild(h('td', 'small', `${e.old || '—'} → ${e.new || '—'}`));
      const res = h('td', 'small');
      res.appendChild(h('span', 'tag' + (e.status === 'saved' ? ' ok' : ' warn'), e.status));
      if (e.error) res.title = e.error;
      tr.appendChild(res);
      body.appendChild(tr);
    });
    if (!d.edits.length) { const tr = h('tr'); const td = h('td', 'empty', 'No edits yet.'); td.colSpan = 6; tr.appendChild(td); body.appendChild(tr); }
  });

  $('f-q').value = state.q;
  $('f-plan').value = state.plan;
  setSeg();
  load();
})();
