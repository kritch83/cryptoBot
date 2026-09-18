"""Crypto grid/DCA bot (CCXT Pro WebSocket on Kraken) -- multi-coin.

All coins and their tunables live in config.py under COINS. Each coin runs
as its own asyncio task with its own ticker stream, state file, and trail
state machines. Paper-mode USD is a shared pool (wallet.json) across all
coins.

Run paper by default:
    python conductor.py
Run live (real orders):
    python conductor.py --live
Add --dry-run alongside --live to exercise the live code path without
sending real orders. --simulate CSV replays prices for the first
enabled coin only.
"""
from __future__ import annotations

import argparse
import asyncio
import http.client
import logging
import sys
import time
import urllib.parse
from typing import Optional

import ccxt
import ccxt.pro as ccxtpro

from helpers import dashboard
from helpers.actions import (
    ACTION_BY_KEY,
    ACTIONS,
    REASONS,
    VALUE_KINDS,
    VALUE_LABELS,
    check_stop_loss,
    check_target_sell,
)
from helpers.blynk import BlynkClient, blynk_heartbeat
from helpers.config import (
    COINS,
    DASHBOARD_ENABLED,
    DASHBOARD_HOST,
    DASHBOARD_PORT,
    EXCHANGE_ID,
    KRAKEN_API_KEY,
    KRAKEN_API_SECRET,
    LOG_FILE,
    PAPER_STARTING_USD,
    PUSHOVER_ENABLED,
    PUSHOVER_SOUND,
    PUSHOVER_TOKEN,
    PUSHOVER_USER,
)
from helpers.coin_runner import run_coin, install_private_call_serialization, reconcile_all_to_exchange
from helpers.config_reload import apply_changes, diff_coins, format_value, parse_config_coins
from helpers.retired import total_retired
from helpers.state import PendingAction, State, load_state, save_state
from helpers.wallet import PaperWallet

# Runtime mode -- set in main() from the --live flag. "paper" until proven otherwise.
MODE = "paper"


# --- Notifications ----------------------------------------------------------

def _pushover_send(message: str) -> None:
    """Blocking Pushover POST. Failures are logged, never raised."""
    if not PUSHOVER_TOKEN or not PUSHOVER_USER:
        logging.warning("Pushover creds missing in .env (PUSHOVER_TOKEN/PUSHOVER_USER) -- skipping push")
        return
    try:
        conn = http.client.HTTPSConnection("api.pushover.net:443", timeout=5)
        conn.request(
            "POST", "/1/messages.json",
            urllib.parse.urlencode({
                "token": PUSHOVER_TOKEN,
                "user": PUSHOVER_USER,
                "message": message,
                "sound": PUSHOVER_SOUND,
            }),
            {"Content-type": "application/x-www-form-urlencoded"},
        )
        resp = conn.getresponse()
        if resp.status != 200:
            logging.warning("Pushover returned %d: %s", resp.status, resp.read()[:200])
        conn.close()
    except Exception as e:
        logging.warning("Pushover send failed: %s", e)


async def push_notify(message: str) -> None:
    """Async wrapper -- runs the blocking HTTP call in a thread so the event loop keeps ticking."""
    if not PUSHOVER_ENABLED:
        return
    await asyncio.to_thread(_pushover_send, message)


# --- Kraken balance refresher (live mode only) ------------------------------

KRAKEN_BALANCE_REFRESH_SEC = 30.0


async def kraken_balance_refresher(exchange, holder: dict, enabled: list) -> None:
    """Periodically poll exchange.fetch_balance() and store balances into holder.

    holder is a shared dict {"usd": Optional[float], "coins": dict, "ts": float}
    read by the keypress-driven status screens. "coins" maps each base asset
    (e.g. "TAO") to its TOTAL balance on the exchange -- the *true* wallet
    holding (free + tied up in open orders), as opposed to state.total_qty_coin
    which is only the bot's current open grid position. "other" maps every
    remaining non-zero asset (stablecoins, coins the bot doesn't trade, staked
    balances) to {"qty", "value"} so the account total matches Kraken's; value
    is None when there's no USD market to price it. `enabled` is read each pass
    so coins hot-added from the dashboard are picked up. The loop never raises --
    errors are logged and the holder is left at its last-good value (or
    None/empty if never fetched).
    """
    while True:
        try:
            bal = await exchange.fetch_balance()
            usd_block = bal.get("USD") or bal.get("ZUSD") or {}
            free = usd_block.get("free")
            if free is None:
                # Fall back to the top-level 'free' dict ccxt sometimes uses
                free = (bal.get("free") or {}).get("USD")
            if free is not None:
                holder["usd"] = float(free)
            # Per-coin TOTAL balances -- the real wallet holding for each base.
            coins: dict = {}
            total_map = bal.get("total") or {}
            bases = {c["symbol"].split("/")[0] for c in enabled}
            for base in bases:
                blk = bal.get(base) or {}
                tot = blk.get("total")
                if tot is None:
                    tot = total_map.get(base)
                if tot is not None:
                    coins[base] = float(tot)
            holder["coins"] = coins
            holder["other"] = await _value_other_assets(exchange, total_map, bases)
            holder["ts"] = time.time()
        except ccxt.NetworkError as e:
            logging.warning("Kraken balance fetch network error: %s", e)
        except Exception as e:
            logging.warning("Kraken balance fetch failed: %s", e)
        await asyncio.sleep(KRAKEN_BALANCE_REFRESH_SEC)


STABLE_USD = {"USDT", "USDC", "DAI", "PYUSD", "USDG", "RLUSD", "USDS", "TUSD", "USDP"}


async def _value_other_assets(exchange, total_map: dict, bases: set) -> dict:
    """{asset: {"qty", "value"}} for held assets outside USD and the bot's coins.

    Staked/earn balances ("ADA.S", "DOT.F") are priced off their base asset.
    A failed ticker fetch falls back to $1 for stablecoins and None otherwise,
    so one bad market never blanks the whole account total.
    """
    held = {a: float(q) for a, q in total_map.items()
            if q and a not in ("USD", "ZUSD") and a not in bases}
    if not held:
        return {}
    underlying = {a: a.split(".")[0] for a in held}
    symbols = sorted({f"{u}/USD" for u in underlying.values()
                      if f"{u}/USD" in (exchange.markets or {})})
    prices: dict = {}
    if symbols:
        try:
            for sym, t in (await exchange.fetch_tickers(symbols)).items():
                if t.get("last"):
                    prices[sym.split("/")[0]] = float(t["last"])
        except Exception as e:
            logging.warning("Pricing non-grid assets failed: %s", e)
    out = {}
    for a, q in held.items():
        px = prices.get(underlying[a]) or (1.0 if underlying[a] in STABLE_USD else None)
        out[a] = {"qty": q, "value": None if px is None else q * px}
    return out


def _kraken_usd_line(balance_holder: Optional[dict], label_width: int) -> Optional[str]:
    """Render the 'Kraken USD' status line, or None if it shouldn't show."""
    if not balance_holder or balance_holder.get("usd") is None:
        return None
    usd = balance_holder["usd"]
    age = max(0, int(time.time() - balance_holder.get("ts", 0.0)))
    return f"{'Kraken USD:':<{label_width}}${usd:,.2f}  ({age}s ago)"


