/* gridBot dashboard.
 *
 * Reads /api/snapshot on a timer and drives every control through the same
 * endpoints the terminal menu drives through the command queue. Nothing here
 * computes trading logic -- it renders what the bot reports and posts back
 * intents. No dependencies, no build step.
 */
'use strict';

const S = {
  snap: null,          // latest /api/snapshot
  meta: null,          // /api/meta (actions + editable field specs)
  open: null,          // symbol whose detail panel is showing
  shown: null,         // last symbol we scrolled the detail panel into view for
  openInactive: null,  // symbol of the expanded inactive row
  history: null,       // /api/history for the current chart scope
  chartScope: null,    // null = all coins
  equity: null,        // /api/equity points for the selected range
  valueDays: null,     // account-value range in days, 0 = all (see VALUE_RANGES)
  drawn: {},           // chart -> data key last drawn, to skip identical redraws
  sort: { key: 'index', dir: 1 },
  dragKey: null,       // column key being dragged
  dragged: false,      // set during a drag so the drop doesn't also fire a sort
  polling: true,
  logFollow: true,
  logSymbol: null,
  failures: 0,
  nodes: new Map(),    // cache key -> live DOM node, see cached()/syncChildren()
};

const POLL_MS = 2500;
const HISTORY_MS = 30000;
const $ = (sel, root = document) => root.querySelector(sel);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
};
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

/* --- keeping DOM alive across polls --------------------------------------
 *
 * The page re-polls every couple of seconds. Rebuilding it wholesale each time
 * (the obvious approach) is what makes a live page jump: destroying and
 * recreating the nodes under the viewport defeats the browser's scroll
 * anchoring, throws away anything the user was typing, resets the scroll
 * position of the log boxes, and re-fires their fetches. So rows and panels are
 * created once, cached by key, and patched in place; only genuinely new nodes
 * are inserted and only out-of-order nodes are moved.
 */

function cached(key, make) {
  let node = S.nodes.get(key);
  if (!node) { node = make(); S.nodes.set(key, node); }
  return node;
}

function dropCached(prefix, keep) {
  for (const key of [...S.nodes.keys()]) {
    if (key.startsWith(prefix) && !keep.has(key)) S.nodes.delete(key);
  }
}

/* A collapsible section: native <details> so the arrow, the keyboard handling
   and the open/closed state come for free. `key` remembers the choice across
   reloads -- storage can throw (private windows, blocked site data), so both
   sides are guarded and simply fall back to `startOpen`. */
/* Write text only when it differs. Assigning textContent replaces the text
   node even when the string is identical, and doing that every poll to a node
   that persists (the header, a disclosure summary) is the same churn that made
   the page jump. */
function setText(node, text) {
  if (node && node.textContent !== text) node.textContent = text;
}

function rememberDisclosure(box, key, startOpen = false) {
  let open = startOpen;
  try {
    const saved = localStorage.getItem('gridbot.' + key);
    if (saved !== null) open = saved === '1';
  } catch (_) { /* storage unavailable -- use the default */ }
  box.open = open;
  box.addEventListener('toggle', () => {
    try { localStorage.setItem('gridbot.' + key, box.open ? '1' : '0'); } catch (_) { /* ignore */ }
  });
  return box;
}

function disclosure(title, key, startOpen = false) {
  const box = el('details', 'disclosure');
  const summary = el('summary');
  summary.append(el('span', 'caret', '▸'), el('span', 'disclosure-title', title));
  const body = el('div', 'disclosure-body');
  box.append(summary, body);
  rememberDisclosure(box, key, startOpen);
  return { box, summary, body };
}

/* Make `parent`'s children exactly `desired`, in order, touching as little as
   possible: an element already in the right place is left completely alone. */
function syncChildren(parent, desired) {
  const want = new Set(desired);
  for (const child of [...parent.children]) {
    if (!want.has(child)) child.remove();
  }
  desired.forEach((node, i) => {
    if (parent.children[i] !== node) parent.insertBefore(node, parent.children[i] || null);
  });
}

/* --- formatting ---------------------------------------------------------- */

const num = (v, dp = 2) => (v === null || v === undefined || Number.isNaN(v))
  ? '—'
  : Number(v).toLocaleString(undefined, { minimumFractionDigits: dp, maximumFractionDigits: dp });

const usd = (v, dp = 2) => (v === null || v === undefined) ? '—' : '$' + num(v, dp);

const usdSigned = (v, dp = 2) => {
  if (v === null || v === undefined) return '—';
  const sign = v < 0 ? '-' : '+';
  return sign + '$' + num(Math.abs(v), dp);
};

const price = (v, prec) => (v === null || v === undefined) ? '—' : '$' + num(v, prec ?? 2);

const pct = (v, dp = 2) => (v === null || v === undefined)
  ? '' : (v >= 0 ? '+' : '') + Number(v).toFixed(dp) + '%';

const qty = (v) => (v === null || v === undefined) ? '—' : Number(v).toFixed(8);

const cls = (v) => (v === null || v === undefined || v === 0) ? '' : (v > 0 ? 'pos' : 'neg');

const ago = (sec) => {
  if (sec === null || sec === undefined) return '';
  if (sec < 90) return Math.round(sec) + 's ago';
  if (sec < 5400) return Math.round(sec / 60) + 'm ago';
  return Math.round(sec / 3600) + 'h ago';
};

const signedCell = (v, dp = 2) => {
  const td = el('td', 'num ' + cls(v));
  td.textContent = usdSigned(v, dp);
  return td;
};

/* --- transport ----------------------------------------------------------- */

async function api(path, opts = {}) {
  const res = await fetch(path, {
    credentials: 'same-origin',
    headers: opts.body ? { 'Content-Type': 'application/json' } : undefined,
    ...opts,
  });
  let data = null;
  try { data = await res.json(); } catch (_) { /* non-JSON error page */ }
  if (res.status === 401) {
    throw new Error('Token rejected — reload with ?token=YOUR_TOKEN');
  }
  if (!res.ok) {
    // Keep the server's structured refusal (why it said no, whether it can be
    // forced) on the error -- the remove-coin flow needs it, not just a string.
    const err = new Error((data && data.error) || `HTTP ${res.status}`);
    if (data) { err.blockers = data.blockers || []; err.canForce = !!data.can_force; }
    throw err;
  }
  return data;
}

const post = (path, body) => api(path, { method: 'POST', body: JSON.stringify(body) });

/* --- toasts -------------------------------------------------------------- */

function toast(title, body, kind = 'info', ms = 7000) {
  const node = el('div', 'toast ' + kind);
  node.appendChild(el('div', 't-title', title));
  if (body) node.appendChild(el('div', 't-body', body));
  $('#toasts').appendChild(node);
  setTimeout(() => node.remove(), ms);
  return node;
}

/* --- modal --------------------------------------------------------------- */

function closeModal() { $('#modal-root').innerHTML = ''; }

/* opts: {title, html, warn, confirmLabel, danger, typeToConfirm, onConfirm} */
function modal(opts) {
  const back = el('div', 'modal-back');
  const box = el('div', 'modal');
  box.innerHTML = `<h3>${esc(opts.title)}</h3>` +
    (opts.warn ? `<div class="warn-box">${opts.warn}</div>` : '') +
    (opts.html || '');
  if (opts.node) box.appendChild(opts.node);

  let input = null;
  if (opts.typeToConfirm) {
    const wrap = el('div');
    wrap.innerHTML = `<p class="small muted">Type <b>${esc(opts.typeToConfirm)}</b> to confirm:</p>`;
    input = el('input');
    input.type = 'text';
    input.autocapitalize = 'characters';
    wrap.appendChild(input);
    box.appendChild(wrap);
  }

  const foot = el('div', 'modal-foot');
  const cancel = el('button', '', 'Cancel');
  const go = el('button', opts.danger ? 'danger' : 'primary', opts.confirmLabel || 'Confirm');
  // Dismiss only THIS dialog, not the whole root: a handler is allowed to open
  // a follow-up modal (the remove-coin flow opens one when the server refuses),
  // and clearing the root would wipe it the moment onConfirm resolved.
  const dismiss = () => { if (back.isConnected) back.remove(); };
  cancel.onclick = dismiss;
  if (input) {
    go.disabled = true;
    input.oninput = () => { go.disabled = input.value.trim().toUpperCase() !== opts.typeToConfirm; };
    input.onkeydown = (e) => { if (e.key === 'Enter' && !go.disabled) go.click(); };
  }
  go.onclick = async () => {
    go.disabled = true;
    go.textContent = 'Working…';
    try { await opts.onConfirm(); } finally { dismiss(); }
  };
  foot.append(cancel, go);
  box.appendChild(foot);
  back.appendChild(box);
  back.onclick = (e) => { if (e.target === back) dismiss(); };
  $('#modal-root').appendChild(back);
  (input || go).focus();
}

/* --- action descriptions (mirror the CLI's Y-confirm wording) ------------- */

function actionCopy(kind, coin) {
  const base = coin.base;
  const held = coin.levels
    ? `${coin.levels} open position(s) (${qty(coin.qty)} ${base}, cost ${usd(coin.cost_usd)})`
    : 'no open positions';
  switch (kind) {
    case 'buy':
      return {
        title: `Force BUY ${coin.symbol}`, danger: true, confirmLabel: 'Buy at market',
        warn: `Places an immediate <b>market buy</b> of ${usd(coin.config.usd_per_buy)} ` +
              `at level ${coin.levels}, ignoring the grid trigger.`,
        html: (coin.buys_paused ? `<p><b>Buying is paused</b> — this is a one-off manual ` +
               `buy; it does not resume automatic buying.</p>` : '') +
              `<p class="small muted">A pending limit buy, if any, is cancelled first. ` +
              `Currently ${held}.</p>`,
      };
    case 'sell_all':
      return {
        title: `Sell ALL ${coin.symbol} now`, danger: true, confirmLabel: 'Market sell everything',
        warn: `<b>Market-sells the entire tracked position</b> — ${qty(coin.qty)} ${base} ` +
              `(~${usd(coin.position_value)}) — right now, whatever the price.`,
        html: `<p class="small muted">Avg entry ${price(coin.avg_entry, coin.price_prec)}, ` +
              `current ${price(coin.price, coin.price_prec)}, unrealized ` +
              `${usdSigned(coin.unrealized)}. Always market, never a limit.</p>`,
      };
    case 'retire':
      return {
        title: `RETIRE ${coin.symbol}`, danger: true, confirmLabel: 'Retire coin',
        typeToConfirm: base,
        warn: `Books <b>${usdSigned(coin.realized)}</b> realized PnL and ${coin.cycle} cycle(s) ` +
              `into data/retired_pnl.json, then does a <b>full reset</b>.` +
              (coin.levels ? `<br><br>It also wipes ${held} — the bot forgets them, ` +
                             `but <b>they stay on Kraken</b> and nothing is sold.` : ''),
        html: `<p class="small muted">The coin is left paused. You'll be offered the ` +
              `<code>enabled: False</code> config edit right after.</p>`,
      };
    case 'clear_stats':
      return {
        title: `Clear stats for ${coin.symbol}`, danger: true, confirmLabel: 'Wipe state',
        typeToConfirm: base,
        warn: `Zeroes realized PnL (${usdSigned(coin.realized)}) and the cycle count — ` +
              `<b>this PnL is not booked anywhere</b>, it is simply gone. Use Retire to keep it.` +
              (coin.levels ? `<br><br>Also wipes ${held}; they stay on Kraken, untracked.` : ''),
        html: `<p class="small muted">The coin is left paused.</p>`,
      };
    case 'pause_toggle':
      return coin.paused
        ? { title: `Resume ${coin.symbol}`, confirmLabel: 'Resume',
            html: `<p>Trading resumes on the next tick.</p>` }
        : { title: `Pause ${coin.symbol}`, confirmLabel: 'Pause',
            html: `<p>No new buys or sells. Resting limit orders are still serviced ` +
                  `and stop-loss is skipped while paused.</p>` };
    case 'pause_buys_toggle':
      return coin.buys_paused
        ? { title: `Resume buying on ${coin.symbol}`, confirmLabel: 'Resume buying',
            html: `<p>The coin goes back to opening grid levels on its own.` +
                  (coin.levels ? '' : ' The first-buy reference restarts from the current price.') +
                  `</p>` }
        : { title: `Pause buying on ${coin.symbol}`, confirmLabel: 'Pause buying',
            html: `<p>No new buys — automatic or trail-fired. Selling, the stop-loss and any ` +
                  `target sell carry on, so the coin can still exit its position.</p>` +
                  (coin.next_buy.kind === 'limit'
                    ? `<p class="small muted">The resting limit buy is cancelled.</p>` : '') +
                  `<p class="small muted">"Force BUY now" still works as a one-off.</p>` };
    case 'arm_sell_trail':
      if (coin.trailing_sell?.armed && coin.trailing_sell.manual) {
        return { title: `Disarm sell-trail on ${coin.symbol}`, confirmLabel: 'Disarm',
          html: `<p>Cancels the manual trailing stop. The automatic take-profit trail ` +
                `takes over again from the next tick.</p>` };
      }
      return { title: `Arm sell-trail on ${coin.symbol}`, confirmLabel: 'Arm',
        html: `<p>Arms a manual trailing stop at ${price(coin.price, coin.price_prec)}, ` +
              `trailing ${pct(coin.config.trail_sell_pct * 100, 3)} off the high. Floored at ` +
              `avg entry ${price(coin.avg_entry, coin.price_prec)} — it will not sell at a loss.</p>` };
    case 'arm_buy_trail':
      return { title: `Arm buy-trail on ${coin.symbol}`, confirmLabel: 'Arm',
        html: `<p>Trailing dip-buy: arms now, follows the low down, fires <b>one</b> buy on a ` +
              `${pct(coin.config.trail_buy_pct * 100, 3)} rebound. Clear it with “clear targets”.</p>` };
    case 'arm_breakeven_exit':
      return { title: `${coin.breakeven_exit_armed ? 'Disarm' : 'Arm'} breakeven-exit on ${coin.symbol}`,
        confirmLabel: coin.breakeven_exit_armed ? 'Disarm' : 'Arm',
        html: `<p>Lowers the sell threshold to breakeven (avg entry + maker fee) instead of ` +
              `the take-profit target.</p>` };
    case 'arm_pause_after_sell':
      return { title: `${coin.pause_after_sell ? 'Disarm' : 'Arm'} pause-after-sell on ${coin.symbol}`,
        confirmLabel: coin.pause_after_sell ? 'Disarm' : 'Arm',
        html: `<p>One-shot: the coin pauses as soon as the current cycle closes.</p>` };
    case 'clear_targets':
      return { title: `Clear targets on ${coin.symbol}`, confirmLabel: 'Clear',
        html: `<p>Disarms any half-formed buy-trail. With no position open it also ` +
              `re-baselines the first buy to ${pct(-coin.config.drop_pct * 100, 2)} from ` +
              `the current price.</p>` };
    default:
      return { title: kind, confirmLabel: 'Confirm', html: '' };
  }
}

