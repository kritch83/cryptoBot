"""Per-coin trading task.

Each enabled coin gets one `run_coin` task in the asyncio event loop. The
task subscribes to its own ticker feed, runs the trail state machines on
every tick, executes buys/sells against either the paper wallet or live
exchange, persists state, and drains a per-coin manual-command queue.

Trail handlers and order execution live here (rather than conductor.py) so
they can take a `coin_cfg` dict instead of importing single-coin globals.
"""
from __future__ import annotations

import asyncio
import csv
import logging
import time
from datetime import datetime, timezone
from typing import AsyncIterator, Optional

import ccxt

from .blynk import BlynkClient
from .config import ACTION_COOLDOWN_SEC, COINS, LIVE_BUY_FILL_TIMEOUT_SEC, MAKER_FEE_PCT, TAKER_FEE_PCT
from .retired import add_retired
from .state import PendingAction, PendingBuyOrder, PendingSellOrder, Position, State, TrailState, save_state
from .wallet import PaperWallet


class CoinAdapter(logging.LoggerAdapter):
    """Prefix every log line with [SYMBOL]."""
    def process(self, msg, kwargs):
        return f"[{self.extra['symbol']}] {msg}", kwargs


# --- Trail state machines (coin-aware) --------------------------------------

def handle_initial_entry_trail(state: State, price: float, cfg: dict, log: logging.LoggerAdapter) -> Optional[PendingAction]:
    """Trail for the very first entry of a fresh cycle.

    Tracks a running high from when the bot starts watching. Arms a trail when
    price drops drop_pct below that high, then fires on a trail_buy_pct rebound
    off the local low -- same shape as the regular grid trail. Prevents buying
    blindly at the top on the first tick.
    """
    if state.positions:
        return None

    drop_pct       = cfg["drop_pct"]
    trail_buy_pct  = cfg["trail_buy_pct"]
    pp             = cfg["price_prec"]

    if state.initial_entry_high is None or price > state.initial_entry_high:
        state.initial_entry_high = price

    if not state.trailing_buy.armed:
        if price <= state.initial_entry_high * (1 - drop_pct):
            if trail_buy_pct == 0:
                return PendingAction(
                    "buy", 0,
                    f"initial entry: -{drop_pct:.1%} from observed high ${state.initial_entry_high:.{pp}f}",
                )
            state.trailing_buy = TrailState(armed=True, extreme=price, armed_at_price=price)
            d = (state.initial_entry_high - price) / state.initial_entry_high * 100
            log.info(
                f"[{state.mode.upper()}] INITIAL-ENTRY TRAIL ARMED price=${price:.{pp}f} "
                f"(high ${state.initial_entry_high:.{pp}f}, -{d:.1f}%)"
            )
        return None

    if price < state.trailing_buy.extreme:
        state.trailing_buy.extreme = price
        log.info(f"[{state.mode.upper()}] INITIAL-ENTRY TRAIL low=${price:.{pp}f}")
        return None

    fire_threshold = state.trailing_buy.extreme * (1 + trail_buy_pct)
    if price >= fire_threshold:
        action = PendingAction(
            "buy", 0,
            f"initial entry: trail-back +{trail_buy_pct:.1%} off low ${state.trailing_buy.extreme:.{pp}f}",
        )
        state.trailing_buy = TrailState()
        return action

    if state.trailing_buy.armed_at_price is not None and price >= state.trailing_buy.armed_at_price:
        log.info(
            f"[{state.mode.upper()}] INITIAL-ENTRY TRAIL DISARMED "
            f"price=${price:.{pp}f} back above arm ${state.trailing_buy.armed_at_price:.{pp}f}"
        )
        state.trailing_buy = TrailState()

    return None


def handle_buy_trail(state: State, price: float, cfg: dict, log: logging.LoggerAdapter) -> Optional[PendingAction]:
    drop_pct        = cfg["drop_pct"]
    trail_buy_pct   = cfg["trail_buy_pct"]
    max_grid_levels = cfg["max_grid_levels"]
    pp              = cfg["price_prec"]

    if state.last_buy_price is None or len(state.positions) >= max_grid_levels:
        if state.trailing_buy.armed:
            state.trailing_buy = TrailState()
        return None

    trigger_price = state.last_buy_price * (1 - drop_pct)
    next_level = len(state.positions)

    if not state.trailing_buy.armed:
        if price <= trigger_price:
            if trail_buy_pct == 0:
                return PendingAction(
                    "buy", next_level,
                    f"-{drop_pct:.1%} from last buy ${state.last_buy_price:.{pp}f}",
                )
            state.trailing_buy = TrailState(armed=True, extreme=price, armed_at_price=price)
            log.info(
                f"[{state.mode.upper()}] BUY-TRAIL ARMED level={next_level} "
                f"price=${price:.{pp}f} trigger=${trigger_price:.{pp}f}"
            )
        return None

    # Already armed -- track the low and watch for the rebound.
    if price < state.trailing_buy.extreme:
        state.trailing_buy.extreme = price
        log.info(f"[{state.mode.upper()}] BUY-TRAIL low=${price:.{pp}f}")
        return None

    fire_threshold = state.trailing_buy.extreme * (1 + trail_buy_pct)
    if price >= fire_threshold:
        action = PendingAction(
            "buy", next_level,
            f"trail-back +{trail_buy_pct:.1%} off low ${state.trailing_buy.extreme:.{pp}f}",
        )
        state.trailing_buy = TrailState()
        return action

    if state.trailing_buy.armed_at_price is not None and price >= state.trailing_buy.armed_at_price:
        log.info(
            f"[{state.mode.upper()}] BUY-TRAIL DISARMED price=${price:.{pp}f} "
            f"back above arm ${state.trailing_buy.armed_at_price:.{pp}f}"
        )
        state.trailing_buy = TrailState()

    return None


def handle_manual_buy_trail(state: State, price: float, cfg: dict, log: logging.LoggerAdapter) -> Optional[PendingAction]:
    """Manually-armed buy-trail (menu 8) -- the mirror of the manual sell-trail.

    Arms immediately at the current price (bypassing the grid's drop_pct
    trigger), trails the price DOWN tracking the low, and fires ONE buy when
    price rebounds trail_buy_pct off that low. Works whether flat (fires the
    initial entry, level 0) or holding (fires the next grid level). Unlike the
    automatic buy-trail it never disarms on its own -- it ends only by firing,
    a full sell/reset, or clear-targets (menu t). Refuses to fire if the grid is
    full. While armed it owns the trailing_buy slot, so the tick loop routes here
    instead of the automatic entry/grid trails.

    The fired buy carries no force_market, so it honors cfg['buy_order_type']
    exactly like the automatic buy-trail (and mirrors how the manual sell-trail
    fires a plain sell_all).
    """
    tb = state.trailing_buy
    if not tb.armed or not tb.manual:
        return None
    trail_buy_pct   = cfg["trail_buy_pct"]
    max_grid_levels = cfg["max_grid_levels"]
    pp              = cfg["price_prec"]

    # New low -> keep trailing down.
    if tb.extreme is None or price < tb.extreme:
        state.trailing_buy.extreme = price
        log.info(f"[{state.mode.upper()}] MANUAL BUY-TRAIL low=${price:.{pp}f}")
        return None

    # Rebound off the low -> fire a single buy at the next level.
    fire_threshold = tb.extreme * (1 + trail_buy_pct)
    if price >= fire_threshold:
        if len(state.positions) >= max_grid_levels:
            log.info(
                f"[{state.mode.upper()}] MANUAL BUY-TRAIL rebound hit ${price:.{pp}f} but grid "
                f"full ({max_grid_levels} levels) -- disarming without buying"
            )
            state.trailing_buy = TrailState()
            return None
        level = len(state.positions)
        action = PendingAction(
            "buy", level,
            f"manual buy-trail: +{trail_buy_pct:.1%} off low ${tb.extreme:.{pp}f}",
        )
        state.trailing_buy = TrailState()
        return action

    return None


def sell_threshold(state: State, cfg: dict) -> tuple[float, str, str]:
    """Compute the sell-trail arm threshold and a human label for it.

    Returns (threshold, source_label, tag). `tag` is the trail-log prefix
    ("SELL-TRAIL" or "BREAKEVEN-EXIT SELL-TRAIL").

    When `state.breakeven_exit_armed`, the threshold is the avg entry
    grossed up by the sell-side fee rate so net proceeds >= total cost:
    p = avg / (1 - fee_rate). Otherwise it's avg * (1 + take_profit_pct).
    """
    avg = state.avg_entry_price
    if state.breakeven_exit_armed:
        is_limit = cfg.get("order_type", "market") == "limit"
        sell_fee_rate = MAKER_FEE_PCT if is_limit else TAKER_FEE_PCT
        fee_label = "maker fee" if is_limit else "taker fee"
        threshold = avg / (1 - sell_fee_rate)
        return threshold, f"breakeven (avg + {fee_label} {sell_fee_rate:.2%})", "BREAKEVEN-EXIT SELL-TRAIL"
    take_profit_pct = cfg["take_profit_pct"]
    return avg * (1 + take_profit_pct), f"+{take_profit_pct:.1%} from avg", "SELL-TRAIL"


