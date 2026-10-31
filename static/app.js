// PaperTrade client. Simulated money only: every order goes to the local simulator API.

/* ============================== utilities ============================== */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (ch) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]
));

const usd = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' });
const usdSmall = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 4 });
const intFmt = new Intl.NumberFormat('en-US');
const compact = new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 2 });
const MINUS = '−';

const money = (c) => (c == null ? '—' : usd.format(c / 100));
const signedMoney = (c) => (c == null ? '—' : (c > 0 ? '+' : c < 0 ? MINUS : '') + usd.format(Math.abs(c) / 100));
const price = (n) => (n == null ? '—' : (Math.abs(n) < 1 && n !== 0 ? usdSmall : usd).format(n));
const signedPrice = (n) => (n == null ? '—' : (n > 0 ? '+' : n < 0 ? MINUS : '') + usd.format(Math.abs(n)));
const pct = (n) => (n == null || !isFinite(n) ? '—' : (n > 0 ? '+' : n < 0 ? MINUS : '') + Math.abs(n).toFixed(2) + '%');
const tone = (n) => (n > 0 ? 'pos' : n < 0 ? 'neg' : '');
const arrow = (n) => (n > 0 ? '▲' : n < 0 ? '▼' : '');
const shares = (n) => intFmt.format(n) + (n === 1 ? ' share' : ' shares');

const dateTimeFmt = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' });
const timeFmt = new Intl.DateTimeFormat(undefined, { hour: 'numeric', minute: '2-digit', second: '2-digit' });
const fmtDateTime = (iso) => (iso ? dateTimeFmt.format(new Date(iso)) : '—');
const fmtTime = (iso) => (iso ? timeFmt.format(new Date(iso)) : '—');

function changeHtml(change, changePct, { cents = false } = {}) {
  if (change == null) return '<span class="muted">—</span>';
  const abs = cents ? signedMoney(change) : signedPrice(change);
  return `<span class="change ${tone(change)}"><span aria-hidden="true">${arrow(change)}</span> ${abs}${changePct != null ? ` (${pct(changePct)})` : ''}</span>`;
}

function sourceBadge(quote) {
  if (!quote) return '';
  const label = { live: 'Live', delayed: 'Delayed', demo: 'Demo' }[quote.source] || quote.source;
  const title = quote.source === 'demo' ? 'Simulated demo prices, not real market data'
    : quote.stale ? 'Cached price: the free-tier rate limit delayed this refresh' : 'Market data from Twelve Data';
  return `<span class="badge ${esc(quote.source)}" title="${esc(title)}">${esc(label)}</span>`;
}

class ApiError extends Error {
  constructor(message, status, field) { super(message); this.status = status; this.field = field; }
}

async function api(path, { method = 'GET', body } = {}) {
  const opts = { method, headers: { Accept: 'application/json' } };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(path, opts);
  } catch {
    throw new ApiError("Can't reach the PaperTrade server. Make sure it's still running.", 0);
  }
  let data = null;
  try { data = await res.json(); } catch { /* non-JSON */ }
  if (res.status === 401 && data?.error?.field === 'auth') showLogin();
  if (!res.ok) throw new ApiError(data?.error?.message || `Request failed (${res.status}).`, res.status, data?.error?.field);
  return data;
}

function toast(message, { error = false } = {}) {
  const el = document.createElement('div');
  el.className = 'toast' + (error ? ' error' : '');
  el.textContent = message;
  $('#toasts').append(el);
  setTimeout(() => el.remove(), error ? 7000 : 4500);
}

function applyWidths(root) {
  // CSP forbids inline style attributes, so widths are applied through the CSSOM.
  $$('[data-width]', root).forEach((el) => { el.style.width = `${Math.max(0, Math.min(100, +el.dataset.width))}%`; });
}

const skeletonRows = (n = 4) => `<div class="skeleton-rows" aria-hidden="true">${'<span class="skeleton"></span>'.repeat(n)}</div>`;

function errorHtml(message, retry = true) {
  return `<div class="alert" role="alert"><span>${esc(message)}</span>${retry ? '<button type="button" class="btn sm" data-retry>Try again</button>' : ''}</div>`;
}

function bindRetry(root, fn) {
  $$('[data-retry]', root).forEach((b) => b.addEventListener('click', () => fn()));
}

/* ============================== app state & data status ============================== */

const app = {
  status: null,
  view: null,
  pollTimer: null,
  firstRoute: true,
};

/* ============================== preferences ============================== */

const PREF_DEFAULTS = { theme: 'system', motion: 'system', orderType: 'market', demoBanner: true };
const prefs = (() => {
  try { return { ...PREF_DEFAULTS, ...JSON.parse(localStorage.getItem('pt-prefs') || '{}') }; } catch { return { ...PREF_DEFAULTS }; }
})();

function setPref(key, value) {
  prefs[key] = value;
  try { localStorage.setItem('pt-prefs', JSON.stringify(prefs)); } catch { /* private mode: applies for this visit only */ }
  applyPrefs();
}

function applyPrefs() {
  const root = document.documentElement;
  if (prefs.theme === 'light' || prefs.theme === 'dark') root.dataset.theme = prefs.theme; else delete root.dataset.theme;
  if (prefs.motion === 'reduced') root.dataset.motion = 'reduced'; else delete root.dataset.motion;
  updateBanner();
}

function updateBanner() {
  $('#mode-banner').hidden = !(app.status?.mode === 'demo' && prefs.demoBanner);
}

// Brief green/red flash when a displayed price changes between refreshes.
const lastTicks = new Map();
function flashTicks(container) {
  $$('[data-tick]', container).forEach((el) => {
    const [key, raw] = el.dataset.tick.split('|');
    const value = Number(raw);
    const prev = lastTicks.get(key);
    lastTicks.set(key, value);
    if (prev === undefined || prev === value || !motionOk()) return;
    el.classList.remove('tick-up', 'tick-down');
    void el.offsetWidth;
    el.classList.add(value > prev ? 'tick-up' : 'tick-down');
  });
}

async function loadStatus() {
  try {
    app.status = await api('/api/status');
  } catch (err) {
    app.status = null;
    setDataStatus(null, err.message);
    return;
  }
  const s = app.status;
  const banner = $('#mode-banner');
  if (s.mode === 'demo') {
    banner.innerHTML = `<span><strong>Demo mode</strong> Prices are simulated, not real market data.
      <a href="#/settings" data-jump-to="data">How to use real prices</a></span>
      <button type="button" class="icon-btn" data-dismiss-banner aria-label="Hide demo-mode banner">✕</button>`;
    $('[data-dismiss-banner]', banner).addEventListener('click', () => {
      setPref('demoBanner', false);
      toast('Banner hidden. The Demo badge in the header stays, and you can bring the banner back in Settings.');
    });
  }
  updateBanner();
  $('#footer-provider').textContent = s.mode === 'demo'
    ? 'Market data: simulated demo data.'
    : `Market data: ${s.provider} (${s.label === 'live' ? 'live' : 'delayed'}), refreshed about every ${s.quote_refresh_seconds} seconds.`;
  setDataStatus([]);
  schedulePoll();
}

function setDataStatus(quotes, errorMessage) {
  const el = $('#data-status');
  const text = $('.data-status-text', el);
  if (errorMessage) {
    el.dataset.source = 'error';
    text.textContent = 'Server unreachable';
    el.title = errorMessage;
    return;
  }
  const s = app.status;
  if (!s) return;
  let source = s.mode === 'demo' ? 'demo' : s.label;
  const list = (quotes || []).filter(Boolean);
  if (s.mode !== 'demo' && list.some((q) => q.stale)) source = 'delayed';
  const label = { live: 'Live', delayed: 'Delayed', demo: 'Demo data' }[source];
  const latest = list.map((q) => q.fetched_at).sort().pop();
  const closed = s.mode !== 'demo' && list.length && list.every((q) => q.is_market_open === false);
  el.dataset.source = source;
  text.innerHTML = `<strong>${label}</strong>${latest ? `<span class="status-time"> · Updated ${esc(fmtTime(latest))}</span>` : ''}${closed ? ' · Market closed' : ''}`;
  el.title = source === 'demo' ? 'Prices are simulated demo data, not real market data.'
    : source === 'delayed' ? 'Some prices are cached because the free-tier rate limit was reached.'
      : 'Prices from Twelve Data.';
}

function schedulePoll() {
  clearInterval(app.pollTimer);
  const seconds = app.status?.quote_refresh_seconds || 30;
  app.pollTimer = setInterval(() => {
    if (document.hidden || !app.view?.refresh) return;
    if ($('#order-dialog').open || $('#confirm-dialog').open) return;
    app.view.refresh(true);
  }, Math.max(10, seconds) * 1000);
}

document.addEventListener('visibilitychange', () => {
  if (!document.hidden && app.view?.refresh) app.view.refresh(true);
});

/* ============================== search combobox ============================== */

function initSearch() {
  const input = $('#search-input');
  const panel = $('#search-panel');
  const list = $('#search-results');
  const msg = $('#search-message');
  const live = $('#search-live');
  let items = [];
  let active = -1;
  let seq = 0;
  let timer = null;

  const open = () => { panel.hidden = false; input.setAttribute('aria-expanded', 'true'); };
  const close = () => {
    panel.hidden = true;
    input.setAttribute('aria-expanded', 'false');
    input.removeAttribute('aria-activedescendant');
    active = -1;
  };
  const setActive = (i) => {
    active = i;
    $$('[role="option"]', list).forEach((li, idx) => li.setAttribute('aria-selected', String(idx === i)));
    const li = list.children[i];
    if (li) { input.setAttribute('aria-activedescendant', li.id); li.scrollIntoView({ block: 'nearest' }); }
  };
  const message = (text) => {
    items = [];
    list.innerHTML = '';
    msg.textContent = text;
    msg.hidden = false;
    live.textContent = text;
    open();
  };
  const render = (results) => {
    items = results;
    if (!results.length) { message('No matching US stocks or ETFs.'); return; }
    msg.hidden = true;
    list.innerHTML = results.map((r, i) => {
      const q = r.quote;
      return `<li id="search-opt-${i}" role="option" aria-selected="false" class="search-option" data-symbol="${esc(r.symbol)}">
        <span class="sym">${esc(r.symbol)}</span>
        <span class="name">${esc(r.name)}${r.exchange ? ` · ${esc(r.exchange)}` : ''}</span>
        <span class="px">${q ? `${price(q.price)}<small class="${tone(q.change)}">${pct(q.change_percent)}</small>` : '<span class="muted">—</span>'}</span>
      </li>`;
    }).join('');
    live.textContent = `${results.length} result${results.length === 1 ? '' : 's'}. Use up and down arrows to choose.`;
    open();
  };
  const choose = (symbol) => {
    if (!symbol) return;
    location.hash = `#/stock/${encodeURIComponent(symbol)}`;
    input.value = '';
    close();
    input.blur();
  };
  const run = async (q) => {
    const mine = ++seq;
    message('Searching…');
    try {
      const data = await api(`/api/search?q=${encodeURIComponent(q)}`);
      if (mine === seq) render(data.results);
    } catch (err) {
      if (mine === seq) message(err.message);
    }
  };

  input.addEventListener('input', () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (!q) { seq++; close(); return; }
    timer = setTimeout(() => run(q), 280);
  });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      if (panel.hidden && items.length) open();
      if (items.length) setActive(Math.min(active + 1, items.length - 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      if (items.length) setActive(Math.max(active - 1, 0));
    } else if (e.key === 'Enter') {
      e.preventDefault();
      if (active >= 0 && items[active]) choose(items[active].symbol);
      else if (items.length) choose(items[0].symbol);
      else if (/^[A-Za-z][A-Za-z0-9.\-]{0,9}$/.test(input.value.trim())) choose(input.value.trim().toUpperCase());
    } else if (e.key === 'Escape') {
      if (!panel.hidden) close(); else input.value = '';
    }
  });
  list.addEventListener('mousedown', (e) => {
    const li = e.target.closest('[role="option"]');
    if (li) { e.preventDefault(); choose(li.dataset.symbol); }
  });
  input.addEventListener('blur', () => setTimeout(close, 120));
  input.addEventListener('focus', () => { if (items.length && input.value.trim()) open(); });

  document.addEventListener('keydown', (e) => {
    if (e.key !== '/' || e.metaKey || e.ctrlKey || e.altKey) return;
    const tag = document.activeElement?.tagName;
    if (['INPUT', 'TEXTAREA', 'SELECT'].includes(tag) || document.querySelector('dialog[open]')) return;
    e.preventDefault();
    input.focus();
  });
}