function valueCopy(kind, coin, value) {
  if (kind === 'set_stop_loss') {
    if (!value) {
      return { title: `Clear stop-loss on ${coin.symbol}`, confirmLabel: 'Clear',
        html: `<p>Removes the ${pct(-(coin.stop_loss_pct || 0) * 100)} stop.</p>` };
    }
    const trigger = coin.avg_entry ? coin.avg_entry * (1 - value / 100) : null;
    const already = trigger && coin.price && coin.price <= trigger;
    return {
      title: `Set stop-loss on ${coin.symbol}`, danger: true, confirmLabel: 'Arm stop-loss',
      warn: trigger
        ? `Market-sells <b>everything</b> and pauses the coin if price hits ` +
          `<b>${price(trigger, coin.price_prec)}</b> (−${value}% from avg entry ` +
          `${price(coin.avg_entry, coin.price_prec)}).` +
          (already ? `<br><br><b>Price is already at or below that trigger — it will fire ` +
                     `on the next tick.</b>` : '')
        : `Arms at −${value}% below avg entry once a position opens.`,
      html: `<p class="small muted">One-shot: it disarms itself after firing, and it tracks ` +
            `avg entry as the grid grows. Survives restarts.</p>`,
    };
  }
  if (!value) {
    return { title: `Clear target sell on ${coin.symbol}`, confirmLabel: 'Clear target',
      html: `<p>Cancels the resting post-only order, unfreezes grid buys and disarms ` +
            `pause-after-sell.</p>` };
  }
  const away = coin.price ? (value / coin.price - 1) * 100 : null;
  return {
    title: `Target sell ${coin.symbol}`, danger: true, confirmLabel: 'Place target',
    warn: `Places a <b>post-only limit sell for the whole position</b> — ${qty(coin.qty)} ` +
          `${coin.base}, ~${usd(coin.qty * value)} gross — at ` +
          `<b>${price(value, coin.price_prec)}</b>${away === null ? '' : ` (${pct(away)} from now)`}.`,
    html: `<p class="small muted">It never auto-cancels, grid buys freeze while it rests, ` +
          `and the coin pauses when it fills.</p>`,
  };
}

/* --- action execution ---------------------------------------------------- */

function coinBySymbol(symbol) {
  return (S.snap?.coins || []).find((c) => c.symbol === symbol);
}

async function runAction(symbol, kind, value) {
  const key = symbol.replace('/', '_');
  try {
    const res = await post(`/api/coin/${key}/action`, { kind, value: value ?? null });
    toast(`${symbol}: ${kind} queued`, res.message, 'ok', 5000);
    watchLog(symbol);
    if (kind === 'retire') offerDisable(symbol);
    refresh();
  } catch (err) {
    toast(`${symbol}: ${kind} refused`, err.message, 'err', 9000);
  }
}

/* After a retire, offer the config edit the CLI tells you to do by hand. */
function offerDisable(symbol) {
  setTimeout(() => modal({
    title: `Disable ${symbol} in config.py?`,
    confirmLabel: 'Set enabled: False',
    warn: `Retire leaves the coin paused but still <b>enabled</b>, so a restart would start ` +
          `trading it again. Setting <code>enabled: False</code> in helpers/config.py makes ` +
          `the retirement stick.`,
    html: `<p class="small muted">The coin keeps running (paused) until the next restart.</p>`,
    onConfirm: () => saveConfig(symbol, { enabled: false }),
  }), 700);
}

/* Show what the bot logged in response to a queued command. */
function watchLog(symbol) {
  const key = symbol.replace('/', '_');
  const seen = new Set();
  const peek = async () => {
    try {
      const res = await api(`/api/log?symbol=${encodeURIComponent(key)}&limit=12`);
      const hits = res.lines.filter((l) => /MANUAL|DASHBOARD|REFUSED|ABORTED|RETIRE|CLEARED/i.test(l));
      const last = hits[hits.length - 1];
      if (last && !seen.has(last)) {
        seen.add(last);
        toast(symbol, last.replace(/^\S+ \S+ \w+ /, ''), 'info', 11000);
      }
    } catch (_) { /* log polling is best-effort */ }
  };
  setTimeout(peek, 1800);
  setTimeout(peek, 5000);
}

async function saveConfig(symbol, fields) {
  const key = symbol.replace('/', '_');
  try {
    const res = await post(`/api/coin/${key}/config`, { fields });
    const changed = res.edits.map((e) => `${e.key}: ${e.old} → ${e.new}`).join('\n');
    toast(`${symbol}: config.py saved`,
          changed + (res.notes.length ? '\n' + res.notes.join('\n') : ''), 'ok', 11000);
    if (res.started) toast(`${symbol} is now running`, res.notes.join('\n'), 'ok', 14000);
    else if (res.notes.length) toast(`${symbol}: heads up`, res.notes.join('\n'), 'info', 12000);
    // Rebuild the panel so the form reflects what config.py now holds.
    S.nodes.delete('detail:' + symbol);
    S.nodes.delete('idetail:' + symbol);
    refresh();
  } catch (err) {
    toast(`${symbol}: config not saved`, err.message, 'err', 11000);
  }
}

/* --- totals -------------------------------------------------------------- */

/* The account total: cash + coins at the latest tick price. Real assets only --
   no unrealized PnL is folded in here, so the number is what you could actually
   liquidate to, not what the position might be worth on paper.
   Deliberately explicit about what it could not see, because a portfolio total
   that quietly omits part of the portfolio is worse than no total at all. */
function accountCard(snap) {
  const t = snap.totals;
  const label = snap.mode === 'live' ? 'Kraken account' : 'Paper portfolio';
  const caveats = [];
  // Kraken can omit a base from the balance payload; those coins fall back to
  // the tracked grid qty, so say so rather than implying a full wallet total.
  const partial = t.coins_active - t.coins_valued_from_wallet;
  if (t.holdings_basis === 'wallet' && partial > 0) {
    caveats.push(`${partial} coin(s) valued from grid qty`);
  }
  if (t.coins_no_price) caveats.push(`${t.coins_no_price} coin(s) with no price yet`);
  if (t.other_unpriced && t.other_unpriced.length) {
    caveats.push(`no USD price for ${t.other_unpriced.join(', ')}`);
  }

  if (snap.mode === 'live' && snap.wallet.kraken_age_sec !== null) {
    caveats.push(`balance ${ago(snap.wallet.kraken_age_sec)}`);
  }

  if (t.total_value === null || t.total_value === undefined) {
    return { k: label, v: usd(t.holdings_value) + ' +cash',
             sub: 'waiting for the first balance fetch',
             title: 'The USD balance has not been fetched yet, so only the coin side is known.' };
  }
  return {
    k: label,
    v: usd(t.total_value),
    sub: `cash ${usd(t.cash_usd)} + coins ${usd(t.holdings_value)}` +
         (t.other_value ? ` + other ${usd(t.other_value)}` : '') +
         (caveats.length ? ` · ${caveats.join(' · ')}` : ''),
    title: 'Real assets only — cash plus the market value of what you hold. ' +
      'No unrealized PnL is included.\n\n' +
      (t.holdings_basis === 'wallet'
        ? 'Coins are valued from your real exchange balances' +
          (t.untracked_value ? ` (${usd(t.untracked_value)} of it held outside the grids).` : '.') +
          ((t.other_assets || []).length
            ? '\n\nOther assets (not traded by the bot):\n' + t.other_assets.map((o) =>
                `  ${o.asset} ${o.qty} = ${o.value === null ? 'no USD price' : usd(o.value)}`).join('\n')
            : '')
        : 'Coins are valued from the bot\'s tracked grid positions. Real exchange '
          + 'balances are only available in live mode.'),
  };
}

function renderTotals(snap) {
  const t = snap.totals;
  // The account total is the headline and it is real assets only. The old
  // "Net position" card (realized + unrealized) is gone: adding banked PnL to a
  // mark-to-market figure produced a number that was neither.
  const cards = [
    accountCard(snap),
    { k: 'Total realized', v: usdSigned(t.realized_total), c: cls(t.realized_total),
      sub: `active ${usdSigned(t.realized_active)} · retired ${usdSigned(t.retired)}` },
    { k: 'Position value', v: usd(t.position_value),
      sub: `cost basis ${usd(t.cost_basis)} · ${t.open_levels} level(s) across ` +
           `${t.coins_holding} coin(s)` },
    { k: 'Unrealized', v: usdSigned(t.unrealized), c: cls(t.unrealized),
      sub: 'mark-to-market — not banked, not in the account total' },
  ];

  renderCards($('#totals'), 'tcard:', cards);
}

/* Cards persist and are patched, like every other repeating region -- see
   cached()/syncChildren(). Cheap, and it keeps text selection alive. Each card
   is {k: label, v: value, c: value class, sub, title, id: stable key}. */