def _kraken_coin_line(balance_holder: Optional[dict], base: str, tracked_qty: float,
                      label_width: int) -> Optional[str]:
    """Render the real Kraken wallet balance for `base` next to the bot-tracked
    grid qty and the untracked remainder. None if no balance has been fetched.

    `tracked_qty` is state.total_qty_coin -- only the bot's open grid position.
    The wallet total can be larger (coins held outside the current grid: prior
    holdings, manual buys, leftovers from 'clear stats') -- that gap is what
    shows up as 'untracked'.
    """
    if not balance_holder:
        return None
    total = (balance_holder.get("coins") or {}).get(base)
    if total is None:
        return None
    age = max(0, int(time.time() - balance_holder.get("ts", 0.0)))
    untracked = total - tracked_qty
    return (
        f"{('Kraken ' + base + ':'):<{label_width}}{total:.8f}  "
        f"(grid {tracked_qty:.8f}, {untracked:+.8f} untracked, {age}s ago)"
    )


# --- Big ASCII banner (for the action-menu coin name) -----------------------
# 5-row block font. Dependency-free so it works on any terminal. Covers A-Z,
# 0-9 and '/' -- everything that appears in a Kraken pair symbol.
_BANNER_FONT = {
    "A": [" ## ", "#  #", "####", "#  #", "#  #"],
    "B": ["### ", "#  #", "### ", "#  #", "### "],
    "C": [" ###", "#   ", "#   ", "#   ", " ###"],
    "D": ["### ", "#  #", "#  #", "#  #", "### "],
    "E": ["####", "#   ", "### ", "#   ", "####"],
    "F": ["####", "#   ", "### ", "#   ", "#   "],
    "G": [" ###", "#   ", "# ##", "#  #", " ###"],
    "H": ["#  #", "#  #", "####", "#  #", "#  #"],
    "I": ["###", " # ", " # ", " # ", "###"],
    "J": ["####", "  # ", "  # ", "# # ", " ## "],
    "K": ["#  #", "# # ", "##  ", "# # ", "#  #"],
    "L": ["#   ", "#   ", "#   ", "#   ", "####"],
    "M": ["#   #", "## ##", "# # #", "#   #", "#   #"],
    "N": ["#  #", "## #", "# ##", "#  #", "#  #"],
    "O": [" ## ", "#  #", "#  #", "#  #", " ## "],
    "P": ["### ", "#  #", "### ", "#   ", "#   "],
    "Q": [" ## ", "#  #", "# ##", "#  #", " ###"],
    "R": ["### ", "#  #", "### ", "# # ", "#  #"],
    "S": [" ###", "#   ", " ## ", "   #", "### "],
    "T": ["#####", "  #  ", "  #  ", "  #  ", "  #  "],
    "U": ["#  #", "#  #", "#  #", "#  #", " ## "],
    "V": ["#   #", "#   #", "#   #", " # # ", "  #  "],
    "W": ["#   #", "#   #", "# # #", "## ##", "#   #"],
    "X": ["#  #", " ## ", " ## ", " ## ", "#  #"],
    "Y": ["#   #", " # # ", "  #  ", "  #  ", "  #  "],
    "Z": ["####", "  # ", " #  ", "#   ", "####"],
    "0": [" ## ", "#  #", "#  #", "#  #", " ## "],
    "1": [" # ", "## ", " # ", " # ", "###"],
    "2": ["### ", "   #", " ## ", "#   ", "####"],
    "3": ["### ", "   #", " ## ", "   #", "### "],
    "4": ["#  #", "#  #", "####", "   #", "   #"],
    "5": ["####", "#   ", "### ", "   #", "### "],
    "6": [" ###", "#   ", "### ", "#  #", " ## "],
    "7": ["####", "   #", "  # ", " #  ", " #  "],
    "8": [" ## ", "#  #", " ## ", "#  #", " ## "],
    "9": [" ## ", "#  #", " ###", "   #", "### "],
    "/": ["   #", "  # ", " #  ", "#   ", "#   "],
    "-": ["    ", "    ", "####", "    ", "    "],
    " ": ["  ", "  ", "  ", "  ", "  "],
}


def big_banner(text: str, gap: int = 2) -> str:
    """Render `text` as a 5-line ASCII block banner. Unknown chars render blank."""
    rows = ["", "", "", "", ""]
    spacer = " " * gap
    for ch in text.upper():
        glyph = _BANNER_FONT.get(ch, _BANNER_FONT[" "])
        w = max(len(r) for r in glyph)
        for i in range(5):
            rows[i] += glyph[i].ljust(w) + spacer
    return "\n".join(r.rstrip() for r in rows)


# --- Retro "80s synthwave" banner -------------------------------------------
# Sunset gradient (top->bottom): yellow -> orange -> hot pink -> magenta-purple.
# Neon-cyan double-line border. 256-color ANSI; set BANNER_RETRO=False to fall
# back to the plain monochrome banner on terminals without color.
_RETRO_ROW_COLORS = [226, 214, 208, 199, 165]
_RETRO_BORDER = 51   # neon cyan
BANNER_RETRO = True


def _ansi(code: int, s: str) -> str:
    return f"\x1b[1;38;5;{code}m{s}\x1b[0m"


def retro_banner(text: str) -> str:
    """80s synthwave banner: neon-cyan box + sunset-gradient solid-block letters."""
    body = big_banner(text, gap=1).replace("#", "█").split("\n")
    width = max((len(r) for r in body), default=0)
    bar = _ansi(_RETRO_BORDER, "║")
    top = _ansi(_RETRO_BORDER, "╔" + "═" * (width + 2) + "╗")
    bot = _ansi(_RETRO_BORDER, "╚" + "═" * (width + 2) + "╝")
    out = [top]
    for i, r in enumerate(body):
        color = _RETRO_ROW_COLORS[min(i, len(_RETRO_ROW_COLORS) - 1)]
        out.append(f"{bar} {_ansi(color, r.ljust(width))} {bar}")
    out.append(bot)
    return "\n".join(out)


def menu_banner(text: str) -> str:
    """Banner used by the action menu -- retro by default, plain as a fallback."""
    return retro_banner(text) if BANNER_RETRO else "\n" + big_banner(text) + "\n"


# --- Stats display ----------------------------------------------------------

def _status_tags(st: State, cfg: dict) -> list:
    """Compact status tags for the summary views (all-coins 'a' table + the
    startup banner). Per side, a resting limit order takes precedence over an
    armed trail -- mirroring the 'Next BUY'/'Next SELL' precedence in
    format_stats. 'UP' = sell-trail riding the high up (fires on a pullback);
    'DN' = buy-trail riding the low down (fires on a rebound); '->$' is the
    live trigger price the trail will fire at (same math as format_stats)."""
    pp = cfg["price_prec"]
    tags = []
    if st.paused:
        tags.append("[PAUSED]")
    if st.buys_paused:
        tags.append("[BUYS-PAUSED]")
    if st.pending_buy_order is not None:
        tags.append(f"[BUY LIMIT @${st.pending_buy_order.limit_price:,.{pp}f}]")
    elif st.trailing_buy.armed:
        man = " MAN" if st.trailing_buy.manual else ""
        ext = st.trailing_buy.extreme
        if ext is not None:
            fire = ext * (1 + cfg["trail_buy_pct"])
            tags.append(f"[BUY-TRAIL DN{man} ->${fire:,.{pp}f}]")
        else:
            tags.append(f"[BUY-TRAIL DN{man}]")
    if st.pending_sell_order is not None:
        lbl = "TARGET" if st.pending_sell_order.manual else "SELL LIMIT"
        tags.append(f"[{lbl} @${st.pending_sell_order.limit_price:,.{pp}f}]")
    elif st.trailing_sell.armed:
        man = " MAN" if st.trailing_sell.manual else ""
        ext = st.trailing_sell.extreme
        if ext is not None:
            fire = ext * (1 - cfg["trail_sell_pct"])
            tags.append(f"[SELL-TRAIL UP{man} ->${fire:,.{pp}f}]")
        else:
            tags.append(f"[SELL-TRAIL UP{man}]")
    if st.breakeven_exit_armed:
        tags.append("[BE-EXIT]")
    if st.pause_after_sell:
        tags.append("[PAUSE-AFTER-SELL]")
    if st.stop_loss_pct:
        if st.avg_entry_price:
            tags.append(f"[SL ->${st.avg_entry_price * (1 - st.stop_loss_pct):,.{pp}f}]")
        else:
            tags.append(f"[SL -{st.stop_loss_pct:.1%}]")
    return tags