def handle_sell_trail(state: State, price: float, cfg: dict, log: logging.LoggerAdapter) -> Optional[PendingAction]:
    trail_sell_pct  = cfg["trail_sell_pct"]
    pp              = cfg["price_prec"]

    if not state.positions or state.avg_entry_price is None:
        return None

    tp_threshold, source_label, tag = sell_threshold(state, cfg)

    if not state.trailing_sell.armed:
        if price >= tp_threshold:
            if trail_sell_pct == 0:
                return PendingAction(
                    "sell_all",
                    reason=f"{source_label} hit ${tp_threshold:.{pp}f} (avg ${state.avg_entry_price:.{pp}f})",
                )
            state.trailing_sell = TrailState(armed=True, extreme=price, armed_at_price=price)
            log.info(
                f"[{state.mode.upper()}] {tag} ARMED price=${price:.{pp}f} "
                f"threshold=${tp_threshold:.{pp}f} avg=${state.avg_entry_price:.{pp}f}"
            )
        return None

    if price > state.trailing_sell.extreme:
        state.trailing_sell.extreme = price
        log.info(f"[{state.mode.upper()}] {tag} high=${price:.{pp}f}")
        return None

    # A manually-armed trail (menu a) is a trailing stop clamped to avg entry:
    # it bypasses the take-profit floor (so it can arm/trail below avg*(1+tp_pct)),
    # but it never sells below avg entry -- a pull-back that would realize a loss
    # is held (keep trailing for a profitable exit) instead of firing or disarming.
    # An auto trail uses the take-profit threshold as its floor and disarms (to
    # re-arm cleanly later) whenever price slips back below it.
    manual = state.trailing_sell.manual
    floor = state.avg_entry_price if manual else tp_threshold

    fire_threshold = state.trailing_sell.extreme * (1 - trail_sell_pct)
    if price <= fire_threshold:
        if price >= floor:
            action = PendingAction(
                "sell_all",
                reason=(
                    f"trail-back -{trail_sell_pct:.1%} off high "
                    f"${state.trailing_sell.extreme:.{pp}f}" + (" (manual stop)" if manual else "")
                ),
            )
            state.trailing_sell = TrailState()
            return action
        if manual:
            # Pull-back would lock in a loss -- stay armed and keep trailing for a
            # profitable exit rather than selling below avg entry.
            return None
        log.info(
            f"[{state.mode.upper()}] {tag} DISARMED at ${price:.{pp}f} "
            f"(below ${floor:.{pp}f} -- {source_label})"
        )
        state.trailing_sell = TrailState()
        return None

    if not manual and price < tp_threshold:
        log.info(
            f"[{state.mode.upper()}] {tag} DISARMED price=${price:.{pp}f} "
            f"below ${tp_threshold:.{pp}f} ({source_label})"
        )
        state.trailing_sell = TrailState()

    return None


def handle_stop_loss(state: State, price: float, cfg: dict, log: logging.LoggerAdapter) -> Optional[PendingAction]:
    """Hard stop-loss: fires when price <= avg_entry * (1 - stop_loss_pct).

    Deliberately sells AT A LOSS -- unlike the manual sell-trail there is no
    avg-entry floor. The trigger is recomputed from avg_entry_price each tick,
    so it tracks the average down as the grid adds levels. Emits a force-market
    sell_all tagged from_stop_loss=True; the tick loop clears stop_loss_pct only
    after execute_sell_all returns, so a failed order re-fires/retries on the
    next tick. Arms pause_after_sell so _finalize_sell pauses the coin once the
    close books. Called outside the cooldown gate but never while paused.
    """
    pct = state.stop_loss_pct
    if not pct or not state.positions or state.avg_entry_price is None:
        return None
    trigger = state.avg_entry_price * (1 - pct)
    if price > trigger:
        return None
    pp = cfg["price_prec"]
    state.pause_after_sell = True   # consumed by _finalize_sell on the full close
    log.warning(
        f"[{state.mode.upper()}] STOP-LOSS TRIGGERED price=${price:.{pp}f} <= trigger=${trigger:.{pp}f} "
        f"(-{pct:.2%} below avg ${state.avg_entry_price:.{pp}f}) -- market-selling ALL, then pausing"
    )
    return PendingAction(
        "sell_all",
        reason=f"STOP-LOSS -{pct:.2%} below avg ${state.avg_entry_price:.{pp}f} hit at ${price:.{pp}f}",
        force_market=True,
        from_stop_loss=True,
    )


# --- Fill helpers -----------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def paper_buy(wallet: PaperWallet, usd: float, state: State, ticker: dict) -> dict:
    fill_price = ticker.get("ask") or ticker.get("last")
    if fill_price is None:
        raise RuntimeError("paper_buy: no ask/last on ticker")
    fee_usd = usd * TAKER_FEE_PCT
    qty_coin = (usd - fee_usd) / fill_price
    await wallet.spend(usd)
    state.paper_wallet_coin += qty_coin
    return {"qty": qty_coin, "price": fill_price, "fee_usd": fee_usd, "ts": _now_iso()}


async def paper_sell_all(wallet: PaperWallet, qty_coin: float, state: State, ticker: dict) -> dict:
    fill_price = ticker.get("bid") or ticker.get("last")
    if fill_price is None:
        raise RuntimeError("paper_sell_all: no bid/last on ticker")
    gross = qty_coin * fill_price
    fee_usd = gross * TAKER_FEE_PCT
    net = gross - fee_usd
    await wallet.credit(net)
    state.paper_wallet_coin -= qty_coin
    return {"qty": qty_coin, "price": fill_price, "fee_usd": fee_usd, "ts": _now_iso(), "proceeds_net": net}


_TERMINAL_ORDER_STATUSES = frozenset({"closed", "canceled", "cancelled", "rejected", "expired"})


def install_private_call_serialization(exchange) -> None:
    """Serialize every private REST call behind one async lock, and hand out
    strictly-increasing nonces.

    All coin tasks share ONE exchange object and ONE Kraken API key. Kraken
    requires the nonce on each private request to be strictly greater than the
    last one it saw for that key; when concurrent coroutines fire private calls
    (create/cancel/fetch_order/fetch_balance) they race and some arrive
    out-of-order -> `EAPI:Invalid nonce`. That was the root cause of the
    tracked-vs-wallet drift: a nonce-failed cancel left a live order orphaned.

    ccxt routes every REST request through `fetch2(path, api, ...)`; holding a
    single lock around each `api == "private"` call (nonce generation happens
    inside, in `sign()`) guarantees Kraken sees them one at a time, in order.
    Public calls (tickers via WebSocket, load_markets) are untouched.
    """
    lock = asyncio.Lock()
    last = {"n": 0}

    def _nonce():
        n = exchange.milliseconds()
        if n <= last["n"]:
            n = last["n"] + 1
        last["n"] = n
        return n
    exchange.nonce = _nonce

    orig_fetch2 = exchange.fetch2

    async def _serialized_fetch2(path, api="public", *args, **kwargs):
        is_private = (api == "private") or (isinstance(api, (list, tuple)) and "private" in api)
        if is_private:
            async with lock:
                return await orig_fetch2(path, api, *args, **kwargs)
        return await orig_fetch2(path, api, *args, **kwargs)
    exchange.fetch2 = _serialized_fetch2


async def reconcile_all_to_exchange(exchange, states: dict, cfgs: list) -> None:
    """Startup reconciliation: mirror each coin's tracked position to the real
    Kraken balance, BOTH directions (operator policy: match the exchange).

    * real balance below the market's minimum order size -> CLEAR the position
      (untradeable dust / ghost); realized PnL and cycle count are kept.
    * real materially (>0.5%) != tracked -> scale every lot and the totals so
      total_qty_coin == real, PRESERVING avg_entry (cost scaled proportionally)
      and the grid level COUNT (each lot scaled, not added/removed).
    * a coin with no open position is left flat -- there's no avg to attribute a
      surplus to, so it just trades a fresh cycle.

    Runs once at startup (live mode only), before the coin tasks begin, and
    persists each change. This is the safety net that keeps tracked == wallet:
    even if something desyncs while running, the next launch re-mirrors it.
    """
    try:
        bal = await exchange.fetch_balance()
    except Exception as e:
        logging.warning("Startup reconciliation skipped -- balance fetch failed: %s", e)
        return
    total = bal.get("total") or {}
    for c in cfgs:
        sym = c["symbol"]
        st = states.get(sym)
        if st is None or not st.positions or st.total_qty_coin <= 0:
            continue
        base = sym.split("/")[0]
        real = float(total.get(base) or 0.0)
        tracked = st.total_qty_coin
        clog = CoinAdapter(logging.getLogger(), {"symbol": sym})
        try:
            mn = exchange.markets[sym]["limits"]["amount"]["min"] or 0.0
        except Exception:
            mn = 0.0

        if real < max(mn, 1e-12):
            avg_s = f"${st.avg_entry_price:.6f}" if st.avg_entry_price else "-"
            clog.warning(
                "RECONCILE: Kraken balance %.8f below min order %.8f -- tracked %.8f is untradeable "
                "dust/ghost; CLEARING position (was cost $%.2f, avg %s). Realized PnL/cycle kept.",
                real, mn, tracked, st.total_cost_usd, avg_s,
            )
            st.positions = []
            st.total_qty_coin = 0.0
            st.total_cost_usd = 0.0
            st.avg_entry_price = None
            st.last_buy_price = None
            st.initial_entry_high = None
            st.trailing_buy = TrailState()
            st.trailing_sell = TrailState()
            st.pending_buy_order = None
            st.pending_sell_order = None
            save_state(sym, st)
            continue

        factor = real / tracked
        if abs(factor - 1.0) <= 0.005:
            continue
        for p in st.positions:
            p.qty_coin *= factor
            p.fee_usd  *= factor
        st.total_qty_coin = real
        st.total_cost_usd *= factor
        st.avg_entry_price = (st.total_cost_usd / real) if real > 0 else None
        avg_s = f"${st.avg_entry_price:.6f}" if st.avg_entry_price else "-"
        clog.warning(
            "RECONCILE: tracked %.8f -> %.8f to match Kraken (%s %.1f%%); avg entry preserved %s, "
            "cost -> $%.2f. Bot now manages the full balance.",
            tracked, real, "UP" if factor > 1 else "DOWN", (factor - 1) * 100, avg_s, st.total_cost_usd,
        )
        save_state(sym, st)