/* ============================== chart ============================== */

const NY_TZ = 'America/New_York';
const axisFormats = {
  '1D': new Intl.DateTimeFormat('en-US', { hour: 'numeric', minute: '2-digit', timeZone: NY_TZ }),
  '1W': new Intl.DateTimeFormat('en-US', { weekday: 'short', timeZone: NY_TZ }),
  '1M': new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', timeZone: NY_TZ }),
  '6M': new Intl.DateTimeFormat('en-US', { month: 'short', timeZone: NY_TZ }),
  '1Y': new Intl.DateTimeFormat('en-US', { month: 'short', year: '2-digit', timeZone: NY_TZ }),
  '5Y': new Intl.DateTimeFormat('en-US', { year: 'numeric', timeZone: NY_TZ }),
};
const tipIntraday = new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', timeZone: NY_TZ });
const tipDaily = new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', year: 'numeric', timeZone: NY_TZ });
const RANGE_LABELS = { '1D': 'today', '1W': 'past week', '1M': 'past month', '6M': 'past 6 months', '1Y': 'past year', '5Y': 'past 5 years' };

function niceTicks(min, max, count) {
  const span = max - min || Math.abs(max) || 1;
  const raw = span / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) || raw;
  const ticks = [];
  for (let v = Math.ceil(min / step) * step; v <= max + 1e-9; v += step) ticks.push(+v.toFixed(10));
  return ticks;
}