def format_stats(state: State, cfg: dict, last_price: Optional[float], wallet: PaperWallet,
                 balance_holder: Optional[dict] = None) -> str:
    pp              = cfg["price_prec"]
    base            = cfg["symbol"].split("/")[0]
    max_grid_levels = cfg["max_grid_levels"]
    drop_pct        = cfg["drop_pct"]
    trail_buy_pct   = cfg["trail_buy_pct"]
    trail_sell_pct  = cfg["trail_sell_pct"]
    take_profit_pct = cfg["take_profit_pct"]

    L = 17  # label column width -- aligns all "Label:" values into one column

    bar = "=" * 60
    out = [bar, f"STATS  {cfg['symbol']}", bar]

    mode_line = state.mode.upper()
    if state.paused:
        mode_line += "  [PAUSED]"
    if state.breakeven_exit_armed:
        mode_line += "  [BE-EXIT]"
    if state.pause_after_sell:
        mode_line += "  [PAUSE-AFTER-SELL]"
    if state.buys_paused:
        mode_line += "  [BUYS-PAUSED]"
    if state.stop_loss_pct:
        mode_line += f"  [SL -{state.stop_loss_pct:.2%}]"
    out.append(f"{'Mode:':<{L}}{mode_line}")
    out.append(f"{'Cycle:':<{L}}{state.cycle_count}")
    out.append(f"{'Realized PnL:':<{L}}${state.realized_pnl_usd:+,.{pp}f}")
    if last_price is not None:
        out.append(f"{'Current price:':<{L}}${last_price:,.{pp}f}")
    if PAPER_STARTING_USD != 0:
        out.append(
            f"{'Paper wallet:':<{L}}${wallet.usd:,.{pp}f} USD (shared) "
            f"+ {state.paper_wallet_coin:.8f} {base}"
        )
    kraken_line = _kraken_usd_line(balance_holder, L)
    if kraken_line:
        out.append(kraken_line)
    coin_line = _kraken_coin_line(balance_holder, base, state.total_qty_coin, L)
    if coin_line:
        out.append(coin_line)
    out.append("")

    if state.positions:
        if last_price is not None and state.avg_entry_price:
            unreal = (last_price - state.avg_entry_price) * state.total_qty_coin
            unreal_pct = (last_price / state.avg_entry_price - 1) * 100
            unreal_val = f"${unreal:+,.{pp}f} ({unreal_pct:+.5f}%)"
        else:
            unreal_val = "(no price yet)"
        out.append(f"{'Open positions:':<{L}}{len(state.positions)} / {max_grid_levels}")
        out.append(f"{'  Avg entry:':<{L}}${state.avg_entry_price:,.{pp}f}")
        out.append(
            f"{'  Total cost:':<{L}}${state.total_cost_usd:,.{pp}f}  "
            f"({state.total_qty_coin:.8f} {base})"
        )
        out.append(f"{'  Last buy:':<{L}}${state.last_buy_price:,.{pp}f}")
        out.append(f"{'  Unrealized:':<{L}}{unreal_val}")
    else:
        out.append(f"{'Open positions:':<{L}}0  (waiting for initial entry)")
        if state.initial_entry_high is not None:
            out.append(f"{'  Running high:':<{L}}${state.initial_entry_high:,.{pp}f}")

    def delta_str(target: float) -> str:
        if last_price is None or last_price == 0:
            return ""
        d = (target / last_price - 1) * 100
        return f"  ({d:+.5f}% from now)"

    out.append("")
    out.append("Next BUY:")
    if state.buys_paused:
        out.append(f"{'  Status:':<{L}}buying PAUSED -- no new buys; sells carry on (menu b resumes)")
    elif state.pending_buy_order is not None:
        pbo = state.pending_buy_order
        offset_pct = cfg.get("limit_buy_offset_pct", 0.001)
        cancel_buffer = max(trail_buy_pct, offset_pct)
        cancel = pbo.placed_at_price * (1 + cancel_buffer)
        out.append(f"{'  Pending LIMIT BUY:':<{L}}@ ${pbo.limit_price:,.{pp}f}  qty={pbo.qty_coin:.8f} {cfg['symbol'].split('/')[0]}  level={pbo.level}")
        out.append(f"{'  Fills when:':<{L}}ask <= ${pbo.limit_price:,.{pp}f}{delta_str(pbo.limit_price)}")
        out.append(f"{'  Cancels when:':<{L}}price > ${cancel:,.{pp}f}{delta_str(cancel)}")
    elif state.trailing_buy.armed and state.trailing_buy.extreme is not None:
        fire = state.trailing_buy.extreme * (1 + trail_buy_pct)
        if state.trailing_buy.manual:
            what = "grid add" if state.positions else "initial entry"
            out.append(f"{'  Trail:':<{L}}ARMED (manual dip-buy -- fires ONE {what}, level {len(state.positions)})  (low so far ${state.trailing_buy.extreme:,.{pp}f})")
            out.append(f"{'  Fires at:':<{L}}${fire:,.{pp}f}{delta_str(fire)}  (then normal grid resumes)")
        else:
            out.append(f"{'  Trail:':<{L}}ARMED  (low so far ${state.trailing_buy.extreme:,.{pp}f})")
            out.append(f"{'  Fires at:':<{L}}${fire:,.{pp}f}{delta_str(fire)}")
    elif len(state.positions) >= max_grid_levels:
        out.append(f"{'  Status:':<{L}}grid full ({max_grid_levels} levels) -- no more buys until sell-all")
    elif state.positions and state.last_buy_price is not None:
        arm = state.last_buy_price * (1 - drop_pct)
        out.append(f"{'  Will arm at:':<{L}}${arm:,.{pp}f}{delta_str(arm)}")
        out.append(f"{'  Source:':<{L}}-{drop_pct:.1%} from last buy ${state.last_buy_price:,.{pp}f}")
    elif not state.positions and state.initial_entry_high is not None:
        arm = state.initial_entry_high * (1 - drop_pct)
        out.append(f"{'  Will arm at:':<{L}}${arm:,.{pp}f}{delta_str(arm)}")
        out.append(f"{'  Source:':<{L}}-{drop_pct:.1%} from observed high ${state.initial_entry_high:,.{pp}f}")
    else:
        out.append(f"{'  Status:':<{L}}waiting for first price tick...")

    out.append("")
    out.append("Next SELL:")
    if state.pending_sell_order is not None:
        pso = state.pending_sell_order
        if pso.manual:
            out.append(f"{'  Pending TARGET SELL:':<{L}}@ ${pso.limit_price:,.{pp}f}  qty={pso.qty_coin:.8f} {cfg['symbol'].split('/')[0]}  (manual, menu p)")
            out.append(f"{'  Fills when:':<{L}}bid >= ${pso.limit_price:,.{pp}f}{delta_str(pso.limit_price)}")
            out.append(f"{'  Cancels when:':<{L}}never (rests until filled, or cleared via menu p -> 0); grid buys frozen; pauses on fill")
        else:
            cancel = pso.limit_price * (1 - trail_sell_pct)
            out.append(f"{'  Pending LIMIT SELL:':<{L}}@ ${pso.limit_price:,.{pp}f}  qty={pso.qty_coin:.8f} {cfg['symbol'].split('/')[0]}")
            out.append(f"{'  Fills when:':<{L}}bid >= ${pso.limit_price:,.{pp}f}{delta_str(pso.limit_price)}")
            out.append(f"{'  Cancels when:':<{L}}price < ${cancel:,.{pp}f}{delta_str(cancel)}")
    elif state.trailing_sell.armed and state.trailing_sell.extreme is not None:
        fire = state.trailing_sell.extreme * (1 - trail_sell_pct)
        if state.trailing_sell.manual and state.avg_entry_price is not None:
            avg = state.avg_entry_price
            out.append(f"{'  Trail:':<{L}}ARMED (manual stop -- floor avg ${avg:,.{pp}f}, no loss)  (high so far ${state.trailing_sell.extreme:,.{pp}f})")
            out.append(f"{'  Fires at:':<{L}}${fire:,.{pp}f}{delta_str(fire)}  (clamped: only sells >= ${avg:,.{pp}f})")
        else:
            out.append(f"{'  Trail:':<{L}}ARMED  (high so far ${state.trailing_sell.extreme:,.{pp}f})")
            out.append(f"{'  Fires at:':<{L}}${fire:,.{pp}f}{delta_str(fire)}")
    elif state.positions and state.avg_entry_price is not None:
        from helpers.coin_runner import sell_threshold
        arm, source_label, _ = sell_threshold(state, cfg)
        out.append(f"{'  Will arm at:':<{L}}${arm:,.{pp}f}{delta_str(arm)}")
        out.append(f"{'  Source:':<{L}}{source_label} (avg ${state.avg_entry_price:,.{pp}f})")
    else:
        out.append(f"{'  Status:':<{L}}N/A (no open positions)")

    if state.stop_loss_pct:
        out.append("")
        if state.avg_entry_price:
            trig = state.avg_entry_price * (1 - state.stop_loss_pct)
            out.append(f"{'Stop-loss:':<{L}}-{state.stop_loss_pct:.2%} below avg -> "
                       f"market-sell ALL + pause at ${trig:,.{pp}f}{delta_str(trig)}")
        else:
            out.append(f"{'Stop-loss:':<{L}}-{state.stop_loss_pct:.2%} below avg entry "
                       f"(arms when a position opens)")

    out.append(bar)
    return "\n".join(out)