async def live_buy(exchange, symbol: str, usd: float, ticker: dict) -> dict:
    """Place a market buy sized in USD; wait for the fill to settle.

    Kraken's REST AddOrder returns synchronously with the txid but no fill
    details -- `filled`/`average`/`status` are None until QueryOrders catches
    up. We poll fetch_order(id) until the order reaches a terminal status
    (closed/canceled/etc) or LIVE_BUY_FILL_TIMEOUT_SEC elapses. On timeout we
    cancel and return qty=0 so we don't leave a silent fill on the exchange.
    """
    if exchange.has.get("createMarketBuyOrderWithCostWs"):
        order = await exchange.create_market_buy_order_with_cost_ws(symbol, usd)
    else:
        order = await exchange.create_market_buy_order_with_cost(symbol, usd)

    order_id = order.get("id")
    status = (order.get("status") or "").lower()

    if not order_id:
        logging.error(
            "live_buy %s placed order has no id -- cannot poll for fill. raw: %r",
            symbol, order,
        )
    elif status not in _TERMINAL_ORDER_STATUSES:
        # Poll fetch_order until terminal or timeout
        deadline = time.monotonic() + LIVE_BUY_FILL_TIMEOUT_SEC
        backoff = 0.25
        while time.monotonic() < deadline:
            await asyncio.sleep(backoff)
            try:
                order = await exchange.fetch_order(order_id, symbol)
            except ccxt.OrderNotFound:
                logging.warning(
                    "live_buy %s order %s vanished from exchange mid-poll",
                    symbol, order_id,
                )
                break
            except ccxt.NetworkError as e:
                logging.warning(
                    "live_buy %s fetch_order network error: %s -- retrying",
                    symbol, e,
                )
                backoff = min(backoff * 2, 2.0)
                continue
            status = (order.get("status") or "").lower()
            if status in _TERMINAL_ORDER_STATUSES:
                break
            backoff = min(backoff * 1.5, 1.0)
        else:
            # Timeout: cancel the still-open order so it can't surprise-fill
            logging.error(
                "live_buy %s timed out after %.1fs waiting for fill -- cancelling order %s",
                symbol, LIVE_BUY_FILL_TIMEOUT_SEC, order_id,
            )
            await live_cancel_order(exchange, symbol, order_id)
            return {"qty": 0.0, "price": float(ticker.get("ask") or ticker.get("last") or 0.0),
                    "fee_usd": 0.0, "ts": _now_iso()}

    qty = float(order.get("filled") or order.get("amount") or 0.0)
    if qty <= 0:
        logging.warning(
            "live_buy %s returned zero qty (ws=%s, status=%s) -- raw order: %r",
            symbol, exchange.has.get("createMarketBuyOrderWithCostWs"), status, order,
        )
    price = float(order.get("average") or order.get("price") or ticker.get("ask") or ticker.get("last"))
    fee_usd = float((order.get("fee") or {}).get("cost") or 0.0)
    return {"qty": qty, "price": price, "fee_usd": fee_usd, "ts": _now_iso()}


async def live_sell_all(exchange, symbol: str, qty_coin: float, ticker: dict) -> dict:
    qty_str = exchange.amount_to_precision(symbol, qty_coin)
    if exchange.has.get("createMarketSellOrderWs"):
        order = await exchange.create_market_sell_order_ws(symbol, float(qty_str))
    else:
        order = await exchange.create_market_sell_order(symbol, float(qty_str))
    price = float(order.get("average") or order.get("price") or ticker.get("bid") or ticker.get("last"))
    fee_usd = float((order.get("fee") or {}).get("cost") or 0.0)
    proceeds_net = float(order.get("cost") or qty_coin * price) - fee_usd
    return {"qty": qty_coin, "price": price, "fee_usd": fee_usd, "ts": _now_iso(), "proceeds_net": proceeds_net}


async def live_limit_sell(exchange, symbol: str, qty_coin: float, limit_price: float) -> dict:
    """Place a post-only limit sell. Returns the resting order id + sanitized qty/price."""
    qty_str = exchange.amount_to_precision(symbol, qty_coin)
    price_str = exchange.price_to_precision(symbol, limit_price)
    order = await exchange.create_limit_sell_order(
        symbol, float(qty_str), float(price_str),
        params={"postOnly": True},
    )
    return {
        "order_id":    order["id"],
        "limit_price": float(price_str),
        "qty":         float(qty_str),
    }


async def live_limit_buy(exchange, symbol: str, qty_coin: float, limit_price: float) -> dict:
    """Place a post-only limit buy. Returns the resting order id + sanitized qty/price."""
    qty_str = exchange.amount_to_precision(symbol, qty_coin)
    price_str = exchange.price_to_precision(symbol, limit_price)
    order = await exchange.create_limit_buy_order(
        symbol, float(qty_str), float(price_str),
        params={"postOnly": True},
    )
    return {
        "order_id":    order["id"],
        "limit_price": float(price_str),
        "qty":         float(qty_str),
    }


async def live_cancel_order(exchange, symbol: str, order_id: str) -> None:
    """Best-effort cancel -- swallow OrderNotFound (already filled or gone)."""
    try:
        await exchange.cancel_order(order_id, symbol)
    except ccxt.OrderNotFound:
        pass


async def live_sellable_qty(exchange, symbol: str, tracked_qty: float,
                            log: logging.LoggerAdapter) -> float:
    """Return the qty we can actually sell right now: min(tracked, free balance),
    truncated DOWN to lot precision.

    Kraken deducts trading fees from the received base coin, so our tracked
    position (sum of gross buy fills) can exceed the actual free balance by a
    hair. Requesting the full tracked qty then fails with EOrder:Insufficient
    funds on both the limit-sell and the market fallback, stranding the position.
    Clamping to the real free balance (rounded down) lets it exit cleanly; the
    leftover dust is just the fee Kraken already took.
    """
    base = symbol.split("/")[0]
    try:
        bal = await exchange.fetch_balance()
        free = (bal.get(base) or {}).get("free")
    except Exception as e:
        log.warning("Balance check failed (%s) -- selling tracked qty %.8f", e, tracked_qty)
        return tracked_qty
    if free is None:
        return tracked_qty
    target = min(tracked_qty, float(free))
    # Truncate DOWN to the market's amount precision so we never over-request.
    try:
        safe = float(exchange.decimal_to_precision(
            target, ccxt.TRUNCATE,
            exchange.markets[symbol]["precision"]["amount"],
            exchange.precisionMode, exchange.paddingMode,
        ))
    except Exception:
        safe = target
    if safe < tracked_qty:
        log.info(
            "Sell qty clamped to free balance: tracked=%.8f free=%.8f -> selling %.8f %s",
            tracked_qty, float(free), safe, base,
        )
    # Never return a qty below the exchange minimum: Kraken rejects it
    # (EGeneral:volume minimum not met) and the sell would just spin forever
    # (the TAO dust loop). Signal 0 so the caller skips; startup reconciliation
    # clears genuine untradeable dust.
    try:
        mn = exchange.markets[symbol]["limits"]["amount"]["min"] or 0.0
    except Exception:
        mn = 0.0
    if 0 < safe < mn:
        log.warning(
            "Free %s balance %.8f is below the exchange minimum order %.8f -- can't sell this "
            "dust; skipping. (Reconcile/clear-stats to stop retrying.)", base, safe, mn,
        )
        return 0.0
    return safe


# --- Execute (paper-vs-live swap site) --------------------------------------

def _finalize_buy(state: State, fill: dict, level: int, cfg: dict, reason: str,
                  tag: str, log: logging.LoggerAdapter) -> None:
    """Post-fill state cleanup shared by market buys and limit-buy fills.

    `fill` must carry: qty, price, fee_usd, ts.
    `tag` is a short log label ("BUY" for market, "LIMIT-BUY FILL" for limit).
    """
    pp = cfg["price_prec"]

    if fill["qty"] <= 0:
        # Exchange reported a zero-qty fill -- skip recording the position to
        # avoid corrupting totals (a 0/0 in avg_entry_price below) and getting
        # stuck on a phantom position next run. Clear pending/trail so the
        # coin can re-arm on the next tick.
        log.warning(
            f"[{state.mode.upper()}] {tag} skipped -- exchange reported zero qty "
            f"filled (price=${fill['price']:.{pp}f}, reason={reason})"
        )
        state.pending_buy_order = None
        state.trailing_buy = TrailState()
        return

    cost_usd = fill["qty"] * fill["price"] + fill["fee_usd"]
    state.positions.append(Position(
        level=level, qty_coin=fill["qty"], entry_price=fill["price"],
        fee_usd=fill["fee_usd"], ts=fill["ts"],
    ))
    state.total_qty_coin += fill["qty"]
    state.total_cost_usd += cost_usd
    state.avg_entry_price = state.total_cost_usd / state.total_qty_coin
    state.last_buy_price = fill["price"]
    state.last_action_ts = time.time()
    state.pending_buy_order = None
    state.trailing_buy = TrailState()
    log.info(
        f"[{state.mode.upper()}] {tag} level={level} "
        f"price=${fill['price']:.{pp}f} qty={fill['qty']:.8f} "
        f"fee=${fill['fee_usd']:.{pp}f} cost=${cost_usd:.{pp}f} "
        f"avg_entry=${state.avg_entry_price:.{pp}f} ({reason})"
    )


async def cancel_pending_buy_and_book(exchange, state: State, cfg: dict,
                                      log: logging.LoggerAdapter, context: str) -> bool:
    """Cancel a resting limit buy; book any portion that filled before the
    cancel landed so tracked qty stays honest (same race as check_pending_buy's
    cancel path). Returns True if a raced fill was booked -- the caller decides
    what that means (a force-buy treats it as satisfied and skips its market
    buy; a stop-loss goes on to sell the now-larger position).
    """
    pending = state.pending_buy_order
    if pending is None:
        return False
    symbol = cfg["symbol"]
    if state.mode == "live" and pending.order_id:
        try:
            await live_cancel_order(exchange, symbol, pending.order_id)
            log.info(f"[LIVE] cancelled pending LIMIT-BUY id={pending.order_id} ({context})")
        except Exception as e:
            # Cancel FAILED -> order still live. Keep tracking it (don't orphan)
            # and tell the caller so it doesn't stack an order on top.
            log.warning("Could not cancel pending LIMIT-BUY %s: %s -- still live, keeping tracked (%s aborts)",
                        pending.order_id, e, context)
            return False
        state.pending_buy_order = None
        # The limit buy may have filled before the cancel landed. Book any
        # filled qty so the position totals include it.
        try:
            final = await exchange.fetch_order(pending.order_id, symbol)
            filled_qty = float(final.get("filled") or 0.0)
        except Exception as e:
            log.warning("post-cancel fetch_order failed for %s: %s -- assuming unfilled",
                        pending.order_id, e)
            filled_qty = 0.0
        if filled_qty > 1e-12:
            fill_price = float(final.get("average") or final.get("price") or pending.limit_price)
            fee_usd    = float((final.get("fee") or {}).get("cost") or 0.0)
            fill = {"qty": filled_qty, "price": fill_price, "fee_usd": fee_usd, "ts": _now_iso()}
            log.info("%s: pending LIMIT-BUY %s had %.8f filled before cancel -- booking it",
                     context, pending.order_id, filled_qty)
            _finalize_buy(state, fill, pending.level, cfg,
                          f"limit-buy filled (raced {context})", "LIMIT-BUY FILL", log)
            return True
    else:
        log.info(f"[PAPER] dropped pending LIMIT-BUY ({context})")
        state.pending_buy_order = None
    return False