function drawChart(container, points, { range, symbol, baseline, onReadout }) {
  container.innerHTML = '';
  if (!points.length) {
    container.innerHTML = '<div class="empty"><h3>No chart data</h3><p>There is no price history for this range yet.</p></div>';
    return;
  }
  const W = Math.max(280, container.clientWidth - 16);
  const H = window.matchMedia('(max-width: 640px)').matches ? 230 : 300;
  const pad = { l: 6, r: 58, t: 14, b: 26 };
  const values = points.map((p) => p.c);
  let lo = Math.min(...values);
  let hi = Math.max(...values);
  const showBaseline = range === '1D' && baseline != null;
  if (showBaseline) { lo = Math.min(lo, baseline); hi = Math.max(hi, baseline); }
  if (hi === lo) { hi += hi * 0.01 || 1; lo -= lo * 0.01 || 1; }
  const margin = (hi - lo) * 0.08;
  const yMin = lo - margin;
  const yMax = hi + margin;
  const n = points.length;
  const x = (i) => (n === 1 ? (pad.l + W - pad.r) / 2 : pad.l + (i / (n - 1)) * (W - pad.l - pad.r));
  const y = (v) => pad.t + ((yMax - v) / (yMax - yMin)) * (H - pad.t - pad.b);
  const first = values[0];
  const last = values[n - 1];
  const ref = showBaseline ? baseline : first;
  const up = last >= ref;
  const gid = `grad-${Math.random().toString(36).slice(2)}`;

  const line = points.map((p, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(p.c).toFixed(1)}`).join('');
  const area = `${line}L${x(n - 1).toFixed(1)},${H - pad.b}L${x(0).toFixed(1)},${H - pad.b}Z`;
  const ticks = niceTicks(yMin, yMax, 4);
  const xIdx = n < 2 ? [0] : (W < 480 ? [0, 0.5, 1] : [0, 0.25, 0.5, 0.75, 1]).map((f) => Math.round(f * (n - 1)));
  const fmtAxis = axisFormats[range] || axisFormats['1M'];

  const changeAbs = last - ref;
  const changePct = ref ? (changeAbs / ref) * 100 : 0;
  const label = `${symbol} price chart, ${RANGE_LABELS[range]}: ${price(ref)} to ${price(last)}, ${pct(changePct)}. `
    + `Low ${price(Math.min(...values))}, high ${price(Math.max(...values))}. Use the left and right arrow keys to read individual prices.`;

  container.innerHTML = `
    <svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" tabindex="0" aria-label="${esc(label)}" class="${up ? 'up' : 'down'}">
      <defs><pattern id="${gid}" width="5" height="5" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
        <line x1="0" y1="0" x2="0" y2="5" class="hatch-line"/>
      </pattern></defs>
      ${ticks.map((t) => `<line class="grid-line" x1="${pad.l}" x2="${W - pad.r}" y1="${y(t).toFixed(1)}" y2="${y(t).toFixed(1)}"/>
        <text class="axis-label" x="${W - pad.r + 8}" y="${(y(t) + 4).toFixed(1)}">${esc(price(t))}</text>`).join('')}
      ${xIdx.map((i, k) => `<text class="axis-label" x="${x(i).toFixed(1)}" y="${H - 6}" text-anchor="${k === 0 ? 'start' : k === xIdx.length - 1 ? 'end' : 'middle'}">${esc(fmtAxis.format(new Date(points[i].t)))}</text>`).join('')}
      <path d="${area}" fill="url(#${gid})" class="chart-area"/>
      ${showBaseline ? `<line class="baseline" x1="${pad.l}" x2="${W - pad.r}" y1="${y(baseline).toFixed(1)}" y2="${y(baseline).toFixed(1)}"/>
        <text class="baseline-label" x="${pad.l + 4}" y="${(y(baseline) - 6).toFixed(1)}">Prev close ${esc(price(baseline))}</text>` : ''}
      <path d="${line}" class="chart-line"/>
      <rect class="end-dot" x="${(x(n - 1) - 3).toFixed(1)}" y="${(y(last) - 3).toFixed(1)}" width="6" height="6"/>
      <g class="crosshair" visibility="hidden"><line y1="${pad.t}" y2="${H - pad.b}"/><circle r="4"/></g>
      <rect class="hit" x="${pad.l}" y="0" width="${W - pad.l - pad.r}" height="${H}" fill="transparent"/>
    </svg>
    <div class="chart-tooltip" hidden></div>`;

  const svg = $('svg', container);
  const cross = $('.crosshair', svg);
  const crossLine = $('line', cross);
  const dot = $('circle', cross);
  const tip = $('.chart-tooltip', container);
  const intraday = range === '1D' || range === '1W';
  let current = -1;

  const describe = (i) => {
    const p = points[i];
    const when = intraday ? `${tipIntraday.format(new Date(p.t))} ET` : tipDaily.format(new Date(p.t));
    return { when, value: price(p.c) };
  };
  const show = (i, announce) => {
    current = Math.max(0, Math.min(n - 1, i));
    const cx = x(current);
    const cy = y(points[current].c);
    crossLine.setAttribute('x1', cx); crossLine.setAttribute('x2', cx);
    dot.setAttribute('cx', cx); dot.setAttribute('cy', cy);
    cross.setAttribute('visibility', 'visible');
    const d = describe(current);
    tip.innerHTML = `<strong>${esc(d.value)}</strong>${esc(d.when)}`;
    tip.hidden = false;
    const tipW = tip.offsetWidth;
    tip.style.left = `${Math.min(Math.max(cx + 8, tipW / 2 + 8), W - tipW / 2 + 8)}px`;
    if (announce && onReadout) onReadout(`${d.when}: ${d.value}`);
  };
  const hide = () => { cross.setAttribute('visibility', 'hidden'); tip.hidden = true; current = -1; };
  const indexAt = (clientX) => {
    const rect = svg.getBoundingClientRect();
    const px = ((clientX - rect.left) / rect.width) * W;
    return Math.round(((px - pad.l) / (W - pad.l - pad.r)) * (n - 1));
  };

  svg.addEventListener('pointermove', (e) => show(indexAt(e.clientX), false));
  svg.addEventListener('pointerdown', (e) => show(indexAt(e.clientX), false));
  svg.addEventListener('pointerleave', hide);
  svg.addEventListener('focus', () => show(n - 1, true));
  svg.addEventListener('blur', hide);
  svg.addEventListener('keydown', (e) => {
    const step = e.shiftKey ? 10 : 1;
    let next = null;
    if (e.key === 'ArrowLeft') next = (current < 0 ? n - 1 : current) - step;
    else if (e.key === 'ArrowRight') next = (current < 0 ? n - 1 : current) + step;
    else if (e.key === 'Home') next = 0;
    else if (e.key === 'End') next = n - 1;
    if (next !== null) { e.preventDefault(); show(next, true); }
  });

  return { changeAbs, changePct, ref, last };
}

/* ============================== dialogs ============================== */

function confirmDialog({ title, body, confirmText = 'Confirm', danger = false }) {
  const dlg = $('#confirm-dialog');
  const opener = document.activeElement;
  dlg.innerHTML = `
    <form method="dialog">
      <div class="dialog-head"><h2 id="confirm-dialog-title">${esc(title)}</h2></div>
      <div class="dialog-body"><p>${body}</p></div>
      <div class="dialog-foot">
        <button class="btn" value="cancel">Cancel</button>
        <button class="btn ${danger ? 'danger-solid' : 'primary'}" value="ok">${esc(confirmText)}</button>
      </div>
    </form>`;
  return new Promise((resolve) => {
    dlg.addEventListener('close', () => {
      resolve(dlg.returnValue === 'ok');
      opener?.focus?.();
    }, { once: true });
    dlg.returnValue = '';
    dlg.showModal();
    $('button[value="cancel"]', dlg).focus();
  });
}

function openOrderDialog(order, onDone) {
  const dlg = $('#order-dialog');
  const opener = document.activeElement;
  const sideWord = order.side === 'buy' ? 'Buy' : 'Sell';
  let preview = null;

  const shell = (inner, foot) => {
    dlg.innerHTML = `
      <div class="dialog-head">
        <h2 id="order-dialog-title">Review ${order.side} order</h2>
        <button type="button" class="icon-btn" data-close aria-label="Close">✕</button>
      </div>
      <div class="dialog-body" id="order-dialog-body">${inner}</div>
      <div class="dialog-foot">${foot}</div>`;
    $$('[data-close]', dlg).forEach((b) => b.addEventListener('click', () => dlg.close()));
  };

  const renderPreview = (errorMessage, offerRefresh) => {
    const p = preview;
    const e = p.estimate;
    const q = p.quote;
    const isLimit = p.order.type === 'limit';
    const rows = [
      ['Action', `<span class="badge side-${p.order.side}">${sideWord}</span>`],
      ['Stock', `${esc(p.order.symbol)} <span class="muted">· ${esc(q.name)}</span>`],
      ['Order type', isLimit ? `Limit at ${price(p.order.limit_price)}` : 'Market'],
      ['Quantity', esc(shares(p.order.quantity))],
      ['Current price', `${price(q.price)} ${sourceBadge(q)}`],
      ['Executes', e.fills_now ? 'Immediately' : `When the price reaches ${price(p.order.limit_price)}`],
      [e.fills_now ? 'Estimated fill price' : 'Limit price', price(e.price)],
    ];
    const totalLabel = order.side === 'buy' ? (e.fills_now ? 'Estimated cost' : 'Cash reserved') : 'Estimated proceeds';
    const after = [
      ['Cash after', e.fills_now ? money(e.cash_after_cents) : `${money(e.cash_after_cents)} <span class="muted">(${money(e.available_cash_after_cents)} available)</span>`],
      [`${esc(p.order.symbol)} shares after`, intFmt.format(e.shares_after)],
    ];
    const alerts = [
      p.errors.length ? `<div class="alert flush" role="alert"><ul>${p.errors.map((m) => `<li>${esc(m)}</li>`).join('')}</ul></div>` : '',
      p.warnings.length ? `<div class="alert warn flush"><ul>${p.warnings.map((m) => `<li>${esc(m)}</li>`).join('')}</ul></div>` : '',
      p.notes.length ? `<div class="alert info flush"><ul>${p.notes.map((m) => `<li>${esc(m)}</li>`).join('')}</ul></div>` : '',
      errorMessage ? `<div class="alert flush" role="alert"><span>${esc(errorMessage)}</span></div>` : '',
    ].join('');
    const confirmLabel = isLimit && !e.fills_now ? `Place limit ${order.side}` : `Confirm ${order.side}`;
    shell(
      `<dl class="summary-rows">
        ${rows.map(([k, v]) => `<div><dt>${k}</dt><dd>${v}</dd></div>`).join('')}
        <div class="total"><dt>${totalLabel}</dt><dd>${money(e.total_cents)}</dd></div>
        ${after.map(([k, v]) => `<div><dt>${k}</dt><dd>${v}</dd></div>`).join('')}
      </dl>
      ${alerts}
      <p class="help">Simulated order. No real money is used and nothing is sent to a broker.</p>`,
      `<button type="button" class="btn" data-close>Cancel</button>
       ${offerRefresh ? '<button type="button" class="btn primary" data-refresh>Refresh preview</button>'
        : `<button type="button" class="btn ${order.side}" data-confirm ${p.can_submit ? '' : 'disabled'}>${confirmLabel}</button>`}`,
    );
    $('[data-refresh]', dlg)?.addEventListener('click', load);
    const confirmBtn = $('[data-confirm]', dlg);
    if (confirmBtn) {
      confirmBtn.addEventListener('click', submit);
      if (p.can_submit) confirmBtn.focus(); else $('[data-close]', dlg.querySelector('.dialog-foot')).focus();
    }
  };

  async function load() {
    shell(`<div aria-busy="true">${skeletonRows(6)}<p class="visually-hidden">Loading order preview…</p></div>`,
      '<button type="button" class="btn" data-close>Cancel</button><button type="button" class="btn primary" disabled>Loading…</button>');
    try {
      preview = await api('/api/orders/preview', { method: 'POST', body: order });
      renderPreview();
    } catch (err) {
      shell(errorHtml(err.message), '<button type="button" class="btn" data-close>Close</button>');
      bindRetry(dlg, load);
    }
  }

  async function submit(e) {
    const btn = e.currentTarget;
    const cancelBtn = $('.dialog-foot [data-close]', dlg);
    btn.disabled = true;
    if (cancelBtn) cancelBtn.disabled = true;
    btn.classList.add('is-pending');
    btn.setAttribute('aria-busy', 'true');
    btn.innerHTML = `<span>${order.side === 'buy' ? 'Buying' : 'Selling'}…</span>`;
    const body = { ...order };
    if (order.type === 'market') body.expected_price = preview.quote.price;
    try {
      // A short minimum keeps the "working" state visible long enough to register.
      const [result] = await Promise.all([api('/api/orders', { method: 'POST', body }), wait(motionOk() ? 520 : 0)]);
      btn.classList.remove('is-pending');
      btn.classList.add('is-done');
      btn.removeAttribute('aria-busy');
      btn.innerHTML = `<svg class="check" viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><path d="M3 8.5l3.2 3.2L13 4.8"/></svg><span>${result.transaction ? 'Filled' : 'Order placed'}</span>`;
      navigator.vibrate?.(result.transaction ? [12, 40, 18] : 12);
      await wait(motionOk() ? 720 : 200);
      dlg.close();
      showTradeTicket(result);
      onDone?.(result);
    } catch (err) {
      if (err.status === 409) renderPreview(err.message, true);
      else renderPreview(err.message);
    }
  }

  dlg.addEventListener('close', () => opener?.focus?.(), { once: true });
  dlg.showModal();
  load();
}

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const motionOk = () => prefs.motion !== 'reduced' && !window.matchMedia('(prefers-reduced-motion: reduce)').matches;

function showTradeTicket(result) {
  const t = result.transaction;
  const o = result.order;
  const side = o.side;
  const filled = Boolean(t);
  const stampText = filled ? (side === 'buy' ? 'Bought' : 'Sold') : 'Open';
  const headline = filled
    ? `${side === 'buy' ? 'Bought' : 'Sold'} ${shares(t.quantity)} of ${t.symbol}`
    : `Limit ${side} placed: ${shares(o.quantity)} of ${o.symbol}`;
  const detail = filled
    ? `at ${price(t.price)} · ${side === 'buy' ? 'cost' : 'proceeds'} ${money(t.amount_cents)}`
    : `fills at ${price(o.limit_price)} or better`;
  const el = document.createElement('div');
  el.className = `ticket ${side}${filled ? '' : ' open'}`;
  el.innerHTML = `
    <div class="ticket-head"><span>Trade ticket · No. ${String(o.id).padStart(4, '0')}</span><span>${esc(fmtTime(t?.created_at || o.created_at))}</span></div>
    <div class="ticket-body">
      <strong>${esc(headline)}</strong>
      <span>${esc(detail)}</span>
      <div class="stamp" aria-hidden="true">${stampText}${Array.from({ length: 10 }, () => '<i class="spark"></i>').join('')}</div>
    </div>
    <div class="ticket-foot">Simulated trade · no real money moved</div>`;
  $$('.spark', el).forEach((s, i) => {
    s.style.setProperty('--a', `${i * 36 + Math.random() * 14}deg`);
    s.style.setProperty('--d', `${28 + Math.random() * 22}px`);
  });
  $('#toasts').append(el);
  const dismiss = () => {
    if (el.classList.contains('leaving')) return;
    el.classList.add('leaving');
    setTimeout(() => el.remove(), motionOk() ? 320 : 0);
  };
  el.addEventListener('click', dismiss);
  setTimeout(dismiss, 5200);
}

/* ============================== shared renderers ============================== */

function emptyState(title, text, action = '') {
  return `<div class="empty"><h3>${esc(title)}</h3><p>${text}</p>${action}</div>`;
}

function ordersListHtml(orders, { showSymbol = true } = {}) {
  if (!orders.length) return '';
  return `<ul class="list">${orders.map((o) => `
    <li class="list-item">
      <div>
        <span class="badge side-${o.side}">${o.side}</span>
        ${showSymbol ? `<a class="title" href="#/stock/${encodeURIComponent(o.symbol)}">${esc(o.symbol)}</a>` : ''}
        <span class="sub">${esc(shares(o.quantity))} · limit ${price(o.limit_price)}${o.side === 'buy' ? ` · ${money(o.reserved_cents)} reserved` : ''}</span>
      </div>
      <span class="right muted"><small>${esc(fmtDateTime(o.created_at))}</small></span>
      <button type="button" class="btn sm danger" data-cancel="${o.id}" aria-label="Cancel ${esc(o.side)} order for ${esc(o.symbol)}">Cancel</button>
    </li>`).join('')}</ul>`;
}

function bindCancel(root, after) {
  $$('[data-cancel]', root).forEach((btn) => btn.addEventListener('click', async () => {
    btn.disabled = true;
    try {
      await api(`/api/orders/${btn.dataset.cancel}/cancel`, { method: 'POST', body: {} });
      toast('Order cancelled. Any reserved cash or shares were released.');
      after();
    } catch (err) {
      toast(err.message, { error: true });
      btn.disabled = false;
    }
  }));
}

function txnVerb(t) { return t.side === 'buy' ? 'Bought' : 'Sold'; }

/* ============================== dashboard ============================== */

function DashboardView(root) {
  root.innerHTML = `
    <div id="away"></div>
    <div class="hero-grid">
    <section class="hero" aria-labelledby="hero-label">
      <h1 class="eyebrow" id="hero-label">Simulated portfolio value</h1>
      <p class="hero-value" id="hero-value"><span class="skeleton lg" aria-hidden="true"></span></p>
      <p class="hero-sub" id="hero-sub">&nbsp;</p>
    </section>
    <section class="card hero-side" aria-labelledby="alloc-h">
      <div class="card-head"><h2 id="alloc-h">Allocation</h2><span class="meta" id="alloc-meta"></span></div>
      <div id="allocation">${skeletonRows(3)}</div>
    </section>
    </div>
    <section aria-label="Account summary"><div class="stats" id="stats">
      ${['Cash', 'Invested', 'Total profit / loss', "Today's change"].map((l) => `
        <div class="card stat"><div class="stat-label">${l}</div><span class="skeleton lg"></span></div>`).join('')}
    </div></section>
    <div class="grid-main">
      <div class="stack">
        <section class="card" aria-labelledby="holdings-h">
          <div class="card-head"><h2 id="holdings-h">Holdings</h2><span class="meta" id="holdings-meta"></span></div>
          <div id="holdings">${skeletonRows(5)}</div>
        </section>
        <section class="card" aria-labelledby="activity-h">
          <div class="card-head"><h2 id="activity-h">Recent activity</h2><a href="#/history" class="meta">View all</a></div>
          <div id="activity">${skeletonRows(3)}</div>
        </section>
      </div>
      <aside class="stack" aria-label="Watchlist and orders">
        <section class="card" aria-labelledby="watch-h">
          <div class="card-head"><h2 id="watch-h">Watchlist</h2></div>
          <div id="watchlist">${skeletonRows(4)}</div>
          <form class="toolbar" id="watch-form" novalidate>
            <label for="watch-input" class="visually-hidden">Add ticker to watchlist</label>
            <input id="watch-input" class="input" placeholder="Add ticker, e.g. KO" maxlength="10" autocomplete="off" spellcheck="false">
            <button class="btn sm" type="submit">Add</button>
          </form>
        </section>
        <section class="card" aria-labelledby="orders-h">
          <div class="card-head"><h2 id="orders-h">Open orders</h2></div>
          <div id="open-orders">${skeletonRows(2)}</div>
        </section>
      </aside>
    </div>`;

  let loaded = false;

  async function refresh(silent = false) {
    const [acct, watch] = await Promise.allSettled([api('/api/account?seen=1'), api('/api/watchlist')]);
    const quotes = [];
    if (acct.status === 'fulfilled') {
      renderAccount(acct.value);
      acct.value.holdings.forEach((h) => quotes.push(h.quote));
    } else if (!silent || !loaded) {
      $('#hero-value').textContent = '—';
      $('#stats').innerHTML = `<div class="card">${errorHtml(acct.reason.message)}</div>`;
      $('#holdings').innerHTML = '';
      $('#activity').innerHTML = '';
      $('#open-orders').innerHTML = '';
      bindRetry($('#stats'), () => refresh());
    }
    if (watch.status === 'fulfilled') {
      renderWatchlist(watch.value.items);
      watch.value.items.forEach((i) => quotes.push(i.quote));
    } else if (!silent || !loaded) {
      $('#watchlist').innerHTML = errorHtml(watch.reason.message);
      bindRetry($('#watchlist'), () => refresh());
    }
    if (acct.status === 'fulfilled' || watch.status === 'fulfilled') {
      loaded = true;
      setDataStatus(quotes);
    } else if (silent) {
      setDataStatus(null, acct.reason.message);
    }
  }

  function renderAway(away) {
    const el = $('#away');
    if (!away || el.dataset.shown) return;
    el.dataset.shown = '1';
    const fills = away.fills || [];
    el.innerHTML = `
      <section class="away card" aria-labelledby="away-h">
        <div class="away-head">
          <h2 id="away-h" class="eyebrow">While you were away · since ${esc(fmtDateTime(away.since))}</h2>
          <button type="button" class="icon-btn" data-dismiss aria-label="Dismiss summary">✕</button>
        </div>
        <p class="away-value">Portfolio ${changeHtml(away.value_change_cents, away.value_change_percent, { cents: true })}
          <span class="muted">${money(away.previous_value_cents)} → ${money(away.previous_value_cents + away.value_change_cents)}</span></p>
        ${fills.length ? `<ul class="list">${fills.map((t) => `
          <li class="list-item">
            <div><span class="badge side-${t.side}">${t.side}</span> <a class="title" href="#/stock/${encodeURIComponent(t.symbol)}">${esc(t.symbol)}</a>
              <span class="sub">Limit order filled: ${esc(shares(t.quantity))} at ${price(t.price)} · ${esc(fmtDateTime(t.created_at))}</span></div>
            <span class="right">${t.side === 'buy' ? MINUS : '+'}${money(t.amount_cents)}</span><span></span>
          </li>`).join('')}</ul>` : '<p class="away-none">No orders filled while you were gone.</p>'}
      </section>`;
    $('[data-dismiss]', el).addEventListener('click', () => { el.innerHTML = ''; });
    fills.slice(0, 3).forEach((t, i) => setTimeout(() => showTradeTicket({
      transaction: t, order: { id: t.order_id, side: t.side, quantity: t.quantity, symbol: t.symbol, created_at: t.created_at },
    }), 400 + i * 700));
  }

  function renderAccount(a) {
    renderAway(a.away);
    renderAllocation(a);
    const [whole, cents] = money(a.portfolio_value_cents).split('.');
    $('#hero-value').innerHTML = `${esc(whole)}<span class="cents">.${esc(cents)}</span>`;
    $('#hero-sub').innerHTML = `${changeHtml(a.day_change_cents, a.day_change_percent, { cents: true })} today. `
      + `Started with ${money(a.starting_balance_cents)} of practice money; nothing here is real.`;
    const stats = [
      { label: 'Cash', value: money(a.available_cash_cents),
        sub: a.reserved_cash_cents ? `${money(a.reserved_cash_cents)} reserved for open orders` : 'Available to trade' },
      { label: 'Invested', value: money(a.holdings_value_cents),
        sub: `${a.holdings.length} position${a.holdings.length === 1 ? '' : 's'}` },
      { label: 'Total profit / loss', value: signedMoney(a.total_pl_cents), cls: tone(a.total_pl_cents),
        sub: `<span class="${tone(a.total_pl_cents)}">${pct(a.total_pl_percent)}</span> all time · realized ${signedMoney(a.realized_pl_cents)}` },
      { label: "Today's change", value: signedMoney(a.day_change_cents), cls: tone(a.day_change_cents),
        sub: `<span class="${tone(a.day_change_cents)}">${pct(a.day_change_percent)}</span>${a.day_change_complete ? ' since previous close' : ' · some prices unavailable'}` },
    ];
    $('#stats').innerHTML = stats.map((s) => `
      <div class="card stat">
        <div class="stat-label">${s.label}</div>
        <div class="stat-value ${s.cls || ''}">${s.value}</div>
        <div class="stat-sub">${s.sub}</div>
      </div>`).join('');

    const holdings = $('#holdings');
    $('#holdings-meta').textContent = a.holdings.length ? `${a.holdings.length} position${a.holdings.length === 1 ? '' : 's'} · ${money(a.holdings_value_cents)}` : '';
    if (!a.holdings.length) {
      holdings.innerHTML = emptyState('No holdings yet',
        'Search for a stock above, or pick one from your watchlist, to place your first simulated trade.',
        '<button type="button" class="btn primary" data-focus-search>Find a stock</button>');
      $('[data-focus-search]', holdings).addEventListener('click', () => $('#search-input').focus());
    } else {
      const errs = Object.keys(a.quote_errors || {});
      holdings.innerHTML = `
        ${errs.length ? `<div class="alert warn">Live prices unavailable for ${esc(errs.join(', '))}; showing cost basis instead.</div>` : ''}
        <div class="table-wrap"><table class="responsive">
          <caption class="visually-hidden">Holdings with profit and loss</caption>
          <thead><tr>
            <th scope="col">Symbol</th><th scope="col">Shares</th><th scope="col">Avg cost</th><th scope="col">Price</th>
            <th scope="col">Today</th><th scope="col">Market value</th><th scope="col">Total P/L</th><th scope="col">Allocation</th>
          </tr></thead>
          <tbody>${a.holdings.map((h) => `
            <tr class="clickable" data-href="#/stock/${encodeURIComponent(h.symbol)}">
              <td class="sym-cell"><a href="#/stock/${encodeURIComponent(h.symbol)}">${esc(h.symbol)}</a><span class="sub">${esc(h.name)}</span></td>
              <td data-label="Shares">${intFmt.format(h.shares)}${h.reserved_shares ? ` <span class="muted">(${intFmt.format(h.reserved_shares)} reserved)</span>` : ''}</td>
              <td data-label="Avg cost">${price(h.avg_cost)}</td>
              <td data-label="Price">${h.price_available ? price(h.price) : '<span class="muted">—</span>'}${h.quote?.stale ? ' <span class="badge delayed">cached</span>' : ''}</td>
              <td data-label="Today">${h.day_change_cents == null ? '—' : `<span class="${tone(h.day_change_cents)}">${signedMoney(h.day_change_cents)}</span>`}</td>
              <td data-label="Market value">${money(h.market_value_cents)}</td>
              <td data-label="Total P/L"><span class="${tone(h.unrealized_pl_cents)}">${signedMoney(h.unrealized_pl_cents)}<br><small>${pct(h.unrealized_pl_percent)}</small></span></td>
              <td data-label="Allocation"><span class="alloc"><span class="alloc-bar" aria-hidden="true"><span data-width="${h.allocation_percent.toFixed(1)}"></span></span>${h.allocation_percent.toFixed(1)}%</span></td>
            </tr>`).join('')}
          </tbody>
        </table></div>`;
      applyWidths(holdings);
      $('tbody', holdings).addEventListener('click', (e) => {
        if (e.target.closest('a, button')) return;
        const tr = e.target.closest('tr[data-href]');
        if (tr) location.hash = tr.dataset.href;
      });
    }

    const activity = $('#activity');
    activity.innerHTML = a.recent_transactions.length
      ? `<ul class="list">${a.recent_transactions.map((t) => `
          <li class="list-item">
            <div><a class="title" href="#/stock/${encodeURIComponent(t.symbol)}">${txnVerb(t)} ${esc(t.symbol)}</a>
              <span class="sub">${esc(shares(t.quantity))} at ${price(t.price)} · ${esc(fmtDateTime(t.created_at))}</span></div>
            <span class="right">${t.side === 'buy' ? MINUS : '+'}${money(t.amount_cents)}
              ${t.realized_pl_cents != null ? `<small class="${tone(t.realized_pl_cents)}">${signedMoney(t.realized_pl_cents)} P/L</small>` : ''}</span>
            <span></span>
          </li>`).join('')}</ul>`
      : emptyState('No trades yet', 'Your filled orders will show up here.');

    const ordersEl = $('#open-orders');
    ordersEl.innerHTML = a.open_orders.length
      ? ordersListHtml(a.open_orders)
      : '<p class="empty">No open orders. Limit orders that haven\'t filled appear here.</p>';
    bindCancel(ordersEl, () => refresh());
  }

  function renderAllocation(a) {
    const total = a.portfolio_value_cents || 1;
    const top = a.holdings.slice(0, 5).map((h, i) => ({ label: h.symbol, value: h.market_value_cents, cls: `seg-${i}`, href: `#/stock/${encodeURIComponent(h.symbol)}` }));
    const rest = a.holdings.slice(5).reduce((sum, h) => sum + h.market_value_cents, 0);
    if (rest) top.push({ label: 'Other', value: rest, cls: 'seg-other' });
    const segs = [...top, { label: 'Cash', value: a.cash_cents, cls: 'seg-cash' }];
    $('#alloc-meta').textContent = `${a.holdings.length} position${a.holdings.length === 1 ? '' : 's'} + cash`;
    $('#allocation').innerHTML = `
      <div class="alloc-stack" aria-hidden="true">${segs.map((s) => `<span class="${s.cls}" data-width="${(s.value / total * 100).toFixed(2)}"></span>`).join('')}</div>
      <ul class="alloc-legend">${segs.map((s) => `
        <li><i class="${s.cls}" aria-hidden="true"></i>
          ${s.href ? `<a href="${s.href}">${esc(s.label)}</a>` : `<span>${esc(s.label)}</span>`}
          <span class="pct">${(s.value / total * 100).toFixed(1)}%</span>
          <span class="muted">${money(s.value)}</span></li>`).join('')}
      </ul>
      ${a.holdings.length ? '' : '<p class="help alloc-hint">All cash for now. Buy a stock and it shows up here.</p>'}`;
    applyWidths($('#allocation'));
  }

  function renderWatchlist(items) {
    const el = $('#watchlist');
    if (!items.length) {
      el.innerHTML = emptyState('Your watchlist is empty', 'Add a ticker below, or use the Watch button on any stock page.');
      return;
    }
    el.innerHTML = `<ul class="list">${items.map((i) => {
      const q = i.quote;
      return `<li class="list-item">
        <div><a class="title" href="#/stock/${encodeURIComponent(i.symbol)}">${esc(i.symbol)}</a><span class="sub">${esc(q?.name || i.name || '')}</span></div>
        <span class="right">${q ? `<span data-tick="w-${esc(i.symbol)}|${q.price}">${price(q.price)}</span><small class="${tone(q.change)}"><span aria-hidden="true">${arrow(q.change)}</span> ${pct(q.change_percent)}</small>`
          : `<small class="muted" title="${esc(i.error || '')}">Price unavailable</small>`}</span>
        <button type="button" class="icon-btn" data-unwatch="${esc(i.symbol)}" aria-label="Remove ${esc(i.symbol)} from watchlist">✕</button>
      </li>`;
    }).join('')}</ul>`;
    flashTicks(el);
    $$('[data-unwatch]', el).forEach((btn) => btn.addEventListener('click', async () => {
      btn.disabled = true;
      try {
        await api(`/api/watchlist/${encodeURIComponent(btn.dataset.unwatch)}`, { method: 'DELETE' });
        toast(`Removed ${btn.dataset.unwatch} from your watchlist.`);
        refresh(true);
      } catch (err) { toast(err.message, { error: true }); btn.disabled = false; }
    }));
  }

  $('#watch-form', root).addEventListener('submit', async (e) => {
    e.preventDefault();
    const input = $('#watch-input', root);
    const symbol = input.value.trim().toUpperCase();
    if (!/^[A-Z][A-Z0-9.\-]{0,9}$/.test(symbol)) {
      toast('Enter a valid ticker symbol, like AAPL.', { error: true });
      input.focus();
      return;
    }
    try {
      await api('/api/watchlist', { method: 'POST', body: { symbol } });
      input.value = '';
      toast(`Added ${symbol} to your watchlist.`);
      refresh(true);
    } catch (err) { toast(err.message, { error: true }); }
  });

  refresh();
  return { refresh, title: 'Dashboard' };
}

