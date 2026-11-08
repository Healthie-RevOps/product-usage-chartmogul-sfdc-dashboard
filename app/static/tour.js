/* Guided tour across the four sections. Launch from the "Tour" tab any time; offered
 * once on a person's first visit. Steps on other pages navigate there and resume
 * (progress kept in sessionStorage). All text goes into the DOM via textContent. */
(function () {
  const STEPS = [
    { page: '/', target: '#tasks', title: 'Start here',
      text: 'Salesforce, ChartMogul and Healthie product data in one place. Each card is a task: pick the question you want answered. This tour shows each one in about two minutes.' },
    { target: '.tabs', title: 'Always one click away',
      text: 'The same tasks live in the top bar. Every page works the same way: numbered steps from choosing what you want, to reviewing it, to a list you can download or send on.' },
    { page: '/enterprise', target: '#step-choose', title: '1 · Choose accounts',
      text: 'Pick a plan (Enterprise by default, or Group, Group + API, private practice, all plans), then narrow by CSM, segment or status.' },
    { page: '/enterprise', target: '#summary-card', title: '2 · Find the gap',
      text: 'For each add-on: who has it, who is being pitched, who said no, and who was never asked. Click any number to list those accounts. For example, AI Scribe → Not pitched.' },
    { page: '/enterprise', target: '#matrix-card', title: '3 · Review accounts',
      text: 'One row per account. Hover a cell for the deal, stage, loss reason and revisit date; click an open or lost cell to open the deal in Salesforce. Tick accounts to narrow your list.' },
    { page: '/enterprise', target: '#step-use', title: '4 · Use this list',
      text: 'Download the providers and admins at those accounts for a campaign (HubSpot → Contacts → Import), or the accounts themselves. Each option says what it contains and where it goes.' },
    { page: '/accounts', target: '#summary-card', title: 'Accounts: check what’s missing',
      text: 'How complete each Enterprise account field is: CSM, renewal, references, case study, logo. Click Missing to list the gaps, or Yes to find referenceable customers.' },
    { page: '/accounts', target: '#table-card', title: 'Fill in the gaps',
      text: 'One row per account. If you have edit access, reference fields (marked ✎) save straight to Salesforce, and every change is logged with your name.' },
    { page: '/retention', target: '#step-compare', title: 'Retention: compare plans',
      text: 'Enterprise, Group, Group + API and private practice side by side: customers, MRR, how many are still paying after 12 months, revenue retention, and how many pay without using Healthie.' },
    { page: '/retention', target: '#step-who', title: 'See who to look at',
      text: 'Customers paying but not using Healthie are the likeliest to leave next; switch to see who left in the last 90 days. Both lists download as CSV.' },
    { page: '/deals', target: '#step-scope', title: 'Win / loss: set the scope',
      text: 'Choose the deal type and close dates. It starts with new-sales deals closing this year.' },
    { page: '/deals', target: '#step-results', title: 'Results',
      text: 'Win rate, won and lost ACV, and open pipeline. If reps rarely record competitors, a note here says so and links to the fix.' },
    { page: '/deals', target: '#comp-card', title: 'Who we compete with',
      text: 'Win rate, ACV and main loss reasons against each competitor. Click a row to filter the whole page to that competitor.' },
    { page: '/deals', target: '#reason-card', title: 'Why we lose',
      text: 'Loss reasons by share and lost ACV, regrettable or not. Click a reason to see the deals; download them all at the bottom.' },
    { page: '/explorer', target: '#step-choose', title: 'Products: choose products',
      text: 'Pick one or more products to see who has them, current or former.' },
    { page: '/explorer', target: '#step-trend', title: 'How they’re doing',
      text: 'Customer counts, MRR and contract value, with revenue and customers over time.' },
    { page: '/explorer', target: '#customers-card', title: 'Review customers',
      text: 'Every matching customer. Tick some to narrow the people in the next step, or click a name for the full customer page.' },
    { page: '/quality', target: '#rules-card', title: 'Data quality: pick what to fix',
      text: 'Each issue is a Salesforce field left blank. Fixing them makes Enterprise add-ons and Win / loss more accurate after the next sync.' },
    { page: '/quality', target: '#owners-card', title: 'See who fixes it',
      text: 'Click an owner, then download their fix list at the bottom of the page and send it to them.' },
    { target: '#tour-tab', title: 'That’s the tour',
      text: 'Start any task from Home, follow the numbered steps, and finish with “Use this list”. Replay this tour any time from here.' },
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
