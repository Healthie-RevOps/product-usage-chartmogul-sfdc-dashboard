/* Shared page helpers: the "What you can do here" guide and the no-data empty state.
 * All text goes into the DOM via textContent. */
(function () {
  function h(tag, cls, text) {
    const el = document.createElement(tag);
    if (cls) el.className = cls;
    if (text != null) el.textContent = text;
    return el;
  }
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private mode */ } },
  };

  /* ---------------------------------------------------------------- page guide */
  function initGuide() {
    const guide = document.querySelector('.guide');
    if (!guide) return;
    const key = 'guide-hidden:' + guide.dataset.guide;
    const toggle = document.getElementById('guide-toggle');
    const show = on => {
      guide.hidden = !on;
      if (toggle) toggle.setAttribute('aria-expanded', on);
      store.set(key, on ? '0' : '1');
    };
    guide.hidden = store.get(key) === '1';
    if (toggle) {
      toggle.setAttribute('aria-expanded', !guide.hidden);
      toggle.addEventListener('click', () => show(guide.hidden));
    }
    guide.querySelector('.guide-close')?.addEventListener('click', () => show(false));
  }

  /* ---------------------------------------------------------------- empty state */
  /* Show or hide the page body depending on whether the synced data exists yet.
   * d = API response with has_data + sync; label = e.g. "Enterprise data". */
  function dataState(d, label) {
    const body = document.getElementById('page-body');
    const box = document.getElementById('empty-state');
    if (!body || !box) return true;
    if (d.has_data) { body.hidden = false; box.hidden = true; return true; }
    body.hidden = true; box.hidden = false;
    box.textContent = '';
    const s = d.sync || {};
    const card = h('section', 'card empty-card');
    card.appendChild(h('h2', 'empty-title', `No ${label} yet`));
    let why;
    if (s.running_since) {
      why = `A sync is running now (started ${s.running_since_label}). This page fills in when it finishes, usually within 20–40 minutes. Refresh then.`;
    } else if (s.data_as_of) {
      why = `This page needs data from a newer sync than the one loaded now (${s.data_as_of_label})` +
        (s.last_failed ? ', and the most recent sync attempt failed.' : '.') +
        ` The next scheduled sync runs ${s.next_run_label}.`;
    } else {
      why = `No sync has finished yet. The next scheduled sync runs ${s.next_run_label}.`;
    }
    card.appendChild(h('p', null, why));
    if (!s.running_since) {
      card.appendChild(h('p', null, 'Can’t wait? Anyone with Railway access can start one now: Railway → product-usage-dashboard → sync service → Run now.'));
    }
    const actions = h('div', 'empty-actions');
    const tour = h('button', 'btn small', 'Take the tour meanwhile');
    tour.type = 'button';
    tour.addEventListener('click', () => window.Tour && window.Tour.start());
    actions.appendChild(tour);
    card.appendChild(actions);
    box.appendChild(card);
    return false;
  }

  window.App = { h, store, dataState };
  document.addEventListener('DOMContentLoaded', initGuide);
})();