/* ============================== stock detail ============================== */

function StockView(root, { symbol }) {
  let data = null;
  let range = '1D';
  let historyCache = null;
  let resizeObserver = null;
  let loaded = false;
  let flashPosition = false;

  root.innerHTML = `<div aria-busy="true"><span class="skeleton lg"></span><span class="skeleton block"></span></div>`;

  function layout() {
    root.innerHTML = `
      <nav aria-label="Breadcrumb" class="page-sub"><a href="#/">Dashboard</a> / ${esc(symbol)}</nav>
      <div class="stock-head">
        <div>
          <div class="stock-title"><h1>${esc(symbol)}</h1><span class="name" id="s-name"></span></div>
          <div class="stock-price"><span class="px" id="s-price"></span><span id="s-change"></span></div>
          <div class="stock-meta" id="s-meta"></div>
        </div>
        <button type="button" class="btn watch-btn" id="watch-btn" aria-pressed="false"></button>
      </div>
      <div class="grid-main">
        <div class="stack">
          <section class="card" aria-labelledby="chart-h">
            <div class="card-head">
              <h2 id="chart-h">Price chart</h2>
              <div class="ranges" role="group" aria-label="Chart time range">
                ${Object.keys(RANGE_LABELS).map((r) => `<button type="button" data-range="${r}" aria-pressed="${r === range}" aria-label="${RANGE_LABELS[r]}">${r}</button>`).join('')}
              </div>
            </div>
            <div class="chart-wrap" id="chart"></div>
            <div class="chart-foot"><span id="chart-change"></span><span id="chart-readout" aria-live="polite"></span></div>
          </section>
          <section class="card" aria-labelledby="kstats-h">
            <div class="card-head"><h2 id="kstats-h">Key statistics</h2></div>
            <dl class="kv" id="kstats"></dl>
          </section>
          <section class="card" aria-labelledby="sym-activity-h">
            <div class="card-head"><h2 id="sym-activity-h">Your ${esc(symbol)} orders &amp; trades</h2><a class="meta" href="#/history">Full history</a></div>
            <div id="sym-orders"></div>
            <div id="sym-txns"></div>
          </section>
        </div>
        <aside class="stack">
          <section class="card" aria-labelledby="trade-h">
            <div class="card-head"><h2 id="trade-h">Trade ${esc(symbol)}</h2><span class="badge demo" id="sim-badge">Simulated</span></div>
            <div class="card-body">
              <form id="trade-form" novalidate>
                <fieldset class="segmented">
                  <legend class="visually-hidden">Order side</legend>
                  <input type="radio" name="side" id="side-buy" value="buy" checked><label for="side-buy">Buy</label>
                  <input type="radio" name="side" id="side-sell" value="sell"><label for="side-sell">Sell</label>
                </fieldset>
                <div class="field">
                  <label for="order-type">Order type</label>
                  <select id="order-type" class="input" aria-describedby="type-help">
                    <option value="market">Market: fill now at the current price</option>
                    <option value="limit">Limit: fill only at your price or better</option>
                  </select>
                </div>
                <div class="field">
                  <label for="qty">Quantity (whole shares)</label>
                  <input id="qty" class="input" type="number" inputmode="numeric" min="1" step="1" max="1000000" placeholder="0" aria-describedby="qty-help qty-error">
                  <p class="help" id="qty-help"></p>
                  <p class="field-error" id="qty-error" hidden></p>
                </div>
                <div class="field" id="limit-field" hidden>
                  <label for="limit">Limit price</label>
                  <div class="input-prefix"><span aria-hidden="true">$</span>
                    <input id="limit" class="input" type="number" inputmode="decimal" min="0.0001" step="0.01" placeholder="0.00" aria-describedby="limit-help limit-error">
                  </div>
                  <p class="help" id="limit-help"></p>
                  <p class="field-error" id="limit-error" hidden></p>
                </div>
                <dl class="estimate" aria-live="polite">
                  <div><dt id="est-label">Estimated cost</dt><dd id="est-total">—</dd></div>
                  <div><dt>Buying power</dt><dd id="est-bp">—</dd></div>
                </dl>
                <button type="submit" class="btn primary block">Review order</button>
              </form>
            </div>
          </section>
          <section class="card" aria-labelledby="pos-h">
            <div class="card-head"><h2 id="pos-h">Your position</h2></div>
            <div id="position"></div>
          </section>
        </aside>
      </div>`;

    $$('[data-range]', root).forEach((btn) => btn.addEventListener('click', () => {
      range = btn.dataset.range;
      $$('[data-range]', root).forEach((b) => b.setAttribute('aria-pressed', String(b === btn)));
      loadChart();
    }));
    $('#watch-btn', root).addEventListener('click', toggleWatch);
    const form = $('#trade-form', root);
    form.addEventListener('input', updateForm);
    form.addEventListener('change', updateForm);
    form.addEventListener('submit', submitOrder);
    resizeObserver = new ResizeObserver(() => { if (historyCache) renderChart(); });
    resizeObserver.observe($('#chart', root));
  }

  async function refresh(silent = false) {
    try {
      data = await api(`/api/stocks/${encodeURIComponent(symbol)}`);
    } catch (err) {
      if (silent && loaded) return;
      const notFound = err.status === 404;
      root.innerHTML = `
        <div class="card">${notFound
          ? emptyState(`We couldn't find ${symbol}`, esc(err.message), '<a class="btn primary" href="#/">Back to dashboard</a>')
          : `<div class="card-head"><h2>${esc(symbol)}</h2></div>${errorHtml(err.message)}`}</div>`;
      bindRetry(root, () => refresh());
      return;
    }
    const first = !loaded;
    if (!loaded) { layout(); loaded = true; loadChart(); }
    else if (silent && range === '1D') loadChart(true);
    render();
    if (first && prefs.orderType === 'limit') {
      $('#order-type', root).value = 'limit';
      $('#limit', root).value = data.quote.price.toFixed(2);
      updateForm();
    }
  }

  function render() {
    const { quote: q, position, open_orders: orders, transactions } = data;
    document.title = `${symbol} ${price(q.price)} · PaperTrade`;
    $('#s-name', root).textContent = q.name;
    const px = $('#s-price', root);
    px.textContent = price(q.price);
    px.dataset.tick = `s-${symbol}|${q.price}`;
    flashTicks(px.parentElement);
    $('#s-change', root).innerHTML = `${changeHtml(q.change, q.change_percent)} <span class="muted">today</span>`;
    const asOf = q.as_of ? `Price as of ${fmtDateTime(q.as_of)}` : '';
    $('#s-meta', root).innerHTML = [
      sourceBadge(q),
      q.source !== 'demo' && q.is_market_open === false ? '<span class="badge delayed">Market closed</span>' : '',
      `<span>${esc(asOf)}</span>`,
      `<span>Updated ${esc(fmtTime(q.fetched_at))}</span>`,
      q.exchange ? `<span>${esc(q.exchange)} · ${esc(q.currency || 'USD')}</span>` : '',
    ].filter(Boolean).join('<span aria-hidden="true">·</span>');
    setDataStatus([q]);

    const wb = $('#watch-btn', root);
    wb.setAttribute('aria-pressed', String(data.watched));
    wb.innerHTML = data.watched ? '<span aria-hidden="true">★</span> Watching' : '<span aria-hidden="true">☆</span> Watch';
    wb.setAttribute('aria-label', data.watched ? `Remove ${symbol} from watchlist` : `Add ${symbol} to watchlist`);

    const stat = (k, v) => `<div><dt>${k}</dt><dd>${v}</dd></div>`;
    $('#kstats', root).innerHTML = [
      stat('Open', price(q.open)), stat('Day high', price(q.high)), stat('Day low', price(q.low)),
      stat('Previous close', price(q.previous_close)), stat('Volume', q.volume != null ? compact.format(q.volume) : '—'),
      stat('52-week range', q.week52_low != null ? `${price(q.week52_low)} – ${price(q.week52_high)}` : '—'),
    ].join('');

    const pos = $('#position', root);
    if (position) {
      pos.innerHTML = `<dl class="kv two">
        ${stat('Shares', intFmt.format(position.shares) + (data.available_shares !== position.shares ? ` <span class="muted">(${intFmt.format(data.available_shares)} free)</span>` : ''))}
        ${stat('Average cost', price(position.avg_cost))}
        ${stat('Market value', money(position.market_value_cents))}
        ${stat('Cost basis', money(position.cost_basis_cents))}
        ${stat('Total P/L', `<span class="${tone(position.unrealized_pl_cents)}">${signedMoney(position.unrealized_pl_cents)} (${pct(position.unrealized_pl_percent)})</span>`)}
        ${stat("Today's P/L", `<span class="${tone(position.day_change_cents)}">${signedMoney(position.day_change_cents)}</span>`)}
      </dl>`;
    } else {
      pos.innerHTML = `<p class="empty">You don't own any ${esc(symbol)} yet.</p>`;
    }
    if (flashPosition) {
      flashPosition = false;
      const card = pos.closest('.card');
      card.classList.remove('flash');
      void card.offsetWidth; // restart the animation
      card.classList.add('flash');
    }

    const ordersEl = $('#sym-orders', root);
    ordersEl.innerHTML = orders.length ? `<h3 class="card-body page-sub">Open orders</h3>${ordersListHtml(orders, { showSymbol: false })}` : '';
    bindCancel(ordersEl, () => refresh(true));
    $('#sym-txns', root).innerHTML = transactions.length
      ? `<ul class="list">${transactions.map((t) => `
          <li class="list-item">
            <div><span class="badge side-${t.side}">${t.side}</span> <strong>${esc(shares(t.quantity))}</strong>
              <span class="sub">at ${price(t.price)} · ${esc(fmtDateTime(t.created_at))}</span></div>
            <span class="right">${money(t.amount_cents)}${t.realized_pl_cents != null ? `<small class="${tone(t.realized_pl_cents)}">${signedMoney(t.realized_pl_cents)} realized</small>` : ''}</span><span></span>
          </li>`).join('')}</ul>`
      : (orders.length ? '' : `<p class="empty">No ${esc(symbol)} trades yet.</p>`);

    updateForm();
  }

  async function loadChart(silent = false) {
    const el = $('#chart', root);
    const want = range;
    if (!silent) {
      el.innerHTML = '<span class="skeleton block" aria-hidden="true"></span><p class="visually-hidden">Loading chart…</p>';
      $('#chart-change', root).textContent = '';
    }
    try {
      const hist = await api(`/api/stocks/${encodeURIComponent(symbol)}/history?range=${want}`);
      if (want !== range) return;
      historyCache = hist;
      renderChart();
    } catch (err) {
      if (want !== range || silent) return;
      historyCache = null;
      el.innerHTML = errorHtml(err.message);
      bindRetry(el, () => loadChart());
    }
  }

  function renderChart() {
    const hist = historyCache;
    const el = $('#chart', root);
    if (!hist || !el) return;
    const result = drawChart(el, hist.points, {
      range: hist.range, symbol, baseline: data?.quote?.previous_close,
      onReadout: (text) => { $('#chart-readout', root).textContent = text; },
    });
    if (result) {
      $('#chart-change', root).innerHTML = `${changeHtml(result.changeAbs, result.changePct)} <span class="muted">${RANGE_LABELS[hist.range]}</span>`
        + (hist.stale ? ' <span class="badge delayed">cached</span>' : '');
    }
  }

  function readForm() {
    return {
      side: $('input[name="side"]:checked', root).value,
      type: $('#order-type', root).value,
      qtyRaw: $('#qty', root).value.trim(),
      limitRaw: $('#limit', root).value.trim(),
    };
  }

  function setFieldError(id, message) {
    const input = $(`#${id}`, root);
    const err = $(`#${id}-error`, root);
    input.setAttribute('aria-invalid', message ? 'true' : 'false');
    err.textContent = message || '';
    err.hidden = !message;
  }

  function validate(f) {
    const errors = {};
    const qty = Number(f.qtyRaw);
    if (!f.qtyRaw) errors.qty = 'Enter how many shares.';
    else if (!Number.isInteger(qty) || qty < 1) errors.qty = 'Quantity must be a whole number of at least 1.';
    else if (qty > 1000000) errors.qty = 'Quantity cannot exceed 1,000,000 shares.';
    let limit = null;
    if (f.type === 'limit') {
      limit = Number(f.limitRaw);
      if (!f.limitRaw) errors.limit = 'Enter a limit price.';
      else if (!isFinite(limit) || limit <= 0) errors.limit = 'Limit price must be greater than $0.';
      else if (!/^\d+(\.\d{1,4})?$/.test(f.limitRaw)) errors.limit = 'Use at most 4 decimal places.';
    }
    if (!errors.qty && data) {
      const unit = f.type === 'limit' && limit ? limit : data.quote.price;
      if (f.side === 'buy' && Math.round(unit * qty * 100) > data.available_cash_cents) {
        errors.qty = `That would cost about ${money(Math.round(unit * qty * 100))}, more than your ${money(data.available_cash_cents)} buying power.`;
      }
      if (f.side === 'sell' && qty > data.available_shares) {
        errors.qty = data.available_shares > 0 ? `You can sell at most ${shares(data.available_shares)}.` : `You don't have any ${symbol} shares available to sell.`;
      }
    }
    return { errors, qty, limit };
  }

  function updateForm(e) {
    if (!data) return;
    const f = readForm();
    const q = data.quote;
    $('#limit-field', root).hidden = f.type !== 'limit';
    if (f.type === 'limit' && !f.limitRaw && e?.target?.id === 'order-type') {
      $('#limit', root).value = q.price.toFixed(2);
      f.limitRaw = $('#limit', root).value;
    }
    const unit = f.type === 'limit' && Number(f.limitRaw) > 0 ? Number(f.limitRaw) : q.price;
    const maxBuy = Math.floor(data.available_cash_cents / Math.round(unit * 100));
    $('#qty-help', root).innerHTML = f.side === 'buy'
      ? `Up to ${intFmt.format(Math.max(0, maxBuy))} shares at ${price(unit)}. <button type="button" class="link" data-max>Use max</button>`
      : (data.available_shares > 0 ? `You can sell ${shares(data.available_shares)}. <button type="button" class="link" data-max>Sell all</button>` : `You don't own any ${esc(symbol)} to sell.`);
    $('[data-max]', root)?.addEventListener('click', () => {
      $('#qty', root).value = f.side === 'buy' ? Math.max(0, maxBuy) : data.available_shares;
      updateForm();
    });
    $('#limit-help', root).textContent = f.side === 'buy'
      ? `Fills when ${symbol} is at or below this price. Current: ${price(q.price)}.`
      : `Fills when ${symbol} is at or above this price. Current: ${price(q.price)}.`;
    const qty = Number(f.qtyRaw);
    const valid = Number.isInteger(qty) && qty > 0;
    $('#est-label', root).textContent = f.side === 'buy' ? 'Estimated cost' : 'Estimated proceeds';
    $('#est-total', root).textContent = valid ? money(Math.round(unit * qty * 100)) : '—';
    $('#est-bp', root).textContent = money(data.available_cash_cents);
    const submit = $('#trade-form button[type="submit"]', root);
    submit.className = `btn block ${f.side}`;
    submit.textContent = `Review ${f.side} order`;
    // Clear stale field errors as the user edits; full validation happens on submit.
    if (e && e.target?.id === 'qty') setFieldError('qty', null);
    if (e && e.target?.id === 'limit') setFieldError('limit', null);
  }

  function submitOrder(e) {
    e.preventDefault();
    const f = readForm();
    const { errors, qty } = validate(f);
    setFieldError('qty', errors.qty);
    setFieldError('limit', errors.limit);
    if (errors.qty || errors.limit) {
      $(errors.qty ? '#qty' : '#limit', root).focus();
      return;
    }
    const order = { symbol, side: f.side, type: f.type, quantity: qty };
    if (f.type === 'limit') order.limit_price = f.limitRaw;
    openOrderDialog(order, () => {
      $('#qty', root).value = '';
      flashPosition = true;
      refresh(true);
    });
  }

  async function toggleWatch() {
    const btn = $('#watch-btn', root);
    btn.disabled = true;
    try {
      if (data.watched) {
        await api(`/api/watchlist/${encodeURIComponent(symbol)}`, { method: 'DELETE' });
        toast(`Removed ${symbol} from your watchlist.`);
      } else {
        await api('/api/watchlist', { method: 'POST', body: { symbol } });
        toast(`Added ${symbol} to your watchlist.`);
      }
      data.watched = !data.watched;
      render();
    } catch (err) {
      toast(err.message, { error: true });
    } finally {
      btn.disabled = false;
    }
  }

  refresh();
  return {
    refresh,
    title: symbol,
    destroy: () => resizeObserver?.disconnect(),
  };
}