async def execute_buy(exchange, state: State, wallet: PaperWallet, cfg: dict,
                      ticker: dict, level: int, reason: str,
                      log: logging.LoggerAdapter,
                      force_market: bool = False) -> None:
    """Execute a buy. Branches on cfg['buy_order_type'] unless force_market=True.

    force_market=True is used by menu 7 (force BUY now) so "buy now" always means
    market, regardless of the coin's configured buy_order_type.

    For the limit path, this places a post-only limit buy, records it in
    state.pending_buy_order, and returns -- the position is added when/if the
    order fills (handled by check_pending_buy).
    """
    pp         = cfg["price_prec"]
    symbol     = cfg["symbol"]
    usd        = cfg["usd_per_buy"]
    order_type = "market" if force_market else cfg.get("buy_order_type", "market")

    # Force-market path may need to cancel an outstanding limit buy first.
    # A raced pre-cancel fill satisfies the force-buy -- a market buy on top
    # would double the lot (same race as check_pending_buy).
    if force_market and state.pending_buy_order is not None:
        booked = await cancel_pending_buy_and_book(exchange, state, cfg, log, "manual force-buy")
        if state.pending_buy_order is not None:
            # Cancel failed -- the limit buy is still live. Don't stack a market
            # buy on top (would double the lot); bail and retry next tick.
            log.warning("force-buy aborted -- resting limit buy still live after a failed cancel")
            return
        if booked:
            return

    if order_type == "limit":
        offset = cfg.get("limit_buy_offset_pct", 0.001)
        # Reference price: prefer bid (typical maker placement for buys) then last then ask.
        ref = ticker.get("bid") or ticker.get("last") or ticker.get("ask")
        if ref is None:
            log.warning("LIMIT-BUY skipped -- no reference price on ticker; falling back to market")
        else:
            limit_price = ref * (1 - offset)
            # qty sized to spend ~usd_per_buy worth at the limit price (ignoring fee impact)
            qty_coin = usd / limit_price
            if state.mode == "live":
                try:
                    placed = await live_limit_buy(exchange, symbol, qty_coin, limit_price)
                    state.pending_buy_order = PendingBuyOrder(
                        order_id=placed["order_id"],
                        limit_price=placed["limit_price"],
                        qty_coin=placed["qty"],
                        level=level,
                        placed_at_price=ref,
                        placed_ts=time.time(),
                    )
                except ccxt.ExchangeError as e:
                    log.error("LIMIT-BUY rejected (%s) -- falling back to market", e)
                    state.pending_buy_order = None
                else:
                    state.last_action_ts = time.time()
                    state.trailing_buy = TrailState()
                    log.info(
                        f"[LIVE] LIMIT-BUY placed @ ${limit_price:.{pp}f} "
                        f"qty={qty_coin:.8f} ref=${ref:.{pp}f} -{offset:.3%} "
                        f"level={level} id={placed['order_id']} ({reason})"
                    )
                    return
            else:
                state.pending_buy_order = PendingBuyOrder(
                    order_id=None,
                    limit_price=limit_price,
                    qty_coin=qty_coin,
                    level=level,
                    placed_at_price=ref,
                    placed_ts=time.time(),
                )
                state.last_action_ts = time.time()
                state.trailing_buy = TrailState()
                log.info(
                    f"[PAPER] LIMIT-BUY placed @ ${limit_price:.{pp}f} "
                    f"qty={qty_coin:.8f} ref=${ref:.{pp}f} -{offset:.3%} "
                    f"level={level} ({reason})"
                )
                return
        # Falls through to market path on missing-ref or live exchange rejection

    if state.mode == "live":
        fill = await live_buy(exchange, symbol, usd, ticker)
    else:
        fill = await paper_buy(wallet, usd, state, ticker)

    _finalize_buy(state, fill, level, cfg, reason, "BUY", log)


async def check_pending_buy(exchange, state: State, wallet: PaperWallet, cfg: dict,
                             ticker: dict, log: logging.LoggerAdapter) -> bool:
    """Poll/simulate fill for an outstanding limit buy. Returns True if the order
    filled this tick (so the caller knows to save state and consider follow-on
    actions), False otherwise (still pending, cancelled, or no pending order).

    Cancel rule: cancel if current_price > placed_at_price * (1 + cancel_buffer),
    where cancel_buffer = max(trail_buy_pct, limit_buy_offset_pct). The max()
    guards against coins with trail_buy_pct=0 -- otherwise any tick above the
    placement price would trigger an immediate cancel.
    """
    pending = state.pending_buy_order
    if pending is None:
        return False

    pp             = cfg["price_prec"]
    symbol         = cfg["symbol"]
    trail_buy_pct  = cfg["trail_buy_pct"]
    offset         = cfg.get("limit_buy_offset_pct", 0.001)
    cancel_buffer  = max(trail_buy_pct, offset)
    price          = ticker.get("last")
    cancel_price   = pending.placed_at_price * (1 + cancel_buffer)

    if state.mode == "live":
        try:
            order = await exchange.fetch_order(pending.order_id, symbol)
        except ccxt.OrderNotFound:
            log.warning("LIMIT-BUY %s not found at exchange -- clearing pending", pending.order_id)
            state.pending_buy_order = None
            return False
        status = (order.get("status") or "").lower()
        if status == "closed":
            fill_price = float(order.get("average") or order.get("price") or pending.limit_price)
            raw_filled = order.get("filled")
            if raw_filled is None or float(raw_filled) <= 0:
                # Closed limit buy but no filled qty reported -- assume it fully
                # filled (a real cancel shows as canceled/expired, not closed),
                # but flag it: if Kraken ever mislabels a no-fill as closed, this
                # is where we would over-track.
                filled_qty = float(pending.qty_coin)
                log.warning("LIMIT-BUY %s closed but filled qty missing -- assuming full %.8f",
                            pending.order_id, filled_qty)
            else:
                filled_qty = float(raw_filled)
            fee_usd    = float((order.get("fee") or {}).get("cost") or 0.0)
            fill = {"qty": filled_qty, "price": fill_price, "fee_usd": fee_usd, "ts": _now_iso()}
            _finalize_buy(state, fill, pending.level, cfg, "limit-buy filled", "LIMIT-BUY FILL", log)
            return True
        if status in ("canceled", "cancelled", "rejected", "expired"):
            log.info("LIMIT-BUY %s ended with status=%s -- clearing pending", pending.order_id, status)
            state.pending_buy_order = None
            return False
        # Still open -- check cancel rule
        if price is not None and price > cancel_price:
            log.info(
                f"[LIVE] LIMIT-BUY cancelling @ ${price:.{pp}f} "
                f"(limit ${pending.limit_price:.{pp}f}, threshold ${cancel_price:.{pp}f})"
            )
            try:
                await live_cancel_order(exchange, symbol, pending.order_id)
            except Exception as e:
                # Cancel FAILED -> the order is still live on Kraken. Do NOT drop
                # our tracking of it (that orphans the order: it later fills and
                # we never book it -> tracked drifts from the wallet). Keep it
                # and retry the cancel next tick.
                log.warning("LIMIT-BUY cancel FAILED for %s: %s -- order still live, keeping it tracked; will retry",
                            pending.order_id, e)
                return False
            state.pending_buy_order = None
            # Re-fetch: part (or all) of the order may have executed before the
            # cancel landed. Booking it keeps tracked qty in sync -- otherwise
            # filled buys are silently dropped and the coin under-tracks (the
            # M/USD initial-entry desync; mirror of the LIMIT-SELL cancel fix).
            try:
                final = await exchange.fetch_order(pending.order_id, symbol)
                filled_qty = float(final.get("filled") or 0.0)
            except Exception as e:
                log.warning("post-cancel fetch_order failed for %s: %s -- assuming unfilled",
                            pending.order_id, e)
                filled_qty = 0.0
            if filled_qty > 1e-12:
                fill_price = float(final.get("average") or final.get("price") or pending.limit_price)
                fee_usd    = float((final.get("fee") or {}).get("cost") or 0.0)
                fill = {"qty": filled_qty, "price": fill_price, "fee_usd": fee_usd, "ts": _now_iso()}
                log.info("LIMIT-BUY %s cancelled with %.8f filled -- booking it",
                         pending.order_id, filled_qty)
                _finalize_buy(state, fill, pending.level, cfg,
                              "limit-buy cancelled (partial fill)", "LIMIT-BUY FILL", log)
                return True
        return False

    # Paper mode -- check fill on ask crossing
    ask = ticker.get("ask") or price
    if ask is not None and ask <= pending.limit_price:
        gross   = pending.qty_coin * pending.limit_price
        fee_usd = gross * MAKER_FEE_PCT
        await wallet.spend(gross)  # debit the limit price worth of USD
        state.paper_wallet_coin += pending.qty_coin
        fill = {"qty": pending.qty_coin, "price": pending.limit_price,
                "fee_usd": fee_usd, "ts": _now_iso()}
        _finalize_buy(state, fill, pending.level, cfg, "limit-buy filled (paper)", "LIMIT-BUY FILL", log)
        return True

    if price is not None and price > cancel_price:
        log.info(
            f"[PAPER] LIMIT-BUY cancelling @ ${price:.{pp}f} "
            f"(limit ${pending.limit_price:.{pp}f}, threshold ${cancel_price:.{pp}f})"
        )
        state.pending_buy_order = None
    return False


# A live sell is clamped to the exchange free balance (see live_sellable_qty),
# so the qty actually sold can be below the tracked position. Selling at least
# this fraction of the tracked qty is a normal full close -- the tiny remainder
# is just fee dust Kraken shaved off the base coin. Below it, the fill is a
# *partial* close: the tracked position has desynced from the real balance, so
# we book PnL on the sold portion only, keep the rest on the books, and pause
# the coin. Charging the whole stack's cost against a half-size fill is what
# fabricated the ~$175 phantom "loss" on SYN cycle 6.
FULL_CLOSE_MIN_FILL_FRAC = 0.98