def format_config(cfg: dict) -> str:
    L = 21  # label column width

    bar = "=" * 60
    out = [bar, f"CONFIG  {cfg['symbol']}", bar]

    out.append(f"{'Symbol:':<{L}}{cfg['symbol']}")
    out.append(f"{'Enabled:':<{L}}{cfg.get('enabled', True)}")
    out.append(f"{'Price precision:':<{L}}{cfg['price_prec']} decimals")
    out.append("")
    out.append("Buy settings:")
    out.append(f"{'  USD per buy:':<{L}}${cfg['usd_per_buy']:,.2f}")
    out.append(f"{'  Max grid levels:':<{L}}{cfg['max_grid_levels']}")
    out.append(f"{'  Drop pct:':<{L}}{cfg['drop_pct']:.3%}  (spacing between grid buys)")
    out.append(f"{'  Trail buy pct:':<{L}}{cfg['trail_buy_pct']:.3%}  (rebound off low to fire buy)")
    buy_order_type = cfg.get("buy_order_type", "market")
    out.append(f"{'  Buy order type:':<{L}}{buy_order_type}  (menu 7 'force BUY' is always market)")
    if buy_order_type == "limit":
        buy_offset = cfg.get("limit_buy_offset_pct", 0.001)
        out.append(f"{'  Buy limit offset:':<{L}}{buy_offset:.3%}  (limit_price = bid * (1-offset))")
    out.append("")
    out.append("Sell settings:")
    out.append(f"{'  Take profit pct:':<{L}}{cfg['take_profit_pct']:.3%}  (above avg entry to arm sell-trail)")
    out.append(f"{'  Trail sell pct:':<{L}}{cfg['trail_sell_pct']:.3%}  (pullback off high to fire sell)")
    order_type = cfg.get("order_type", "market")
    out.append(f"{'  Sell order type:':<{L}}{order_type}  (menu 9 'sell ALL' is always market)")
    if order_type == "limit":
        offset = cfg.get("limit_sell_offset_pct", 0.001)
        out.append(f"{'  Sell limit offset:':<{L}}{offset:.3%}  (limit_price = last * (1+offset))")
    out.append("")
    out.append(f"{'Blynk pin:':<{L}}{cfg.get('blynk_pin', '-')}")
    out.append(f"{'Max grid spend:':<{L}}${cfg['max_grid_levels'] * cfg['usd_per_buy']:,.2f}")

    out.append(bar)
    return "\n".join(out)


def format_all_stats(states: dict, cfgs: list, last_price: dict, wallet: PaperWallet,
                     balance_holder: Optional[dict] = None) -> str:
    bar = "=" * 60
    out = [bar, "ALL COINS", bar]

    L = 23  # label width for the summary section
    total_realized = sum(s.realized_pnl_usd for s in states.values())
    total_unreal = 0.0
    for c in cfgs:
        sym = c["symbol"]
        st = states.get(sym)
        if st and st.avg_entry_price and last_price.get(sym):
            total_unreal += (last_price[sym] - st.avg_entry_price) * st.total_qty_coin
    if PAPER_STARTING_USD != 0:
        out.append(f"{'Paper wallet (shared):':<{L}}${wallet.usd:,.2f} USD")
    kraken_line = _kraken_usd_line(balance_holder, L)
    if kraken_line:
        out.append(kraken_line)
    retired_total = total_retired()
    out.append(f"{'Retired PnL:':<{L}}${retired_total:+,.2f}")
    out.append(f"{'Total realized PnL:':<{L}}${total_realized + retired_total:+,.2f}")
    out.append(f"{'Total unrealized:':<{L}}${total_unreal:+,.2f}")
    out.append("")

    rows = []
    for i, c in enumerate(cfgs, start=1):
        sym = c["symbol"]
        st = states.get(sym)
        if not st:
            continue
        pp = c["price_prec"]
        base = sym.split("/")[0]
        lp = last_price.get(sym)
        if st.positions and st.avg_entry_price and lp:
            u = (lp - st.avg_entry_price) * st.total_qty_coin
            unreal = f"${u:+,.2f}"
        elif not st.positions:
            unreal = "-"
        else:
            unreal = "?"
        status_tags = _status_tags(st, c)
        wal = (balance_holder.get("coins") or {}).get(base) if balance_holder else None
        rows.append({
            "n":       f"{i})",
            "sym":     sym,
            "pos":     f"{len(st.positions)}/{c['max_grid_levels']}",
            "real":    f"${st.realized_pnl_usd:+,.{pp}f}",
            "cycle":   str(st.cycle_count),
            "avg":     f"${st.avg_entry_price:,.{pp}f}" if st.avg_entry_price else "-",
            "qty":     f"{st.total_qty_coin:.8f}" if st.positions else "-",
            "wallet":  f"{wal:.8f}" if wal is not None else "-",
            "price":   f"${lp:,.{pp}f}" if lp else "?",
            "unreal":  unreal,
            "status":  " ".join(status_tags),
        })

    if rows:
        w_n      = max(len(r["n"])      for r in rows)
        w_sym    = max(len("Symbol"),     *(len(r["sym"])    for r in rows))
        w_pos    = max(len("Pos"),        *(len(r["pos"])    for r in rows))
        w_real   = max(len("Realized"),   *(len(r["real"])   for r in rows))
        w_cycle  = max(len("Cycle"),      *(len(r["cycle"])  for r in rows))
        w_avg    = max(len("Avg entry"),  *(len(r["avg"])    for r in rows))
        w_qty    = max(len("Grid"),       *(len(r["qty"])    for r in rows))
        w_wallet = max(len("Wallet"),     *(len(r["wallet"]) for r in rows))
        w_price  = max(len("Price"),      *(len(r["price"])  for r in rows))
        w_unreal = max(len("Unrealized"), *(len(r["unreal"]) for r in rows))

        out.append(
            f"  {'':<{w_n}} {'Symbol':<{w_sym}}  "
            f"{'Cycle':>{w_cycle}}  {'Pos':>{w_pos}}  "
            f"{'Grid':<{w_qty}}  {'Wallet':<{w_wallet}}  {'Price':>{w_price}}  "
            f"{'Avg entry':>{w_avg}}  {'Unrealized':>{w_unreal}}  "
            f"{'Realized':>{w_real}}  Status"
        )
        for r in rows:
            out.append(
                f"  {r['n']:<{w_n}} {r['sym']:<{w_sym}}  "
                f"{r['cycle']:>{w_cycle}}  {r['pos']:>{w_pos}}  "
                f"{r['qty']:<{w_qty}}  {r['wallet']:<{w_wallet}}  {r['price']:>{w_price}}  "
                f"{r['avg']:>{w_avg}}  {r['unreal']:>{w_unreal}}  "
                f"{r['real']:>{w_real}}  {r['status']}"
            )

    out.append(bar)
    return "\n".join(out)


