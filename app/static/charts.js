/* Small SVG chart kit: time-series lines (crosshair + tooltip) and a product timeline.
 * Rules (see dataviz skill): one y-axis, hairline solid grid, 2px lines, ~10% area wash,
 * >=8px markers with a 2px surface ring, tooltips enhance never gate (every chart has a
 * table view), labels via textContent only. */
(function () {
  const NS = 'http://www.w3.org/2000/svg';
  const SURFACE = '#ffffff';

  function svgEl(tag, attrs, parent) {
    const el = document.createElementNS(NS, tag);
    for (const k in attrs) el.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(el);
    return el;
  }
  function h(tag, cls, text) {
    const el = document.createElement(tag);
    if (cls) el.className = cls;
    if (text != null) el.textContent = text;
    return el;
  }

  const fmt = {
    money(cents) {
      const v = (cents || 0) / 100;
      return (v < 0 ? '-$' : '$') + Math.abs(Math.round(v)).toLocaleString('en-US');
    },
    moneyAxis(cents) {
      const v = Math.abs((cents || 0) / 100), s = cents < 0 ? '-' : '';
      if (v >= 1e6) return s + '$' + (v / 1e6).toFixed(v >= 1e7 ? 0 : 1).replace(/\.0$/, '') + 'M';
      if (v >= 1e4) return s + '$' + Math.round(v / 1e3) + 'K';
      return s + '$' + Math.round(v).toLocaleString('en-US');
    },
    signedMoney(cents) { return (cents > 0 ? '+' : cents < 0 ? '−' : '') + fmt.money(Math.abs(cents)); },
    int(n) { return Math.round(n || 0).toLocaleString('en-US'); },
    intAxis(n) { return n >= 1e4 ? Math.round(n / 1e3) + 'K' : Math.round(n).toLocaleString('en-US'); },
    date(d) { return d.toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric', timeZone: 'UTC' }); },
    month(d) { return d.toLocaleDateString('en-US', { year: 'numeric', month: 'short', timeZone: 'UTC' }); },
  };

  const parseDate = s => new Date(s + (s.length === 10 ? 'T00:00:00Z' : ''));

  function niceTicks(min, max, count) {
    if (max <= min) max = min + 1;
    const span = max - min, raw = span / count;
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => span / s <= count) || 10 * mag;
    const ticks = [];
    for (let v = Math.floor(min / step) * step; v <= max + step * 1e-9; v += step) ticks.push(Math.round(v * 1e6) / 1e6);
    if (ticks[ticks.length - 1] < max) ticks.push(ticks[ticks.length - 1] + step);
    return ticks;
  }

  function timeTicks(t0, t1, width) {
    const years = (t1 - t0) / (365.25 * 864e5);
    const maxTicks = Math.max(2, Math.floor(width / 70));
    const out = [];
    if (years > 2) {
      const step = Math.max(1, Math.ceil(years / maxTicks));
      const y0 = new Date(t0).getUTCFullYear() + 1, y1 = new Date(t1).getUTCFullYear();
      for (let y = y0; y <= y1; y += step) out.push({ t: Date.UTC(y, 0, 1), label: String(y) });
    } else {
      const months = Math.max(1, Math.round(years * 12));
      const step = Math.max(1, Math.ceil(months / maxTicks));
      const d = new Date(t0); d.setUTCDate(1); d.setUTCMonth(d.getUTCMonth() + 1);
      while (d.getTime() <= t1) {
        out.push({ t: d.getTime(), label: d.toLocaleDateString('en-US', { month: 'short', year: '2-digit', timeZone: 'UTC' }) });
        d.setUTCMonth(d.getUTCMonth() + step);
      }
    }
    return out;
  }

  function onResize(el, draw) {
    let w = 0;
    const ro = new ResizeObserver(() => {
      const nw = Math.round(el.clientWidth);
      if (nw && nw !== w) { w = nw; draw(nw); }
    });
    ro.observe(el);
    return () => ro.disconnect();
  }

  /* ------------------------------------------------------------------ line chart
   * opts: { dates: ['YYYY-MM-DD'...], series: [{name, color, values}], yFormat, yAxisFormat,
   *         height, step, area, markers: bool, events: [{...}] (parallel to dates, optional),
   *         eventLine: fn(evt)->text, dateFormat, endLabels }
   */
  function line(el, opts) {
    el.classList.add('chart');
    if (el._dispose) el._dispose();
    el.textContent = '';
    const tip = h('div', 'tip'); tip.hidden = true;
    const holder = h('div');
    el.append(holder, tip);
    const height = opts.height || 240;
    const yFmt = opts.yFormat || fmt.money, yAxisFmt = opts.yAxisFormat || fmt.moneyAxis;
    const dateFmt = opts.dateFormat || fmt.month;
    const xs = opts.dates.map(d => parseDate(d).getTime());
    const series = opts.series;

    el._dispose = onResize(el, width => {
      holder.textContent = '';
      if (!xs.length) { holder.appendChild(h('div', 'empty', opts.emptyText || 'No history yet.')); return; }
      const all = series.flatMap(s => s.values);
      const yMax = Math.max(0, ...all), yMin = Math.min(0, ...all);
      const yt = niceTicks(yMin, yMax, 4);
      const labelW = Math.max(...yt.map(v => yAxisFmt(v).length)) * 6.6 + 10;
      // direct end labels only when they won't collide; otherwise don't reserve the margin
      const ihPre = height - 10 - 26;
      const yPre = v => (v - yt[0]) / (yt[yt.length - 1] - yt[0]) * ihPre;
      const endYs = series.map(s => yPre(s.values[s.values.length - 1])).sort((a, b) => a - b);
      const endLabels = opts.endLabels && series.length > 1 && !endYs.some((y, i) => i && y - endYs[i - 1] < 13);
      const m = { t: 10, r: endLabels ? 120 : 14, b: 26, l: labelW };
      const W = width, H = height, iw = W - m.l - m.r, ih = H - m.t - m.b;
      const t0 = xs[0], t1 = xs.length > 1 ? xs[xs.length - 1] : xs[0] + 864e5;
      const X = t => m.l + (t1 === t0 ? iw / 2 : (t - t0) / (t1 - t0) * iw);
      const Y = v => m.t + ih - (v - yt[0]) / (yt[yt.length - 1] - yt[0]) * ih;

      const svg = svgEl('svg', { viewBox: `0 0 ${W} ${H}`, height: H, role: 'img',
        'aria-label': opts.ariaLabel || 'Chart', tabindex: 0 }, holder);
      const grid = svgEl('g', { class: 'grid' }, svg);
      yt.forEach(v => {
        svgEl('line', { x1: m.l, x2: W - m.r, y1: Y(v), y2: Y(v) }, grid);
        const tx = svgEl('text', { x: m.l - 8, y: Y(v) + 4, 'text-anchor': 'end' }, svg);
        tx.textContent = yAxisFmt(v);
      });
      const axis = svgEl('g', { class: 'axis' }, svg);
      svgEl('line', { x1: m.l, x2: W - m.r, y1: Y(Math.max(0, yt[0])), y2: Y(Math.max(0, yt[0])) }, axis);
      timeTicks(t0, t1, iw).forEach(tk => {
        if (tk.t < t0 || tk.t > t1) return;
        const tx = svgEl('text', { x: X(tk.t), y: H - 6, 'text-anchor': 'middle' }, svg);
        tx.textContent = tk.label;
      });

      series.forEach((s, si) => {
        let d = '';
        s.values.forEach((v, i) => {
          const x = X(xs[i]), y = Y(v);
          if (i === 0) d += `M${x},${y}`;
          else if (opts.step) d += `H${x}V${y}`;
          else d += `L${x},${y}`;
        });
        if (opts.area && series.length === 1) {
          const base = Y(Math.max(0, yt[0]));
          svgEl('path', { d: `${d}V${base}H${X(xs[0])}Z`, fill: s.color, 'fill-opacity': 0.1, stroke: 'none' }, svg);
        }
        svgEl('path', { d, fill: 'none', stroke: s.color, 'stroke-width': 2,
          'stroke-linejoin': 'round', 'stroke-linecap': 'round' }, svg);
        if (opts.markers) {
          s.values.forEach((v, i) => {
            if (opts.events && !opts.events[i]) return;
            svgEl('circle', { cx: X(xs[i]), cy: Y(v), r: 4, fill: s.color, stroke: SURFACE, 'stroke-width': 2 }, svg);
          });
        }
      });

      // direct end labels, only when they don't collide (legend carries identity regardless)
      if (endLabels) {
        series.map(s => ({ s, y: Y(s.values[s.values.length - 1]) })).forEach(e => {
          const tx = svgEl('text', { x: W - m.r + 8, y: e.y + 4, class: 'label' }, svg);
          tx.textContent = e.s.name.length > 16 ? e.s.name.slice(0, 15) + '…' : e.s.name;
        });
      }

      // crosshair + tooltip, snapping to the nearest x
      const cross = svgEl('line', { class: 'crosshair', y1: m.t, y2: m.t + ih, visibility: 'hidden' }, svg);
      const dot = svgEl('g', { visibility: 'hidden' }, svg);
      const hit = svgEl('rect', { x: m.l, y: m.t, width: iw, height: ih, fill: 'transparent' }, svg);
      let cur = -1;
      const show = i => {
        if (i < 0 || i >= xs.length) return;
        cur = i;
        const x = X(xs[i]);
        cross.setAttribute('x1', x); cross.setAttribute('x2', x); cross.setAttribute('visibility', 'visible');
        dot.textContent = '';
        series.forEach(s => svgEl('circle', { cx: x, cy: Y(s.values[i]), r: 4, fill: s.color, stroke: SURFACE, 'stroke-width': 2 }, dot));
        dot.setAttribute('visibility', 'visible');
        tip.textContent = '';
        tip.appendChild(h('div', 't', dateFmt(new Date(xs[i]))));
        series.forEach(s => {
          const r = h('div', 'r');
          const k = h('span', 'k'); k.style.background = s.color;
          r.append(k, h('b', null, yFmt(s.values[i])), h('span', 'muted', s.name));
          tip.appendChild(r);
        });
        if (opts.events && opts.events[i] && opts.eventLine) {
          for (const line of [].concat(opts.eventLine(opts.events[i]))) tip.appendChild(h('div', 'd', line));
        }
        tip.hidden = false;
        const tw = tip.offsetWidth;
        tip.style.left = Math.min(Math.max(0, x + 12), width - tw) + 'px';
        tip.style.top = (m.t + 4) + 'px';
        if (x + 12 + tw > width) tip.style.left = Math.max(0, x - tw - 12) + 'px';
      };
      const hide = () => { cross.setAttribute('visibility', 'hidden'); dot.setAttribute('visibility', 'hidden'); tip.hidden = true; };
      const nearest = px => {
        const t = t0 + (px - m.l) / iw * (t1 - t0);
        let best = 0, bd = Infinity;
        xs.forEach((x, i) => { const dd = Math.abs(x - t); if (dd < bd) { bd = dd; best = i; } });
        return best;
      };
      hit.addEventListener('pointermove', e => {
        const r = svg.getBoundingClientRect();
        show(nearest((e.clientX - r.left) * (W / r.width)));
      });
      hit.addEventListener('pointerleave', hide);
      svg.addEventListener('focus', () => show(cur < 0 ? xs.length - 1 : cur));
      svg.addEventListener('blur', hide);
      svg.addEventListener('keydown', e => {
        if (e.key === 'ArrowLeft') { show(Math.max(0, cur - 1)); e.preventDefault(); }
        if (e.key === 'ArrowRight') { show(Math.min(xs.length - 1, cur + 1)); e.preventDefault(); }
      });
    });
  }

  /* ------------------------------------------------------------------ timeline
   * segments: [{product, plan, start, end|null, current, mrr, qty}], today: 'YYYY-MM-DD'
   * One row per product; current subscriptions in the accent, ended ones in gray (emphasis). */
  function timeline(el, segments, today) {
    el.classList.add('chart');
    if (el._dispose) el._dispose();
    el.textContent = '';
    const segs = segments.filter(s => s.start);
    if (!segs.length) { el.appendChild(h('div', 'empty', 'No subscription dates on record.')); return; }
    const tip = h('div', 'tip'); tip.hidden = true;
    const holder = h('div');
    el.append(holder, tip);
    const end = parseDate(today).getTime();
    const firstStart = {};
    segs.forEach(s => { const t = parseDate(s.start).getTime(); firstStart[s.product] = Math.min(firstStart[s.product] ?? t, t); });
    const products = Object.keys(firstStart).sort((a, b) => firstStart[a] - firstStart[b]);
    const rowH = 26, bar = 12;
    const ACCENT = getComputedStyle(document.documentElement).getPropertyValue('--series-1').trim() || '#552BD8';
    const ENDED = '#B4B9C4';

    el._dispose = onResize(el, width => {
      holder.textContent = '';
      const labelW = Math.min(200, Math.max(...products.map(p => p.length)) * 6.8 + 12);
      const m = { t: 6, r: 14, b: 26, l: labelW };
      const H = m.t + products.length * rowH + m.b, iw = width - m.l - m.r;
      const t0 = Math.min(...segs.map(s => parseDate(s.start).getTime()));
      const t1 = Math.max(end, ...segs.map(s => s.end ? parseDate(s.end).getTime() : end));
      const X = t => m.l + (t - t0) / ((t1 - t0) || 1) * iw;
      const svg = svgEl('svg', { viewBox: `0 0 ${width} ${H}`, height: H, role: 'img',
        'aria-label': 'Product timeline' }, holder);
      const grid = svgEl('g', { class: 'grid' }, svg);
      timeTicks(t0, t1, iw).forEach(tk => {
        svgEl('line', { x1: X(tk.t), x2: X(tk.t), y1: m.t, y2: H - m.b }, grid);
        const tx = svgEl('text', { x: X(tk.t), y: H - 6, 'text-anchor': 'middle' }, svg);
        tx.textContent = tk.label;
      });
      products.forEach((p, i) => {
        const cy = m.t + i * rowH + rowH / 2;
        const tx = svgEl('text', { x: m.l - 10, y: cy + 4, 'text-anchor': 'end', class: 'label' }, svg);
        tx.textContent = p.length > 28 ? p.slice(0, 27) + '…' : p;
        segs.filter(s => s.product === p).forEach(s => {
          const a = X(parseDate(s.start).getTime());
          const b = Math.max(a + 6, X(s.end ? parseDate(s.end).getTime() : end));
          const g = svgEl('g', { tabindex: 0, role: 'img' }, svg);
          svgEl('rect', { x: a, y: cy - rowH / 2, width: b - a, height: rowH, fill: 'transparent' }, g);
          svgEl('rect', { x: a, y: cy - bar / 2, width: b - a, height: bar, rx: 4,
            fill: s.current ? ACCENT : ENDED, stroke: SURFACE, 'stroke-width': 1 }, g);
          const label = `${p}: ${s.plan || ''}, ${s.start} – ${s.end || 'now'}`;
          g.setAttribute('aria-label', label);
          const show = e => {
            tip.textContent = '';
            tip.appendChild(h('div', 't', s.plan || p));
            const r = h('div', 'r');
            r.append(h('b', null, s.current ? fmt.money(s.mrr) + '/mo' : 'Ended'),
                     h('span', 'muted', s.current && s.qty ? `${s.qty} × ` + p : p));
            tip.appendChild(r);
            tip.appendChild(h('div', 'd', `${fmt.date(parseDate(s.start))} → ${s.end ? fmt.date(parseDate(s.end)) : 'now'}`));
            tip.hidden = false;
            const box = svg.getBoundingClientRect();
            const px = e && e.clientX ? (e.clientX - box.left) : (a + b) / 2 * (box.width / width);
            const tw = tip.offsetWidth;
            tip.style.left = Math.min(Math.max(0, px + 12), box.width - tw) + 'px';
            tip.style.top = (cy + rowH / 2 + 4) + 'px';
          };
          g.addEventListener('pointermove', show);
          g.addEventListener('focus', () => show(null));
          g.addEventListener('pointerleave', () => { tip.hidden = true; });
          g.addEventListener('blur', () => { tip.hidden = true; });
        });
      });
    });
  }

  /* table view twin for any chart */
  function table(el, columns, rows) {
    el.textContent = '';
    const wrap = h('div', 'table-wrap'); wrap.style.maxHeight = '320px';
    const t = h('table', 'data');
    const tr = h('tr');
    columns.forEach(c => { const th = h('th', c.num ? 'num' : '', c.label); tr.appendChild(th); });
    const thead = h('thead'); thead.appendChild(tr); t.appendChild(thead);
    const tb = h('tbody');
    rows.forEach(r => {
      const row = h('tr');
      columns.forEach(c => row.appendChild(h('td', c.num ? 'num' : '', c.value(r))));
      tb.appendChild(row);
    });
    t.appendChild(tb); wrap.appendChild(t); el.appendChild(wrap);
  }

  function legend(el, items, box) {
    el.textContent = '';
    el.className = 'legend';
    items.forEach(it => {
      const s = h('span');
      const i = h('i', box ? 'box' : ''); i.style.background = it.color;
      s.append(i, document.createTextNode(it.name));
      el.appendChild(s);
    });
  }

  window.Charts = { line, timeline, table, legend, fmt, parseDate };
})();