async def _finalize_sell(state: State, fill: dict, cfg: dict, reason: str, tag: str,
                          log: logging.LoggerAdapter, push_notify,
                          pause_on_partial: bool = True) -> float:
    """Post-fill state cleanup shared by market sells and limit-sell fills.

    `fill` must carry: qty, price, fee_usd, proceeds_net (or we compute it).
    `tag` is a short log label ("SELL-ALL" for market, "LIMIT-SELL FILL" for limit).
    Returns realized PnL.

    Realized PnL is always booked against the cost basis of the coins *actually*
    sold. On a full close that is the whole position; on a partial fill it is the
    proportional (average-cost) share and the unsold remainder is kept on the
    books. `pause_on_partial` (default True) pauses the coin on a partial -- the
    caller sets it False when the remainder is known-good (e.g. the unfilled part
    of a cancelled order, which is genuinely back in free balance) rather than a
    balance/tracking desync that needs manual reconciliation.
    """
    pp     = cfg["price_prec"]
    symbol = cfg["symbol"]
    base   = symbol.split("/")[0]

    qty          = fill["qty"]
    proceeds_net = fill.get("proceeds_net", qty * fill["price"] - fill["fee_usd"])
    tracked      = state.total_qty_coin
    n_levels     = len(state.positions)
    avg_entry    = state.avg_entry_price or 0.0

    partial = tracked > 0 and (qty / tracked) < FULL_CLOSE_MIN_FILL_FRAC

    if partial:
        # Cost basis of only the coins that sold (average-cost allocation).
        frac_sold     = qty / tracked
        cost_basis    = state.total_cost_usd * frac_sold
        remaining_qty = tracked - qty
    else:
        cost_basis = state.total_cost_usd

    realized = proceeds_net - cost_basis
    pct      = (realized / cost_basis * 100) if cost_basis > 0 else 0.0
    state.realized_pnl_usd += realized

    if partial:
        # Scale every level down proportionally so per-level data stays
        # consistent with the new totals and avg_entry is preserved.
        keep = 1.0 - frac_sold
        for p in state.positions:
            p.qty_coin *= keep
            p.fee_usd  *= keep
        state.total_qty_coin  = remaining_qty
        state.total_cost_usd -= cost_basis
        state.avg_entry_price = (
            state.total_cost_usd / remaining_qty if remaining_qty > 0 else None
        )
        state.last_action_ts  = time.time()
        state.trailing_sell   = TrailState()
        state.pending_sell_order = None
        if pause_on_partial:
            state.paused = True   # desync -- stop trading this coin until reconciled
    else:
        state.positions = []
        state.total_qty_coin = 0.0
        state.total_cost_usd = 0.0
        state.avg_entry_price = None
        state.last_buy_price = None
        state.cycle_count += 1
        state.last_action_ts = time.time()
        state.initial_entry_high = None
        state.breakeven_exit_armed = False
        state.trailing_buy = TrailState()
        state.trailing_sell = TrailState()
        state.pending_sell_order = None

    qty_field = f"qty={qty:.8f}/{tracked:.8f}" if partial else f"qty={qty:.8f}"
    log.info(
        f"[{state.mode.upper()}] {tag}{' PARTIAL' if partial else ''} "
        f"price=${fill['price']:.{pp}f} {qty_field} "
        f"fee=${fill['fee_usd']:.{pp}f} net=${proceeds_net:.{pp}f} "
        f"avg_entry=${avg_entry:.{pp}f} levels={n_levels} "
        f"realized=${realized:.{pp}f} ({pct:+.5f}%) "
        f"cycle={state.cycle_count} ({reason})"
    )

    if partial:
        if pause_on_partial:
            log.warning(
                f"[{state.mode.upper()}] PARTIAL SELL -- only {qty:.8f}/{tracked:.8f} {base} "
                f"({frac_sold:.1%}) sold; free balance was below the tracked position. "
                f"Booked PnL on the sold portion only; kept {remaining_qty:.8f} {base} "
                f"(cost ${state.total_cost_usd:.{pp}f}) on the books and PAUSED the coin. "
                f"Reconcile tracked qty vs the exchange, then resume or clear-stats "
                f"(select coin -> menu 4 -> Y)."
            )
        else:
            log.warning(
                f"[{state.mode.upper()}] PARTIAL SELL -- {qty:.8f}/{tracked:.8f} {base} "
                f"({frac_sold:.1%}) filled before the order ended; booked PnL on the sold "
                f"portion and kept {remaining_qty:.8f} {base} "
                f"(cost ${state.total_cost_usd:.{pp}f}) on the books -- continuing."
            )
        mode_tag   = "[PAPER] " if state.mode == "paper" else ""
        tail       = "coin PAUSED" if pause_on_partial else f"{remaining_qty:.4f} {base} left"
        await push_notify(
            f"{mode_tag}{base} PARTIAL sell {qty:.4f}/{tracked:.4f} for {pct:+.5f}% "
            f"(${realized:.{pp}f}) -- {tail}"
        )
        return realized

    # One-shot "pause after sell": if armed, pause the coin now that the cycle
    # has closed so it won't open a new buy cycle until manually resumed.
    paused_now = False
    if state.pause_after_sell:
        state.paused = True
        state.pause_after_sell = False
        paused_now = True
        log.info(
            f"[{state.mode.upper()}] PAUSE-AFTER-SELL fired -- coin paused; "
            f"no new buy cycle until resumed (select coin -> menu 4 -> Y)"
        )

    mode_tag = "[PAPER] " if state.mode == "paper" else ""
    pause_note = " -- now PAUSED" if paused_now else ""
    await push_notify(f"{mode_tag}{base} sold for {pct:+.5f}% (${realized:.{pp}f}){pause_note}")
    return realized


async def execute_sell_all(exchange, state: State, wallet: PaperWallet, cfg: dict,
                            ticker: dict, reason: str,
                            log: logging.LoggerAdapter, push_notify,
                            force_market: bool = False) -> float:
    """Execute a sell. Branches on cfg['order_type'] unless force_market=True.

    force_market=True is used by menu 9 (sell ALL now) so "sell now" always means
    market, regardless of the coin's configured order_type.

    For the market path (or force_market), this returns realized PnL synchronously.
    For the limit path, this places a post-only limit sell, records it in
    state.pending_sell_order, and returns 0.0 -- realized PnL is booked later by
    check_pending_sell() when/if the order fills.
    """
    qty = state.total_qty_coin
    if qty <= 0:
        return 0.0

    pp         = cfg["price_prec"]
    symbol     = cfg["symbol"]
    order_type = "market" if force_market else cfg.get("order_type", "market")

    # Force-market path may need to cancel an outstanding limit sell first.
    if force_market and state.pending_sell_order is not None:
        pending = state.pending_sell_order
        if state.mode == "live" and pending.order_id:
            try:
                await live_cancel_order(exchange, symbol, pending.order_id)
                log.info(f"[LIVE] cancelled pending LIMIT-SELL id={pending.order_id}")
            except Exception as e:
                log.warning("Could not cancel pending LIMIT-SELL %s: %s", pending.order_id, e)
        else:
            log.info("[PAPER] dropped pending LIMIT-SELL to force market sell")
        state.pending_sell_order = None

    # In live mode, clamp to the actual free balance (rounded down) so a
    # fee-shaved or precision-overcounted position can still exit instead of
    # bouncing off EOrder:Insufficient funds on both the limit and market paths.
    if state.mode == "live":
        qty = await live_sellable_qty(exchange, symbol, qty, log)
        if qty <= 0:
            log.error("SELL skipped -- no free %s balance on exchange to sell", symbol.split("/")[0])
            return 0.0

    if order_type == "limit":
        offset = cfg.get("limit_sell_offset_pct", 0.001)
        # Reference price: prefer ask (typical maker placement) then last then bid.
        ref = ticker.get("ask") or ticker.get("last") or ticker.get("bid")
        if ref is None:
            log.warning("LIMIT-SELL skipped -- no reference price on ticker; falling back to market")
        else:
            limit_price = ref * (1 + offset)
            if state.mode == "live":
                try:
                    placed = await live_limit_sell(exchange, symbol, qty, limit_price)
                    state.pending_sell_order = PendingSellOrder(
                        order_id=placed["order_id"],
                        limit_price=placed["limit_price"],
                        qty_coin=placed["qty"],
                        placed_ts=time.time(),
                    )
                except ccxt.ExchangeError as e:
                    log.error("LIMIT-SELL rejected (%s) -- falling back to market", e)
                    state.pending_sell_order = None
                else:
                    state.last_action_ts = time.time()
                    state.trailing_sell = TrailState()
                    log.info(
                        f"[LIVE] LIMIT-SELL placed @ ${limit_price:.{pp}f} "
                        f"qty={qty:.8f} ref=${ref:.{pp}f} +{offset:.3%} "
                        f"id={placed['order_id']} ({reason})"
                    )
                    return 0.0
            else:
                state.pending_sell_order = PendingSellOrder(
                    order_id=None,
                    limit_price=limit_price,
                    qty_coin=qty,
                    placed_ts=time.time(),
                )
                state.last_action_ts = time.time()
                state.trailing_sell = TrailState()
                log.info(
                    f"[PAPER] LIMIT-SELL placed @ ${limit_price:.{pp}f} "
                    f"qty={qty:.8f} ref=${ref:.{pp}f} +{offset:.3%} ({reason})"
                )
                return 0.0
        # Falls through to market path on missing-ref or live exchange rejection

    if state.mode == "live":
        fill = await live_sell_all(exchange, symbol, qty, ticker)
    else:
        fill = await paper_sell_all(wallet, qty, state, ticker)

    return await _finalize_sell(state, fill, cfg, reason, "SELL-ALL", log, push_notify)


async def execute_clear_stats(exchange, state: State, cfg: dict,
                              log: logging.LoggerAdapter) -> None:
    """Full reset of a coin's state back to fresh.

    Cancels any resting live order first (so it isn't orphaned on the exchange),
    then wipes positions, totals, realized PnL, cycle count, trails, flags, and
    pending orders. The coin is left PAUSED so it doesn't immediately open a new
    cycle right after the wipe -- resume it from the menu when ready.

    NOTE: in live mode any coins actually held on the exchange are NOT sold --
    the bot simply stops tracking them. The caller is expected to confirm.
    """
    symbol = cfg["symbol"]

    if state.mode == "live":
        for po in (state.pending_buy_order, state.pending_sell_order):
            oid = getattr(po, "order_id", None) if po is not None else None
            if oid:
                try:
                    await live_cancel_order(exchange, symbol, oid)
                    log.info(f"[LIVE] clear-stats cancelled resting order {oid}")
                except Exception as e:
                    log.warning("clear-stats: could not cancel %s: %s", oid, e)

    old_pnl, old_cycle, old_pos = state.realized_pnl_usd, state.cycle_count, len(state.positions)

    state.positions = []
    state.total_qty_coin = 0.0
    state.total_cost_usd = 0.0
    state.avg_entry_price = None
    state.last_buy_price = None
    state.realized_pnl_usd = 0.0
    state.cycle_count = 0
    state.last_action_ts = time.time()
    state.initial_entry_high = None
    state.breakeven_exit_armed = False
    state.pause_after_sell = False
    state.stop_loss_pct = None
    state.trailing_buy = TrailState()
    state.trailing_sell = TrailState()
    state.pending_buy_order = None
    state.pending_sell_order = None
    state.paper_wallet_coin = 0.0
    state.paused = True   # safety: don't auto-trade right after a wipe

    log.warning(
        f"[{state.mode.upper()}] STATS CLEARED -- full reset "
        f"(was realized=${old_pnl:.2f}, cycle={old_cycle}, {old_pos} position(s)); "
        f"coin is now PAUSED -- resume via: select coin -> menu 4 (toggle pause) -> Y"
    )