/* ============================== history ============================== */

function HistoryView(root) {
  let tab = 'transactions';
  let txns = null;
  let orders = null;

  root.innerHTML = `
    <div class="page-head">
      <div><h1 class="page-title">History</h1><p class="page-sub">Every simulated trade and order on this account.</p></div>
    </div>
    <section class="card">
      <div class="tabs" role="tablist" aria-label="History type">
        <button type="button" role="tab" id="tab-transactions" aria-controls="panel" aria-selected="true">Transactions</button>
        <button type="button" role="tab" id="tab-orders" aria-controls="panel" aria-selected="false" tabindex="-1">Orders</button>
      </div>
      <div class="toolbar">
        <label for="hist-filter" class="visually-hidden">Filter by symbol</label>
        <input id="hist-filter" class="input" placeholder="Filter by symbol" autocomplete="off" spellcheck="false">
        <button type="button" class="btn sm" id="export-csv">Download CSV</button>
        <span class="muted" id="hist-count" aria-live="polite"></span>
      </div>
      <div id="panel" role="tabpanel" aria-labelledby="tab-transactions">${skeletonRows(6)}</div>
    </section>`;

  const tabs = $$('[role="tab"]', root);
  const selectTab = (name, focus) => {
    tab = name;
    tabs.forEach((t) => {
      const on = t.id === `tab-${name}`;
      t.setAttribute('aria-selected', String(on));
      t.tabIndex = on ? 0 : -1;
      if (on && focus) t.focus();
    });
    $('#panel', root).setAttribute('aria-labelledby', `tab-${name}`);
    render();
  };
  tabs.forEach((t, i) => {
    t.addEventListener('click', () => selectTab(t.id.replace('tab-', '')));
    t.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
        e.preventDefault();
        selectTab(tabs[(i + (e.key === 'ArrowRight' ? 1 : tabs.length - 1)) % tabs.length].id.replace('tab-', ''), true);
      }
    });
  });
  $('#hist-filter', root).addEventListener('input', render);
  $('#export-csv', root).addEventListener('click', exportCsv);

  async function refresh(silent = false) {
    try {
      const [t, o] = await Promise.all([api('/api/transactions'), api('/api/orders')]);
      txns = t.transactions;
      orders = o.orders;
      render();
    } catch (err) {
      if (silent && txns) return;
      $('#panel', root).innerHTML = errorHtml(err.message);
      bindRetry($('#panel', root), () => refresh());
    }
  }

  const filtered = (rows) => {
    const f = $('#hist-filter', root).value.trim().toUpperCase();
    return f ? rows.filter((r) => r.symbol.includes(f)) : rows;
  };

  function render() {
    if (!txns) return;
    const panel = $('#panel', root);
    const rows = filtered(tab === 'transactions' ? txns : orders);
    $('#hist-count', root).textContent = `${rows.length} ${tab === 'transactions' ? 'transaction' : 'order'}${rows.length === 1 ? '' : 's'}`;
    $('#export-csv', root).disabled = !rows.length;
    if (!rows.length) {
      const any = (tab === 'transactions' ? txns : orders).length;
      panel.innerHTML = any ? emptyState('No matches', 'No rows match that symbol.')
        : emptyState(tab === 'transactions' ? 'No transactions yet' : 'No orders yet',
          'Place a simulated trade from any stock page and it will be recorded here.',
          '<a class="btn primary" href="#/">Go to dashboard</a>');
      return;
    }
    if (tab === 'transactions') {
      panel.innerHTML = `<div class="table-wrap"><table class="responsive">
        <caption class="visually-hidden">Transactions</caption>
        <thead><tr><th scope="col">Symbol</th><th scope="col">Date</th><th scope="col">Side</th><th scope="col">Shares</th><th scope="col">Price</th>
          <th scope="col">Amount</th><th scope="col">Realized P/L</th><th scope="col">Cash after</th></tr></thead>
        <tbody>${rows.map((t) => `<tr>
          <td class="sym-cell"><a href="#/stock/${encodeURIComponent(t.symbol)}">${esc(t.symbol)}</a><span class="sub">${esc(t.name || '')}</span></td>
          <td data-label="Date">${esc(fmtDateTime(t.created_at))}</td>
          <td data-label="Side"><span class="badge side-${t.side}">${t.side}</span></td>
          <td data-label="Shares">${intFmt.format(t.quantity)}</td>
          <td data-label="Price">${price(t.price)}</td>
          <td data-label="Amount">${t.side === 'buy' ? MINUS : '+'}${money(t.amount_cents)}</td>
          <td data-label="Realized P/L">${t.realized_pl_cents == null ? '<span class="muted">—</span>' : `<span class="${tone(t.realized_pl_cents)}">${signedMoney(t.realized_pl_cents)}</span>`}</td>
          <td data-label="Cash after">${money(t.cash_after_cents)}</td>
        </tr>`).join('')}</tbody></table></div>`;
    } else {
      panel.innerHTML = `<div class="table-wrap"><table class="responsive">
        <caption class="visually-hidden">Orders</caption>
        <thead><tr><th scope="col">Symbol</th><th scope="col">Placed</th><th scope="col">Side</th><th scope="col">Type</th><th scope="col">Shares</th>
          <th scope="col">Limit</th><th scope="col">Status</th><th scope="col">Fill price</th><th scope="col"><span class="visually-hidden">Actions</span></th></tr></thead>
        <tbody>${rows.map((o) => `<tr>
          <td class="sym-cell"><a href="#/stock/${encodeURIComponent(o.symbol)}">${esc(o.symbol)}</a><span class="sub">${esc(o.name || '')}</span></td>
          <td data-label="Placed">${esc(fmtDateTime(o.created_at))}</td>
          <td data-label="Side"><span class="badge side-${o.side}">${o.side}</span></td>
          <td data-label="Type">${o.order_type === 'limit' ? 'Limit' : 'Market'}</td>
          <td data-label="Shares">${intFmt.format(o.quantity)}</td>
          <td data-label="Limit">${o.limit_price == null ? '<span class="muted">—</span>' : price(o.limit_price)}</td>
          <td data-label="Status"><span class="badge status-${o.status}">${o.status}</span></td>
          <td data-label="Fill price">${o.fill_price == null ? '<span class="muted">—</span>' : price(o.fill_price)}</td>
          <td data-label="" class="full">${o.status === 'open' ? `<button type="button" class="btn sm danger" data-cancel="${o.id}" aria-label="Cancel ${o.side} order for ${esc(o.symbol)}">Cancel</button>` : ''}</td>
        </tr>`).join('')}</tbody></table></div>`;
      bindCancel(panel, () => refresh(true));
    }
  }

  function exportCsv() {
    const rows = filtered(tab === 'transactions' ? txns : orders);
    const cols = tab === 'transactions'
      ? [['created_at', 'Date (UTC)'], ['symbol', 'Symbol'], ['side', 'Side'], ['quantity', 'Shares'], ['price', 'Price'],
        ['amount', 'Amount'], ['realized', 'Realized P/L'], ['cash_after', 'Cash after']]
      : [['created_at', 'Placed (UTC)'], ['symbol', 'Symbol'], ['side', 'Side'], ['order_type', 'Type'], ['quantity', 'Shares'],
        ['limit_price', 'Limit'], ['status', 'Status'], ['fill_price', 'Fill price']];
    const value = (r, key) => ({
      amount: (r.amount_cents / 100).toFixed(2),
      realized: r.realized_pl_cents == null ? '' : (r.realized_pl_cents / 100).toFixed(2),
      cash_after: (r.cash_after_cents / 100).toFixed(2),
    }[key] ?? r[key] ?? '');
    const cell = (v) => {
      let s = String(v);
      if (/^[=+\-@]/.test(s) && isNaN(Number(s))) s = `'${s}`; // guard against spreadsheet formula injection
      return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
    };
    const csv = [cols.map((c) => c[1]).join(','), ...rows.map((r) => cols.map(([k]) => cell(value(r, k))).join(','))].join('\n');
    const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv' }));
    const a = Object.assign(document.createElement('a'), { href: url, download: `papertrade-${tab}.csv` });
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  refresh();
  return { refresh, title: 'History' };
}