# --- Keypress (TTY single-char read) ---------------------------------------

def _enable_keypress():
    if not sys.stdin.isatty():
        return None, None
    try:
        import termios
        import tty
    except ImportError:
        return None, None
    fd = sys.stdin.fileno()
    try:
        old = termios.tcgetattr(fd)
        tty.setcbreak(fd)
        return fd, old
    except Exception:
        return None, None


def _disable_keypress(fd, old) -> None:
    if fd is None or old is None:
        return
    try:
        import termios
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except Exception:
        pass


# --- Main loop --------------------------------------------------------------

async def run(simulate_file: Optional[str], dry_run: bool,
              dashboard_host: str, dashboard_port: int, dashboard_on: bool) -> None:
    exchange_cls = getattr(ccxtpro, EXCHANGE_ID)
    creds: dict = {"enableRateLimit": True}
    if MODE == "live":
        creds["apiKey"] = KRAKEN_API_KEY
        creds["secret"] = KRAKEN_API_SECRET
        if not creds["apiKey"] or not creds["secret"]:
            raise RuntimeError("MODE=live but KRAKEN_API_KEY/SECRET missing in .env")
    exchange = exchange_cls(creds)
    # All coin tasks share this one exchange + API key. Serialize private REST
    # calls behind one lock with a monotonic nonce so their concurrent requests
    # can't race into Kraken 'Invalid nonce' errors (the root cause of the
    # orphaned-order / tracked-vs-wallet drift).
    install_private_call_serialization(exchange)

    if not simulate_file:
        await exchange.load_markets()

    enabled = [c for c in COINS if c.get("enabled", True)]
    if not enabled:
        raise RuntimeError("No enabled coins in COINS -- nothing to do")
    if simulate_file and len(enabled) > 1:
        logging.warning(
            "--simulate runs only the first enabled coin (%s); skipping %d others",
            enabled[0]["symbol"], len(enabled) - 1,
        )
        enabled = enabled[:1]

    # Load per-coin state. Migration of legacy state.json happens inside load_state.
    states: dict[str, State] = {}
    for c in enabled:
        s = load_state(c["symbol"], mode=MODE)
        s.mode = MODE
        states[c["symbol"]] = s

    # Shared paper wallet: prefer wallet.json; else bootstrap from the first
    # coin's legacy paper_wallet_usd (preserved through state.json migration).
    bootstrap_usd = states[enabled[0]["symbol"]].paper_wallet_usd
    wallet = PaperWallet.load_or_init(PAPER_STARTING_USD, bootstrap_usd=bootstrap_usd)
    # The legacy per-state paper_wallet_usd field is no longer authoritative --
    # zero it so subsequent saves reflect the new shared-wallet model.
    for st in states.values():
        st.paper_wallet_usd = 0.0

    if MODE == "paper":
        max_spend = sum(c["max_grid_levels"] * c["usd_per_buy"] for c in enabled)
        if max_spend > wallet.usd + sum(s.total_cost_usd for s in states.values()):
            logging.warning(
                "Max combined grid spend across coins (${:.2f}) exceeds available paper USD".format(max_spend)
            )

    if dry_run:
        logging.warning("DRY RUN: live order calls will be skipped")

    # Reconcile every coin's tracked position to the real Kraken balance before
    # trading (live only). Keeps tracked == wallet across restarts; see
    # reconcile_all_to_exchange for the both-directions policy.
    if MODE == "live" and not simulate_file:
        await reconcile_all_to_exchange(exchange, states, enabled)

    blynk = BlynkClient()

    # Manual command plumbing
    cmd_queues: dict[str, asyncio.Queue] = {c["symbol"]: asyncio.Queue() for c in enabled}
    last_price: dict[str, Optional[float]] = {c["symbol"]: None for c in enabled}

    # Startup banner -- vertical coin list for easy digit selection
    rows = []
    for i, c in enumerate(enabled, start=1):
        sym = c["symbol"]
        st = states[sym]
        pp = c["price_prec"]
        rows.append({
            "n":      f"{i})",
            "sym":    sym,
            "pos":    f"{len(st.positions)}/{c['max_grid_levels']}",
            "cycle":  str(st.cycle_count),
            "avg":    f"${st.avg_entry_price:.{pp}f}" if st.avg_entry_price else "-",
            "status": " ".join(_status_tags(st, c)),
        })

    w_n     = max(len(r["n"])     for r in rows)
    w_sym   = max(len("Symbol"),    *(len(r["sym"])   for r in rows))
    w_pos   = max(len("Pos"),       *(len(r["pos"])   for r in rows))
    w_cycle = max(len("Cycle"),     *(len(r["cycle"]) for r in rows))
    w_avg   = max(len("Avg entry"), *(len(r["avg"])   for r in rows))

    logging.info("Coins:")
    logging.info(
        f"  {'':<{w_n}} {'Symbol':<{w_sym}}  "
        f"{'Cycle':>{w_cycle}}  {'Pos':>{w_pos}}  "
        f"{'Avg entry':>{w_avg}}  Status"
    )
    for r in rows:
        logging.info(
            f"  {r['n']:<{w_n}} {r['sym']:<{w_sym}}  "
            f"{r['cycle']:>{w_cycle}}  {r['pos']:>{w_pos}}  "
            f"{r['avg']:>{w_avg}}  {r['status']}"
        )
    logging.info("Keys: <number> then ENTER selects a coin (opens its action menu).  'a' = all-coin stats.  'c' = reload config tunables.  '?' = help.")

    # Keypress listener state
    selected_idx: list = [None]    # coin index whose action menu is open (None = idle)
    confirm_cmd:  list = [None]    # (idx, kind, label) awaiting Y/N confirmation
    pending_digits: list = [""]    # buffered coin-number digits (idle); ENTER commits
    pending_reload: list = [None]  # ReloadPlan parsed at 'c' preview time, awaiting Y
    val_entry:   list = [None]     # (coin idx, kind) while typing a value (stop-loss % / target $)
    val_digits:  list = [""]       # buffered value chars (digits + '.')
    pending_val: list = [None]     # parsed value awaiting Y-confirm (0.0 = clear)
    balance_holder: dict = {"usd": None, "coins": {}, "ts": 0.0}

    # Action menu, value kinds and reasons all come from helpers/actions.py so
    # the web dashboard offers exactly the same commands with the same wording.
    # Digits 1-9 are the common actions; the tail uses letters ('a'/'t'/'r'/'c').
    # Note 'a' and 'c' also mean something at the IDLE screen (all-coin stats /
    # reload config), but the action menu is a separate input context (a coin is
    # selected), so there is no clash. Actions ask Y to confirm except live
    # stats and config.

    HELP_TEXT = (
        "Controls:\n"
        "  <number>  type it then ENTER -> select a coin (1-{n}), opens its action menu\n"
        "  a         all-coin summary\n"
        "  c         reload config.py tunables into running coins (preview, then Y to apply;\n"
        "            added/removed/enabled-flipped coins still need a restart)\n"
        "  ?         this help\n"
        "Action menu (after selecting a coin), type the key:\n"
        "  1 stats      2 config             3 set/clear stop-loss   4 pause\n"
        "  5 breakeven  6 pause-after-sell   7 BUY now               8 arm buy-trail\n"
        "  9 sell ALL now (market)           p sell ALL at MY price (target)\n"
        "  a toggle sell-trail (arm/disarm)   b toggle pause buying (sells carry on)\n"
        "  t clear targets   r RETIRE coin   c clear stats (full reset)\n"
        "  8 buy-trail: trailing dip-buy -- arms at current price, trails the low, fires ONE\n"
        "    buy on a +trail_buy_pct rebound (works flat or holding; disarm via t)\n"
        "  3 stop-loss: % below avg entry (tracks avg as the grid grows); on hit market-sells\n"
        "    ALL + pauses + disarms (one-shot; survives restarts; 0/empty clears)\n"
        "  p target sell: type an absolute price; places a real post-only limit sell for the\n"
        "    WHOLE position that never auto-cancels; grid buys freeze while it rests; coin\n"
        "    pauses when it fills (survives restarts; 0/empty clears; must be above market)\n"
        "  Every action asks 'Y' to confirm except 1 (stats) and 2 (config).  0/ENTER cancels.\n"
        "The web dashboard drives these same actions -- its URL is logged at startup.\n"
        "Ctrl-C to quit."
    ).format(n=len(enabled))

    def _action_menu(idx: int) -> str:
        sym = enabled[idx]["symbol"]
        lines = [menu_banner(sym), "  choose an action (type the key; 0/ENTER cancels):"]
        for act in ACTIONS:
            lines.append(f"   {act.key}) {act.label}")
        lines.append("  all actions ask 'Y' to confirm except 1) live stats and 2) config")
        return "\n".join(lines)

    def _enqueue(idx: int, kind: str, value: Optional[float] = None) -> None:
        sym = enabled[idx]["symbol"]
        reason = REASONS.get(kind)
        if reason is None:
            return
        cmd_queues[sym].put_nowait(PendingAction(kind, 0, reason, value=value))
        print(f"  queued: {sym} -> {kind}")

    def _commit_stop_loss(idx: int, sym: str, pct: float) -> None:
        """ENTER pressed in stop-loss value entry: validate, arm the Y-confirm."""
        st = states[sym]
        problem = check_stop_loss(pct, st)
        if problem is not None:
            print(f"  {sym}: {problem}")
            return
        if pct == 0:
            pending_val[0] = 0.0
            confirm_cmd[0] = (idx, "set_stop_loss", "clear stop-loss")
            print(f"  {sym}: CLEAR stop-loss (currently -{st.stop_loss_pct:.2%})."
                  f"  Type Y to confirm, any other key cancels")
            return
        frac = pct / 100.0
        pp = enabled[idx]["price_prec"]
        lp = last_price[sym]
        if st.avg_entry_price:
            trigger = st.avg_entry_price * (1 - frac)
            msg = (f"  {sym}: SET stop-loss -{pct:g}% below avg entry -> trigger "
                   f"${trigger:,.{pp}f} (avg ${st.avg_entry_price:,.{pp}f}"
                   + (f", now ${lp:,.{pp}f}" if lp is not None else "") + ")")
            if lp is not None and lp <= trigger:
                msg += "\n  !! price is ALREADY at/below the trigger -- it will fire on the next tick !!"
        else:
            msg = (f"  {sym}: SET stop-loss -{pct:g}% below avg entry "
                   f"(no position yet -- arms when one opens; tracks avg as the grid grows)")
        msg += ".  On hit: market-sell ALL + pause (one-shot).  Type Y to confirm, any other key cancels"
        print(msg)
        pending_val[0] = frac
        confirm_cmd[0] = (idx, "set_stop_loss", f"set stop-loss -{pct:g}%")

    def _commit_target_sell(idx: int, sym: str, price: float) -> None:
        """ENTER pressed in target-sell value entry: validate, arm the Y-confirm."""
        st  = states[sym]
        pp  = enabled[idx]["price_prec"]
        lp  = last_price[sym]
        pso = st.pending_sell_order
        problem = check_target_sell(price, st, lp)
        if problem is not None:
            print(f"  {sym}: {problem}")
            return
        if price == 0:
            pending_val[0] = 0.0
            confirm_cmd[0] = (idx, "set_target_sell", "clear target sell")
            print(f"  {sym}: CLEAR target sell (currently @ ${pso.limit_price:,.{pp}f}) -- cancels "
                  f"the resting order, disarms pause-after-sell, grid trading resumes."
                  f"  Type Y to confirm, any other key cancels")
            return
        qty  = st.total_qty_coin
        away = (price / lp - 1) * 100
        avg  = st.avg_entry_price
        avg_note = f", avg entry ${avg:,.{pp}f}" if avg is not None else ""
        replace_note = ""
        if pso is not None:
            what = "manual target" if pso.manual else "bot's auto limit-sell"
            replace_note = f"  (replaces the resting {what} @ ${pso.limit_price:,.{pp}f})\n"
        print(f"  {sym}: SET target sell @ ${price:,.{pp}f} ({away:+.2f}% from now ${lp:,.{pp}f}"
              f"{avg_note}) -- post-only limit for ~{qty:.8f} {sym.split('/')[0]} "
              f"(~${qty * price:,.2f} gross).\n{replace_note}"
              f"  Rests until filled or cleared -- never auto-cancels; grid buys FREEZE while it\n"
              f"  rests; coin PAUSES when it fills.  Type Y to confirm, any other key cancels")
        pending_val[0] = price
        confirm_cmd[0] = (idx, "set_target_sell", f"set target sell @ ${price:,.{pp}f}")

    def _preview_reload() -> None:
        """'c' key: parse helpers/config.py fresh, print a tunables diff vs the
        running coins plus needs-restart warnings, then arm the Y-confirm.
        Parse/diff failures abort with nothing changed. The plan is frozen at
        preview time: Y applies exactly what was shown, even if the file
        changes again before the confirm (what you saw is what you get)."""
        try:
            new_coins = parse_config_coins()
            plan = diff_coins(new_coins, enabled)
        except Exception as e:
            logging.warning("Config reload preview aborted: %s", e)
            print(f"  reload aborted -- {e}")
            return
        print("  RELOAD PREVIEW -- helpers/config.py (running coins' tunables only)")
        for change in plan.changes:
            print(f"    {change.symbol}:")
            for key, old_v, new_v in change.field_changes:
                print(f"      {key}: {format_value(old_v)} -> {format_value(new_v)}")
        for w in plan.warnings:
            print(f"    ! {w}")
        print("    note: module constants (fees, cooldown, fill timeout, Blynk/Pushover,"
              " paper wallet USD) load at startup -- changing those still needs a restart")
        if not plan.changes:
            print("  no tunable changes to apply.")
            return
        n_fields = sum(len(c.field_changes) for c in plan.changes)
        pending_reload[0] = plan
        confirm_cmd[0] = (None, "reload_config", "reload config tunables")
        print(f"  apply {n_fields} change(s) to {len(plan.changes)} coin(s)?"
              f"  Type Y to confirm, any other key cancels")

    def _apply_reload(plan) -> None:
        for line in apply_changes(plan):
            logging.info(line)          # audit trail -> data/grid_bot.log + console
        logging.info(
            "CONFIG RELOAD applied -- %d coin(s) re-tuned; running tasks pick the new "
            "values up on their next tick", len(plan.changes),
        )

    def _on_keypress() -> None:
        try:
            ch = sys.stdin.read(1)
        except Exception:
            return
        if not ch:
            return

        # 1) Awaiting Y/N confirmation of an action
        if confirm_cmd[0] is not None:
            idx, kind, label = confirm_cmd[0]
            confirm_cmd[0] = None
            if kind == "reload_config":   # global, applies locally -- no coin queue
                plan, pending_reload[0] = pending_reload[0], None
                if ch in ("y", "Y") and plan is not None:
                    _apply_reload(plan)
                else:
                    print(f"  cancelled: {label}")
                return
            if ch in ("y", "Y"):
                if kind in VALUE_KINDS:
                    val, pending_val[0] = pending_val[0], None
                    _enqueue(idx, kind, value=val)
                else:
                    _enqueue(idx, kind)
            else:
                pending_val[0] = None
                print(f"  cancelled: {label}")
            return

        # 1.5) Value entry (stop-loss % / target-sell $): digits/'.' buffer,
        #      ENTER commits, ESC/other cancels
        if val_entry[0] is not None:
            idx, kind = val_entry[0]
            sym = enabled[idx]["symbol"]
            unit, name = VALUE_LABELS[kind]
            if ch.isdigit() or ch == ".":
                val_digits[0] += ch
                print(f"  {sym} {unit}: {val_digits[0]}_  (ENTER sets, 0/empty clears, ESC cancels)")
                return
            if ch in ("\x7f", "\x08"):            # backspace/delete -- edit the number
                if val_digits[0]:
                    val_digits[0] = val_digits[0][:-1]
                print(f"  {sym} {unit}: {val_digits[0] or '(empty)'}")
                return
            if ch in ("\n", "\r"):                # ENTER -- commit the buffered value
                raw = val_digits[0]
                val_entry[0], val_digits[0] = None, ""
                try:
                    num = float(raw) if raw else 0.0        # empty = clear
                except ValueError:                          # ".", "1.2.3", ...
                    print(f"  invalid number '{raw}' -- {name} unchanged")
                    return
                if kind == "set_stop_loss":
                    _commit_stop_loss(idx, sym, num)
                else:
                    _commit_target_sell(idx, sym, num)
                return
            # ESC or any other key: cancel entry mode
            val_entry[0], val_digits[0] = None, ""
            print(f"  ({name} entry cancelled)")
            return

        # 2) A coin is selected -> its action menu is open; expect an action key
        if selected_idx[0] is not None:
            idx = selected_idx[0]
            sym = enabled[idx]["symbol"]
            if ch in ("\n", "\r", "0"):
                selected_idx[0] = None
                print("  (menu cancelled)")
                return
            key = ch.lower() if ch.isalpha() else ch
            action = ACTION_BY_KEY.get(key)
            if action is None:
                valid = "/".join(a.key for a in ACTIONS)
                print(f"  no action '{ch}' (valid: {valid}); 0/ENTER cancels")
                return
            label, kind, needs_confirm = action.label, action.kind, action.needs_confirm
            selected_idx[0] = None
            if kind in VALUE_KINDS:   # value entry first; Y-confirm comes after ENTER
                st = states[sym]
                if kind == "set_stop_loss":
                    cur = f"-{st.stop_loss_pct:.2%}" if st.stop_loss_pct else "none"
                    print(f"  {sym}: stop-loss as % below avg entry (current: {cur})."
                          f"  Type a number then ENTER; 0/empty clears; ESC cancels")
                else:
                    pp = enabled[idx]["price_prec"]
                    pso = st.pending_sell_order
                    cur = (f"@ ${pso.limit_price:,.{pp}f}"
                           if pso is not None and pso.manual else "none")
                    lp = last_price[sym]
                    now = f"; now ${lp:,.{pp}f}" if lp is not None else ""
                    print(f"  {sym}: target sell price in $ for the WHOLE position "
                          f"(current target: {cur}{now})."
                          f"  Type a price then ENTER; 0/empty clears; ESC cancels")
                val_entry[0], val_digits[0] = (idx, kind), ""
                return
            if not needs_confirm:   # shown immediately, no Y confirm: live stats & config
                if kind == "config":
                    print(format_config(enabled[idx]))
                else:
                    print(format_stats(states[sym], enabled[idx], last_price[sym], wallet, balance_holder))
                return
            # everything else asks for a Y confirmation first
            if kind == "clear_stats":
                st = states[sym]
                msg = f"  CLEAR {sym}: zeroes realized PnL + cycle"
                if st.positions:
                    base = sym.split("/")[0]
                    msg += (f" AND wipes {len(st.positions)} open position(s) "
                            f"({st.total_qty_coin:.8f} {base}) -- the bot will FORGET them "
                            f"(they stay on Kraken!)")
                msg += ".  Coin will be PAUSED after.  Type Y to confirm:"
                print(msg)
            elif kind == "retire":
                st = states[sym]
                msg = (f"  RETIRE {sym}: moves ${st.realized_pnl_usd:+,.2f} realized PnL "
                       f"+ {st.cycle_count} cycle(s) into data/retired_pnl.json, then FULL reset")
                if st.positions:
                    base = sym.split("/")[0]
                    msg += (f" AND wipes {len(st.positions)} open position(s) "
                            f"({st.total_qty_coin:.8f} {base}) -- the bot will FORGET them "
                            f"(they stay on Kraken!)")
                msg += (f".  Coin will be PAUSED after -- set \"enabled\": False for {sym} "
                        f"in helpers/config.py before the next restart.  Type Y to confirm:")
                print(msg)
            elif kind == "clear_targets":
                dp = enabled[idx].get("drop_pct", 0.0)
                print(
                    f"  {sym}: clear targets -- re-baseline first-buy to "
                    f"-{dp:.1%} from current price.  Type Y to confirm, any other key cancels"
                )
            else:
                print(f"  {sym}: {label} -- type Y to confirm, any other key cancels")
            confirm_cmd[0] = (idx, kind, label)
            return

        # 3) Idle -- type a coin number then ENTER; 'a' = all stats; '?' = help
        if ch.isdigit():
            pending_digits[0] += ch
            print(f"  coin {pending_digits[0]}_ (ENTER selects, BACKSPACE edits)")
            return
        if ch in ("\x7f", "\x08"):            # backspace/delete -- edit the number
            if pending_digits[0]:
                pending_digits[0] = pending_digits[0][:-1]
                print(f"  coin {pending_digits[0] or '(cleared)'}")
            return
        if ch in ("\n", "\r"):                # ENTER -- commit the buffered number
            if not pending_digits[0]:
                return
            n = int(pending_digits[0])
            pending_digits[0] = ""
            idx = n - 1
            if 0 <= idx < len(enabled):
                selected_idx[0] = idx
                print(_action_menu(idx))
            else:
                print(f"  no coin {n} (valid 1-{len(enabled)})")
            return
        # any other key: discard a partial number, then handle 'a'/'?'
        pending_digits[0] = ""
        if ch == "?":
            print(HELP_TEXT)
            return
        if ch in ("a", "A"):
            print(format_all_stats(states, enabled, last_price, wallet, balance_holder))
            return
        if ch in ("c", "C"):
            _preview_reload()
            return
        print(f"  type a coin number then ENTER (1-{len(enabled)}), 'a' = all-coin stats, 'c' = reload config, '?' = help")

    fd, old_term = _enable_keypress()
    loop = asyncio.get_event_loop()
    reader_attached = False
    if fd is not None:
        try:
            loop.add_reader(sys.stdin, _on_keypress)
            reader_attached = True
        except (NotImplementedError, OSError):
            _disable_keypress(fd, old_term)
            fd, old_term = None, None

    bypass_cooldown = bool(simulate_file)
    heartbeat_task = asyncio.create_task(blynk_heartbeat(blynk, states, enabled))
    balance_task = None
    if MODE == "live" and not simulate_file:
        balance_task = asyncio.create_task(kraken_balance_refresher(exchange, balance_holder, enabled))
    # Coin tasks live in a set rather than a fixed list, and the run loop waits
    # on an Event instead of one gather() over that list -- so a coin added at
    # runtime (dashboard "+ Add coin") can join the same supervision, be logged
    # the same way if it dies, and be cancelled the same way on shutdown.
    coin_tasks: set = set()
    stop = asyncio.Event()

    def _coin_task_done(task: asyncio.Task) -> None:
        coin_tasks.discard(task)
        if not task.cancelled():
            exc = task.exception()
            if exc is not None:
                logging.error("Coin task %s ended with: %s", task.get_name(), exc)
        if not coin_tasks:
            stop.set()          # every coin has finished -- nothing left to run

    def _start_coin_task(cfg: dict) -> asyncio.Task:
        task = asyncio.create_task(
            run_coin(
                exchange, cfg, states[cfg["symbol"]], wallet,
                cmd_queues[cfg["symbol"]], last_price, states, blynk,
                push_notify, dry_run, simulate_file, bypass_cooldown,
            ),
            name=cfg["symbol"],
        )
        task.add_done_callback(_coin_task_done)
        coin_tasks.add(task)
        return task

    for c in enabled:
        _start_coin_task(c)

    async def spawn_coin(cfg: dict) -> str:
        """Start trading a coin that wasn't running at startup.

        Everything the startup path does for one coin, in the same order: load
        its state file, give it a queue and a price slot, reconcile the tracked
        position to the exchange (live only -- a fresh coin is flat, so this is
        a no-op unless an old state file came back with it), register it with
        the menu/dashboard, then start its task. Returns a note for the caller
        to show the user. Raises ValueError if it's already running.
        """
        symbol = cfg["symbol"]
        if symbol in states:
            raise ValueError(f"{symbol} is already running")

        state = load_state(symbol, mode=MODE)
        state.mode = MODE
        state.paper_wallet_usd = 0.0
        states[symbol] = state
        cmd_queues[symbol] = asyncio.Queue()
        last_price[symbol] = None

        if MODE == "live" and not simulate_file:
            try:
                await reconcile_all_to_exchange(exchange, {symbol: state}, [cfg])
            except Exception as e:
                logging.warning("%s: reconcile on hot-add failed: %s", symbol, e)

        # One dict per symbol, shared by both lists: `enabled` drives menu
        # numbering and the dashboard's active table, COINS drives the Blynk
        # per-coin pin push and the dashboard's inactive list.
        for registry in (enabled, COINS):
            match = next((c for c in registry if c["symbol"] == symbol), None)
            if match is None:
                registry.append(cfg)
            elif match is not cfg:
                registry[registry.index(match)] = cfg

        _start_coin_task(cfg)
        resumed = (f" -- resumed {len(state.positions)} position(s) from its state file"
                   if state.positions else "")
        logging.warning("HOT-ADD %s: now trading as coin %d%s%s", symbol, len(enabled),
                        resumed, "  [PAUSED]" if state.paused else "")
        return (f"{symbol} started as coin {len(enabled)} without a restart"
                + (resumed or "") + (" (it is PAUSED)" if state.paused else ""))

    # Web dashboard: reads these very objects and enqueues onto these very
    # queues, so it and the keypress menu are always the same bot.
    dash_runner = None
    if dashboard_on:
        dash_runner = await dashboard.start(
            dashboard.DashboardContext(
                states=states, cfgs=enabled, wallet=wallet, last_price=last_price,
                cmd_queues=cmd_queues, balance_holder=balance_holder,
                exchange=exchange, spawn_coin=spawn_coin,
                mode=MODE, dry_run=dry_run,
            ),
            dashboard_host, dashboard_port,
        )


    try:
        await stop.wait()
    except (asyncio.CancelledError, KeyboardInterrupt):
        logging.info("Shutdown requested")
    finally:
        for task in list(coin_tasks):
            task.cancel()
        if coin_tasks:
            # Let each run_coin's finally: save_state run before we save again.
            await asyncio.gather(*coin_tasks, return_exceptions=True)
        heartbeat_task.cancel()
        if balance_task is not None:
            balance_task.cancel()
        if dash_runner is not None:
            try:
                await dash_runner.cleanup()
            except Exception:
                pass
        if reader_attached:
            try:
                loop.remove_reader(sys.stdin)
            except Exception:
                pass
        _disable_keypress(fd, old_term)
        for sym, st in states.items():
            save_state(sym, st)
        wallet.save()
        try:
            await exchange.close()
        except Exception:
            pass