async def execute_retire(exchange, state: State, cfg: dict,
                         log: logging.LoggerAdapter) -> None:
    """Retire a coin permanently: book its lifetime realized PnL + cycles into
    the retired ledger (data/retired_pnl.json), then do a clear_stats-style
    full reset and leave it PAUSED.

    Order matters (live money): the ledger write happens FIRST and the reset
    is ABORTED if it fails -- never zero PnL that isn't durably booked. The
    booking and the zeroing happen in the same action so the totals (all-coins
    summary + Blynk V0) never double-count.

    The state file is deliberately NOT deleted: the zeroed+paused file is the
    restart guard until the user sets "enabled": False in helpers/config.py --
    a missing file would be recreated fresh and UNPAUSED on the next restart.
    """
    symbol = cfg["symbol"]
    pnl, cycles = state.realized_pnl_usd, state.cycle_count

    if not add_retired(symbol, pnl, cycles):
        log.error("RETIRE ABORTED -- ledger write failed; state left untouched")
        return

    log.warning(
        f"[{state.mode.upper()}] RETIRED -- booked ${pnl:+.2f} realized PnL "
        f"+ {cycles} cycle(s) into data/retired_pnl.json"
    )
    await execute_clear_stats(exchange, state, cfg, log)
    # Persist the zeroed state NOW -- shrinks the booked-but-not-yet-zeroed
    # crash window to ~ms (the dispatch loop's save_state re-runs harmlessly).
    save_state(symbol, state)
    log.warning(
        f"RETIRE complete: set \"enabled\": False for {symbol} in helpers/config.py "
        f"before the next restart. Until then the zeroed PAUSED state file keeps it "
        f"idle. After disabling, data/state_{symbol.replace('/', '_')}.json can be "
        f"deleted by hand (its PnL lives in the ledger now)."
    )


def _sell_fill_from_order(order: dict, pending: PendingSellOrder, filled_qty: float) -> dict:
    """Build a _finalize_sell `fill` dict from an exchange order's executed portion.

    `cost` is the gross quote received for the filled base amount; net proceeds
    subtract the fee. Falls back to filled_qty * price when a field is missing.
    """
    fill_price   = float(order.get("average") or order.get("price") or pending.limit_price)
    fee_usd      = float((order.get("fee") or {}).get("cost") or 0.0)
    proceeds_net = float(order.get("cost") or filled_qty * fill_price) - fee_usd
    return {"qty": filled_qty, "price": fill_price, "fee_usd": fee_usd,
            "ts": _now_iso(), "proceeds_net": proceeds_net}


async def cancel_pending_sell_and_book(exchange, state: State, cfg: dict,
                                       log: logging.LoggerAdapter, push_notify,
                                       context: str) -> Optional[float]:
    """Cancel a resting limit sell; book any portion that filled before the
    cancel landed (same race check_pending_sell's cancel path guards -- the SYN
    cycle-6 bug). Mirrors cancel_pending_buy_and_book. Returns realized PnL if
    a raced fill was booked, else None. Used by the target-sell set/clear
    paths; execute_sell_all's force-market block and check_pending_sell keep
    their own (already-tested) cancel logic.
    """
    pending = state.pending_sell_order
    if pending is None:
        return None
    symbol = cfg["symbol"]
    if state.mode == "live" and pending.order_id:
        # Clamped-at-placement order => free balance was short => real desync
        # (same rule as check_pending_sell).
        desync = pending.qty_coin < state.total_qty_coin * FULL_CLOSE_MIN_FILL_FRAC
        try:
            await live_cancel_order(exchange, symbol, pending.order_id)
            log.info(f"[LIVE] cancelled pending LIMIT-SELL id={pending.order_id} ({context})")
        except Exception as e:
            # Cancel FAILED -> order still resting. Keep tracking it (don't
            # orphan) and tell the caller so it doesn't stack an order on top.
            log.warning("Could not cancel pending LIMIT-SELL %s: %s -- still live, keeping tracked (%s aborts)",
                        pending.order_id, e, context)
            return None
        state.pending_sell_order = None
        # The sell may have (partly) filled before the cancel landed. Book any
        # filled qty so PnL and tracked totals stay honest.
        try:
            final = await exchange.fetch_order(pending.order_id, symbol)
            filled_qty = float(final.get("filled") or 0.0)
        except Exception as e:
            log.warning("post-cancel fetch_order failed for %s: %s -- assuming unfilled",
                        pending.order_id, e)
            filled_qty = 0.0
        if filled_qty > 1e-12:
            log.info("%s: pending LIMIT-SELL %s had %.8f filled before cancel -- booking it",
                     context, pending.order_id, filled_qty)
            fill = _sell_fill_from_order(final, pending, filled_qty)
            return await _finalize_sell(state, fill, cfg,
                                        f"limit-sell filled (raced {context})",
                                        "LIMIT-SELL FILL", log, push_notify,
                                        pause_on_partial=desync)
    else:
        log.info(f"[PAPER] dropped pending LIMIT-SELL ({context})")
        state.pending_sell_order = None
    return None


async def execute_target_sell(exchange, state: State, cfg: dict, ticker: dict,
                              price: Optional[float],
                              log: logging.LoggerAdapter, push_notify) -> Optional[float]:
    """Manual 'sell at my price' (menu p). price > 0 = place/replace the target;
    None/<=0 = clear. The resting order is manual=True: exempt from the
    auto-cancel rule in check_pending_sell and freezes grid buys while it
    rests (run_coin gate). Arms pause_after_sell so the fill pauses the coin.
    Live postOnly rejection is a hard refusal -- NO market fallback (unlike
    execute_sell_all's auto limit path). Returns realized PnL if a raced
    partial was booked while cancelling, else None.
    """
    pp      = cfg["price_prec"]
    symbol  = cfg["symbol"]
    pending = state.pending_sell_order

    # --- Clear ---
    if price is None or price <= 0:
        if pending is None:
            log.info("TARGET-SELL clear -- nothing resting; nothing to do")
            return None
        if not pending.manual:
            log.info(
                f"TARGET-SELL clear -- resting LIMIT-SELL @ ${pending.limit_price:.{pp}f} "
                f"is the bot's own (auto) order, not a manual target -- leaving it alone"
            )
            return None
        old_px = pending.limit_price
        realized = await cancel_pending_sell_and_book(
            exchange, state, cfg, log, push_notify, "target-clear")
        state.pause_after_sell = False
        state.last_action_ts = time.time()
        note = " (it had filled/part-filled first -- see booking above)" if realized is not None else ""
        log.info(
            f"[{state.mode.upper()}] TARGET CLEARED (was @ ${old_px:.{pp}f}){note} -- "
            f"pause-after-sell disarmed; grid trading resumes"
        )
        return realized

    # --- Set ---
    if not state.positions or state.total_qty_coin <= 0:
        log.info("TARGET-SELL refused -- no open position to sell")
        return None
    last = ticker.get("last") or ticker.get("bid")
    if last is not None and price <= last:
        log.error(
            f"TARGET-SELL refused -- target ${price:.{pp}f} <= current ${last:.{pp}f}; "
            f"a post-only sell would be rejected. To sell NOW use menu 9 (market)."
        )
        return None

    realized = None
    if pending is not None:   # replace whatever rests (auto or a previous target)
        realized = await cancel_pending_sell_and_book(
            exchange, state, cfg, log, push_notify, "target-sell replace")
        if state.pending_sell_order is not None:
            # Cancel failed -- old sell still resting. Don't place a second sell
            # on top of it; bail (the user can retry once it clears).
            log.error("TARGET-SELL aborted -- couldn't cancel the resting sell (still live); try again")
            return realized
    # A resting limit BUY would grow the position under the target -- cancel it
    # and fold a raced pre-cancel fill in so the target covers it too.
    await cancel_pending_buy_and_book(exchange, state, cfg, log, "target-sell")

    qty = state.total_qty_coin
    if qty <= 0:
        log.warning("TARGET-SELL abandoned -- position closed by a raced fill while replacing")
        return realized
    if state.mode == "live":
        qty = await live_sellable_qty(exchange, symbol, qty, log)
        if qty <= 0:
            log.error("TARGET-SELL refused -- no free %s balance on exchange", symbol.split("/")[0])
            return realized
        try:
            placed = await live_limit_sell(exchange, symbol, qty, price)
        except ccxt.ExchangeError as e:
            log.error(
                f"TARGET-SELL rejected by exchange ({e}) -- NOT falling back to market; "
                f"nothing placed, state unchanged. If price already passed ${price:.{pp}f}, "
                f"use menu 9 to sell at market."
            )
            return realized
        limit_price, qty = placed["limit_price"], placed["qty"]
        state.pending_sell_order = PendingSellOrder(
            order_id=placed["order_id"], limit_price=limit_price,
            qty_coin=qty, placed_ts=time.time(), manual=True,
        )
    else:
        limit_price = price
        state.pending_sell_order = PendingSellOrder(
            order_id=None, limit_price=limit_price, qty_coin=qty,
            placed_ts=time.time(), manual=True,
        )
    state.pause_after_sell = True          # consumed by _finalize_sell on the fill
    state.trailing_sell    = TrailState()  # resting order replaces any armed sell-trail
    state.trailing_buy     = TrailState()  # kill half-formed buy intent; buys are frozen anyway
    state.last_action_ts   = time.time()
    away = f" ({(limit_price / last - 1) * 100:+.2f}% from now)" if last else ""
    log.info(
        f"[{state.mode.upper()}] TARGET SELL SET @ ${limit_price:.{pp}f}{away} "
        f"qty={qty:.8f} (~${qty * limit_price:,.2f} gross) -- rests until filled or "
        f"cleared (menu p -> 0/empty); never auto-cancels; grid buys FROZEN; pauses on fill"
    )
    return realized