function renderCards(host, prefix, cards) {
  const keep = new Set();
  const nodes = cards.map((c) => {
    const key = prefix + (c.id || c.k);
    keep.add(key);
    const card = cached(key, () => {
      const box = el('div', 'card panel');
      box.append(el('div', 'k', c.k), el('div', 'v'), el('div', 'sub'));
      return box;
    });
    setText(card.children[0], c.k);
    const value = card.children[1];
    value.className = 'v ' + (c.c || '');
    setText(value, c.v);
    setText(card.children[2], c.sub || '');
    if (card.title !== (c.title || '')) card.title = c.title || '';
    return card;
  });
  dropCached(prefix, keep);
  syncChildren(host, nodes);
}

/* --- status badges ------------------------------------------------------- */

function badges(coin) {
  const out = [];
  const p = coin.price_prec;
  if (coin.queued_commands) {
    out.push(['info', `${coin.queued_commands} QUEUED — waiting for next tick`]);
  }
  if (coin.paused) out.push(['warn', 'PAUSED']);
  const nb = coin.next_buy, ns = coin.next_sell;
  if (nb.kind === 'limit') out.push(['info', `BUY LIMIT ${price(nb.price, p)}`]);
  else if (nb.kind === 'trail') out.push(['info', `BUY-TRAIL${nb.manual ? ' MAN' : ''} →${price(nb.price, p)}`]);
  else if (nb.kind === 'grid_full') out.push(['bad', 'GRID FULL']);
  else if (nb.kind === 'buys_paused') out.push(['warn', 'BUYS PAUSED']);
  if (ns.kind === 'target') out.push(['good', `TARGET ${price(ns.price, p)}`]);
  else if (ns.kind === 'limit') out.push(['good', `SELL LIMIT ${price(ns.price, p)}`]);
  else if (ns.kind === 'trail') out.push(['good', `SELL-TRAIL${ns.manual ? ' MAN' : ''} →${price(ns.price, p)}`]);
  if (coin.breakeven_exit_armed) out.push(['plain', 'BE-EXIT']);
  if (coin.pause_after_sell) out.push(['plain', 'PAUSE-AFTER-SELL']);
  if (coin.stop_loss_pct) {
    out.push(['bad', coin.stop_loss_trigger
      ? `SL →${price(coin.stop_loss_trigger, p)}` : `SL −${(coin.stop_loss_pct * 100).toFixed(1)}%`]);
  }
  const span = el('span', 'tags');
  for (const [k, text] of out) span.appendChild(el('span', 'badge ' + k, text));
  return span;
}

function nextCell(coin) {
  const p = coin.price_prec;
  const wrap = el('div', 'small');
  const line = (label, block, color) => {
    const d = el('div');
    d.innerHTML = `<span class="dim">${label}</span> <span class="${color}">${
      block.price ? esc(price(block.price, p)) : esc(block.label)}</span>` +
      (block.delta_pct === null || block.delta_pct === undefined
        ? '' : ` <span class="muted num">${esc(pct(block.delta_pct, 2))}</span>`);
    return d;
  };
  const buyColor = coin.next_buy.kind === 'grid_full' ? 'neg'
    : coin.next_buy.kind === 'buys_paused' ? 'warn-text' : 'pos';
  wrap.appendChild(line('buy', coin.next_buy, buyColor));
  wrap.appendChild(line('sell', coin.next_sell, 'neg'));
  return wrap;
}

/* --- coin icons ----------------------------------------------------------- */

/* Deterministic colour per ticker, so a coin without a cached logo still gets
   a badge you learn to recognise instead of a generic grey blob. */
function tickerHue(base) {
  let hash = 0;
  for (let i = 0; i < base.length; i++) hash = (hash * 31 + base.charCodeAt(i)) >>> 0;
  return hash % 360;
}

function monogram(base) {
  const span = el('span', 'coin-icon coin-badge', base.slice(0, 3));
  const hue = tickerHue(base);
  span.style.background = `hsl(${hue} 46% 34%)`;
  span.style.color = `hsl(${hue} 75% 84%)`;
  if (base.length > 2) span.classList.add('tight');
  return span;
}

/* Cached logo when we have one, monogram otherwise. onerror covers the gap
   between a manifest entry and a file that has since gone missing. */
function coinIcon(coin) {
  if (!coin.has_icon) return monogram(coin.base);
  const img = el('img', 'coin-icon');
  img.alt = '';
  // Not lazy: these are a few KB each from the same host, and lazy loading
  // would defer the onerror swap for off-screen rows, so a missing icon would
  // pop into a monogram only once you scrolled to it.
  img.onerror = () => img.replaceWith(monogram(coin.base));
  img.src = `/icons/${encodeURIComponent(coin.base)}`;
  return img;
}

function symbolCell(coin, tag = 'span') {
  const wrap = el(tag, 'sym-wrap');
  wrap.append(coinIcon(coin), el('span', 'sym', coin.symbol));
  return wrap;
}

/* --- coins table --------------------------------------------------------- */

function sortedCoins(coins) {
  const { key, dir } = S.sort;
  return [...coins].sort((a, b) => {
    const x = a[key], y = b[key];
    if (x === y) return a.index - b.index;
    if (x === null || x === undefined) return 1;
    if (y === null || y === undefined) return -1;
    return (typeof x === 'string' ? x.localeCompare(y) : x - y) * dir;
  });
}

/* One entry per column: header label, how to sort it, how to build its cell.
   The header and the rows are both generated from this list, so the two can't
   drift, and reordering the table is just reordering keys. `cls` lands on both
   the <th> and the <td> ('l' = left-aligned, 'hide-sm' = dropped on narrow
   screens). */
const COLUMNS = [
  { key: 'index', label: '#', cls: 'l', sort: 'index', text: true,
    cell: (c) => { const t = el('td'); t.innerHTML = `<span class="idx">${c.index}</span>`; return t; } },
  { key: 'symbol', label: 'Symbol', cls: 'l', sort: 'symbol', text: true,
    cell: (c) => { const t = el('td'); t.appendChild(symbolCell(c)); return t; } },
  { key: 'price', label: 'Price', sort: 'price',
    cell: (c) => el('td', 'num', price(c.price, c.price_prec)) },
  { key: 'levels', label: 'Levels', sort: 'levels',
    cell: (c) => { const t = el('td', 'num');
                   t.innerHTML = `${c.levels}<span class="dim">/${c.max_levels}</span>`; return t; } },
  { key: 'avg', label: 'Avg entry', cls: 'hide-sm', sort: 'avg_entry',
    cell: (c) => el('td', 'num', price(c.avg_entry, c.price_prec)) },
  { key: 'position', label: 'Position', sort: 'position_value',
    cell: (c) => el('td', 'num', c.levels ? usd(c.position_value) : '—') },
  { key: 'unrealized', label: 'Unrealized', sort: 'unrealized',
    cell: (c) => { const t = signedCell(c.unrealized);
                   if (c.unrealized_pct !== null && c.unrealized_pct !== undefined) {
                     t.title = pct(c.unrealized_pct, 3);
                   }
                   return t; } },
  { key: 'realized', label: 'Realized', sort: 'realized',
    cell: (c) => signedCell(c.realized) },
  { key: 'cycles', label: 'Cycles', cls: 'hide-sm', sort: 'cycle',
    cell: (c) => el('td', 'num', String(c.cycle)) },
  { key: 'next', label: 'Next buy / sell', cls: 'l',
    cell: (c) => { const t = el('td'); t.appendChild(nextCell(c)); return t; } },
  { key: 'status', label: 'Status', cls: 'l',
    cell: (c) => { const t = el('td'); t.appendChild(badges(c)); return t; } },
];

const COLUMN_KEYS = COLUMNS.map((c) => c.key);
const COLUMN_BY_KEY = new Map(COLUMNS.map((c) => [c.key, c]));

/* Saved order, repaired against the current column set: unknown keys (renamed
   or removed since the order was saved) are dropped and new columns appended,
   so a stale localStorage entry can never blank out the table. */
function columnOrder() {
  let saved = [];
  try {
    saved = (localStorage.getItem('gridbot.colOrder') || '').split(',').filter(Boolean);
  } catch (_) { /* storage unavailable */ }
  const order = saved.filter((k) => COLUMN_BY_KEY.has(k));
  for (const key of COLUMN_KEYS) if (!order.includes(key)) order.push(key);
  return order;
}

function saveColumnOrder(order) {
  try { localStorage.setItem('gridbot.colOrder', order.join(',')); } catch (_) { /* ignore */ }
  $('#btn-reset-cols').hidden = order.join(',') === COLUMN_KEYS.join(',');
}

function moveColumn(fromKey, toKey, after) {
  const order = columnOrder().filter((k) => k !== fromKey);
  const at = order.indexOf(toKey);
  order.splice(after ? at + 1 : at, 0, fromKey);
  saveColumnOrder(order);
  buildHeader();
  render();
}

function buildHeader() {
  const row = $('#coins-head');
  const order = columnOrder();
  row.replaceChildren(...order.map((key) => {
    const col = COLUMN_BY_KEY.get(key);
    const th = el('th', col.cls || '');
    th.draggable = true;
    th.dataset.col = key;
    th.title = 'Drag to reorder' + (col.sort ? ' · click to sort' : '');
    th.append(el('span', null, col.label));

    if (col.sort) {
      if (S.sort.key === col.sort) {
        th.classList.add('sorted');
        th.append(el('span', 'sort-caret', S.sort.dir === 1 ? ' ▲' : ' ▼'));
      }
      th.onclick = () => {
        if (S.dragged) return;      // a drag just ended -- don't also sort
        const ascFirst = col.sort === 'symbol' || col.sort === 'index';
        S.sort = { key: col.sort,
                   dir: S.sort.key === col.sort ? -S.sort.dir : (ascFirst ? 1 : -1) };
        buildHeader();
        render();
      };
    }

    const isAfter = (e) => {
      const box = th.getBoundingClientRect();
      return e.clientX > box.left + box.width / 2;
    };
    th.ondragstart = (e) => {
      S.dragKey = key;
      S.dragged = true;
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', key);   // Firefox needs a payload
      th.classList.add('dragging');
    };
    th.ondragend = () => {
      th.classList.remove('dragging');
      clearDropMarks();
      S.dragKey = null;
      setTimeout(() => { S.dragged = false; }, 0);
    };
    th.ondragover = (e) => {
      if (!S.dragKey || S.dragKey === key) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      th.classList.toggle('drop-after', isAfter(e));
      th.classList.toggle('drop-before', !isAfter(e));
    };
    th.ondragleave = () => th.classList.remove('drop-before', 'drop-after');
    th.ondrop = (e) => {
      e.preventDefault();
      const from = S.dragKey;
      clearDropMarks();
      if (from && from !== key) moveColumn(from, key, isAfter(e));
    };
    return th;
  }));

  $('#btn-reset-cols').hidden = order.join(',') === COLUMN_KEYS.join(',');
}

function clearDropMarks() {
  for (const th of document.querySelectorAll('#coins-head th')) {
    th.classList.remove('drop-before', 'drop-after');
  }
}

function coinCells(coin) {
  return columnOrder().map((key) => {
    const col = COLUMN_BY_KEY.get(key);
    const td = col.cell(coin);
    if (col.cls) td.classList.add(...col.cls.split(' '));
    return td;
  });
}

function renderCoins(snap) {
  const body = $('#coins-body');
  const desired = [];
  const keep = new Set();
  let selected = null;

  for (const coin of sortedCoins(snap.coins)) {
    const symbol = coin.symbol;
    const rowKey = 'row:' + symbol;
    keep.add(rowKey);

    // The <tr> persists; only its cells are rebuilt (they hold no state).
    const tr = cached(rowKey, () => {
      const node = el('tr', 'coin');
      node.onclick = () => openCoin(S.open === symbol ? null : symbol);
      return node;
    });
    tr.className = 'coin' + (coin.paused ? ' paused' : '') + (S.open === symbol ? ' open' : '');
    tr.replaceChildren(...coinCells(coin));
    desired.push(tr);
    if (S.open === symbol) selected = coin;
  }

  dropCached('row:', keep);
  syncChildren(body, desired);
  renderCoinDetail(selected);
}

/* Select a coin (or null to close): highlights its row, opens its detail
   panel and scopes the realized chart to it. Shared by the table, the
   allocation donut and the performance quadrant. */
function openCoin(symbol) {
  S.open = symbol;
  if (symbol) { S.chartScope = symbol; loadHistory(); }
  render();
}

