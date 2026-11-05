/* Customer page: revenue history chart, product timeline, contacts selection + export. */
(function () {
  const $ = id => document.getElementById(id);
  const { fmt } = Charts;
  const D = JSON.parse($('customer-data').textContent);
  const ACCENT = getComputedStyle(document.documentElement).getPropertyValue('--series-1').trim();
  const h = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };

  /* ---- revenue over time: step line of MRR after each event, extended to today ---- */
  const evs = D.history.events.filter(e => e.date);
  const dates = evs.map(e => e.date), values = evs.map(e => e.mrr), events = evs.slice();
  if (evs.length && D.history.today > evs[evs.length - 1].date) {
    dates.push(D.history.today); values.push(D.mrr); events.push(null);
  }
  const typeLabel = t => (t || '').replace('new_biz', 'new business').replace('_', ' ');
  Charts.line($('mrr-chart'), {
    dates, series: [{ name: 'MRR', color: ACCENT, values }], step: true, area: true, markers: true,
    events, height: 260, dateFormat: fmt.date, ariaLabel: 'Customer MRR over time',
    eventLine: e => [`${typeLabel(e.type)} ${fmt.signedMoney(e.movement)}`, e.description || ''].filter(Boolean),
  });
  Charts.table($('mrr-table'), [
    { label: 'Date', value: r => r.date },
    { label: 'Change', value: r => typeLabel(r.type) },
    { label: 'MRR change', num: true, value: r => fmt.signedMoney(r.movement) },
    { label: 'MRR after', num: true, value: r => fmt.money(r.mrr) },
  ], evs.slice().reverse());

  /* ---- products over time ---- */
  Charts.timeline($('timeline-chart'), D.history.segments, D.history.today);
  Charts.table($('timeline-table'), [
    { label: 'Product', value: s => s.product },
    { label: 'Plan', value: s => s.plan || '' },
    { label: 'Start', value: s => s.start || '—' },
    { label: 'End', value: s => s.end || (s.current ? 'current' : '—') },
    { label: 'MRR', num: true, value: s => s.current ? fmt.money(s.mrr) : '—' },
  ], D.history.segments);

  document.querySelectorAll('[data-table-toggle]').forEach(b => b.addEventListener('click', () => {
    const k = b.dataset.tableToggle, t = $(k + '-table'), c = $(k + '-chart');
    const showTable = t.hidden;
    t.hidden = !showTable; c.hidden = showTable;
    b.textContent = showTable ? 'View as chart' : 'View as table';
  }));

  /* ---- contacts ---- */
  const body = $('contact-body');
  if (!body) return;
  let activeOnly = false;
  const selected = new Set();
  const visible = () => D.contacts.filter(c => !activeOnly || c.active);
  function render() {
    body.textContent = '';
    const rows = visible();
    rows.forEach(c => {
      const tr = h('tr');
      if (selected.has(c.id)) tr.classList.add('selected');
      const cb = h('input'); cb.type = 'checkbox'; cb.checked = selected.has(c.id);
      cb.setAttribute('aria-label', 'Select ' + (c.name || c.email));
      cb.addEventListener('change', () => { cb.checked ? selected.add(c.id) : selected.delete(c.id); tr.classList.toggle('selected', cb.checked); sync(); });
      const td = h('td', 'check'); td.appendChild(cb); tr.appendChild(td);
      const nm = h('td'); const a = h('a', 'strong', c.name || '(no name)');
      a.href = `${D.lightning}/lightning/r/Contact/${encodeURIComponent(c.id)}/view`; a.target = '_blank'; a.rel = 'noopener';
      nm.appendChild(a); tr.appendChild(nm);
      const em = h('td'); if (c.email) { const m = h('a', null, c.email); m.href = 'mailto:' + c.email; em.appendChild(m); } else em.textContent = '—';
      tr.appendChild(em);
      tr.appendChild(h('td', 'small', c.phone || c.mobile || '—'));
      tr.appendChild(h('td', 'small', c.title || '—'));
      const role = h('td', 'small');
      if (c.active) role.appendChild(h('span', 'tag ok', 'active user'));
      if (c.owner) role.appendChild(h('span', 'tag', 'owner')); else if (c.admin) role.appendChild(h('span', 'tag', 'admin'));
      if (c.user_type) role.appendChild(document.createTextNode(c.user_type));
      tr.appendChild(role);
      body.appendChild(tr);
    });
    $('contact-count').textContent = fmt.int(rows.length);
    sync();
  }
  function sync() {
    const rows = visible();
    [...selected].forEach(id => { if (!rows.some(c => c.id === id)) selected.delete(id); });
    const n = selected.size;
    $('export-selected').disabled = !n;
    $('export-selected').textContent = n ? `↓ Export selected (${n})` : '↓ Export selected';
    $('export-all').textContent = `↓ Export ${activeOnly ? 'active users' : 'all contacts'} (${rows.length})`;
    const all = rows.length && rows.every(c => selected.has(c.id));
    $('check-all').checked = !!all; $('check-all').indeterminate = !all && n > 0;
  }
  $('check-all').addEventListener('change', e => { visible().forEach(c => e.target.checked ? selected.add(c.id) : selected.delete(c.id)); render(); });
  $('active-seg').addEventListener('click', e => {
    const b = e.target.closest('button'); if (!b) return;
    activeOnly = b.dataset.active === '1';
    $('active-seg').querySelectorAll('button').forEach(x => x.setAttribute('aria-pressed', x === b));
    render();
  });
  function exportCsv(ids) {
    $('export-spec').value = JSON.stringify({ products: [], view: 'all', match: 'all', q: '',
      customer_uuids: [D.uuid], active_only: activeOnly, contact_ids: ids });
    $('export-form').submit();
  }
  $('export-all').addEventListener('click', () => exportCsv(null));
  $('export-selected').addEventListener('click', () => exportCsv([...selected]));
  render();
})();