async def check_pending_sell(exchange, state: State, wallet: PaperWallet, cfg: dict,
                              ticker: dict, log: logging.LoggerAdapter, push_notify) -> Optional[float]:
    """Poll/simulate fill for an outstanding limit sell. Returns realized PnL on
    fill, or None if still pending / cancelled with nothing filled / no pending order.

    Cancel rule: if current price has dropped more than cfg['trail_sell_pct']
    below the resting limit, cancel and clear pending_sell_order so the
    sell-trail can re-arm on the next rally. Manual target orders
    (pending.manual, menu p) are EXEMPT -- they rest until filled or cleared
    by the user. Any portion that executed before the order ended (closed,
    cancelled, or our own cancel) is always booked -- a dropped partial fill
    is what desynced SYN in cycle 6.
    """
    pending = state.pending_sell_order
    if pending is None:
        return None

    pp             = cfg["price_prec"]
    symbol         = cfg["symbol"]
    trail_sell_pct = cfg["trail_sell_pct"]
    price          = ticker.get("last")
    cancel_price   = pending.limit_price * (1 - trail_sell_pct)

    if state.mode == "live":
        try:
            order = await exchange.fetch_order(pending.order_id, symbol)
        except ccxt.OrderNotFound:
            log.warning("LIMIT-SELL %s not found at exchange -- clearing pending", pending.order_id)
            state.pending_sell_order = None
            return None
        status     = (order.get("status") or "").lower()
        filled_qty = float(order.get("filled") or 0.0)
        # A clamped order (placed for materially less than the tracked position)
        # means free balance was short -> a real desync, pause on partial. A
        # full-size order that only partly filled leaves the rest in free
        # balance, so keep trading.
        desync = pending.qty_coin < state.total_qty_coin * FULL_CLOSE_MIN_FILL_FRAC

        if status == "closed":
            if filled_qty <= 0:
                filled_qty = float(pending.qty_coin)
            fill = _sell_fill_from_order(order, pending, filled_qty)
            return await _finalize_sell(state, fill, cfg, "limit-sell filled",
                                        "LIMIT-SELL FILL", log, push_notify,
                                        pause_on_partial=desync)

        if status in _TERMINAL_ORDER_STATUSES:   # canceled/expired/rejected (closed handled above)
            state.pending_sell_order = None
            if filled_qty > 1e-12:
                log.info("LIMIT-SELL %s ended status=%s with %.8f filled -- booking partial",
                         pending.order_id, status, filled_qty)
                fill = _sell_fill_from_order(order, pending, filled_qty)
                return await _finalize_sell(state, fill, cfg, f"limit-sell {status} (partial fill)",
                                            "LIMIT-SELL FILL", log, push_notify,
                                            pause_on_partial=desync)
            log.info("LIMIT-SELL %s ended with status=%s -- clearing pending", pending.order_id, status)
            return None

        # Still open -- check cancel rule (manual targets never auto-cancel)
        if not pending.manual and price is not None and price < cancel_price:
            log.info(
                f"[LIVE] LIMIT-SELL cancelling @ ${price:.{pp}f} "
                f"(limit ${pending.limit_price:.{pp}f}, threshold ${cancel_price:.{pp}f})"
            )
            try:
                await live_cancel_order(exchange, symbol, pending.order_id)
            except Exception as e:
                # Cancel FAILED -> the order is still resting on Kraken. Do NOT
                # drop our tracking (that is exactly what orphaned the TAO sell:
                # the order later filled and was never booked, so tracked stayed
                # high while the wallet emptied). Keep it and retry next tick.
                log.warning("LIMIT-SELL cancel FAILED for %s: %s -- order still live, keeping it tracked; will retry",
                            pending.order_id, e)
                return None
            state.pending_sell_order = None
            # Re-fetch: part of the order may have executed before the cancel
            # landed. Booking it keeps tracked qty in sync (the SYN cycle-6 bug).
            try:
                final = await exchange.fetch_order(pending.order_id, symbol)
                filled_qty = float(final.get("filled") or 0.0)
            except Exception as e:
                log.warning("post-cancel fetch_order failed for %s: %s -- using last-known filled %.8f",
                            pending.order_id, e, filled_qty)
                final = order
            if filled_qty > 1e-12:
                log.info("LIMIT-SELL %s cancelled with %.8f filled -- booking partial",
                         pending.order_id, filled_qty)
                fill = _sell_fill_from_order(final, pending, filled_qty)
                return await _finalize_sell(state, fill, cfg, "limit-sell cancelled (partial fill)",
                                            "LIMIT-SELL FILL", log, push_notify,
                                            pause_on_partial=desync)
        return None

    # Paper mode -- check fill on bid crossing
    bid = ticker.get("bid") or price
    if bid is not None and bid >= pending.limit_price:
        gross   = pending.qty_coin * pending.limit_price
        fee_usd = gross * MAKER_FEE_PCT
        net     = gross - fee_usd
        await wallet.credit(net)
        state.paper_wallet_coin -= pending.qty_coin
        fill = {"qty": pending.qty_coin, "price": pending.limit_price,
                "fee_usd": fee_usd, "ts": _now_iso(), "proceeds_net": net}
        return await _finalize_sell(state, fill, cfg, "limit-sell filled (paper)", "LIMIT-SELL FILL", log, push_notify)

    if not pending.manual and price is not None and price < cancel_price:
        log.info(
            f"[PAPER] LIMIT-SELL cancelling @ ${price:.{pp}f} "
            f"(limit ${pending.limit_price:.{pp}f}, threshold ${cancel_price:.{pp}f})"
        )
        state.pending_sell_order = None
    return None


# --- Price stream -----------------------------------------------------------