/* The detail panel lives BELOW the whole table, not inside it: opening a coin
   must not push the other rows out from under the reader's cursor. The list
   stays put and the selected row is highlighted instead. */
function renderCoinDetail(coin) {
  const host = $('#coin-detail');
  if (!coin) {
    dropCached('detail:', new Set());
    syncChildren(host, []);
    S.shown = null;
    return;
  }

  const key = 'detail:' + coin.symbol;
  const panel = cached(key, () => {
    const box = el('div', 'coin-detail-panel');
    box.append(detailHead(coin), detailShell(coin));
    return box;
  });
  dropCached('detail:', new Set([key]));
  updateDetail(panel, coin);
  syncChildren(host, [panel]);

  // Only when the selection actually changes, and only as far as needed --
  // 'nearest' does nothing when the panel is already on screen.
  if (S.shown !== coin.symbol) {
    S.shown = coin.symbol;
    panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }
}

function detailHead(coin) {
  const head = el('div', 'detail-head');
  const title = el('div', 'sym-wrap');
  title.append(el('span', 'idx', String(coin.index)), coinIcon(coin),
               el('span', 'sym', coin.symbol));
  const live = el('div', 'detail-head-live small muted num');
  const close = el('button', 'tiny', 'Close ✕');
  close.onclick = () => { S.open = null; render(); };
  head.append(title, live, el('span', 'spacer'), close);
  return head;
}

/* --- detail panel -------------------------------------------------------- */

function kvBlock(title, rows) {
  const block = el('div', 'block');
  block.appendChild(el('h3', null, title));
  const dl = el('dl', 'kv');
  for (const [k, v, klass] of rows) {
    dl.appendChild(el('dt', null, k));
    const dd = el('dd', klass || '');
    if (v instanceof Node) dd.appendChild(v); else dd.textContent = v;
    dl.appendChild(dd);
  }
  block.appendChild(dl);
  return block;
}

/* Built once per opened coin. The volatile numbers live in `.detail-live`
   (replaced on every poll); the controls, the config form and the log box are
   built here and then left alone, so typing in the form and the log's scroll
   position survive the refresh. */
function detailShell(coin) {
  const wrap = el('div', 'detail-wrap');
  const grid = el('div', 'detail-grid');
  grid.append(blockShell('Position', 'pos'), blockShell('Next buy / sell', 'next'),
              actionsBlock(coin));
  wrap.append(grid, el('div', 'detail-positions'), configBlock(coin));
  return wrap;
}

function blockShell(title, key) {
  const block = el('div', 'block');
  block.appendChild(el('h3', null, title));
  const dl = el('dl', 'kv');
  dl.dataset.kv = key;
  block.appendChild(dl);
  return block;
}

/* `rows` is a list of [label, value, class?] pairs. A bare string instead of an
   array becomes a full-width sub-heading, which is how the buy and sell halves
   stay readable inside one block. */
function fillKv(root, key, rows) {
  const dl = root.querySelector(`[data-kv="${key}"]`);
  if (!dl) return;
  const out = [];
  for (const row of rows) {
    if (typeof row === 'string') {
      out.push(el('dt', 'kv-section', row));
      continue;
    }
    const [k, v, klass] = row;
    out.push(el('dt', null, k));
    const dd = el('dd', klass || '');
    if (v instanceof Node) dd.appendChild(v); else dd.textContent = v;
    out.push(dd);
  }
  dl.replaceChildren(...out);
}

function updateDetail(dtr, coin) {
  const p = coin.price_prec;

  setText(dtr.querySelector('.detail-head-live'),
          `${price(coin.price, p)}   ${coin.levels}/${coin.max_levels} levels   ` +
          `unrealized ${usdSigned(coin.unrealized)}   realized ${usdSigned(coin.realized)}`);

  fillKv(dtr, 'pos', [
    ['Levels', `${coin.levels} / ${coin.max_levels}`],
    ['Avg entry', price(coin.avg_entry, p)],
    ['Last buy', price(coin.last_buy, p)],
    ['Tracked qty', qty(coin.qty)],
    ['Wallet qty', coin.wallet_qty === null ? '—' : qty(coin.wallet_qty)],
    ['Untracked', coin.untracked_qty === null ? '—' : qty(coin.untracked_qty)],
    ['Cost basis', usd(coin.cost_usd)],
    ['Value now', usd(coin.position_value)],
    ['Unrealized', usdSigned(coin.unrealized) +
      (coin.unrealized_pct === null ? '' : `  (${pct(coin.unrealized_pct, 3)})`), cls(coin.unrealized)],
    ['Realized', usdSigned(coin.realized), cls(coin.realized)],
    ['Cycles', String(coin.cycle)],
    ['Max grid spend', usd(coin.max_grid_spend)],
    ['Queued commands', coin.queued_commands
      ? `${coin.queued_commands} (applied on the next price tick)` : '0'],
  ]);

  const nb = coin.next_buy, ns = coin.next_sell;
  const buyRows = [['State', nb.label]];
  if (nb.price) buyRows.push(['Price', price(nb.price, p) + '  ' + pct(nb.delta_pct, 3)]);
  if (nb.qty) buyRows.push(['Qty', qty(nb.qty)]);
  if (nb.cancels_at) buyRows.push(['Cancels at', price(nb.cancels_at, p) + '  ' + pct(nb.cancel_delta_pct, 3)]);
  if (nb.source) buyRows.push(['Source', nb.source]);
  if (nb.extreme) buyRows.push(['Low so far', price(nb.extreme, p)]);

  const sellRows = [['State', ns.label]];
  if (ns.price) sellRows.push(['Price', price(ns.price, p) + '  ' + pct(ns.delta_pct, 3)]);
  if (ns.qty) sellRows.push(['Qty', qty(ns.qty)]);
  if (ns.cancels_at) sellRows.push(['Cancels at', price(ns.cancels_at, p) + '  ' + pct(ns.cancel_delta_pct, 3)]);
  if (ns.source) sellRows.push(['Source', ns.source]);
  if (ns.extreme) sellRows.push(['High so far', price(ns.extreme, p)]);
  if (ns.floor) sellRows.push(['Floor (avg)', price(ns.floor, p)]);
  if (coin.stop_loss_pct) {
    sellRows.push(['Stop-loss', `−${(coin.stop_loss_pct * 100).toFixed(2)}% → ` +
      price(coin.stop_loss_trigger, p) + '  ' + pct(coin.stop_loss_delta_pct, 3), 'neg']);
  }
  fillKv(dtr, 'next', ['Buy', ...buyRows, 'Sell', ...sellRows]);

  // Toggles relabel and light up live, without rebuilding the controls.
  for (const btn of dtr.querySelectorAll('.control-group button[data-action]')) {
    const action = S.meta.actions.find((a) => a.kind === btn.dataset.action);
    if (action) paintControl(btn, coin, action);
  }

  const positions = dtr.querySelector('.detail-positions');
  if (positions) {
    positions.replaceChildren(...(coin.positions.length ? [positionsBlock(coin)] : []));
  }
}

/* Controls, grouped by what they act on. Toggles show their state (lit when on)
   and relabel live on every poll; one-shots are plain; anything that moves money
   or wipes state is red. */
const CONTROL_GROUPS = [
  { title: 'Run state', kinds: ['pause_toggle', 'pause_buys_toggle', 'arm_pause_after_sell',
                                'arm_breakeven_exit'] },
  { title: 'Buy',   kinds: ['arm_buy_trail', 'buy'] },
  { title: 'Sell',  kinds: ['arm_sell_trail', 'sell_all'],
    values: ['set_target_sell', 'set_stop_loss'] },
  { title: 'Reset', kinds: ['clear_targets', 'clear_stats', 'retire'] },
];

const CONTROL_LABELS = {
  buy: 'Force BUY now', sell_all: 'Sell ALL now', clear_targets: 'Clear targets',
  clear_stats: 'Clear stats', retire: 'Retire coin',
};

/* Live label and on/off for each toggle, from the latest snapshot. */
function controlState(coin, kind) {
  switch (kind) {
    case 'pause_toggle':
      return { label: coin.paused ? 'Resume trading' : 'Pause trading', on: coin.paused };
    case 'pause_buys_toggle':
      return { label: coin.buys_paused ? 'Resume buying' : 'Pause buying', on: coin.buys_paused };
    case 'arm_pause_after_sell':
      return { label: 'Pause after sell', on: coin.pause_after_sell };
    case 'arm_breakeven_exit':
      return { label: 'Breakeven exit', on: coin.breakeven_exit_armed };
    case 'arm_sell_trail': {
      const on = !!(coin.trailing_sell?.armed && coin.trailing_sell.manual);
      return { label: on ? 'Disarm sell-trail' : 'Arm sell-trail', on };
    }
    case 'arm_buy_trail':
      return { label: 'Arm buy-trail', on: !!(coin.trailing_buy?.armed && coin.trailing_buy.manual) };
    default:
      return null;
  }
}

function paintControl(btn, coin, action) {
  const st = controlState(coin, action.kind);
  const label = st ? st.label : (CONTROL_LABELS[action.kind] || action.short);
  if (btn.textContent !== label) btn.textContent = label;
  btn.classList.toggle('toggle', !!st);
  btn.classList.toggle('on', !!(st && st.on));
  // A buy-trail armed while buying is paused would sit idle and then fire the
  // moment buying resumed; the bot refuses it, so don't offer it here either.
  const blocked = action.kind === 'arm_buy_trail' && coin.buys_paused;
  btn.disabled = blocked;
  btn.title = blocked ? 'Buying is paused — resume buying to arm a buy-trail'
                      : `${action.label} (menu key ${action.key})`;
}

function controlButton(coin, action) {
  const btn = el('button', action.danger ? 'danger' : '');
  btn.dataset.action = action.kind;
  paintControl(btn, coin, action);          // sets label, toggle state and title
  btn.onclick = () => {
    const fresh = coinBySymbol(coin.symbol) || coin;
    modal({ ...actionCopy(action.kind, fresh), onConfirm: () => runAction(coin.symbol, action.kind) });
  };
  return btn;
}

function actionsBlock(coin) {
  const block = el('div', 'block');
  // Collapsible like Config, but open by default -- the controls are the reason
  // you open a coin. The choice is remembered, so collapsing it sticks.
  const { body } = disclosure('Controls', 'controlsOpen', true);
  block.appendChild(body.parentElement);
  const byKind = new Map(S.meta.actions.map((a) => [a.kind, a]));

  // Anything in the catalog that isn't placed below still gets a button, so a
  // new action can never silently go missing from the dashboard.
  const placed = new Set(CONTROL_GROUPS.flatMap((g) => [...g.kinds, ...(g.values || [])]));
  const extras = S.meta.actions.filter((a) => !a.local && !a.takes_value && !placed.has(a.kind));
  const groups = extras.length
    ? [...CONTROL_GROUPS, { title: 'Other', kinds: extras.map((a) => a.kind) }] : CONTROL_GROUPS;

  for (const group of groups) {
    const section = el('div', 'control-group');
    section.appendChild(el('div', 'control-group-title', group.title));
    const row = el('div', 'actions');
    for (const kind of group.kinds) {
      if (byKind.has(kind)) row.appendChild(controlButton(coin, byKind.get(kind)));
    }
    section.appendChild(row);
    for (const kind of group.values || []) {
      if (kind === 'set_target_sell') {
        section.appendChild(valueRow(coin, kind, 'Target sell $',
          coin.next_sell.kind === 'target' ? String(coin.next_sell.price) : '',
          'price', 'post-only limit for the whole position · above market · 0 clears'));
      } else if (kind === 'set_stop_loss') {
        section.appendChild(valueRow(coin, kind, 'Stop-loss %',
          coin.stop_loss_pct ? (coin.stop_loss_pct * 100).toFixed(2) : '',
          'e.g. 8', '% below avg entry · market-sells all + pauses · 0 clears'));
      }
    }
    body.appendChild(section);
  }
  return block;
}

