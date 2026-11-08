/* Data quality: issues -> owners -> deals to fix -> flag mismatches -> CSV fix list.
 * All data text goes into the DOM via textContent. */
(function () {
  const $ = id => document.getElementById(id);
  const { fmt } = Charts;
  const SALESFORCE = window.LIGHTNING_BASE;
  const RULES = JSON.parse($('rules-data').textContent);
  const RULE = Object.fromEntries(RULES.map(r => [r.key, r]));
  const DEAL_RULES = RULES.filter(r => r.key !== 'flag_mismatch');

  const url = new URLSearchParams(location.search);
  const state = {
    range: ['year', '12m', 'all'].includes(url.get('range')) ? url.get('range') : 'year',
    owner: url.get('owner') || '', issue: RULE[url.get('issue')] ? url.get('issue') : '',
    q: url.get('q') || '', data: null, facetsLoaded: false,
  };

  function h(tag, cls, text) {
    const el = document.createElement(tag);
    if (cls) el.className = cls;
    if (text != null) el.textContent = text;
    return el;
  }
  const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
  const pct = x => x == null ? '—' : Math.round(x * 100) + '%';
  const iso = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;

  function dateFrom() {
    const now = new Date();
    if (state.range === 'year') return `${now.getFullYear()}-01-01`;
    if (state.range === '12m') { const d = new Date(now); d.setFullYear(now.getFullYear() - 1); return iso(d); }
    return '';
  }
  function spec() { return { date_from: dateFrom(), owner: state.owner, issue: state.issue, q: state.q }; }
  function syncUrl() {
    const p = new URLSearchParams();
    if (state.range !== 'year') p.set('range', state.range);
    ['owner', 'issue', 'q'].forEach(k => { if (state[k]) p.set(k, state[k]); });
    const qs = p.toString();
    history.replaceState(null, '', qs ? '?' + qs : location.pathname);
    $('f-clear').hidden = !qs;
  }

  let seq = 0;
  async function load() {
    syncUrl();
    const my = ++seq;
    const cards = ['rules-card', 'owners-card', 'work-card', 'mismatch-card'];
    cards.forEach(id => $(id).classList.add('loading'));
    try {
      const p = new URLSearchParams(Object.entries(spec()).filter(([, v]) => v));
      const d = await fetch('/api/quality?' + p).then(r => r.json());
      if (my !== seq) return;
      state.data = d;
      if (!state.facetsLoaded) {
        d.facets.owner.forEach(v => { const o = h('option', null, v); o.value = v; $('f-owner').appendChild(o); });
        $('f-owner').value = state.owner; state.facetsLoaded = true;
      }
      renderChips(); renderRules(); renderOwners(); renderWork(); renderMismatches();
    } finally {
      if (my === seq) cards.forEach(id => $(id).classList.remove('loading'));
    }
  }

  $('f-range').addEventListener('change', e => { state.range = e.target.value; load(); });
  $('f-owner').addEventListener('change', e => { state.owner = e.target.value; load(); });
  $('f-q').addEventListener('input', debounce(e => { state.q = e.target.value.trim(); load(); }, 300));
  $('f-clear').addEventListener('click', () => {
    Object.assign(state, { range: 'year', owner: '', issue: '', q: '' });
    $('f-range').value = 'year'; $('f-owner').value = ''; $('f-q').value = ''; load();
  });

  function renderChips() {
    const el = $('chips');
    el.textContent = '';
    if (!state.issue) return;
    const c = h('span', 'pchip'); c.appendChild(document.createTextNode('Issue: ' + RULE[state.issue].label));
    const x = h('button', null, '×'); x.setAttribute('aria-label', 'Remove issue filter');
    x.addEventListener('click', () => { state.issue = ''; load(); });
    c.appendChild(x); el.appendChild(c);
  }

  function clickable(tr, on, go) {
    tr.classList.add('clickable'); tr.tabIndex = 0; tr.setAttribute('aria-pressed', on);
    if (on) tr.classList.add('selected');
    tr.addEventListener('click', go);
    tr.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); } });
  }
  function meter(frac, label) {
    const wrap = h('div', 'meter-row'), m = h('div', 'meter');
    m.setAttribute('role', 'img'); m.setAttribute('aria-label', label);
    if (frac != null) { const f = h('span'); f.style.width = Math.max(2, frac * 100) + '%'; m.appendChild(f); }
    wrap.append(m, h('span', 'meter-val', pct(frac)));
    return wrap;
  }

  function renderRules() {
    const body = $('rules-body');
    body.textContent = '';
    state.data.summary.forEach(r => {
      const tr = h('tr');
      clickable(tr, state.issue === r.key, () => { state.issue = state.issue === r.key ? '' : r.key; load(); });
      tr.appendChild(h('td', 'strong', r.label));
      tr.appendChild(h('td', 'small', r.affects));
      tr.appendChild(h('td', 'num', fmt.int(r.count)));
      const sh = h('td');
      if (r.eligible != null) {
        sh.appendChild(meter(r.share, `${r.label}: ${pct(r.share)} of ${r.eligible} eligible deals`));
        sh.appendChild(h('div', 'small muted', `of ${fmt.int(r.eligible)} deals`));
      } else sh.appendChild(h('span', 'small muted', 'account-level'));
      tr.appendChild(sh);
      tr.appendChild(h('td', 'small', r.fix));
      body.appendChild(tr);
    });
  }

  (function ownersHead() {
    const tr = $('owners-head');
    tr.appendChild(h('th', null, 'Owner'));
    tr.appendChild(h('th', 'num', 'Total'));
    DEAL_RULES.forEach(r => tr.appendChild(h('th', 'num', r.label)));
  })();
  function renderOwners() {
    const body = $('owners-body');
    body.textContent = '';
    state.data.owners.forEach(o => {
      const tr = h('tr');
      clickable(tr, state.owner === o.owner, () => {
        state.owner = state.owner === o.owner ? '' : o.owner; $('f-owner').value = state.owner; load();
      });
      tr.appendChild(h('td', 'strong', o.owner));
      tr.appendChild(h('td', 'num', fmt.int(o.total)));
      DEAL_RULES.forEach(r => {
        const n = o.counts[r.key] || 0;
        tr.appendChild(h('td', 'num' + (state.issue === r.key ? ' strong' : ''), n ? fmt.int(n) : '—'));
      });
      body.appendChild(tr);
    });
    if (!state.data.owners.length) {
      const tr = h('tr'); const td = h('td', 'empty', 'Nothing to fix here.'); td.colSpan = 2 + DEAL_RULES.length; tr.appendChild(td); body.appendChild(tr);
    }
  }

  function suggestion(r) {
    const out = [];
    r.issues.filter(i => !state.issue || i === state.issue).forEach(i => {
      if (i === 'missing_expansion_product' && r.hints.expansion_product) out.push(`Expansion Product: ${r.hints.expansion_product}`);
      else if (i === 'stale_open') out.push(`Close date ${fmt.int(r.hints.days_overdue)} days ago`);
      else out.push(RULE[i].fix);
    });
    return out.join(' · ');
  }

  function renderWork() {
    const body = $('work-body');
    body.textContent = '';
    state.data.rows.forEach(r => {
      const tr = h('tr');
      const td = h('td');
      const a = h('a', 'strong', r.name || '(no name)');
      a.href = `${SALESFORCE}/lightning/r/Opportunity/${encodeURIComponent(r.id)}/view`; a.target = '_blank'; a.rel = 'noopener';
      td.appendChild(a);
      if (r.account) td.appendChild(h('div', 'small muted', r.account + (r.record_type ? ' · ' + r.record_type : '')));
      tr.appendChild(td);
      tr.appendChild(h('td', 'small', r.owner || '—'));
      tr.appendChild(h('td', 'small', r.stage || '—'));
      tr.appendChild(h('td', 'small date', r.close_date || '—'));
      const is = h('td');
      r.issues.forEach(i => is.appendChild(h('span', 'tag' + (state.issue && i !== state.issue ? ' former' : ''), RULE[i].label)));
      tr.appendChild(is);
      tr.appendChild(h('td', 'small', suggestion(r)));
      body.appendChild(tr);
    });
    if (!state.data.rows.length) {
      const tr = h('tr'); const td = h('td', 'empty', state.issue === 'flag_mismatch' ? 'Account-level issue: see the table below.' : 'No deals to fix for these filters.');
      td.colSpan = 6; tr.appendChild(td); body.appendChild(tr);
    }
    $('work-count').textContent = fmt.int(state.data.total_rows);
    $('work-note').textContent = state.data.total_rows > state.data.rows.length
      ? `Showing the ${fmt.int(state.data.rows.length)} most recent of ${fmt.int(state.data.total_rows)}. The CSV has all of them.` : '';
  }

  function renderMismatches() {
    const body = $('mismatch-body');
    body.textContent = '';
    const rows = state.data.mismatches;
    $('mismatch-card').hidden = !!state.issue && state.issue !== 'flag_mismatch';
    rows.forEach(m => {
      const tr = h('tr');
      const td = h('td');
      const a = h('a', 'strong', m.account);
      a.href = '/customer/' + encodeURIComponent(m.customer_uuid);
      td.appendChild(a); tr.appendChild(td);
      tr.appendChild(h('td', 'small', m.csm || '—'));
      tr.appendChild(h('td', 'small', m.addon));
      tr.appendChild(h('td', 'small', m.problem));
      tr.appendChild(h('td', 'small', m.products.join(', ') || '—'));
      body.appendChild(tr);
    });
    if (!rows.length) {
      const tr = h('tr'); const td = h('td', 'empty', 'No disagreements for these filters.'); td.colSpan = 5; tr.appendChild(td); body.appendChild(tr);
    }
    $('mismatch-count').textContent = fmt.int(rows.length);
  }

  $('export').addEventListener('click', () => {
    $('export-spec').value = JSON.stringify(spec());
    const f = $('export-form'); f.action = '/export/quality.csv'; f.submit();
  });

  $('f-range').value = state.range; $('f-q').value = state.q;
  load();
})();
