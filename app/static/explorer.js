/* Product Explorer: multi-select products -> customers (multi-select) -> contacts (multi-select)
 * -> CSV. All data text goes into the DOM via textContent. */
(function () {
  const $ = id => document.getElementById(id);
  const { fmt } = Charts;
  const PRODUCTS = JSON.parse($('products-data').textContent);
  const PRODUCT_INFO = Object.fromEntries(PRODUCTS.map(p => [p.product, p]));
  const SLOTS = ['--series-1', '--series-2', '--series-3', '--series-4']
    .map(v => getComputedStyle(document.documentElement).getPropertyValue(v).trim());
  const ACCENT = SLOTS[0];

  const url = new URLSearchParams(location.search);
  const state = {
    products: url.getAll('product').filter(p => PRODUCT_INFO[p]),
    view: ['current', 'former', 'all'].includes(url.get('view')) ? url.get('view') : 'current',
    match: ['all', 'matched', 'unmatched'].includes(url.get('match')) ? url.get('match') : 'all',
    q: url.get('q') || '',
    range: ['all', '36', '12'].includes(url.get('range')) ? url.get('range') : 'all',
    includePartner: url.get('partner') === '1',
    sort: { key: 'product_mrr', dir: -1 },
    rows: [], totals: null, limit: 2000, trend: null,
    selected: new Set(), allMatching: false,
    activeOnly: true, role: 'all', contactQ: '',
    contacts: [], contactTotal: 0, selectedContacts: new Set(),
  };
  // colour follows the product, not its position: a slot is kept until that product is removed
  const colorOf = {};
  function assignColors() {
    Object.keys(colorOf).forEach(p => { if (!state.products.includes(p)) delete colorOf[p]; });
    state.products.forEach(p => {
      if (colorOf[p]) return;
      const used = new Set(Object.values(colorOf));
      const free = SLOTS.find(c => !used.has(c));
      if (free) colorOf[p] = free;
    });
  }

  function h(tag, cls, text) {
    const el = document.createElement(tag);
    if (cls) el.className = cls;
    if (text != null) el.textContent = text;
    return el;
  }
  const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };

  function syncUrl() {
    const p = new URLSearchParams();
    state.products.forEach(x => p.append('product', x));
    if (state.view !== 'current') p.set('view', state.view);
    if (state.match !== 'all') p.set('match', state.match);
    if (state.q) p.set('q', state.q);
    if (state.range !== 'all') p.set('range', state.range);
    if (state.includePartner) p.set('partner', '1');
    const qs = p.toString();
    history.replaceState(null, '', qs ? '?' + qs : location.pathname);
  }

  /* ---------------------------------------------------------------- product picker */
  const panel = $('picker-panel'), pbtn = $('picker-btn');
  function renderPicker() {
    const f = $('picker-search').value.trim().toLowerCase();
    const list = $('picker-list');
    list.textContent = '';
    const items = PRODUCTS.filter(p => !f || p.product.toLowerCase().includes(f));
    items.slice(0, 300).forEach(p => {
      const row = h('label', 'picker-item');
      row.setAttribute('role', 'option');
      const cb = h('input'); cb.type = 'checkbox'; cb.checked = state.products.includes(p.product);
      row.setAttribute('aria-selected', cb.checked);
      cb.addEventListener('change', () => toggleProduct(p.product, cb.checked));
      row.append(cb, h('span', 'name', p.product), h('span', 'meta', fmt.money(p.mrr_cents)),
                 h('span', 'meta', `${fmt.int(p.current)} / ${fmt.int(p.former)}`));
      list.appendChild(row);
    });
    if (!items.length) list.appendChild(h('div', 'empty', 'No products match.'));
  }
  function toggleProduct(product, on) {
    const i = state.products.indexOf(product);
    if (on && i < 0) state.products.push(product);
    if (!on && i >= 0) state.products.splice(i, 1);
    resetSelection();
    renderChips(); load(true);
  }
  function renderChips() {
    assignColors();
    const chips = $('chips');
    chips.textContent = '';
    pbtn.textContent = state.products.length ? `Products (${state.products.length}) ▾` : 'Products ▾';
    if (!state.products.length) { chips.appendChild(h('span', 'muted', 'All products')); return; }
    state.products.forEach(p => {
      const c = h('span', 'pchip');
      if (colorOf[p] && state.products.length > 1 && state.products.length <= 4) {
        const k = h('i', 'key'); k.style.background = colorOf[p]; c.appendChild(k);
      }
      c.appendChild(document.createTextNode(p));
      const x = h('button', null, '×'); x.setAttribute('aria-label', 'Remove ' + p);
      x.addEventListener('click', () => toggleProduct(p, false));
      c.appendChild(x);
      chips.appendChild(c);
    });
  }
  pbtn.addEventListener('click', e => {
    e.stopPropagation();
    panel.hidden = !panel.hidden;
    pbtn.setAttribute('aria-expanded', !panel.hidden);
    if (!panel.hidden) { renderPicker(); $('picker-search').focus(); }
  });
  $('picker-search').addEventListener('input', renderPicker);
  $('picker-clear').addEventListener('click', () => { state.products = []; resetSelection(); renderChips(); renderPicker(); load(true); });
  document.addEventListener('click', e => { if (!$('picker').contains(e.target)) { panel.hidden = true; pbtn.setAttribute('aria-expanded', 'false'); } });
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && !panel.hidden) { panel.hidden = true; pbtn.focus(); } });

  /* ---------------------------------------------------------------- filters */
  function setSeg(seg, attr, value) {
    seg.querySelectorAll('button').forEach(b => b.setAttribute('aria-pressed', b.dataset[attr] === value));
  }
  $('view-seg').addEventListener('click', e => {
    const b = e.target.closest('button'); if (!b) return;
    state.view = b.dataset.view; setSeg($('view-seg'), 'view', state.view); resetSelection(); load(false);
  });
  $('match').addEventListener('change', e => { state.match = e.target.value; resetSelection(); load(false); });
  $('q').addEventListener('input', debounce(e => { state.q = e.target.value.trim(); resetSelection(); load(false); }, 300));
  $('range').addEventListener('change', e => { state.range = e.target.value; syncUrl(); renderCharts(); });
  $('include-partner').addEventListener('change', e => { state.includePartner = e.target.checked; resetSelection(); load(true); });

  /* ---------------------------------------------------------------- data */
  let seq = 0;
  async function load(withTrend) {
    syncUrl();
    const my = ++seq;
    const qs = new URLSearchParams();
    state.products.forEach(p => qs.append('product', p));
    if (state.includePartner) qs.set('include_partner', 'true');
    const explore = new URLSearchParams(qs); explore.set('view', state.view); explore.set('match', state.match);
    if (state.q) explore.set('q', state.q);
    document.body.classList.add('busy');
    $('customers-card').classList.add('loading');
    if (withTrend || !state.trend) $('charts').classList.add('loading');
    try {
      const [ex, tr] = await Promise.all([
        fetch('/api/explore?' + explore).then(r => r.json()),
        (withTrend || !state.trend) ? fetch('/api/trend?' + qs).then(r => r.json()) : Promise.resolve(state.trend),
      ]);
      if (my !== seq) return;
      state.rows = ex.rows; state.totals = ex.totals; state.limit = ex.limit; state.trend = tr;
      const pt = $('partner-toggle');
      pt.hidden = !ex.totals.partners;
      $('partner-label').textContent = `Include non-EHR revenue (${fmt.int(ex.totals.partners)})`;
      renderKpis(); renderCharts(); renderTable();
      loadContacts();
    } finally {
      if (my === seq) { $('customers-card').classList.remove('loading'); $('charts').classList.remove('loading'); }
    }
  }

  /* ---------------------------------------------------------------- KPIs */
  function renderKpis() {
    const t = state.totals, el = $('kpis');
    el.textContent = '';
    const scope = state.products.length ? (state.products.length === 1 ? state.products[0] : `${state.products.length} products`) : 'all products';
    const tiles = [
      ['Current customers', fmt.int(t.current), `${scope} · ${fmt.int(t.former)} former`, 'primary'],
      ['MRR', fmt.money(t.product_mrr), `ARR ${fmt.money(t.product_mrr * 12)}`],
      ['Contract value', fmt.money((t.contract_value || 0) * 100), 'latest won deal per account'],
      ['Active users', fmt.int(t.active_users), 'Healthie users at these customers'],
    ];
    if (t.unmatched) tiles.push(['Not in Salesforce', fmt.int(t.unmatched), 'no account matched', 'warn']);
    tiles.forEach(([label, value, note, cls]) => {
      const k = h('div', 'kpi' + (cls ? ' ' + cls : ''));
      k.append(h('div', 'label', label), h('div', 'value', value), h('div', 'note', note));
      el.appendChild(k);
    });
  }

  /* ---------------------------------------------------------------- charts */
  function cutIndex(months) {
    if (state.range === 'all' || !months.length) return 0;
    const cut = new Date(); cut.setUTCMonth(cut.getUTCMonth() - Number(state.range));
    const i = months.findIndex(m => Charts.parseDate(m) >= cut);
    return i < 0 ? months.length : i;
  }
  function sliceRange(tr) {
    if (!tr) return { months: [], mrr: [], series: [], customerMonths: [], customers: [] };
    const a = cutIndex(tr.months), b = cutIndex(tr.customer_months || []);
    return {
      months: tr.months.slice(a), mrr: tr.mrr.slice(a),
      series: tr.series.map(s => ({ ...s, mrr: s.mrr.slice(a) })),
      customerMonths: (tr.customer_months || []).slice(b), customers: tr.customers.slice(b),
    };
  }
  const shown = { mrr: false, cust: false };
  function renderCharts() {
    const tr = sliceRange(state.trend);
    const label = state.products.length === 1 ? state.products[0]
      : state.products.length ? `${state.products.length} selected products` : 'All products';
    $('mrr-title').textContent = `MRR over time · ${label}`;
    $('cust-title').textContent = `Customers over time · ${label}`;
    const multi = tr.series.length > 1;
    const mrrSeries = multi
      ? tr.series.map(s => ({ name: s.product, color: colorOf[s.product] || ACCENT, values: s.mrr }))
      : [{ name: 'MRR', color: ACCENT, values: tr.mrr }];
    if (multi) Charts.legend($('mrr-legend'), mrrSeries);
    else { $('mrr-legend').textContent = ''; $('mrr-legend').className = ''; }
    if (state.products.length > 4) {
      $('mrr-legend').className = 'legend';
      $('mrr-legend').appendChild(h('span', 'muted', 'Total of the selected products (per-product lines show for up to 4).'));
    }
    Charts.line($('mrr-chart'), { dates: tr.months, series: mrrSeries, area: !multi, endLabels: multi,
      height: 240, ariaLabel: 'MRR by month',
      emptyText: 'Revenue history fills in after the next daily sync (12:00 UTC).' });
    Charts.line($('cust-chart'), { dates: tr.customerMonths, series: [{ name: 'Customers', color: ACCENT, values: tr.customers }],
      area: true, height: 240, yFormat: fmt.int, yAxisFormat: fmt.intAxis, ariaLabel: 'Customers by month' });
    const rows = tr.months.map((m, i) => ({ m, i })).reverse();
    Charts.table($('mrr-table'), [{ label: 'Month', value: r => fmt.month(Charts.parseDate(r.m)) },
      ...mrrSeries.map(s => ({ label: s.name, num: true, value: r => fmt.money(s.values[r.i]) }))], rows);
    Charts.table($('cust-table'), [{ label: 'Month', value: r => fmt.month(Charts.parseDate(r.m)) },
      { label: 'Customers', num: true, value: r => fmt.int(tr.customers[r.i]) }],
      tr.customerMonths.map((m, i) => ({ m, i })).reverse());
    ['mrr', 'cust'].forEach(k => { $(k + '-table').hidden = !shown[k]; $(k + '-chart').hidden = shown[k]; });
  }
  document.querySelectorAll('[data-table-toggle]').forEach(b => b.addEventListener('click', () => {
    const k = b.dataset.tableToggle; shown[k] = !shown[k];
    b.textContent = shown[k] ? 'View as chart' : 'View as table';
    $(k + '-table').hidden = !shown[k]; $(k + '-chart').hidden = shown[k];
  }));

  /* ---------------------------------------------------------------- customers table */
  const sortVal = {
    name: r => r.name.toLowerCase(), product_mrr: r => r.product_mrr, mrr: r => r.mrr,
    since: r => r.since || '', ended: r => r.ended || '',
    contract_value: r => r.account ? (r.account.contract_value || 0) : -1,
    contract_end: r => r.account ? (r.account.contract_end || '') : '',
    active_users: r => r.account ? r.account.active_users : -1,
    providers: r => r.account && r.account.hl_active_providers != null ? r.account.hl_active_providers : -1,
    clients: r => r.account && r.account.hl_active_clients != null ? r.account.hl_active_clients : -1,
    owner: r => r.account ? (r.account.owner || '').toLowerCase() : '~',
  };
  document.querySelectorAll('#cust-table-el th.sortable').forEach(th => th.addEventListener('click', () => {
    const k = th.dataset.sort;
    state.sort = { key: k, dir: state.sort.key === k ? -state.sort.dir : (k === 'name' || k === 'owner' ? 1 : -1) };
    renderTable();
  }));

  function renderTable() {
    const { key, dir } = state.sort, f = sortVal[key];
    const rows = state.rows.slice().sort((a, b) => { const x = f(a), y = f(b); return x < y ? -dir : x > y ? dir : 0; });
    document.querySelectorAll('#cust-table-el th.sortable').forEach(th => {
      th.querySelector('.arrow')?.remove();
      if (th.dataset.sort === key) th.appendChild(h('span', 'arrow', dir > 0 ? ' ▲' : ' ▼'));
    });
    $('th-ended').hidden = state.view === 'current';
    const body = $('cust-body');
    body.textContent = '';
    const wanted = new Set(state.products);
    rows.forEach(r => {
      const tr = h('tr');
      const sel = state.allMatching || state.selected.has(r.uuid);
      if (sel) tr.classList.add('selected');
      const cb = h('input'); cb.type = 'checkbox'; cb.checked = sel;
      cb.setAttribute('aria-label', 'Select ' + r.name);
      cb.addEventListener('change', () => {
        if (state.allMatching) { state.allMatching = false; state.rows.forEach(x => state.selected.add(x.uuid)); }
        cb.checked ? state.selected.add(r.uuid) : state.selected.delete(r.uuid);
        tr.classList.toggle('selected', cb.checked);
        selectionChanged();
      });
      const tdc = h('td', 'check'); tdc.appendChild(cb); tr.appendChild(tdc);

      const name = h('td');
      const a = h('a', 'strong', r.name); a.href = '/customer/' + encodeURIComponent(r.uuid);
      name.appendChild(a);
      const sub = h('div', 'small muted');
      if (r.partner) sub.appendChild(h('span', 'tag warn', 'non-EHR revenue'));
      if (r.status && r.status !== 'Active') sub.appendChild(h('span', 'tag former', r.status));
      if (!r.account && !r.partner) sub.appendChild(h('span', 'tag warn', 'not in Salesforce'));
      if (r.billing_name && r.billing_name !== r.name) sub.appendChild(document.createTextNode('Billing: ' + r.billing_name));
      name.appendChild(sub);
      tr.appendChild(name);

      const prods = h('td');
      const list = wanted.size ? r.products.filter(p => wanted.has(p.product)) : r.products;
      list.slice(0, 4).forEach(p => prods.appendChild(h('span', 'tag' + (p.current ? '' : ' former'), p.product + (p.current ? '' : ' (former)'))));
      if (list.length > 4) prods.appendChild(h('span', 'small muted', `+${list.length - 4}`));
      tr.appendChild(prods);

      tr.appendChild(h('td', 'num', fmt.money(r.product_mrr)));
      tr.appendChild(h('td', 'num', fmt.money(r.mrr)));
      tr.appendChild(h('td', 'small date', r.since || '—'));
      const ended = h('td', 'small date', r.ended || '—'); ended.hidden = state.view === 'current'; tr.appendChild(ended);
      const ac = r.account;
      tr.appendChild(h('td', 'num', ac && ac.contract_value ? fmt.money(ac.contract_value * 100) : '—'));
      tr.appendChild(h('td', 'small date', ac && ac.contract_end ? ac.contract_end : '—'));
      tr.appendChild(h('td', 'num', ac ? fmt.int(ac.active_users) : '—'));
      const prov = h('td', 'num', ac && ac.hl_active_providers != null ? fmt.int(ac.hl_active_providers) : '—');
      if (ac && ac.hl_providers != null) prov.title = `${fmt.int(ac.hl_active_providers)} active of ${fmt.int(ac.hl_providers)} providers · ${fmt.int(ac.hl_admins)} admins · last provider activity ${ac.hl_last_activity || '—'} · Org ID ${ac.org_id}`;
      else if (ac && !ac.org_id) prov.title = 'No Healthie Org ID on the Salesforce account';
      tr.appendChild(prov);
      const cli = h('td', 'num', ac && ac.hl_active_clients != null ? fmt.int(ac.hl_active_clients) : '—');
      if (ac && ac.hl_clients != null) cli.title = `${fmt.int(ac.hl_active_clients)} active of ${fmt.int(ac.hl_clients)} clients`;
      tr.appendChild(cli);
      tr.appendChild(h('td', 'small', ac && ac.health ? ac.health : '—'));
      tr.appendChild(h('td', 'small', ac && ac.owner ? ac.owner : '—'));
      body.appendChild(tr);
    });
    if (!rows.length) {
      const tr = h('tr'); const td = h('td', 'empty', 'No customers match these filters.'); td.colSpan = 14; tr.appendChild(td); body.appendChild(tr);
    }
    $('cust-count').textContent = fmt.int(state.totals.shown);
    $('cust-note').textContent = state.totals.shown > rows.length
      ? `Showing the top ${fmt.int(rows.length)} of ${fmt.int(state.totals.shown)} by MRR. Narrow the filters, or use “select all matching” and export.` : '';
    selectionChanged(true);
  }

  $('check-all').addEventListener('change', e => {
    state.allMatching = false;
    if (e.target.checked) state.rows.forEach(r => state.selected.add(r.uuid));
    else state.selected.clear();
    renderTable();
  });

  function resetSelection() { state.selected.clear(); state.allMatching = false; state.selectedContacts.clear(); }

  function selectedCount() { return state.allMatching ? state.totals.shown : state.selected.size; }

  function selectionChanged(fromRender) {
    const n = selectedCount(), total = state.totals ? state.totals.shown : 0;
    const allShown = state.rows.length && state.rows.every(r => state.allMatching || state.selected.has(r.uuid));
    $('check-all').checked = !!allShown;
    $('check-all').indeterminate = !allShown && state.selected.size > 0;
    const note = $('select-all-note');
    note.textContent = '';
    if (allShown && total > state.rows.length) {
      if (state.allMatching) {
        note.append(`All ${fmt.int(total)} matching customers are selected. `);
        const b = h('button', null, 'Clear selection'); b.addEventListener('click', () => { resetSelection(); renderTable(); loadContacts(); });
        note.appendChild(b);
      } else {
        note.append(`All ${fmt.int(state.rows.length)} shown customers are selected. `);
        const b = h('button', null, `Select all ${fmt.int(total)} matching`);
        b.addEventListener('click', () => { state.allMatching = true; renderTable(); loadContacts(); });
        note.appendChild(b);
      }
      note.hidden = false;
    } else note.hidden = true;
    $('selbar').hidden = n === 0;
    $('selbar-text').textContent = `${fmt.int(n)} customer${n === 1 ? '' : 's'} selected`;
    if (!fromRender) loadContactsDebounced();
  }
  $('selbar-clear').addEventListener('click', () => { resetSelection(); renderTable(); loadContacts(); });

  /* ---------------------------------------------------------------- contacts */
  function scopeUuids() {
    if (state.allMatching) return null;
    return state.selected.size ? [...state.selected] : null;
  }
  function spec(extra) {
    return Object.assign({
      products: state.products, view: state.view, match: state.match, q: state.q,
      customer_uuids: scopeUuids(), active_only: state.activeOnly, role: state.role,
      contact_q: state.contactQ, include_partner: state.includePartner,
    }, extra || {});
  }
  let cseq = 0;
  async function loadContacts() {
    const my = ++cseq;
    $('contacts-card').classList.add('loading');
    try {
      const r = await fetch('/api/contacts', { method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'fetch' },
        body: JSON.stringify(spec()) }).then(r => r.json());
      if (my !== cseq) return;
      state.contacts = r.rows; state.contactTotal = r.total;
      const ids = new Set(r.rows.map(c => c.id));
      [...state.selectedContacts].forEach(id => { if (!ids.has(id)) state.selectedContacts.delete(id); });
      renderContacts(r);
    } finally { if (my === cseq) $('contacts-card').classList.remove('loading'); }
  }
  const loadContactsDebounced = debounce(loadContacts, 250);

  function renderContacts(r) {
    const scoped = scopeUuids();
    const who = state.activeOnly ? 'Active Healthie users' : 'All Salesforce contacts';
    $('contact-scope').textContent = scoped
      ? `${who} at the ${fmt.int(scoped.length)} selected customer${scoped.length === 1 ? '' : 's'}.`
      : `${who} at all ${fmt.int(r.customers)} customers matching the filters. Select customers above to narrow this.`;
    $('contact-count').textContent = fmt.int(r.total);
    const body = $('contact-body');
    body.textContent = '';
    state.contacts.forEach(c => {
      const tr = h('tr');
      const sel = state.selectedContacts.has(c.id);
      if (sel) tr.classList.add('selected');
      const cb = h('input'); cb.type = 'checkbox'; cb.checked = sel; cb.setAttribute('aria-label', 'Select ' + (c.name || c.email));
      cb.addEventListener('change', () => {
        cb.checked ? state.selectedContacts.add(c.id) : state.selectedContacts.delete(c.id);
        tr.classList.toggle('selected', cb.checked); contactSelectionChanged();
      });
      const tdc = h('td', 'check'); tdc.appendChild(cb); tr.appendChild(tdc);
      const name = h('td');
      const a = h('a', 'strong', c.name || '(no name)');
      a.href = `${window.LIGHTNING_BASE}/lightning/r/Contact/${encodeURIComponent(c.id)}/view`;
      a.target = '_blank'; a.rel = 'noopener';
      name.appendChild(a); tr.appendChild(name);
      const em = h('td');
      if (c.email) { const m = h('a', null, c.email); m.href = 'mailto:' + c.email; em.appendChild(m); } else em.textContent = '—';
      tr.appendChild(em);
      tr.appendChild(h('td', 'small', c.phone || c.mobile || '—'));
      tr.appendChild(h('td', 'small', c.title || '—'));
      const role = h('td', 'small');
      if (c.owner) role.appendChild(h('span', 'tag', 'owner'));
      else if (c.admin) role.appendChild(h('span', 'tag', 'admin'));
      if (!c.active) role.appendChild(h('span', 'tag former', 'not active'));
      if (c.user_type) role.appendChild(document.createTextNode(c.user_type));
      tr.appendChild(role);
      const cu = h('td', 'small');
      const ca = h('a', null, c.customer); ca.href = '/customer/' + encodeURIComponent(c.customer_uuid);
      cu.appendChild(ca); tr.appendChild(cu);
      body.appendChild(tr);
    });
    if (!state.contacts.length) {
      const tr = h('tr'); const td = h('td', 'empty', 'No contacts for this selection.'); td.colSpan = 7; tr.appendChild(td); body.appendChild(tr);
    }
    $('contact-note').textContent = r.total > state.contacts.length
      ? `Showing ${fmt.int(state.contacts.length)} of ${fmt.int(r.total)}. “Export all” includes every one.` : '';
    $('export-contacts-all').textContent = `↓ Download all ${fmt.int(r.total)} contacts`;
    contactSelectionChanged();
  }
  function contactSelectionChanged() {
    const n = state.selectedContacts.size;
    const b = $('export-contacts-selected');
    b.disabled = n === 0; b.textContent = n ? `↓ Download ${fmt.int(n)} ticked` : '↓ Download ticked';
    const all = state.contacts.length && state.contacts.every(c => state.selectedContacts.has(c.id));
    $('check-all-contacts').checked = !!all;
    $('check-all-contacts').indeterminate = !all && n > 0;
  }
  $('check-all-contacts').addEventListener('change', e => {
    if (e.target.checked) state.contacts.forEach(c => state.selectedContacts.add(c.id));
    else state.selectedContacts.clear();
    renderContacts({ total: state.contactTotal, customers: (scopeUuids() || []).length || state.totals.shown });
  });
  $('active-seg').addEventListener('click', e => {
    const b = e.target.closest('button'); if (!b) return;
    state.activeOnly = b.dataset.active === '1'; setSeg($('active-seg'), 'active', b.dataset.active);
    state.selectedContacts.clear(); loadContacts();
  });
  $('role').addEventListener('change', e => { state.role = e.target.value; state.selectedContacts.clear(); loadContacts(); });
  $('contact-q').addEventListener('input', debounce(e => { state.contactQ = e.target.value.trim(); loadContacts(); }, 300));

  /* ---------------------------------------------------------------- exports */
  function exportCsv(path, s) {
    $('export-spec').value = JSON.stringify(s);
    const f = $('export-form'); f.action = path; f.submit();
  }
  $('export-customers').addEventListener('click', () => exportCsv('/export/customers.csv', spec()));
  $('selbar-export-customers').addEventListener('click', () => exportCsv('/export/customers.csv', spec()));
  $('selbar-export-contacts').addEventListener('click', () => exportCsv('/export/contacts.csv', spec()));
  $('export-contacts-all').addEventListener('click', () => exportCsv('/export/contacts.csv', spec()));
  $('export-contacts-selected').addEventListener('click', () =>
    exportCsv('/export/contacts.csv', spec({ contact_ids: [...state.selectedContacts] })));

  /* ---------------------------------------------------------------- boot */
  $('q').value = state.q; $('match').value = state.match; $('range').value = state.range;
  $('include-partner').checked = state.includePartner;
  setSeg($('view-seg'), 'view', state.view);
  renderChips();
  load(true);
})();