function valueRow(coin, kind, label, current, placeholder, hint) {
  const row = el('div', 'value-row');
  row.appendChild(el('label', null, label));
  const input = el('input');
  input.type = 'text';
  input.inputMode = 'decimal';
  input.value = current;
  input.placeholder = placeholder;
  const go = el('button', 'tiny', 'Set');
  const submit = () => {
    const raw = input.value.trim();
    const value = raw === '' ? 0 : Number(raw);
    if (Number.isNaN(value)) { toast('Invalid number', raw, 'err', 5000); return; }
    const fresh = coinBySymbol(coin.symbol) || coin;
    modal({ ...valueCopy(kind, fresh, value), onConfirm: () => runAction(coin.symbol, kind, value) });
  };
  go.onclick = submit;
  input.onkeydown = (e) => { if (e.key === 'Enter') submit(); };
  row.append(input, go);
  const note = el('div', 'small dim');
  note.textContent = hint;
  const box = el('div');
  box.append(row, note);
  return box;
}

function positionsBlock(coin) {
  const block = el('div', 'block');
  block.appendChild(el('h3', null, `Open levels (${coin.positions.length})`));
  const table = el('table');
  table.innerHTML = '<thead><tr><th class="l">Lvl</th><th>Qty</th><th>Entry</th>' +
    '<th>Cost</th><th>Fee</th><th>Unrealized</th><th class="l">Opened</th></tr></thead>';
  const tb = el('tbody');
  for (const pos of coin.positions) {
    const tr = el('tr');
    const cost = pos.qty * pos.entry_price;
    tr.innerHTML =
      `<td class="l num">${pos.level}</td>` +
      `<td class="num">${esc(qty(pos.qty))}</td>` +
      `<td class="num">${esc(price(pos.entry_price, coin.price_prec))}</td>` +
      `<td class="num">${esc(usd(cost))}</td>` +
      `<td class="num dim">${esc(usd(pos.fee_usd))}</td>` +
      `<td class="num ${cls(pos.unrealized)}">${esc(usdSigned(pos.unrealized))}</td>` +
      `<td class="l dim small">${esc(String(pos.ts).replace('T', ' ').slice(0, 19))}</td>`;
    tb.appendChild(tr);
  }
  table.appendChild(tb);
  block.appendChild(table);
  return block;
}

/* --- config editor ------------------------------------------------------- */