/* ============================== settings ============================== */

const settingRow = (title, desc, control, id = '') => `
  <div class="setting-row"${id ? ` id="${id}"` : ''}>
    <div class="setting-text"><h3>${title}</h3><p>${desc}</p></div>
    <div class="setting-control">${control}</div>
  </div>`;

const choiceGroup = (key, label, options) => `
  <div class="choice" role="radiogroup" aria-label="${esc(label)}">
    ${options.map(([value, text]) => `
      <input type="radio" name="pref-${key}" id="pref-${key}-${value}" value="${value}" ${prefs[key] === value ? 'checked' : ''}>
      <label for="pref-${key}-${value}">${text}</label>`).join('')}
  </div>`;

const switchControl = (key, label) => `
  <label class="switch">
    <input type="checkbox" role="switch" name="pref-${key}" ${prefs[key] ? 'checked' : ''}>
    <span class="switch-track" aria-hidden="true"></span>
    <span class="visually-hidden">${esc(label)}</span>
  </label>`;

function SettingsView(root) {
  const sections = [['account', 'Account'], ['preferences', 'Preferences'], ['data', 'Market data'], ['session', 'Session'], ['danger', 'Danger zone']];
  root.innerHTML = `
    <div class="page-head">
      <div><h1 class="page-title">Settings</h1><p class="page-sub">Your simulated account, display preferences and data source.</p></div>
    </div>
    <div class="settings-layout">
      <nav class="settings-nav" aria-label="Settings sections">
        ${sections.map(([id, label]) => `<a href="#/settings" data-section="${id}"${id === 'session' ? ' hidden' : ''}>${label}</a>`).join('')}
      </nav>
      <div class="settings-sections">
        <section class="card settings-card" id="sec-account" aria-labelledby="account-h">
          <div class="card-head"><h2 id="account-h">Account</h2></div>
          <div id="account-rows">${skeletonRows(4)}</div>
        </section>

        <section class="card settings-card" id="sec-preferences" aria-labelledby="prefs-h">
          <div class="card-head"><h2 id="prefs-h">Preferences</h2><span class="meta" id="prefs-saved" aria-live="polite">Saved on this device</span></div>
          ${settingRow('Theme', 'Follow your device, or always use light or dark.', choiceGroup('theme', 'Theme', [['system', 'System'], ['light', 'Light'], ['dark', 'Dark']]))}
          ${settingRow('Default order type', 'What the trade form starts with on a stock page.', choiceGroup('orderType', 'Default order type', [['market', 'Market'], ['limit', 'Limit']]))}
          ${settingRow('Motion', 'Trade animations, ticket stamps and price flashes. Reduced also applies if your device asks for less motion.', choiceGroup('motion', 'Motion', [['system', 'Full'], ['reduced', 'Reduced']]))}
          ${settingRow('Demo-mode banner', 'Show the reminder at the top of each page while prices are simulated.', switchControl('demoBanner', 'Show demo-mode banner'), 'row-banner')}
        </section>

        <section class="card settings-card" id="sec-data" aria-labelledby="data-h">
          <div class="card-head"><h2 id="data-h">Market data</h2><span id="data-badge"></span></div>
          <div id="data-rows">${skeletonRows(3)}</div>
        </section>

        <section class="card settings-card" id="sec-session" aria-labelledby="session-h" hidden>
          <div class="card-head"><h2 id="session-h">Session</h2></div>
          ${settingRow('Sign out', 'You stay signed in on this device for 30 days. Changing APP_PASSWORD on the server signs out every device.',
            '<button type="button" class="btn" id="signout">Sign out</button>')}
        </section>

        <section class="card settings-card danger-zone" id="sec-danger" aria-labelledby="danger-h">
          <div class="card-head"><h2 id="danger-h">Danger zone</h2></div>
          <form id="reset-form" class="setting-row setting-row-wide" novalidate>
            <div class="setting-text">
              <h3>Reset account</h3>
              <p>Permanently deletes your holdings, open orders and trade history, and sets your cash to a new starting balance. Your watchlist and preferences are kept.</p>
            </div>
            <div class="reset-controls">
              <label for="balance" class="field-label">New starting balance</label>
              <div class="input-prefix"><span aria-hidden="true">$</span>
                <input id="balance" class="input" type="number" inputmode="decimal" min="1000" max="10000000" step="0.01" aria-describedby="balance-help balance-error">
              </div>
              <div class="presets" role="group" aria-label="Preset balances">
                ${[10000, 25000, 100000, 1000000].map((v) => `<button type="button" class="btn sm" data-preset="${v}">${usd.format(v).replace('.00', '')}</button>`).join('')}
              </div>
              <p class="help" id="balance-help">Between $1,000 and $10,000,000.</p>
              <p class="field-error" id="balance-error" hidden></p>
              <button type="submit" class="btn danger">Reset account…</button>
            </div>
          </form>
        </section>
      </div>
    </div>`;

  const balance = $('#balance', root);
  let savedTimer = null;

  // Preferences apply instantly.
  root.addEventListener('change', (e) => {
    const name = e.target.name || '';
    if (!name.startsWith('pref-')) return;
    setPref(name.slice(5), e.target.type === 'checkbox' ? e.target.checked : e.target.value);
    const saved = $('#prefs-saved', root);
    saved.textContent = 'Saved ✓';
    clearTimeout(savedTimer);
    savedTimer = setTimeout(() => { saved.textContent = 'Saved on this device'; }, 1800);
  });

  // Section navigation, highlighting the section in view.
  const navLinks = $$('[data-section]', root);
  let holdUntil = 0;
  const setCurrent = (id) => navLinks.forEach((l) => (l.dataset.section === id ? l.setAttribute('aria-current', 'true') : l.removeAttribute('aria-current')));
  const jump = (id) => {
    const sec = $(`#sec-${id}`, root);
    if (!sec || sec.hidden) return;
    sec.scrollIntoView({ behavior: motionOk() ? 'smooth' : 'auto', block: 'start' });
    const heading = $('h2', sec);
    heading.setAttribute('tabindex', '-1');
    heading.focus({ preventScroll: true });
    setCurrent(id);
    holdUntil = Date.now() + 900; // keep the clicked item highlighted while smooth-scrolling
  };
  navLinks.forEach((l) => l.addEventListener('click', (e) => { e.preventDefault(); jump(l.dataset.section); }));
  const observer = new IntersectionObserver((entries) => {
    if (Date.now() < holdUntil) return;
    const visible = entries.filter((en) => en.isIntersecting).sort((x, y) => x.boundingClientRect.top - y.boundingClientRect.top)[0];
    if (visible) setCurrent(visible.target.id.replace('sec-', ''));
  }, { rootMargin: '-120px 0px -55% 0px' });
  $$('.settings-card', root).forEach((s) => observer.observe(s));
  setCurrent('account');

  const syncPresets = () => $$('[data-preset]', root).forEach((x) => x.setAttribute('aria-pressed', String(Number(x.dataset.preset) === Number(balance.value))));
  $$('[data-preset]', root).forEach((b) => b.addEventListener('click', () => { balance.value = b.dataset.preset; setError(null); syncPresets(); }));
  balance.addEventListener('input', syncPresets);

  function setError(msg) {
    balance.setAttribute('aria-invalid', msg ? 'true' : 'false');
    $('#balance-error', root).textContent = msg || '';
    $('#balance-error', root).hidden = !msg;
  }

  $('#reset-form', root).addEventListener('submit', async (e) => {
    e.preventDefault();
    const raw = balance.value.trim();
    const value = Number(raw);
    if (!raw || !isFinite(value)) { setError('Enter a starting balance.'); balance.focus(); return; }
    if (value < 1000 || value > 10000000) { setError('Starting balance must be between $1,000 and $10,000,000.'); balance.focus(); return; }
    if (!/^\d+(\.\d{1,2})?$/.test(raw)) { setError('Use at most 2 decimal places.'); balance.focus(); return; }
    setError(null);
    const ok = await confirmDialog({
      title: 'Reset your simulated account?',
      body: `Your holdings, open orders and trade history will be permanently deleted, and your cash will be set to <strong>${esc(usd.format(value))}</strong>. This can't be undone.`,
      confirmText: 'Reset account',
      danger: true,
    });
    if (!ok) return;
    try {
      await api('/api/account/reset', { method: 'POST', body: { starting_balance: raw, confirm: true } });
      toast(`Account reset with ${usd.format(value)} in simulated cash.`);
      location.hash = '#/';
    } catch (err) {
      setError(err.message);
    }
  });

  $('#signout', root).addEventListener('click', async () => {
    await api('/api/logout', { method: 'POST', body: {} }).catch(() => {});
    showLogin();
  });

  async function downloadBackup(btn) {
    btn.disabled = true;
    try {
      const [account, t, o, w] = await Promise.all([api('/api/account'), api('/api/transactions'), api('/api/orders'), api('/api/watchlist')]);
      const backup = {
        app: 'PaperTrade',
        note: 'Simulated account backup. No real money.',
        exported_at: new Date().toISOString(),
        account: {
          starting_balance_cents: account.starting_balance_cents,
          cash_cents: account.cash_cents,
          reset_at: account.reset_at,
          holdings: account.holdings.map(({ symbol, name, shares, avg_cost }) => ({ symbol, name, shares, avg_cost })),
        },
        transactions: t.transactions,
        orders: o.orders,
        watchlist: w.items.map((i) => i.symbol),
      };
      const url = URL.createObjectURL(new Blob([JSON.stringify(backup, null, 2)], { type: 'application/json' }));
      const link = Object.assign(document.createElement('a'), { href: url, download: `papertrade-backup-${new Date().toISOString().slice(0, 10)}.json` });
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      toast('Backup downloaded.');
    } catch (err) {
      toast(err.message, { error: true });
    } finally {
      btn.disabled = false;
    }
  }

  let first = true;
  async function refresh() {
    let acct;
    let status;
    try {
      [acct, status] = await Promise.all([api('/api/account'), api('/api/status')]);
    } catch (err) {
      $('#account-rows', root).innerHTML = errorHtml(err.message);
      bindRetry($('#account-rows', root), refresh);
      return;
    }
    if (first && !balance.value) {
      balance.value = (acct.starting_balance_cents / 100).toFixed(2).replace(/\.00$/, '');
      syncPresets();
    }

    $('#account-rows', root).innerHTML = [
      settingRow('Account value', `Cash plus holdings at current prices. You started with ${money(acct.starting_balance_cents)}.`,
        `<span class="setting-value">${money(acct.portfolio_value_cents)}</span><span class="${tone(acct.total_pl_cents)} setting-sub">${signedMoney(acct.total_pl_cents)} (${pct(acct.total_pl_percent)})</span>`),
      settingRow('Cash', acct.reserved_cash_cents ? `${money(acct.reserved_cash_cents)} is reserved for open limit orders.` : 'Available to trade right now.',
        `<span class="setting-value">${money(acct.available_cash_cents)}</span>`),
      settingRow('Account started', 'When this account was created or last reset.', `<span class="setting-value sm">${esc(fmtDateTime(acct.reset_at))}</span>`),
      settingRow('Back up your data', 'Download your holdings, trades, orders and watchlist as a JSON file.',
        '<button type="button" class="btn" data-backup>Download backup</button>'),
    ].join('');
    $('[data-backup]', root).addEventListener('click', (e) => downloadBackup(e.currentTarget));

    const demo = status.mode === 'demo';
    const rl = status.rate_limit;
    const label = demo ? 'Demo' : status.label === 'live' ? 'Live' : 'Delayed';
    $('#data-badge', root).innerHTML = `<span class="badge ${demo ? 'demo' : esc(status.label)}">${label}</span>`;
    const meter = (used, max, what) => `
      <div class="meter-row"><div class="meter" aria-hidden="true"><span data-width="${(used / max * 100).toFixed(1)}"></span></div>
        <span class="mono-sm">${intFmt.format(used)} / ${intFmt.format(max)} ${what}</span></div>`;
    $('#data-rows', root).innerHTML = [
      settingRow('Source', demo ? 'Fictional prices generated on the server, so the app works without an API key.'
        : 'Quotes, charts and search come from Twelve Data. Your API key stays on the server.',
      `<span class="setting-value sm">${esc(status.provider)}</span>`),
      settingRow('Refresh', 'How often prices update while the app is open.', `<span class="setting-value sm">Every ${status.quote_refresh_seconds} s</span>`),
      demo
        ? settingRow('Coverage', `${status.demo_symbols.length} popular US tickers are available in demo mode.`,
          `<div class="chips">${status.demo_symbols.map((s) => `<a class="chip-link" href="#/stock/${encodeURIComponent(s)}">${esc(s)}</a>`).join('')}</div>`)
        : settingRow('Free-plan budget', 'When the budget runs out, cached prices are shown and marked Delayed.',
          `<div class="meters">${meter(rl.used_last_minute, rl.per_minute, 'this minute')}${meter(rl.used_today, rl.per_day, 'today')}</div>`),
      demo ? `<div class="setting-row setting-row-wide">
          <div class="setting-text"><h3>Use real prices</h3><p>Real quotes are free for personal use.</p></div>
          <ol class="steps">
            <li>Get a free API key at <a href="https://twelvedata.com/pricing" target="_blank" rel="noopener noreferrer">twelvedata.com</a>.</li>
            <li>Add <code>TWELVE_DATA_API_KEY=your_key</code> to <code>.env</code>, or use <code>fly secrets set</code> when deployed.</li>
            <li>Restart the server. The badge switches to <span class="badge live">Live</span>.</li>
          </ol>
        </div>` : '',
      status.last_error ? `<div class="alert warn">Last provider error: ${esc(status.last_error)}</div>` : '',
    ].join('');
    applyWidths($('#data-rows', root));
    $('#row-banner', root).hidden = !demo;

    $('#sec-session', root).hidden = !status.auth_enabled;
    $('[data-section="session"]', root).hidden = !status.auth_enabled;

    if (first) {
      first = false;
      const target = sessionStorage.getItem('pt-jump');
      if (target) {
        sessionStorage.removeItem('pt-jump');
        requestAnimationFrame(() => jump(target));
      }
    }
  }

  refresh();
  return { refresh, title: 'Settings', destroy: () => observer.disconnect() };
}

