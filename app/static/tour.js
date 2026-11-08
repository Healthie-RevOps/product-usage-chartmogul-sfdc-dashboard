/* Guided tour across the four sections. Launch from the "Tour" tab any time; offered
 * once on a person's first visit. Steps on other pages navigate there and resume
 * (progress kept in sessionStorage). All text goes into the DOM via textContent. */
(function () {
  const STEPS = [
    { title: 'Welcome to Product Explorer',
      text: 'Salesforce, ChartMogul and Healthie product data in one place, so you can answer customer and product questions without building Salesforce reports. This tour takes about two minutes and covers four sections.' },
    { target: '.tabs', title: 'Four sections',
      text: 'Explorer: who has which product, and what they pay. Enterprise: add-on adoption across Enterprise accounts. Win / loss: why we win and lose deals, and against whom. Data quality: Salesforce fixes that make the numbers right.' },
    { page: '/', target: '#picker-btn', title: 'Pick products',
      text: 'Choose one or more products. Everything on the page filters to customers who have them. The toggle beside it switches between current and former customers.' },
    { page: '/', target: '#charts', title: 'Revenue and customers over time',
      text: 'MRR and customer counts for the products you picked. Hover a chart for exact values, or click “View as table” to see the numbers.' },
    { page: '/', target: '#customers-card', title: 'Customers',
      text: 'Every matching customer with MRR, contract value, providers and owner. Tick customers to narrow the contacts list below, or click a name to open the full customer page.' },
    { page: '/', target: '#contacts-card', title: 'Contacts',
      text: 'Healthie users at those customers. Filter to owners and admins, then export a CSV.' },
    { page: '/enterprise', target: '#summary-card', title: 'Add-on adoption',
      text: 'For each add-on: how many Enterprise accounts have it, are being pitched it, said no, or have never been pitched it. Click any number to list those accounts. For example, AI Scribe → Not pitched.' },
    { page: '/enterprise', target: '#matrix-card', title: 'Every account, every add-on',
      text: 'One row per account. Hover a cell for the deal, stage, loss reason, competitor and revisit date. Click an open or lost cell to open the deal in Salesforce.' },
    { page: '/enterprise', target: '#export-contacts', title: 'Build a campaign list',
      text: 'Exports the active providers and admins at the accounts in view, leaving out anyone who opted out of email. Tick specific accounts first to narrow the list.' },
    { page: '/deals', target: '#kpis', title: 'Win rate at a glance',
      text: 'Win rate, won and lost Year 1 ACV, open pipeline, and how often reps record a competitor and a loss reason. Change the record type and dates at the top of the page.' },
    { page: '/deals', target: '#comp-card', title: 'By competitor',
      text: 'Win rate, ACV and top loss reasons against each competitor. Click a row to filter the whole page to deals involving that competitor.' },
    { page: '/deals', target: '#reason-card', title: 'Why we lose',
      text: 'Loss reasons by share of losses and lost ACV, split into regrettable and non-regrettable. Click a reason to see those deals.' },
    { page: '/quality', target: '#rules-card', title: 'What to fix in Salesforce',
      text: 'Each issue is a field reps left blank. Fixing them makes the Enterprise and Win / loss pages more accurate after the next sync. Click an issue to list the deals.' },
    { page: '/quality', target: '#owners-card', title: 'Send each owner their list',
      text: 'Click an owner, then “Fix list CSV” for a ready-to-send list with links to each deal in Salesforce.' },
    { target: '#tour-tab', title: 'That’s the tour',
      text: 'Every page also has a “What you can do here” panel near the top. Relaunch this tour any time from the Tour tab.' },
  ];
  const KEY = 'tour-step';
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private mode */ } },
  };
  const ss = {
    get() { try { return sessionStorage.getItem(KEY); } catch (e) { return null; } },
    set(v) { try { sessionStorage.setItem(KEY, v); } catch (e) { /* ignore */ } },
    clear() { try { sessionStorage.removeItem(KEY); } catch (e) { /* ignore */ } },
  };
  function h(tag, cls, text) {
    const el = document.createElement(tag);
    if (cls) el.className = cls;
    if (text != null) el.textContent = text;
    return el;
  }

  let ui = null, index = 0, target = null, lastFocus = null;

  function samePage(page) {
    return !page || location.pathname === page;
  }

  function go(i) {
    if (i < 0) return;
    if (i >= STEPS.length) return end();
    const step = STEPS[i];
    if (!samePage(step.page)) {
      ss.set(String(i));
      location.href = step.page;
      return;
    }
    index = i;
    ss.set(String(i));
    render();
  }

  function build() {
    lastFocus = document.activeElement;
    const shade = h('div', 'tour-shade');
    const spot = h('div', 'tour-spot');
    const pop = h('div', 'tour-pop');
    pop.setAttribute('role', 'dialog');
    pop.setAttribute('aria-modal', 'true');
    pop.setAttribute('aria-labelledby', 'tour-title');
    pop.setAttribute('aria-describedby', 'tour-text');
    const close = h('button', 'tour-x', '×');
    close.type = 'button'; close.setAttribute('aria-label', 'End tour');
    close.addEventListener('click', end);
    const title = h('h2', 'tour-title'); title.id = 'tour-title';
    const text = h('p', 'tour-text'); text.id = 'tour-text';
    const note = h('p', 'tour-note');
    const foot = h('div', 'tour-foot');
    const count = h('span', 'tour-count');
    const back = h('button', 'btn small', 'Back'); back.type = 'button';
    const next = h('button', 'btn small primary', 'Next'); next.type = 'button';
    back.addEventListener('click', () => go(index - 1));
    next.addEventListener('click', () => go(index + 1));
    foot.append(count, h('span', 'spacer'), back, next);
    pop.append(close, title, text, note, foot);
    shade.addEventListener('click', e => e.stopPropagation());
    document.body.append(shade, spot, pop);
    ui = { shade, spot, pop, title, text, note, count, back, next };
    document.addEventListener('keydown', onKey, true);
    window.addEventListener('resize', place);
    window.addEventListener('scroll', place, true);
  }

  function onKey(e) {
    if (!ui) return;
    if (e.key === 'Escape') { e.preventDefault(); end(); }
    else if (e.key === 'ArrowRight') { e.preventDefault(); go(index + 1); }
    else if (e.key === 'ArrowLeft') { e.preventDefault(); go(index - 1); }
    else if (e.key === 'Tab') {               // keep focus inside the dialog
      const f = [...ui.pop.querySelectorAll('button:not([disabled])')];
      if (!f.length) return;
      const first = f[0], last = f[f.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  }

  function visible(el) {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && el.closest('[hidden]') == null;
  }

  function render() {
    if (!ui) build();
    const step = STEPS[index];
    target = step.target ? document.querySelector(step.target) : null;
    const shown = visible(target);
    ui.title.textContent = step.title;
    ui.text.textContent = step.text;
    ui.note.textContent = step.target && !shown ? 'This section appears here once its data has loaded.' : '';
    ui.note.hidden = !ui.note.textContent;
    ui.count.textContent = `${index + 1} of ${STEPS.length}`;
    ui.back.disabled = index === 0;
    ui.next.textContent = index === STEPS.length - 1 ? 'Done' : 'Next';
    if (shown) target.scrollIntoView({ block: 'center' });
    else target = null;
    place();
    setTimeout(place, 400);            // async content can shift the layout
    ui.next.focus();
  }

  function place() {
    if (!ui) return;
    const pad = 6, gap = 12;
    const vw = window.innerWidth, vh = window.innerHeight;
    const pw = Math.min(380, vw - 24);
    ui.pop.style.width = pw + 'px';
    if (!target) {
      ui.spot.hidden = true;
      ui.shade.classList.add('dim');
      ui.pop.style.left = Math.round((vw - pw) / 2) + 'px';
      ui.pop.style.top = Math.round(Math.max(24, (vh - ui.pop.offsetHeight) / 2)) + 'px';
      return;
    }
    ui.shade.classList.remove('dim');
    ui.spot.hidden = false;
    const r = target.getBoundingClientRect();
    const top = Math.max(4, r.top - pad), left = Math.max(4, r.left - pad);
    const width = Math.min(vw - 8, r.width + pad * 2), height = Math.min(vh - 8, r.bottom + pad - top);
    Object.assign(ui.spot.style, { top: top + 'px', left: left + 'px', width: width + 'px', height: height + 'px' });
    const ph = ui.pop.offsetHeight;
    let y = top + height + gap;
    if (y + ph > vh - 8) y = top - ph - gap;            // above if no room below
    if (y < 8) y = Math.max(8, Math.min(vh - ph - 8, top + 16));   // overlap the top of a tall target
    let x = Math.min(Math.max(8, r.left), vw - pw - 8);
    ui.pop.style.left = Math.round(x) + 'px';
    ui.pop.style.top = Math.round(y) + 'px';
  }

  function end() {
    ss.clear();
    store.set('tour-offered', '1');
    if (!ui) return;
    document.removeEventListener('keydown', onKey, true);
    window.removeEventListener('resize', place);
    window.removeEventListener('scroll', place, true);
    ui.shade.remove(); ui.spot.remove(); ui.pop.remove();
    ui = null;
    (document.getElementById('tour-tab') || lastFocus)?.focus?.();
  }

  function start() {
    document.querySelector('.tour-offer')?.remove();
    store.set('tour-offered', '1');
    go(0);
  }

  function offer() {
    if (store.get('tour-offered') || !document.querySelector('.tabs')) return;
    const box = h('div', 'tour-offer');
    box.setAttribute('role', 'status');
    box.appendChild(h('span', null, 'New here? Take a two-minute tour of the app.'));
    const yes = h('button', 'btn small primary', 'Start tour'); yes.type = 'button';
    const no = h('button', 'btn small', 'No thanks'); no.type = 'button';
    yes.addEventListener('click', start);
    no.addEventListener('click', () => { store.set('tour-offered', '1'); box.remove(); });
    box.append(yes, no);
    document.body.appendChild(box);
  }

  document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('tour-tab')?.addEventListener('click', start);
    document.querySelectorAll('[data-start-tour]').forEach(b => b.addEventListener('click', start));
    const resume = ss.get();
    if (resume != null && STEPS[Number(resume)] && samePage(STEPS[Number(resume)].page)) {
      setTimeout(() => go(Number(resume)), 700);     // let the page load its data first
    } else {
      ss.clear();
      setTimeout(offer, 900);
    }
  });

  window.Tour = { start, end };
})();