function configBlock(coin) {
  const block = el('div', 'block');
  const { box, body } = disclosure('Config — helpers/config.py', 'configOpen');
  block.appendChild(box);

  const form = el('form');
  form.onsubmit = (e) => e.preventDefault();
  const inputs = new Map();

  for (const group of ['general', 'buy', 'sell']) {
    const fields = S.meta.fields.filter((f) => f.group === group);
    if (!fields.length) continue;
    body.appendChild(Object.assign(el('div', 'small dim'),
      { textContent: group.toUpperCase(), style: 'margin:10px 0 6px;letter-spacing:.08em' }));
    const grid = el('div', 'form-grid');
    for (const spec of fields) {
      const raw = coin.config_text?.[spec.key];
      const current = raw !== undefined ? raw
        : (coin.config?.[spec.key] === undefined ? '' : String(coin.config[spec.key]));
      const field = el('div', 'field');
      const restartTag = spec.key === 'enabled' ? ' (off needs restart)'
        : spec.structural ? ' (restart)' : '';
      const lab = el('label', null, spec.label + restartTag);
      field.appendChild(lab);

      let input;
      if (spec.kind === 'choice' || spec.kind === 'bool') {
        input = el('select');
        const options = spec.kind === 'bool' ? ['True', 'False'] : spec.choices;
        for (const opt of options) {
          const o = el('option', null, opt);
          o.value = opt;
          input.appendChild(o);
        }
        input.value = spec.kind === 'bool'
          ? (coin.config?.enabled === false ? 'False' : 'True')
          : (current.replace(/"/g, '') || spec.choices[0]);
      } else {
        input = el('input');
        input.type = 'text';
        input.inputMode = spec.kind === 'str' ? 'text' : 'decimal';
        input.value = current.replace(/"/g, '');
      }
      const initial = input.value;
      input.oninput = () => {
        field.classList.toggle('changed', input.value !== initial);
        if (spec.as_pct) hint.textContent = pctHint(input.value, spec);
      };
      input.onchange = input.oninput;
      field.appendChild(input);

      const hint = el('div', 'hint' + (spec.as_pct ? ' pcthint' : ''));
      hint.textContent = spec.as_pct ? pctHint(initial, spec) : spec.hint;
      field.appendChild(hint);

      inputs.set(spec.key, { input, initial, spec });
      grid.appendChild(field);
    }
    body.appendChild(grid);
  }

  const foot = el('div', 'form-foot');
  const save = el('button', 'primary', 'Save to config.py');
  const reset = el('button', '', 'Revert');
  const status = el('span', 'small dim');
  reset.onclick = () => {
    for (const [, { input, initial }] of inputs) {
      input.value = initial;
      input.dispatchEvent(new Event('input'));
    }
    status.textContent = 'reverted to the values this panel opened with';
  };
  save.onclick = () => {
    const changes = {};
    for (const [key, { input, initial, spec }] of inputs) {
      if (input.value === initial) continue;
      changes[key] = spec.kind === 'bool' ? input.value === 'True'
        : (spec.kind === 'choice' || spec.kind === 'str') ? input.value
        : input.value.trim();
    }
    const keys = Object.keys(changes);
    if (!keys.length) { status.textContent = 'nothing changed'; return; }
    const structural = keys.filter((k) => S.meta.fields.find((f) => f.key === k)?.structural);
    modal({
      title: `Save ${keys.length} change(s) to ${coin.symbol}`,
      confirmLabel: 'Write config.py',
      warn: structural.length
        ? (structural.includes('enabled') && changes.enabled === true
            ? `Turning <b>enabled</b> on starts this coin trading <b>immediately</b>.`
            : `<b>${structural.join(', ')}</b> ${structural.length > 1 ? 'are' : 'is'} ` +
              `structural — written to the file now, but only takes effect on restart.`)
        : null,
      html: '<p class="small">' + keys.map((k) =>
        `<code>${esc(k)}</code>: ${esc(inputs.get(k).initial)} → <b>${esc(String(changes[k]))}</b>`)
        .join('<br>') + '</p><p class="small muted">Non-structural values are hot-applied to the ' +
        'running coin immediately; a timestamped backup of config.py is kept under ' +
        'data/config_backups/.</p>',
      onConfirm: () => saveConfig(coin.symbol, changes),
    });
  };
  const remove = el('button', 'danger', 'Remove coin');
  remove.title = `Delete ${coin.symbol} from COINS in helpers/config.py`;
  remove.onclick = () => removeCoinFlow(coin);
  foot.append(save, reset, remove, status);
  body.append(foot, form);
  return block;
}

function pctHint(value, spec) {
  const n = Number(value);
  if (!Number.isFinite(n)) return spec.hint;
  return `= ${(n * 100).toFixed(4).replace(/0+$/, '').replace(/\.$/, '')}%  ·  ${spec.hint}`;
}

/* --- log ----------------------------------------------------------------- */

/* Only touch the DOM when the text actually changed, and keep the box where
   the reader left it. Replacing the text of a box that is sitting under the
   viewport is what makes the page jump, so we do it as rarely and as
   surgically as possible. */
function setLogText(pre, text, follow) {
  if (pre.textContent === text) return;
  const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
  const previous = pre.scrollTop;
  pre.textContent = text;
  pre.scrollTop = (follow && atBottom) ? pre.scrollHeight : previous;
}

/* --- inactive / retired -------------------------------------------------- */

function renderInactive(snap) {
  const body = $('#inactive-body');
  const desired = [];
  const keep = new Set();
  const shown = new Set();

  for (const coin of snap.inactive) {
    shown.add(coin.symbol);
    const symbol = coin.symbol;
    const rowKey = 'irow:' + symbol;
    keep.add(rowKey);
    const tr = cached(rowKey, () => {
      const node = el('tr', 'coin');
      node.onclick = () => {
        S.openInactive = S.openInactive === symbol ? null : symbol;
        render();
      };
      return node;
    });
    const state = coin.enabled
      ? '<span class="badge warn">enabled, not running</span>'
      : '<span class="badge plain">disabled</span>';
    const leftover = coin.realized === null ? '—'
      : `${usdSigned(coin.realized)} · ${coin.cycle} cycles` +
        (coin.levels ? ` · ${coin.levels} level(s)` : '');
    tr.innerHTML =
      `<td class="l"></td>` +
      `<td class="l">${state}</td>` +
      `<td class="num ${cls(coin.retired_pnl)}">${esc(coin.retired_pnl === null ? '—' : usdSigned(coin.retired_pnl))}</td>` +
      `<td class="hide-sm dim">${esc(coin.retired_on || '—')}</td>` +
      `<td class="num dim small">${esc(leftover)}</td>` +
      `<td class="l dim small">${S.openInactive === symbol ? 'hide' : 'edit config'}</td>`;
    tr.firstChild.appendChild(symbolCell(coin));
    desired.push(tr);

    if (S.openInactive === symbol) {
      const detailKey = 'idetail:' + symbol;
      keep.add(detailKey);
      desired.push(cached(detailKey, () => {
        const node = el('tr', 'detail');
        const cell = el('td');
        cell.colSpan = 6;
        const wrap = el('div', 'detail-wrap');
        const note = el('div', 'small muted');
        note.textContent = coin.enabled
          ? 'This coin is enabled in config.py but was not running when the bot started — restart to start it.'
          : 'Disabled. Set Enabled to True and restart the bot to trade it again.';
        wrap.append(note, configBlock(coin));
        cell.appendChild(wrap);
        node.appendChild(cell);
        return node;
      }));
    }
  }

  // Ledger entries for coins no longer present in COINS at all.
  for (const rec of snap.retired_ledger) {
    if (shown.has(rec.symbol)) continue;
    const rowKey = 'iledger:' + rec.symbol;
    keep.add(rowKey);
    const tr = cached(rowKey, () => el('tr'));
    tr.innerHTML =
      `<td class="l sym">${esc(rec.symbol)}</td>` +
      `<td class="l"><span class="badge plain">removed from config</span></td>` +
      `<td class="num ${cls(rec.realized_pnl_usd)}">${esc(usdSigned(rec.realized_pnl_usd))}</td>` +
      `<td class="hide-sm dim">${esc(rec.retired || '—')}</td>` +
      `<td class="num dim small">${rec.cycles} cycles</td><td></td>`;
    desired.push(tr);
  }

  dropCached('irow:', keep);
  dropCached('idetail:', keep);
  dropCached('iledger:', keep);
  syncChildren(body, desired);

  // Summary on the collapsed header, so the section is worth leaving shut.
  const retired = snap.retired_ledger.reduce((sum, r) => sum + r.realized_pnl_usd, 0);
  setText($('#inactive-summary'),
          `— ${desired.length} coin(s), retired PnL ${usdSigned(retired)}`);
}

/* --- small shared chart helpers ------------------------------------------ */

function storeGet(key, fallback) {
  try {
    const v = localStorage.getItem('gridbot.' + key);
    return v === null ? fallback : v;
  } catch (_) { return fallback; }
}
function storeSet(key, value) {
  try { localStorage.setItem('gridbot.' + key, String(value)); } catch (_) { /* ignore */ }
}

/* Compact axis money: $950, $1.2k, -$840. */
const usdShort = (v) => {
  const a = Math.abs(v);
  const body = a >= 1000 ? (a / 1000).toFixed(a >= 10000 ? 0 : 1) + 'k' : Math.round(a).toString();
  return (v < 0 ? '-$' : '$') + body;
};

/* Redraw a chart only when what it shows has changed. Snapshots land every
   2.5s and most of them move nothing visible -- rebuilding the SVG anyway
   would kill hover tooltips mid-read. */
function changed(name, key) {
  if (S.drawn[name] === key) return false;
  S.drawn[name] = key;
  return true;
}

/* Donut slice colours: distinct, handed out in config order so a coin keeps
   its colour as values shift. (Hashing the ticker, as the monograms do, puts
   too many coins on near-identical hues to tell slices apart.) */
const SLICE_COLORS = ['#58a6ff', '#f0883e', '#2ec4b6', '#bc8cff', '#e3b341', '#f778ba',
                      '#8ddb5a', '#79c0ff', '#ff9f8a', '#d2a8ff', '#c9a227', '#56d4dd'];
const sliceColor = (i) => SLICE_COLORS[i % SLICE_COLORS.length];

/* --- account value over time (A) ----------------------------------------- */

const VALUE_RANGES = [['1D', 1], ['1W', 7], ['1M', 30], ['3M', 90], ['All', 0]];

function buildValueRanges() {
  const host = $('#value-ranges');
  host.replaceChildren(...VALUE_RANGES.map(([label, days]) => {
    const b = el('button', days === S.valueDays ? 'on' : '', label);
    b.onclick = () => {
      S.valueDays = days;
      storeSet('valueDays', days);
      for (const other of host.children) other.classList.toggle('on', other === b);
      loadEquity();
    };
    return b;
  }));
}

async function loadEquity() {
  try {
    const res = await api('/api/equity' + (S.valueDays ? `?days=${S.valueDays}` : ''));
    S.equity = res.points;
    S.drawn.value = null;
    renderValue();
  } catch (err) {
    $('#value-chart').innerHTML = `<div class="small neg">${esc(err.message)}</div>`;
  }
}

/* Recorded samples plus the live total as the final point, so the line and
   the period change track the headline card instead of lagging 10 minutes. */
function valuePoints() {
  const pts = (S.equity || []).slice();
  const t = S.snap && S.snap.totals;
  if (t && t.total_value !== null && t.total_value !== undefined && !t.coins_no_price) {
    pts.push({ ts: S.snap.ts, total: t.total_value, realized: t.realized_total, live: true });
  }
  return pts;
}

function periodLabel(first) {
  const days = S.valueDays;
  const since = new Date(first.ts * 1000);
  // Recording hasn't covered the whole range yet: say what it does cover.
  if (!days || first.ts > Date.now() / 1000 - days * 86400 + 3600) {
    return 'since ' + since.toLocaleString([], { month: 'short', day: 'numeric',
                                                 hour: 'numeric', minute: '2-digit' });
  }
  return { 1: 'last 24h', 7: 'last 7 days', 30: 'last 30 days', 90: 'last 90 days' }[days];
}

function renderValue() {
  const host = $('#value-chart');
  const head = $('#value-change');
  const pts = valuePoints();
  const last = pts[pts.length - 1];
  if (!changed('value', pts.length + ':' + (last ? Math.round(last.total) : '') + ':' + host.clientWidth)) return;

  if (pts.length < 2) {
    head.textContent = '';
    host.innerHTML = '<div class="chart-hint">Recording started. The account value is saved ' +
      'every 10 minutes while the bot runs, so this line fills in over the next hour.</div>';
    return;
  }

  const first = pts[0];
  const delta = last.total - first.total;
  const pctChange = first.total ? delta / first.total * 100 : 0;
  const banked = (last.realized ?? 0) - (first.realized ?? 0);
  head.innerHTML =
    `<span class="num ${cls(delta)}">${esc(usdSigned(delta))} (${pctChange >= 0 ? '+' : ''}${pctChange.toFixed(2)}%)</span> ` +
    `<span class="muted">${esc(periodLabel(first))}</span>` +
    ` <span class="dim">· bot banked <span class="num ${cls(banked)}">${esc(usdSigned(banked))}</span></span>` +
    ` <span id="value-readout" class="dim"></span>`;

  const W = Math.max(320, host.clientWidth || 900), H = 200, padL = 52, padR = 8, padT = 10, padB = 20;
  const xs = pts.map((p) => p.ts), ys = pts.map((p) => p.total);
  const x0 = xs[0], x1 = xs[xs.length - 1];
  let y0 = Math.min(...ys), y1 = Math.max(...ys);
  const pad = Math.max((y1 - y0) * 0.12, y1 * 0.002, 1);
  y0 -= pad; y1 += pad;
  const sx = (t) => padL + (x1 === x0 ? 0.5 : (t - x0) / (x1 - x0)) * (W - padL - padR);
  const sy = (v) => padT + (1 - (v - y0) / (y1 - y0)) * (H - padT - padB);

  const line = pts.map((p, i) => `${i ? 'L' : 'M'}${sx(p.ts).toFixed(1)},${sy(p.total).toFixed(1)}`).join('');
  const area = `${line}L${sx(x1).toFixed(1)},${H - padB}L${sx(x0).toFixed(1)},${H - padB}Z`;
  const stroke = delta >= 0 ? 'var(--green)' : 'var(--red)';

  const ticks = [0, 0.5, 1].map((f) => y0 + pad + f * (y1 - y0 - 2 * pad));
  const grid = ticks.map((v) =>
    `<line x1="${padL}" x2="${W - padR}" y1="${sy(v).toFixed(1)}" y2="${sy(v).toFixed(1)}" stroke="var(--line)"/>` +
    `<text x="${padL - 6}" y="${(sy(v) + 4).toFixed(1)}" text-anchor="end" class="axis">${esc(usdShort(v))}</text>`).join('');
  const when = (ts) => new Date(ts * 1000).toLocaleString([], S.valueDays === 1
    ? { hour: 'numeric', minute: '2-digit' } : { month: 'short', day: 'numeric' });

  host.innerHTML =
    `<svg class="chart value-svg" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}">` +
      `<defs><linearGradient id="vg" x1="0" x2="0" y1="0" y2="1">` +
        `<stop offset="0%" stop-color="${stroke}" stop-opacity=".22"/>` +
        `<stop offset="100%" stop-color="${stroke}" stop-opacity="0"/>` +
      `</linearGradient></defs>` +
      grid +
      `<line x1="${sx(x0).toFixed(1)}" x2="${W - padR}" y1="${sy(first.total).toFixed(1)}" ` +
        `y2="${sy(first.total).toFixed(1)}" stroke="var(--line-2)" stroke-dasharray="3 4"/>` +
      `<path d="${area}" fill="url(#vg)"/>` +
      `<path d="${line}" fill="none" stroke="${stroke}" stroke-width="1.8" stroke-linejoin="round"/>` +
      `<text x="${padL}" y="${H - 4}" class="axis">${esc(when(x0))}</text>` +
      `<text x="${W - padR}" y="${H - 4}" text-anchor="end" class="axis">${esc(when(x1))}</text>` +
      `<line class="cross" y1="${padT}" y2="${H - padB}" stroke="var(--muted)" stroke-dasharray="2 3" visibility="hidden"/>` +
      `<circle class="cross" r="3.5" fill="${stroke}" visibility="hidden"/>` +
    `</svg>`;

  // Hover crosshair: nearest sample by time, value in the header readout.
  const svg = host.firstChild;
  const [vline, vdot] = svg.querySelectorAll('.cross');
  const readout = $('#value-readout');
  svg.onmousemove = (e) => {
    const r = svg.getBoundingClientRect();
    const t = x0 + ((e.clientX - r.left) * (W / r.width) - padL) / (W - padL - padR) * (x1 - x0);
    let best = pts[0];
    for (const p of pts) if (Math.abs(p.ts - t) < Math.abs(best.ts - t)) best = p;
    const cx = sx(best.ts).toFixed(1), cy = sy(best.total).toFixed(1);
    vline.setAttribute('x1', cx); vline.setAttribute('x2', cx);
    vdot.setAttribute('cx', cx); vdot.setAttribute('cy', cy);
    vline.setAttribute('visibility', 'visible'); vdot.setAttribute('visibility', 'visible');
    const stamp = new Date(best.ts * 1000).toLocaleString([], { month: 'short', day: 'numeric',
                                                                hour: 'numeric', minute: '2-digit' });
    readout.textContent = `· ${best.live ? 'now' : stamp}: ${usd(best.total)}`;
  };
  svg.onmouseleave = () => {
    vline.setAttribute('visibility', 'hidden'); vdot.setAttribute('visibility', 'hidden');
    readout.textContent = '';
  };
}

/* --- realized profit by period (B) --------------------------------------- */

function renderPeriods(h) {
  const trades = (h && h.cumulative) || [];
  const now = Date.now() / 1000;
  const midnight = new Date(); midnight.setHours(0, 0, 0, 0);
  const windowStats = (since) => {
    let sum = 0, n = 0;
    for (const p of trades) if (p.ts >= since) { sum += p.realized; n += 1; }
    return { sum, n };
  };
  const today = windowStats(midnight.getTime() / 1000);
  const week = windowStats(now - 7 * 86400);
  const month = windowStats(now - 30 * 86400);

  // Best calendar day in the last 30 (local time).
  const byDay = new Map();
  for (const p of trades) {
    if (p.ts < now - 30 * 86400) continue;
    const d = new Date(p.ts * 1000).toLocaleDateString([], { month: 'short', day: 'numeric' });
    byDay.set(d, (byDay.get(d) || 0) + p.realized);
  }
  let best = null;
  for (const [day, v] of byDay) if (!best || v > best.v) best = { day, v };

  const scope = S.chartScope ? S.chartScope.split('/')[0] + ' · ' : '';
  const sells = (n) => `${n} sell${n === 1 ? '' : 's'}`;
  renderCards($('#periods'), 'pcard:', [
    { id: 'today', k: scope + 'Realized today', v: usdSigned(today.sum), c: cls(today.sum),
      sub: sells(today.n) },
    { id: 'week', k: scope + 'Last 7 days', v: usdSigned(week.sum), c: cls(week.sum),
      sub: `${sells(week.n)} · ${usdSigned(week.sum / 7)}/day` },
    { id: 'month', k: scope + 'Last 30 days', v: usdSigned(month.sum), c: cls(month.sum),
      sub: `${sells(month.n)} · ${usdSigned(month.sum / 30)}/day` },
    { id: 'best', k: scope + 'Best day · 30d', v: best ? usdSigned(best.v) : '—',
      c: best ? cls(best.v) : '', sub: best ? best.day : 'no sells in 30 days' },
  ]);
}

/* --- where the money is (D) ---------------------------------------------- */

function allocationSlices(snap) {
  const t = snap.totals;
  const items = [];
  if (t.cash_usd) items.push({ name: 'Cash (USD)', value: t.cash_usd, color: '#6e7681' });
  snap.coins.forEach((c, i) => {
    const value = (c.wallet_qty !== null && c.price !== null)
      ? c.wallet_qty * c.price : (c.position_value || 0);
    items.push({ name: c.base, value, color: sliceColor(i), symbol: c.symbol });
  });
  (t.other_assets || []).forEach((o, i) => {
    if (o.value !== null) {
      items.push({ name: o.asset, value: o.value, color: sliceColor(snap.coins.length + i) });
    }
  });
  const total = items.reduce((a, i) => a + i.value, 0);
  const kept = [], small = [];
  for (const i of items.filter((i) => i.value >= 1).sort((a, b) => b.value - a.value)) {
    (i.name === 'Cash (USD)' || i.value / total >= 0.02 ? kept : small).push(i);
  }
  if (small.length === 1) kept.push(small[0]);
  else if (small.length) {
    kept.push({ name: `${small.length} others`, color: '#3a434f',
                value: small.reduce((a, i) => a + i.value, 0),
                detail: small.map((i) => `${i.name} ${usd(i.value)}`).join(', ') });
  }
  return { slices: kept, total };
}

function renderAlloc(snap) {
  const host = $('#alloc');
  const { slices, total } = allocationSlices(snap);
  if (!changed('alloc', slices.map((s) => s.name + Math.round(s.value)).join('|') + S.open)) return;
  if (!total) {
    host.innerHTML = '<div class="chart-hint">No balances yet.</div>';
    return;
  }

  const R = 92, r = 58, C = 100;
  const pt = (a, rad) => `${(C + rad * Math.sin(a)).toFixed(2)},${(C - rad * Math.cos(a)).toFixed(2)}`;
  let angle = 0;
  const arcs = slices.map((sl) => {
    // A lone 100% slice can't be drawn as one arc -- nudge it just short.
    const sweep = Math.min(sl.value / total, 0.99999) * Math.PI * 2;
    const a0 = angle, a1 = angle + sweep;
    angle = a1;
    const big = sweep > Math.PI ? 1 : 0;
    const d = `M${pt(a0, R)}A${R},${R} 0 ${big} 1 ${pt(a1, R)}L${pt(a1, r)}A${r},${r} 0 ${big} 0 ${pt(a0, r)}Z`;
    const share = (sl.value / total * 100).toFixed(1);
    const tip = `${sl.name}: ${usd(sl.value)} (${share}%)` + (sl.detail ? `\n${sl.detail}` : '');
    return `<path class="slice${sl.symbol ? ' coin' : ''}" d="${d}" fill="${sl.color}" ` +
      `stroke="var(--panel)" stroke-width="1.5" data-symbol="${esc(sl.symbol || '')}">` +
      `<title>${esc(tip)}</title></path>`;
  }).join('');

  host.innerHTML =
    `<div class="alloc">` +
      `<svg viewBox="0 0 200 200">${arcs}` +
        `<text x="100" y="98" text-anchor="middle" class="donut-v">${esc(usdShort(total))}</text>` +
        `<text x="100" y="116" text-anchor="middle" class="donut-k">total</text>` +
      `</svg>` +
      `<div class="legend">` + slices.map((sl) =>
        `<div class="row${sl.symbol ? ' coin' : ''}" data-symbol="${esc(sl.symbol || '')}"` +
          (sl.detail ? ` title="${esc(sl.detail)}"` : '') + `>` +
          `<span class="sw" style="background:${sl.color}"></span>` +
          `<span class="name">${esc(sl.name)}</span>` +
          `<span class="amt">${esc(usdShort(sl.value))} · ${(sl.value / total * 100).toFixed(0)}%</span>` +
        `</div>`).join('') +
      `</div>` +
    `</div>`;
  for (const node of host.querySelectorAll('[data-symbol]')) {
    if (node.dataset.symbol) node.onclick = () => openCoin(node.dataset.symbol);
  }
}

/* --- coin performance quadrant (1) --------------------------------------- */

function renderQuadrant(snap) {
  const host = $('#quadrant');
  const coins = snap.coins.map((c) => ({
    symbol: c.symbol, base: c.base,
    x: c.realized || 0,
    y: c.unrealized || 0,
    value: (c.wallet_qty !== null && c.price !== null) ? c.wallet_qty * c.price : (c.position_value || 0),
  }));
  const W = Math.max(300, host.clientWidth || 600), H = 280;
  const key = coins.map((c) => `${c.base}${Math.round(c.x)},${Math.round(c.y)},${Math.round(c.value / 20)}`).join('|');
  if (!changed('quad', key + S.open + W)) return;
  if (!coins.length) {
    host.innerHTML = '<div class="chart-hint">No active coins.</div>';
    return;
  }

  const padL = 46, padR = 14, padT = 12, padB = 24;
  let xa = Math.min(0, ...coins.map((c) => c.x)), xb = Math.max(0, ...coins.map((c) => c.x));
  let ya = Math.min(0, ...coins.map((c) => c.y)), yb = Math.max(0, ...coins.map((c) => c.y));
  const px = Math.max((xb - xa) * 0.08, 20), py = Math.max((yb - ya) * 0.12, 20);
  xa -= xa < 0 ? px : px * 0.4; xb += px;
  ya -= py; yb += py;
  const sx = (v) => padL + (v - xa) / (xb - xa) * (W - padL - padR);
  const sy = (v) => padT + (1 - (v - ya) / (yb - ya)) * (H - padT - padB);
  const maxV = Math.max(...coins.map((c) => c.value), 1);
  const rad = (v) => 4 + 9 * Math.sqrt(Math.max(v, 0) / maxV);

  const niceTicks = (a, b) => {
    const step0 = (b - a) / 4;
    const mag = 10 ** Math.floor(Math.log10(step0));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= step0);
    const out = [];
    for (let v = Math.ceil(a / step) * step; v <= b; v += step) out.push(Math.round(v * 100) / 100);
    return out;
  };
  const grid =
    niceTicks(xa, xb).map((v) =>
      `<line x1="${sx(v).toFixed(1)}" x2="${sx(v).toFixed(1)}" y1="${padT}" y2="${H - padB}" stroke="${v === 0 ? 'var(--line-2)' : 'var(--line)'}" stroke-opacity="${v === 0 ? 1 : .5}"/>` +
      `<text x="${sx(v).toFixed(1)}" y="${H - 6}" text-anchor="middle" class="axis">${esc(usdShort(v))}</text>`).join('') +
    niceTicks(ya, yb).map((v) =>
      `<line x1="${padL}" x2="${W - padR}" y1="${sy(v).toFixed(1)}" y2="${sy(v).toFixed(1)}" stroke="${v === 0 ? 'var(--line-2)' : 'var(--line)'}" stroke-opacity="${v === 0 ? 1 : .5}"/>` +
      `<text x="${padL - 6}" y="${(sy(v) + 4).toFixed(1)}" text-anchor="end" class="axis">${esc(usdShort(v))}</text>`).join('');

  // Corner captions, only for quadrants the axes actually show.
  const x0 = sx(0), y0 = sy(0);
  const q = [];
  if (y0 > padT + 18) q.push([W - padR - 4, padT + 12, 'end', 'banked · in profit now']);
  if (y0 < H - padB - 14) q.push([W - padR - 4, H - padB - 6, 'end', 'banked · underwater now']);
  if (x0 > padL + 150 && y0 < H - padB - 14) q.push([padL + 4, H - padB - 6, 'start', 'losing · underwater']);
  const captions = q.map(([x, y, anchor, t]) =>
    `<text x="${x}" y="${y}" text-anchor="${anchor}" class="qlabel">${t}</text>`).join('');

  // Dots, largest first so small ones stay clickable on top.
  const order = coins.slice().sort((a, b) => b.value - a.value);
  const dots = order.map((c) => {
    const tip = `${c.symbol}\nbanked ${usdSigned(c.x)}\nholding now ${usdSigned(c.y)}\n` +
                `net ${usdSigned(c.x + c.y)}\nposition ${usd(c.value)}`;
    const color = c.y < 0 ? 'var(--red)' : 'var(--blue)';
    const open = S.open === c.symbol;
    return `<g class="dotc" data-symbol="${esc(c.symbol)}">` +
      `<circle cx="${sx(c.x).toFixed(1)}" cy="${sy(c.y).toFixed(1)}" r="${rad(c.value).toFixed(1)}" ` +
        `fill="${color}" fill-opacity="${open ? .95 : .7}" stroke="${open ? 'var(--text)' : 'var(--panel)'}" ` +
        `stroke-width="${open ? 2 : 1.5}"/>` +
      `<title>${esc(tip)}</title></g>`;
  }).join('');

  // Labels right of each dot; nudge down (then up) past earlier labels, and
  // drop a label rather than let two overlap -- the tooltip still has it.
  const placed = [];
  const labels = coins.slice().sort((a, b) => sy(a.y) - sy(b.y)).map((c) => {
    const w = c.base.length * 6.8 + 4, h = 12;
    let lx = sx(c.x) + rad(c.value) + 3;
    if (lx + w > W - padR) lx = sx(c.x) - rad(c.value) - 3 - w;
    const base = sy(c.y) + 4;
    for (const dy of [0, 10, -10, 20, -20]) {
      const ly = base + dy;
      const box = { x: lx, y: ly - h + 2, w, h };
      if (!placed.some((p) => box.x < p.x + p.w && p.x < box.x + box.w && box.y < p.y + p.h && p.y < box.y + box.h)) {
        placed.push(box);
        return `<text x="${lx.toFixed(1)}" y="${ly.toFixed(1)}"${S.open === c.symbol ? ' class="sel"' : ''}>${esc(c.base)}</text>`;
      }
    }
    return '';
  }).join('');

  host.innerHTML =
    `<div class="quad"><svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}">` +
      grid + captions + dots + labels +
    `</svg>` +
    `<div class="small dim" style="display:flex;justify-content:space-between;margin-top:2px">` +
      `<span>↑ unrealized (holding now)</span><span>realized (banked) →</span></div></div>`;
  for (const g of host.querySelectorAll('.dotc')) g.onclick = () => openCoin(g.dataset.symbol);
}

/* --- chart --------------------------------------------------------------- */

function renderChart() {
  const host = $('#chart');
  const h = S.history;
  $('#chart-scope').textContent = S.chartScope
    ? `${S.chartScope} — ${h ? h.count : 0} closed sells`
    : `all coins — ${h ? h.count : 0} closed sells`;
  $('#btn-chart-all').style.display = S.chartScope ? '' : 'none';

  renderPeriods(h);
  if (!h || !h.cumulative.length) {
    host.innerHTML = '<div class="muted small">No closed sells found in the logs yet.</div>';
    return;
  }

  const W = 1000, H = 190, padL = 8, padR = 8, padT = 12, padB = 22;
  const pts = h.cumulative;
  const xs = pts.map((p) => p.ts);
  const ys = pts.map((p) => p.value);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  const y0 = Math.min(0, ...ys), y1 = Math.max(0, ...ys);
  const sx = (t) => padL + (x1 === x0 ? 0.5 : (t - x0) / (x1 - x0)) * (W - padL - padR);
  const sy = (v) => padT + (1 - (v - y0) / ((y1 - y0) || 1)) * (H - padT - padB);

  const line = pts.map((p, i) => `${i ? 'L' : 'M'}${sx(p.ts).toFixed(1)},${sy(p.value).toFixed(1)}`).join('');
  const area = `${line}L${sx(x1).toFixed(1)},${sy(y0).toFixed(1)}L${sx(x0).toFixed(1)},${sy(y0).toFixed(1)}Z`;
  const up = ys[ys.length - 1] >= 0;
  const stroke = up ? 'var(--green)' : 'var(--red)';
  const zeroY = sy(0).toFixed(1);
  const day = (ts) => new Date(ts * 1000).toISOString().slice(0, 10);

  const marks = pts.map((p) =>
    `<circle cx="${sx(p.ts).toFixed(1)}" cy="${sy(p.value).toFixed(1)}" r="2.1" ` +
    `fill="${p.realized >= 0 ? 'var(--green)' : 'var(--red)'}" opacity=".75">` +
    `<title>${esc(p.symbol)} ${esc(usdSigned(p.realized))} → ${esc(usdSigned(p.value))} (${esc(day(p.ts))})</title>` +
    `</circle>`).join('');

  const winRate = h.count ? (h.wins / h.count * 100).toFixed(0) : '0';
  host.innerHTML =
    `<svg class="chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" height="190">` +
      `<defs><linearGradient id="g" x1="0" x2="0" y1="0" y2="1">` +
        `<stop offset="0%" stop-color="${stroke}" stop-opacity=".26"/>` +
        `<stop offset="100%" stop-color="${stroke}" stop-opacity="0"/>` +
      `</linearGradient></defs>` +
      `<line x1="0" y1="${zeroY}" x2="${W}" y2="${zeroY}" stroke="var(--line-2)" stroke-dasharray="3 4"/>` +
      `<path d="${area}" fill="url(#g)"/>` +
      `<path d="${line}" fill="none" stroke="${stroke}" stroke-width="1.6" ` +
        `vector-effect="non-scaling-stroke" stroke-linejoin="round"/>` +
      marks +
    `</svg>` +
    `<div class="chart-head small" style="margin-top:6px">` +
      `<span class="dim">${esc(day(x0))} → ${esc(day(x1))}</span>` +
      `<span class="muted">peak <span class="num">${esc(usdSigned(y1))}</span></span>` +
      `<span class="muted">win rate <span class="num">${winRate}%</span> ` +
        `(${h.wins}W / ${h.losses}L)</span>` +
      `<span class="muted">fees <span class="num">${esc(usd(h.fees))}</span></span>` +
      (h.best ? `<span class="muted">best <span class="num pos">${esc(usdSigned(h.best.realized))}</span> ` +
                `${esc(h.best.symbol)}</span>` : '') +
      (h.worst && h.worst.realized < 0
        ? `<span class="muted">worst <span class="num neg">${esc(usdSigned(h.worst.realized))}</span> ` +
          `${esc(h.worst.symbol)}</span>` : '') +
      `<span class="dim">total <span class="num ${cls(h.realized)}">${esc(usdSigned(h.realized))}</span></span>` +
    `</div>`;
}

async function loadHistory() {
  try {
    const q = S.chartScope ? `?symbol=${encodeURIComponent(S.chartScope.replace('/', '_'))}` : '';
    S.history = await api('/api/history' + q);
    renderChart();
  } catch (err) {
    $('#chart').innerHTML = `<div class="small neg">${esc(err.message)}</div>`;
  }
}

/* --- main log ------------------------------------------------------------ */

async function loadLog() {
  try {
    const res = await api('/api/log?limit=140');
    setLogText($('#log'), res.lines.join('\n'), S.logFollow);
  } catch (err) {
    $('#log').textContent = err.message;
  }
}

/* --- top bar ------------------------------------------------------------- */

function renderHeader(snap) {
  const badge = $('#mode-badge');
  if (snap.dry_run) { badge.className = 'badge dry'; setText(badge, 'LIVE · DRY-RUN'); }
  else if (snap.mode === 'live') { badge.className = 'badge live'; setText(badge, 'LIVE'); }
  else { badge.className = 'badge paper'; setText(badge, 'PAPER'); }

  const hours = snap.uptime_sec / 3600;
  $('#conn').className = 'dot' + (S.failures ? ' stale' : '');
  setText($('#conn-text'), S.failures
    ? `stale (${S.failures} failed poll${S.failures > 1 ? 's' : ''})`
    : `up ${hours < 1 ? Math.round(snap.uptime_sec / 60) + 'm' : hours.toFixed(1) + 'h'}`);
}

/* --- render + poll ------------------------------------------------------- */

function render() {
  if (!S.snap || !S.meta) return;
  renderHeader(S.snap);
  renderTotals(S.snap);
  renderAlloc(S.snap);
  renderQuadrant(S.snap);
  renderValue();
  renderCoins(S.snap);
  renderInactive(S.snap);
}

async function refresh() {
  try {
    S.snap = await api('/api/snapshot');
    S.failures = 0;
    render();
  } catch (err) {
    S.failures += 1;
    $('#conn').className = 'dot ' + (S.failures > 3 ? 'dead' : 'stale');
    $('#conn-text').textContent = err.message;
  }
}

/* Downloads any missing coin logos into data/icons/ on the server. Explicitly
   user-triggered -- the bot never fetches them as a side effect of trading. */
async function fetchIcons() {
  const btn = $('#btn-icons');
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Fetching…';
  try {
    const res = await post('/api/icons/refresh', {});
    const lines = (res.fetched || []).map((f) => `${f.base} → ${f.name}`);
    if (res.unmatched?.length) {
      lines.push('', 'No ticker match: ' + res.unmatched.join(', '),
                 'Add the CoinGecko id to data/icons/overrides.json and retry.');
    }
    for (const f of res.failed || []) lines.push(`${f.base}: ${f.error}`);
    toast(res.ok ? 'Icons updated' : 'Icon fetch had problems',
          (res.message || '') + (lines.length ? '\n' + lines.join('\n') : ''),
          res.ok ? 'ok' : 'err', 16000);
    // Tickers are not unique, so say which coin each logo actually came from.
    if (res.fetched?.length) {
      toast('Check the matches', 'Logos are matched by ticker against the top coins ' +
            'by market cap. If one looks wrong, override it in ' +
            'data/icons/overrides.json.', 'info', 14000);
    }
    S.nodes.clear();          // rebuild rows so the new images are picked up
    refresh();
  } catch (err) {
    toast('Icon fetch failed', err.message, 'err', 10000);
  } finally {
    btn.disabled = false;
    btn.textContent = label;
  }
}

/* Form for a new pair. Values are pre-filled from the server's own defaults so
   the dashboard never invents numbers the bot doesn't agree with. */
function newCoinForm() {
  const wrap = el('div');
  const symField = el('div', 'field');
  symField.appendChild(el('label', null, 'Pair'));
  const sym = el('input');
  sym.type = 'text';
  sym.placeholder = 'SOL/USD';
  sym.autocapitalize = 'characters';
  sym.spellcheck = false;
  symField.append(sym, Object.assign(el('div', 'hint'),
    { textContent: 'BASE/QUOTE, exactly as the exchange lists it.' }));
  wrap.appendChild(symField);

  const inputs = new Map();
  for (const group of ['general', 'buy', 'sell']) {
    const fields = S.meta.fields.filter((f) => f.group === group && f.key !== 'blynk_pin');
    if (!fields.length) continue;
    wrap.appendChild(Object.assign(el('div', 'small dim'),
      { textContent: group.toUpperCase(), style: 'margin:12px 0 6px;letter-spacing:.08em' }));
    const grid = el('div', 'form-grid');
    for (const spec of fields) {
      const initial = (S.meta.new_coin_defaults || {})[spec.key] ?? '';
      const field = el('div', 'field');
      field.appendChild(el('label', null, spec.label));
      let input;
      if (spec.kind === 'choice' || spec.kind === 'bool') {
        input = el('select');
        for (const opt of (spec.kind === 'bool' ? ['True', 'False'] : spec.choices)) {
          const o = el('option', null, opt);
          o.value = opt;
          input.appendChild(o);
        }
        input.value = String(initial).replace(/"/g, '') || (spec.kind === 'bool' ? 'False' : spec.choices[0]);
      } else {
        input = el('input');
        input.type = 'text';
        input.inputMode = 'decimal';
        input.value = String(initial).replace(/"/g, '');
      }
      field.appendChild(input);
      const hint = el('div', 'hint' + (spec.as_pct ? ' pcthint' : ''));
      hint.textContent = spec.as_pct ? pctHint(input.value, spec) : spec.hint;
      if (spec.as_pct) input.oninput = () => { hint.textContent = pctHint(input.value, spec); };
      field.appendChild(hint);
      inputs.set(spec.key, { input, spec });
      grid.appendChild(field);
    }
    wrap.appendChild(grid);
  }
  return { wrap, sym, inputs };
}

function addCoinFlow() {
  const { wrap, sym, inputs } = newCoinForm();
  modal({
    title: 'Add a coin',
    confirmLabel: 'Write to config.py',
    warn: 'Defaults to <b>disabled</b> so you can check the numbers first. Set Enabled ' +
          'to True and it starts trading <b>immediately</b> — no restart.',
    node: wrap,
    onConfirm: async () => {
      const fields = {};
      for (const [key, { input, spec }] of inputs) {
        fields[key] = spec.kind === 'bool' ? input.value === 'True' : input.value.trim();
      }
      try {
        const res = await post('/api/coins', { symbol: sym.value, fields });
        toast(res.started ? `${res.symbol} added and trading` : `${res.symbol} added to config.py`,
              res.notes.join('\n'), 'ok', 15000);
        S.nodes.clear();
        refresh();
      } catch (err) {
        toast('Coin not added', err.message, 'err', 14000);
      }
    },
  });
  sym.focus();
}

function removeCoinFlow(coin) {
  const send = async (force) => {
    try {
      const res = await post(`/api/coins/${coin.key}/remove`, { force });
      toast(`${res.symbol} removed from config.py`, res.notes.join('\n'), 'ok', 16000);
      S.open = S.open === coin.symbol ? null : S.open;
      S.openInactive = S.openInactive === coin.symbol ? null : S.openInactive;
      S.nodes.clear();
      refresh();
    } catch (err) {
      // The server refuses by default on anything that would lose track of real
      // money, and says why. Force needs the ticker typed out.
      const blockers = err.blockers || [];
      if (!err.canForce) { toast('Coin not removed', err.message, 'err', 15000); return; }
      modal({
        title: `Remove ${coin.symbol} anyway?`,
        danger: true, confirmLabel: 'Remove from config.py',
        typeToConfirm: coin.base,
        warn: `<b>${esc(coin.symbol)} still:</b><br>` +
              blockers.map((b) => '• ' + esc(b)).join('<br>') +
              `<br><br><b>Retire the coin first</b> if you want its PnL kept and its ` +
              `position cleared properly.`,
        html: `<p class="small muted">The state file is left on disk either way.</p>`,
        onConfirm: () => send(true),
      });
    }
  };

  modal({
    title: `Remove ${coin.symbol} from config.py`,
    danger: true, confirmLabel: 'Remove',
    warn: 'Deletes the pair\'s entry from <code>COINS</code>. It disappears from the ' +
          'dashboard entirely — including its realized PnL, unless that was booked to the ' +
          'retired ledger.',
    html: `<p class="small muted">The bot keeps trading it until the next restart. Its ` +
          `state file and any retired-ledger row are left alone.</p>`,
    onConfirm: () => send(false),
  });
}

async function configReloadFlow() {
  let preview;
  try {
    preview = await api('/api/config/preview');
  } catch (err) {
    toast('Reload preview failed', err.message, 'err', 9000);
    return;
  }
  const body = preview.changes.length
    ? preview.changes.map((c) => `<b>${esc(c.symbol)}</b><br>` +
        c.fields.map((f) => `&nbsp;&nbsp;<code>${esc(f.key)}</code>: ${esc(f.old)} → ${esc(f.new)}`).join('<br>'))
        .join('<br><br>')
    : '<span class="muted">No tunable changes to apply.</span>';
  modal({
    title: 'Reload helpers/config.py',
    confirmLabel: preview.changes.length ? 'Apply to running coins' : 'Close',
    warn: preview.warnings.length ? preview.warnings.map(esc).join('<br>') : null,
    html: `<p class="small">${body}</p><p class="small muted">Module constants (fees, cooldowns, ` +
          `Blynk/Pushover, paper wallet) are bound at startup and still need a restart.</p>`,
    onConfirm: async () => {
      if (!preview.changes.length) return;
      try {
        const res = await post('/api/config/apply', { id: preview.id });
        toast('Config reloaded', (res.applied || []).join('\n') || res.message, 'ok', 11000);
        refresh();
      } catch (err) {
        toast('Reload failed', err.message, 'err', 9000);
      }
    },
  });
}

async function boot() {
  // Keep the token out of the address bar (and out of screenshots) once the
  // cookie the server set is doing the work.
  if (location.search.includes('token=')) {
    history.replaceState(null, '', location.pathname);
  }

  $('#btn-reload-config').onclick = configReloadFlow;
  $('#btn-icons').onclick = fetchIcons;
  $('#btn-add-coin').onclick = addCoinFlow;
  $('#btn-pause-poll').onclick = (e) => {
    S.polling = !S.polling;
    e.target.textContent = S.polling ? 'Pause refresh' : 'Resume refresh';
  };
  $('#btn-log-refresh').onclick = loadLog;
  $('#btn-log-follow').onclick = (e) => {
    S.logFollow = !S.logFollow;
    e.target.textContent = S.logFollow ? 'Following' : 'Not following';
  };
  $('#btn-chart-all').onclick = () => { S.chartScope = null; loadHistory(); };
  rememberDisclosure($('#inactive-disclosure'), 'inactiveOpen');
  $('#btn-reset-cols').onclick = () => {
    saveColumnOrder(COLUMN_KEYS);
    buildHeader();
    render();
  };
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeModal(); });
  buildHeader();

  try {
    S.meta = await api('/api/meta');
  } catch (err) {
    document.body.innerHTML = `<main><p class="neg">${esc(err.message)}</p></main>`;
    return;
  }
  S.valueDays = Number(storeGet('valueDays', 7));
  buildValueRanges();
  window.addEventListener('resize', () => { S.drawn = {}; render(); });

  await refresh();
  await loadHistory();
  await loadEquity();
  await loadLog();

  setInterval(() => { if (S.polling) refresh(); }, POLL_MS);
  setInterval(() => { if (S.polling) loadHistory(); }, HISTORY_MS);
  setInterval(() => { if (S.polling) loadEquity(); }, 60000);
  setInterval(() => { if (S.polling && S.logFollow) loadLog(); }, 6000);
}

boot();