function showLogin() {
  if (document.body.classList.contains('signed-out')) return;
  clearInterval(app.pollTimer);
  app.view?.destroy?.();
  app.view = null;
  document.body.classList.add('signed-out');
  $$('dialog[open]').forEach((d) => d.close());
  const main = $('#main');
  document.title = 'Sign in · PaperTrade';
  main.innerHTML = `
    <section class="login" aria-labelledby="login-h">
      <p class="eyebrow">PaperTrade · simulated money</p>
      <h1 id="login-h" class="page-title">Welcome back</h1>
      <form id="login-form" class="login-form" novalidate>
        <div class="field">
          <label for="password">Password</label>
          <input id="password" class="input" type="password" autocomplete="current-password" required aria-describedby="password-error">
          <p class="field-error" id="password-error" hidden></p>
        </div>
        <button class="btn primary block" type="submit">Sign in</button>
        <p class="help">Your positions were kept while you were away. Any limit orders that should have filled are settled when you sign in.</p>
      </form>
    </section>`;
  const input = $('#password');
  const err = $('#password-error');
  input.focus();
  $('#login-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const btn = $('#login-form button[type="submit"]');
    if (!input.value) { err.textContent = 'Enter your password.'; err.hidden = false; input.focus(); return; }
    btn.disabled = true;
    btn.textContent = 'Signing in…';
    try {
      await api('/api/login', { method: 'POST', body: { password: input.value } });
      document.body.classList.remove('signed-out');
      await loadStatus();
      app.firstRoute = true;
      route();
    } catch (ex) {
      err.textContent = ex.message;
      err.hidden = false;
      input.setAttribute('aria-invalid', 'true');
      input.select();
      btn.disabled = false;
      btn.textContent = 'Sign in';
    }
  });
}

