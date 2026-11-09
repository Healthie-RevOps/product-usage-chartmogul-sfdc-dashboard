/* Retention: compare plans -> retention trend -> who to look at -> CSVs.
 * All data text goes into the DOM via textContent. */
(function () {
  const $ = id => document.getElementById(id);
  const { fmt } = Charts;
  const SLOTS = ['--series-1', '--series-2', '--series-3', '--series-4']
    .map(v => getComputedStyle(document.documentElement).getPropertyValue(v).trim());
  const url = new URLSearchParams(location.search);
  const state = { plan: url.get('plan') || 'All plans', list: url.get('list') === 'churned' ? 'churned' : 'at_risk', data: null, table: false };

  function h(tag, cls, text) {
    const el = document.createElement(tag);
    if (cls) el.className = cls;
    if (text != null) el.textContent = text;
    return el;
  }
  const pct = x => x == null ? '—' : Math.round(x * 100) + '%';

  function syncUrl() {
    const p = new URLSearchParams();
    if (state.plan !== 'All plans') p.set('plan', state.plan);
    if (state.list !== 'at_risk') p.set('list', state.list);
    const qs = p.toString();
    history.replaceState(null, '', qs ? '?' + qs : location.pathname);
  }
  function setSegs() {
    $('plan-seg').querySelectorAll('button').forEach(b => b.setAttribute('aria-pressed', b.dataset.plan === state.plan));
    $('list-seg').querySelectorAll('button').forEach(b => b.setAttribute('aria-pressed', b.dataset.list === state.list));
  }

  let seq = 0;
  async function load() {
    syncUrl(); setSegs();
    const my = ++seq;
    $('step-who').classList.add('loading');
    try {
      const d = await fetch('/api/retention?plan=' + encodeURIComponent(state.plan)).then(r => r.json());
      if (my !== seq) return;
      if (!App.dataState(d, 'retention data')) return;
      const first = !state.data;
      state.data = d;
      if (first) { renderCompare(); renderTrend(); }
      renderCompareSelection();
      renderList();
    } finally { if (my === seq) $('step-who').classList.remove('loading'); }
  }

  /* ---------------------------------------------------------------- compare */
  function renderCompare() {
    const body = $('compare-body'); body.textContent = '';
    state.data.summary.forEach(s => {
      const tr = h('tr', s.plan === 'All plans' ? 'total-row' : '');
      tr.dataset.plan = s.plan;
      if (s.plan !== 'All plans') {
        tr.classList.add('clickable'); tr.tabIndex = 0;
        const go = () => { state.plan = state.plan === s.plan ? 'All plans' : s.plan; load(); };
        tr.addEventListener('click', go);
        tr.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); } });
      }
      tr.appendChild(h('td', 'strong', s.plan));
      tr.appendChild(h('td', 'num', fmt.int(s.active)));
      tr.appendChild(h('td', 'num', fmt.money(s.mrr)));
      tr.appendChild(h('td', 'num', fmt.int(s.new_12m)));
      tr.appendChild(h('td', 'num', s.median_tenure_months == null ? '—' : `${s.median_tenure_months} mo`));
      const kept = h('td'); const row = h('div', 'meter-row'); const m = h('div', 'meter');
      m.setAttribute('role', 'img'); m.setAttribute('aria-label', `${s.plan}: ${pct(s.logo_retention_12m)} kept after 12 months`);
      if (s.logo_retention_12m != null) { const f = h('span'); f.style.width = Math.max(2, s.logo_retention_12m * 100) + '%'; m.appendChild(f); }
      row.append(m, h('span', 'meter-val', pct(s.logo_retention_12m))); kept.appendChild(row);
      kept.title = `${fmt.int(s.cohort_12m - s.churned_12m)} of ${fmt.int(s.cohort_12m)} still paying · ${fmt.int(s.churned_12m)} left`;
      tr.appendChild(kept);
      tr.appendChild(h('td', 'num', fmt.int(s.churned_90d)));
      tr.appendChild(h('td', 'num', pct(s.nrr_12m)));
      tr.appendChild(h('td', 'num', pct(s.grr_12m)));
      const use = h('td', 'num', pct(s.using_share));
      if (s.usage_coverage != null) use.title = `Usage data for ${pct(s.usage_coverage)} of these customers · median ${s.median_active_providers ?? '—'} active providers`;
      tr.appendChild(use);
      tr.appendChild(h('td', 'num', fmt.int(s.paying_inactive)));
      body.appendChild(tr);
    });
    const out = Object.entries(state.data.outside_billing || {}).filter(([, n]) => n > 0)
      .map(([p, n]) => `${fmt.int(n)} paying ${p} account${n === 1 ? '' : 's'}`);
    $('compare-note').textContent = 'Click a plan to focus the lists in step 3. Hover “Kept after 12 mo” and “Using it” for the underlying counts. Usage comes from Healthie (Metabase) and is today only.' +
      (out.length ? ` Not included: ${out.join(', ')} with no Stripe / ChartMogul billing (e.g. invoiced customers), since retention is measured from billing history.` : '');
  }
  function renderCompareSelection() {
    document.querySelectorAll('#compare-body tr').forEach(tr => tr.classList.toggle('selected', tr.dataset.plan === state.plan && state.plan !== 'All plans'));
  }

  /* ---------------------------------------------------------------- trend */
  function filled(values) {
    // Charts.line has no gaps: fill a missing month from its neighbour
    const out = values.slice();
    for (let i = 0; i < out.length; i++) if (out[i] == null) out[i] = out[i - 1] ?? null;
    for (let i = out.length - 1; i >= 0; i--) if (out[i] == null) out[i] = out[i + 1] ?? 0;
    return out;
  }
  function renderTrend() {
    const t = state.data.trend;
    // a plan with too few customers a year ago would chart noise (or a fake flat line)
    const small = state.data.plans.filter(p => Math.min(...t.cohorts[p]) < t.min_cohort);
    const series = state.data.plans.map((p, i) => ({ name: p, color: SLOTS[i], values: filled(t.series[p]) }))
      .filter(s => !small.includes(s.name));
    $('trend-note').textContent = small.length
      ? `Not charted: ${small.map(p => { const n = Math.min(...t.cohorts[p]); return `${p} (${n === 0 ? 'no customers' : 'as few as ' + fmt.int(n) + ' customers'} a year earlier at some point)`; }).join(', ')}; too few to show a reliable trend. See the table in step 1.` : '';
    Charts.legend($('trend-legend'), series);
    Charts.line($('trend-chart'), { dates: t.dates, series, endLabels: true, height: 260,
      yFormat: v => Math.round(v * 100) + '%', yAxisFormat: v => Math.round(v * 100) + '%',
      ariaLabel: '12-month logo retention by plan, last 12 months' });
    Charts.table($('trend-table'), [{ label: 'Month', value: r => r.d },
      ...state.data.plans.map(p => ({ label: p, num: true, value: r => pct(t.series[p][r.i]) }))],
      t.dates.map((d, i) => ({ d, i })).reverse());
  }
  $('trend-toggle').addEventListener('click', () => {
    state.table = !state.table;
    $('trend-table').hidden = !state.table; $('trend-chart').hidden = state.table; $('trend-legend').hidden = state.table;
    $('trend-toggle').textContent = state.table ? 'View as chart' : 'View as table';
  });

  /* ---------------------------------------------------------------- lists */
  const COLS = {
    at_risk: [['Customer', r => r.name, 'name'], ['Plan', r => r.plan || '—'], ['MRR', r => fmt.money(r.mrr), 'num'],
      ['Active providers', r => `${fmt.int(r.active_providers)} of ${fmt.int(r.providers)}`, 'num'],
      ['Last provider login', r => r.last_activity || '—'], ['Active clients', r => fmt.int(r.active_clients), 'num'],
      ['Tenure', r => `${r.tenure_months} mo`, 'num'], ['Owner', r => r.owner || '—']],
    churned: [['Customer', r => r.name, 'name'], ['Plan (90 days ago)', r => r.plan || '—'],
      ['MRR lost', r => fmt.money(r.mrr_lost), 'num'], ['Ended', r => r.ended || '—'], ['Tenure', r => `${r.tenure_months} mo`, 'num']],
  };
  function renderList() {
    const cols = COLS[state.list], rows = state.data[state.list], total = state.data[state.list + '_total'];
    const head = $('list-head'); head.textContent = '';
    cols.forEach(([label, , cls]) => head.appendChild(h('th', cls === 'num' ? 'num' : '', label)));
    const body = $('list-body'); body.textContent = '';
    rows.forEach(r => {
      const tr = h('tr');
      cols.forEach(([, fn, cls]) => {
        if (cls === 'name') {
          const td = h('td'); const a = h('a', 'strong', fn(r)); a.href = '/customer/' + encodeURIComponent(r.uuid);
          td.appendChild(a); tr.appendChild(td);
        } else tr.appendChild(h('td', cls === 'num' ? 'num' : 'small', fn(r)));
      });
      body.appendChild(tr);
    });
    if (!rows.length) { const tr = h('tr'); const td = h('td', 'empty', 'No customers in this list.'); td.colSpan = cols.length; tr.appendChild(td); body.appendChild(tr); }
    const scope = $('list-scope'); scope.textContent = '';
    const plan = state.plan === 'All plans' ? 'all plans' : state.plan;
    if (state.list === 'at_risk') {
      scope.append(`${fmt.int(total)} customers on ${plan} are `);
      scope.appendChild(h('b', null, 'paying but not using Healthie'));
      scope.append(` (no provider login in the last ${state.data.inactive_days} days, or ever).`);
    } else {
      scope.append(`${fmt.int(total)} customers on ${plan} `);
      scope.appendChild(h('b', null, 'stopped paying in the last 90 days'));
      scope.append('.');
    }
    $('list-note').textContent = total > rows.length ? `Showing the top ${fmt.int(rows.length)} by MRR; the CSV has all ${fmt.int(total)}.` : '';
  }
  $('plan-seg').addEventListener('click', e => { const b = e.target.closest('button'); if (!b) return; state.plan = b.dataset.plan; load(); });
  $('list-seg').addEventListener('click', e => { const b = e.target.closest('button'); if (!b) return; state.list = b.dataset.list; syncUrl(); setSegs(); renderList(); });

  document.querySelectorAll('[data-export]').forEach(b => b.addEventListener('click', () => {
    $('export-spec').value = JSON.stringify({ kind: b.dataset.export, plan: state.plan });
    const f = $('export-form'); f.action = '/export/retention.csv'; f.submit();
  }));

  load();
})();