def setup_logging() -> None:
    from pathlib import Path
    Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
    )


def main() -> None:
    global MODE
    parser = argparse.ArgumentParser(description="Crypto grid/DCA bot -- multi-coin (Kraken)")
    parser.add_argument("--live", action="store_true",
                        help="Send real orders to the exchange. Default is paper.")
    parser.add_argument("--simulate", metavar="CSV",
                        help="Replay prices from a CSV (first enabled coin only)")
    parser.add_argument("--dry-run", action="store_true",
                        help="With --live, log intended orders but skip the API call")
    parser.add_argument("--no-dashboard", action="store_true",
                        help="Don't start the web dashboard")
    parser.add_argument("--dashboard-host", default=DASHBOARD_HOST,
                        help=f"Dashboard bind address (default {DASHBOARD_HOST}; "
                             f"127.0.0.1 for loopback only)")
    parser.add_argument("--dashboard-port", type=int, default=DASHBOARD_PORT,
                        help=f"Dashboard port (default {DASHBOARD_PORT})")
    args = parser.parse_args()

    MODE = "live" if args.live else "paper"
    setup_logging()
    enabled_symbols = [c["symbol"] for c in COINS if c.get("enabled", True)]
    if MODE == "live":
        logging.warning("LIVE MODE -- real orders will be placed on %s for %s",
                        "kraken", ", ".join(enabled_symbols))
    asyncio.run(run(
        args.simulate, args.dry_run,
        args.dashboard_host, args.dashboard_port,
        DASHBOARD_ENABLED and not args.no_dashboard,
    ))


if __name__ == "__main__":
    main()