async def price_stream(exchange, symbol: str, simulate_file: Optional[str]) -> AsyncIterator[dict]:
    if simulate_file:
        prices: list[float] = []
        with open(simulate_file) as f:
            for row in csv.reader(f):
                if not row:
                    continue
                try:
                    prices.append(float(row[0]))
                except ValueError:
                    continue
        logging.info("[%s] Simulating %d prices from %s", symbol, len(prices), simulate_file)
        for p in prices:
            yield {"last": p, "bid": p * 0.9999, "ask": p * 1.0001}
            await asyncio.sleep(0.01)
        return

    backoff = 1.0
    while True:
        try:
            ticker = await exchange.watch_ticker(symbol)
            backoff = 1.0
            yield ticker
        except ccxt.NetworkError as e:
            logging.warning("[%s] WebSocket disconnect: %s -- reconnecting in %.1fs", symbol, e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
        except ccxt.ExchangeError as e:
            logging.error("[%s] Exchange error in stream: %s -- retrying in %.1fs", symbol, e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


# --- Manual command application ---------------------------------------------

def apply_manual(cmd: PendingAction, state: State, cfg: dict, current_price: float, log: logging.LoggerAdapter) -> Optional[PendingAction]:
    """Apply a manual command queued by the keypress listener.

    Pause and arm-sell-trail mutate state immediately and return None. Force-buy
    and force-sell return a PendingAction to fire on the same tick.
    """
    pp = cfg["price_prec"]

    if cmd.kind == "pause_toggle":
        state.paused = not state.paused
        log.info("MANUAL: %s", "PAUSED" if state.paused else "RESUMED")
        return None

    if cmd.kind == "arm_sell_trail":
        if not state.positions:
            log.info("MANUAL: arm-sell-trail ignored -- no open positions")
            return None
        state.trailing_sell = TrailState(armed=True, extreme=current_price, armed_at_price=current_price, manual=True)
        avg = state.avg_entry_price
        floor_note = f"won't sell below avg ${avg:.{pp}f}" if avg is not None else "clamped to avg entry"
        log.info(f"MANUAL: sell-trail ARMED at ${current_price:.{pp}f} (trailing stop -{cfg['trail_sell_pct']:.1%}, {floor_note})")
        return None

    if cmd.kind == "arm_buy_trail":
        # Mirror of arm-sell-trail: a manual trailing BUY. Allowed flat (times an
        # initial entry) or holding (times a grid add); refused only when there's
        # nothing sensible to buy into.
        if len(state.positions) >= cfg["max_grid_levels"]:
            log.info("MANUAL: arm-buy-trail ignored -- grid full (%d levels)", cfg["max_grid_levels"])
            return None
        if state.pending_buy_order is not None:
            log.info("MANUAL: arm-buy-trail ignored -- a limit buy is already pending (wait for it to fill/cancel)")
            return None
        if state.pending_sell_order is not None and state.pending_sell_order.manual:
            log.info("MANUAL: arm-buy-trail ignored -- a manual target sell is resting (menu p); clear it first (buys are frozen)")
            return None
        state.trailing_buy = TrailState(armed=True, extreme=current_price, armed_at_price=current_price, manual=True)
        where = "grid add" if state.positions else "initial entry"
        log.info(
            f"MANUAL: buy-trail ARMED at ${current_price:.{pp}f} (trailing buy +{cfg['trail_buy_pct']:.1%} off the "
            f"low; fires ONE {where} buy on the rebound; disarm via menu t)"
        )
        return None

    if cmd.kind == "arm_breakeven_exit":
        state.breakeven_exit_armed = not state.breakeven_exit_armed
        if state.breakeven_exit_armed and state.avg_entry_price is not None:
            be_threshold, source_label, _ = sell_threshold(state, cfg)
            log.info(
                f"MANUAL: breakeven-exit ARMED -- will sell when price >= "
                f"${be_threshold:.{pp}f} ({source_label})"
            )
        elif state.breakeven_exit_armed:
            log.info("MANUAL: breakeven-exit ARMED (no open positions yet)")
        else:
            log.info("MANUAL: breakeven-exit DISARMED")
        return None

    if cmd.kind == "arm_pause_after_sell":
        state.pause_after_sell = not state.pause_after_sell
        if state.pause_after_sell:
            target = "current position sells" if state.positions else "next sell"
            log.info(f"MANUAL: pause-after-sell ARMED -- coin will pause once the {target}")
        else:
            log.info("MANUAL: pause-after-sell DISARMED")
        return None

    if cmd.kind == "set_stop_loss":
        pct = cmd.value or 0.0
        if pct <= 0:
            if state.stop_loss_pct is not None:
                log.info(f"MANUAL: STOP-LOSS CLEARED (was -{state.stop_loss_pct:.2%} below avg)")
                state.stop_loss_pct = None
            else:
                log.info("MANUAL: stop-loss clear -- none was set")
            return None
        state.stop_loss_pct = pct
        if state.avg_entry_price is not None:
            trigger = state.avg_entry_price * (1 - pct)
            log.info(
                f"MANUAL: STOP-LOSS SET -{pct:.2%} below avg -- trigger ${trigger:.{pp}f} "
                f"(avg ${state.avg_entry_price:.{pp}f}; tracks avg as the grid adds levels; "
                f"on hit: market-sell ALL + pause, one-shot)"
            )
        else:
            log.info(f"MANUAL: STOP-LOSS SET -{pct:.2%} below avg entry (no position yet -- arms when one opens)")
        return None

    if cmd.kind == "set_target_sell":
        # Cheap validation here; the real work (cancel/place) needs exchange
        # access, so the cmd passes through to the run_coin dispatch which
        # calls execute_target_sell (that re-validates on the executing tick).
        price = cmd.value or 0.0
        pso = state.pending_sell_order
        if price <= 0:   # clear
            if pso is None or not pso.manual:
                log.info(
                    "MANUAL: target-sell clear -- no manual target set%s",
                    " (the resting limit sell is the bot's own auto order -- untouched)"
                    if pso is not None else "",
                )
                return None
            cmd.reason = f"{cmd.reason} (clear)"
            return cmd
        if not state.positions or state.total_qty_coin <= 0:
            log.info("MANUAL: target-sell refused -- no open position")
            return None
        if current_price is not None and price <= current_price:
            log.info(
                f"MANUAL: target-sell refused -- target ${price:.{pp}f} <= current "
                f"${current_price:.{pp}f} (post-only would reject; menu 9 sells now)"
            )
            return None
        cmd.reason = f"{cmd.reason} @ ${price:.{pp}f}"
        return cmd

    if cmd.kind == "clear_targets":
        # Re-baseline the initial-entry reference to the current price so the
        # first buy of a fresh cycle arms at current * (1 - drop_pct) again,
        # instead of trailing a stale "observed high" that drifted while the
        # coin was paused / a limit buy was resting. Only meaningful pre-entry;
        # with a position open there is no "first low" to reset, so we just
        # disarm any half-formed buy trail and leave the grid alone.
        if state.positions:
            state.trailing_buy = TrailState()
            log.info(
                f"MANUAL: clear-targets -- position open; disarmed pending buy-trail. "
                f"Grid buys still measured from last buy ${state.last_buy_price or 0.0:.{pp}f}."
            )
            return None
        old = state.initial_entry_high
        state.initial_entry_high = current_price
        state.trailing_buy = TrailState()
        arm = current_price * (1 - cfg["drop_pct"])
        was = f" (was ${old:.{pp}f})" if old is not None else ""
        log.info(
            f"MANUAL: clear-targets -- first-buy reference reset to "
            f"${current_price:.{pp}f}{was}; will arm at ${arm:.{pp}f} "
            f"(-{cfg['drop_pct']:.1%} from now)"
        )
        return None

    if cmd.kind == "clear_stats":
        # Needs exchange access (to cancel resting orders), so it's executed in
        # the run_coin dispatch loop -- just pass it through here.
        return cmd

    if cmd.kind == "retire":
        # Books the ledger then reuses execute_clear_stats; needs exchange
        # access (cancels resting orders), so the dispatch loop runs it.
        return cmd

    if cmd.kind == "buy":
        if len(state.positions) >= cfg["max_grid_levels"]:
            log.info("MANUAL: force-buy refused -- grid full (%d levels)", cfg["max_grid_levels"])
            return None
        cmd.level = len(state.positions)
        # Menu 7 (force BUY) is always immediate market buy regardless of cfg['buy_order_type'].
        # If a limit buy is currently pending, execute_buy will cancel it first.
        cmd.force_market = True
        if state.pending_buy_order is not None:
            log.info("MANUAL: force-buy queued (will cancel pending LIMIT-BUY first)")
        else:
            log.info("MANUAL: force-buy queued")
        return cmd

    if cmd.kind == "sell_all":
        if not state.positions:
            log.info("MANUAL: force-sell-all ignored -- no open positions")
            return None
        # Menu 8 (sell ALL now) is always immediate market sell regardless of cfg['order_type'].
        # If a limit sell is currently pending, execute_sell_all will cancel it first.
        cmd.force_market = True
        if state.pending_sell_order is not None:
            log.info("MANUAL: force-sell-all queued (will cancel pending LIMIT-SELL first)")
        else:
            log.info("MANUAL: force-sell-all queued")
        return cmd

    return None


# --- Per-coin task ----------------------------------------------------------

async def run_coin(
    exchange,
    cfg: dict,
    state: State,
    wallet: PaperWallet,
    cmd_queue: asyncio.Queue,
    last_price: dict,
    states: dict,
    blynk: BlynkClient,
    push_notify,
    dry_run: bool,
    simulate_file: Optional[str],
    bypass_cooldown: bool,
) -> None:
    symbol = cfg["symbol"]
    pp     = cfg["price_prec"]
    log    = CoinAdapter(logging.getLogger(), {"symbol": symbol})

    if state.positions:
        avg_entry = state.avg_entry_price or 0.0
        last_buy  = state.last_buy_price or 0.0
        log.info(
            f"Resumed {len(state.positions)} positions, "
            f"avg_entry=${avg_entry:.{pp}f}, last_buy=${last_buy:.{pp}f}, "
            f"cycle={state.cycle_count}"
        )
    if state.paused:
        log.warning("PAUSED (persisted from previous session). Resume via: select coin -> menu 4 (toggle pause) -> Y.")

    try:
        async for ticker in price_stream(exchange, symbol, simulate_file):
            try:
                price = ticker.get("last")
                if price is None:
                    continue
                last_price[symbol] = price

                action: Optional[PendingAction] = None

                # Drain manual commands for this coin (apply pause/arm-trail; queue buy/sell)
                while not cmd_queue.empty():
                    queued = cmd_queue.get_nowait()
                    qa = apply_manual(queued, state, cfg, price, log)
                    if qa is not None:
                        action = qa

                # Service any outstanding limit orders BEFORE the trail handlers,
                # so a fill in this tick books PnL/positions and the trail logic
                # sees the post-fill state.
                if state.pending_sell_order is not None:
                    realized = await check_pending_sell(
                        exchange, state, wallet, cfg, ticker, log, push_notify
                    )
                    if realized is not None:
                        asyncio.create_task(blynk.push_pnl(states, COINS))
                if state.pending_buy_order is not None:
                    await check_pending_buy(exchange, state, wallet, cfg, ticker, log)

                # Stop-loss: outside the cooldown gate (a crash right after a
                # buy must still stop out) and ahead of the trail handlers.
                # Runs even with limit orders resting (execute_sell_all cancels
                # the sell; the dispatch below cancels the buy). Skipped while
                # paused -- paused means hands-off, and firing pauses anyway.
                # Placed after pending-order servicing so a limit-buy fill this
                # tick updates avg_entry before the trigger is computed.
                if action is None and not state.paused:
                    action = handle_stop_loss(state, price, cfg, log)

                if action is None and not state.paused:
                    cooldown_ok = (
                        bypass_cooldown
                        or (time.time() - state.last_action_ts) >= ACTION_COOLDOWN_SEC
                    )
                    if cooldown_ok:
                        # A manually-armed buy-trail (menu 8) owns the trailing_buy
                        # slot and overrides the automatic entry/grid trails.
                        manual_buy_trail = state.trailing_buy.armed and state.trailing_buy.manual
                        if not state.positions:
                            # Skip entry trails while a limit buy is resting.
                            if state.pending_buy_order is None:
                                if manual_buy_trail:
                                    action = handle_manual_buy_trail(state, price, cfg, log)
                                else:
                                    action = handle_initial_entry_trail(state, price, cfg, log)
                        else:
                            # A manual target sell resting = user is exiting at
                            # their price -- freeze new grid buys until it fills
                            # or is cleared (menu p -> 0).
                            manual_target = (
                                state.pending_sell_order is not None
                                and state.pending_sell_order.manual
                            )
                            # Skip sell-trail while a limit sell is resting --
                            # don't want two sell signals racing.
                            if state.pending_sell_order is None:
                                action = handle_sell_trail(state, price, cfg, log)
                            # Buy side: a manual buy-trail overrides the auto grid
                            # trail (shared trailing_buy slot). Frozen while a limit
                            # buy rests or a manual target sell rests.
                            if action is None and state.pending_buy_order is None and not manual_target:
                                if manual_buy_trail:
                                    action = handle_manual_buy_trail(state, price, cfg, log)
                                else:
                                    action = handle_buy_trail(state, price, cfg, log)

                if action is not None:
                    if dry_run and state.mode == "live":
                        log.warning(
                            f"[DRY-RUN] would {action.kind} reason={action.reason} "
                            f"price=${price:.{pp}f}"
                        )
                        if action.from_stop_loss:
                            state.stop_loss_pct = None   # keep dry-run one-shot (no per-tick refire spam)
                    elif action.kind == "buy":
                        await execute_buy(
                            exchange, state, wallet, cfg, ticker, action.level,
                            action.reason, log, force_market=action.force_market,
                        )
                    elif action.kind == "sell_all":
                        if action.from_stop_loss and state.pending_buy_order is not None:
                            # Kill the re-entry vector: a resting limit buy would
                            # otherwise fill during the crash even while paused
                            # (pending-order servicing runs before the paused
                            # gate). A raced pre-cancel fill is booked into the
                            # position first so the sell below covers it too.
                            await cancel_pending_buy_and_book(exchange, state, cfg, log, "stop-loss")
                        await execute_sell_all(
                            exchange, state, wallet, cfg, ticker, action.reason,
                            log, push_notify, force_market=action.force_market,
                        )
                        if action.from_stop_loss:
                            state.stop_loss_pct = None   # one-shot: disarm only after the sell call succeeded
                        # Realized PnL changed -- push to Blynk immediately (fire-and-forget)
                        asyncio.create_task(blynk.push_pnl(states, COINS))
                    elif action.kind == "set_target_sell":
                        realized = await execute_target_sell(
                            exchange, state, cfg, ticker, action.value, log, push_notify,
                        )
                        if realized is not None:   # a raced partial booked PnL during a cancel
                            asyncio.create_task(blynk.push_pnl(states, COINS))
                    elif action.kind == "clear_stats":
                        await execute_clear_stats(exchange, state, cfg, log)
                        # Realized PnL was zeroed -- refresh Blynk
                        asyncio.create_task(blynk.push_pnl(states, COINS))
                    elif action.kind == "retire":
                        await execute_retire(exchange, state, cfg, log)
                        # PnL moved active -> retired -- refresh Blynk with the new split
                        asyncio.create_task(blynk.push_pnl(states, COINS))

                save_state(symbol, state)
            except ccxt.NetworkError as e:
                log.warning("Network error: %s", e)
            except ccxt.ExchangeError as e:
                log.error("Exchange error: %s", e)
    except asyncio.CancelledError:
        log.info("Coin task cancelled")
        raise
    finally:
        save_state(symbol, state)