function NotFoundView(root) {
  root.innerHTML = `<div class="card">${emptyState('Page not found', "That page doesn't exist.", '<a class="btn primary" href="#/">Go to dashboard</a>')}</div>`;
  return { title: 'Not found' };
}

/* ============================== router ============================== */

const ROUTES = [
  { re: /^\/?$/, view: DashboardView, nav: 'dashboard' },
  { re: /^\/stock\/([^/]+)$/, view: StockView, params: (m) => ({ symbol: decodeURIComponent(m[1]).toUpperCase() }) },
  { re: /^\/history$/, view: HistoryView, nav: 'history' },
  { re: /^\/settings$/, view: SettingsView, nav: 'settings' },
];

function route() {
  if (document.body.classList.contains('signed-out')) return;
  const path = location.hash.replace(/^#/, '') || '/';
  app.view?.destroy?.();
  const main = $('#main');
  let match = null;
  let def = null;
  for (const r of ROUTES) {
    match = path.match(r.re);
    if (match) { def = r; break; }
  }
  if (def?.params && !/^[A-Z][A-Z0-9.\-]{0,9}$/.test(def.params(match).symbol)) def = null;
  app.view = def ? def.view(main, def.params ? def.params(match) : {}) : NotFoundView(main);
  main.classList.remove('enter');
  void main.offsetWidth;
  main.classList.add('enter');
  document.title = `${app.view.title} · PaperTrade`;
  $$('[data-nav]').forEach((a) => {
    if (def?.nav && a.dataset.nav === def.nav) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  });
  if (!app.firstRoute) {
    window.scrollTo(0, 0);
    main.focus({ preventScroll: true });
  }
  app.firstRoute = false;
}

window.addEventListener('hashchange', route);
document.addEventListener('click', (e) => {
  const link = e.target.closest('[data-jump-to]');
  if (link) sessionStorage.setItem('pt-jump', link.dataset.jumpTo);
});
initSearch();
applyPrefs();

(async function boot() {
  try {
    const session = await api('/api/session');
    if (session.auth_required && !session.authenticated) { showLogin(); return; }
  } catch { /* loadStatus shows the connection error */ }
  await loadStatus();
  route();
}());
