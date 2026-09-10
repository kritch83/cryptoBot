"""Embedded web dashboard: live stats + the full manual control set.

Runs as one more asyncio task inside the bot process, which is what makes it
useful: it reads the SAME `states`, `last_price`, `wallet` and balance objects
the coin tasks are mutating, and it acts by putting a PendingAction on the SAME
per-coin cmd_queue the keypress menu uses. There is no second source of truth
and no IPC to get out of sync -- a button here and a keystroke in the terminal
are the same command, validated by the same code (helpers/actions.py) and
executed by the same coin task on its next tick.

The server never mutates State directly. Everything that touches money goes
through the queue so it lands inside the coin's own tick, preserving the
single-threaded invariants the trading logic is written against.

Config edits are the one thing that also touches disk: helpers/config_edit.py
rewrites the value in helpers/config.py (so it survives a restart) and the
change is then hot-applied through config_reload.apply_changes -- the same path
the 'c' key uses.

Auth: a shared token (DASHBOARD_TOKEN in .env) sent as ?token=, an
X-Dashboard-Token header, or the cookie the first successful ?token= sets. This
is plain HTTP on a trusted LAN, not an internet-facing service -- the token
stops a curious device on the network from selling your positions; it is not a
substitute for keeping the port off the public internet.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import icons, trade_history
from .actions import (
    ACTIONS,
    LOCAL_KINDS,
    QUEUEABLE_KINDS,
    REASONS,
    VALUE_KINDS,
    check_value,
    normalize_value,
)
from .coin_runner import sell_threshold
from .config import (
    COINS,
    DASHBOARD_HOST,
    EXCHANGE_ID,
    DASHBOARD_PORT,
    DASHBOARD_TOKEN,
    MAKER_FEE_PCT,
    PAPER_STARTING_USD,
    TAKER_FEE_PCT,
)
from .config_edit import (
    FIELD_SPECS,
    NEW_COIN_DEFAULTS,
    STRUCTURAL_FIELDS,
    ConfigEditError,
    add_coin,
    normalize_symbol,
    remove_coin,
    render,
    set_fields,
)
from .config_reload import ReloadPlan, apply_changes, diff_coins, format_value, parse_config_coins
from .retired import load_retired, total_retired
from .state import PendingAction, State, load_state
from .wallet import PaperWallet

WEB_DIR = Path(__file__).resolve().with_name("web")
COOKIE_NAME = "gridbot_token"
COOKIE_MAX_AGE = 30 * 24 * 3600
INACTIVE_STATE_TTL = 15.0        # seconds; disabled coins' state files are re-read at most this often

_inactive_cache: dict[str, tuple[float, Optional[State]]] = {}
_reload_plans: dict[str, ReloadPlan] = {}    # preview id -> frozen plan, newest only
_icon_cache: dict = {"ts": 0.0, "map": {}}   # which bases have a cached logo
ICON_TTL = 30.0


def _icon_map(bases) -> dict:
    """{BASE: bool} for the snapshot, re-read at most every ICON_TTL.

    The snapshot is polled every couple of seconds and this is a disk check,
    so it is cached; a refresh clears it so new icons show up immediately.
    """
    now = time.time()
    if now - _icon_cache["ts"] > ICON_TTL:
        _icon_cache.update(ts=now, map=icons.have_icons(bases))
    return _icon_cache["map"]


@dataclass
class DashboardContext:
    """Live references shared with the running bot. Never copied -- the whole
    point is that a snapshot reads what the coin tasks just wrote."""
    states: dict                      # symbol -> State (live objects)
    cfgs: list                        # running (enabled) coin dicts, menu order
    wallet: PaperWallet
    last_price: dict                  # symbol -> Optional[float]
    cmd_queues: dict                  # symbol -> asyncio.Queue
    balance_holder: dict              # {"usd", "coins", "ts"} from kraken_balance_refresher
    exchange: Any = None              # ccxt exchange, for validating a new pair exists
    spawn_coin: Any = None            # async (cfg) -> note; starts a coin without a restart
    mode: str = "paper"
    dry_run: bool = False
    started_ts: float = field(default_factory=time.time)


def sym_key(symbol: str) -> str:
    """URL-safe form of a pair: VVV/USD -> VVV_USD (same shape as state files)."""
    return symbol.replace("/", "_")


# --- snapshot ---------------------------------------------------------------

def _delta_pct(target: Optional[float], price: Optional[float]) -> Optional[float]:
    if target is None or not price:
        return None
    return (target / price - 1) * 100


def _trail_dict(trail, price: Optional[float], pct: float, direction: str) -> Optional[dict]:
    if not trail.armed:
        return None
    fires_at = None
    if trail.extreme is not None:
        fires_at = trail.extreme * (1 + pct) if direction == "buy" else trail.extreme * (1 - pct)
    return {
        "armed": True,
        "manual": trail.manual,
        "extreme": trail.extreme,
        "armed_at_price": trail.armed_at_price,
        "fires_at": fires_at,
        "delta_pct": _delta_pct(fires_at, price),
    }


def _next_buy(state: State, cfg: dict, price: Optional[float]) -> dict:
    """Mirror of conductor.format_stats' 'Next BUY' block, as data."""
    if state.buys_paused:
        return {"kind": "buys_paused", "label": "Buying paused"}
    pbo = state.pending_buy_order
    if pbo is not None:
        buffer_pct = max(cfg["trail_buy_pct"], cfg.get("limit_buy_offset_pct", 0.001))
        cancels_at = pbo.placed_at_price * (1 + buffer_pct)
        return {
            "kind": "limit", "price": pbo.limit_price, "qty": pbo.qty_coin,
            "level": pbo.level, "cancels_at": cancels_at,
            "delta_pct": _delta_pct(pbo.limit_price, price),
            "cancel_delta_pct": _delta_pct(cancels_at, price),
            "label": "Pending LIMIT BUY",
        }
    if state.trailing_buy.armed and state.trailing_buy.extreme is not None:
        fires_at = state.trailing_buy.extreme * (1 + cfg["trail_buy_pct"])
        manual = state.trailing_buy.manual
        return {
            "kind": "trail", "manual": manual, "price": fires_at,
            "extreme": state.trailing_buy.extreme,
            "delta_pct": _delta_pct(fires_at, price),
            "label": "Buy-trail armed" + (" (manual dip-buy)" if manual else ""),
        }
    if len(state.positions) >= cfg["max_grid_levels"]:
        return {"kind": "grid_full", "label": f"Grid full ({cfg['max_grid_levels']} levels)"}
    if state.positions and state.last_buy_price is not None:
        arms_at = state.last_buy_price * (1 - cfg["drop_pct"])
        return {
            "kind": "waiting", "price": arms_at, "delta_pct": _delta_pct(arms_at, price),
            "label": "Arms at",
            "source": f"-{cfg['drop_pct']:.1%} from last buy ${state.last_buy_price:,.{cfg['price_prec']}f}",
        }
    if not state.positions and state.initial_entry_high is not None:
        arms_at = state.initial_entry_high * (1 - cfg["drop_pct"])
        return {
            "kind": "waiting", "price": arms_at, "delta_pct": _delta_pct(arms_at, price),
            "label": "Arms at",
            "source": f"-{cfg['drop_pct']:.1%} from observed high ${state.initial_entry_high:,.{cfg['price_prec']}f}",
        }
    return {"kind": "idle", "label": "Waiting for first price tick"}


def _next_sell(state: State, cfg: dict, price: Optional[float]) -> dict:
    """Mirror of conductor.format_stats' 'Next SELL' block, as data."""
    pso = state.pending_sell_order
    if pso is not None:
        out = {
            "kind": "target" if pso.manual else "limit",
            "price": pso.limit_price, "qty": pso.qty_coin,
            "delta_pct": _delta_pct(pso.limit_price, price),
            "label": "Pending TARGET SELL" if pso.manual else "Pending LIMIT SELL",
        }
        if not pso.manual:
            cancels_at = pso.limit_price * (1 - cfg["trail_sell_pct"])
            out["cancels_at"] = cancels_at
            out["cancel_delta_pct"] = _delta_pct(cancels_at, price)
        return out
    if state.trailing_sell.armed and state.trailing_sell.extreme is not None:
        fires_at = state.trailing_sell.extreme * (1 - cfg["trail_sell_pct"])
        manual = state.trailing_sell.manual
        return {
            "kind": "trail", "manual": manual, "price": fires_at,
            "extreme": state.trailing_sell.extreme,
            "floor": state.avg_entry_price if manual else None,
            "delta_pct": _delta_pct(fires_at, price),
            "label": "Sell-trail armed" + (" (manual stop, floored at avg)" if manual else ""),
        }
    if state.positions and state.avg_entry_price is not None:
        arms_at, source_label, _ = sell_threshold(state, cfg)
        return {
            "kind": "waiting", "price": arms_at, "delta_pct": _delta_pct(arms_at, price),
            "label": "Arms at", "source": source_label,
        }
    return {"kind": "idle", "label": "No open positions"}


def _coin_snapshot(ctx: DashboardContext, cfg: dict, index: int) -> dict:
    symbol = cfg["symbol"]
    state = ctx.states[symbol]
    price = ctx.last_price.get(symbol)
    base = symbol.split("/")[0]
    wallet_qty = (ctx.balance_holder.get("coins") or {}).get(base)

    unrealized = unrealized_pct = None
    if state.positions and state.avg_entry_price and price:
        unrealized = (price - state.avg_entry_price) * state.total_qty_coin
        unrealized_pct = (price / state.avg_entry_price - 1) * 100

    stop_trigger = None
    if state.stop_loss_pct and state.avg_entry_price:
        stop_trigger = state.avg_entry_price * (1 - state.stop_loss_pct)

    return {
        "symbol": symbol,
        "key": sym_key(symbol),
        "base": base,
        "index": index,
        "active": True,
        "price_prec": cfg["price_prec"],
        "price": price,
        "paused": state.paused,
        "mode": state.mode,
        # Commands are drained on the coin's next price tick. On a quiet pair
        # that can be a while, and a UI that showed nothing would look broken --
        # so report the queue depth and let the dashboard say "waiting".
        "queued_commands": ctx.cmd_queues[symbol].qsize(),
        "cycle": state.cycle_count,
        "levels": len(state.positions),
        "max_levels": cfg["max_grid_levels"],
        "avg_entry": state.avg_entry_price,
        "last_buy": state.last_buy_price,
        "qty": state.total_qty_coin,
        "cost_usd": state.total_cost_usd,
        "wallet_qty": wallet_qty,
        "untracked_qty": None if wallet_qty is None else wallet_qty - state.total_qty_coin,
        "position_value": (price * state.total_qty_coin) if price else None,
        "realized": state.realized_pnl_usd,
        "unrealized": unrealized,
        "unrealized_pct": unrealized_pct,
        "initial_entry_high": state.initial_entry_high,
        "breakeven_exit_armed": state.breakeven_exit_armed,
        "pause_after_sell": state.pause_after_sell,
        "buys_paused": state.buys_paused,
        "stop_loss_pct": state.stop_loss_pct,
        "stop_loss_trigger": stop_trigger,
        "stop_loss_delta_pct": _delta_pct(stop_trigger, price),
        "trailing_buy": _trail_dict(state.trailing_buy, price, cfg["trail_buy_pct"], "buy"),
        "trailing_sell": _trail_dict(state.trailing_sell, price, cfg["trail_sell_pct"], "sell"),
        "next_buy": _next_buy(state, cfg, price),
        "next_sell": _next_sell(state, cfg, price),
        "positions": [
            {"level": p.level, "qty": p.qty_coin, "entry_price": p.entry_price,
             "fee_usd": p.fee_usd, "ts": p.ts,
             "unrealized": ((price - p.entry_price) * p.qty_coin) if price else None}
            for p in state.positions
        ],
        "config": {key: cfg.get(key) for key in FIELD_SPECS if key in cfg or key == "enabled"},
        "config_text": {key: render(cfg[key]) for key in FIELD_SPECS if key in cfg},
        "max_grid_spend": cfg["max_grid_levels"] * cfg["usd_per_buy"],
    }


def _inactive_state(symbol: str) -> Optional[State]:
    """State file of a coin that isn't running, re-read at most every TTL."""
    now = time.time()
    cached = _inactive_cache.get(symbol)
    if cached and now - cached[0] < INACTIVE_STATE_TTL:
        return cached[1]
    try:
        state = load_state(symbol)
    except Exception:
        state = None
    _inactive_cache[symbol] = (now, state)
    return state


def _inactive_snapshot(cfg: dict, retired: dict) -> dict:
    symbol = cfg["symbol"]
    state = _inactive_state(symbol)
    entry = retired.get(symbol)
    if isinstance(entry, dict):
        retired_pnl = float(entry.get("realized_pnl_usd", 0.0))
        retired_on = entry.get("retired")
        retired_cycles = int(entry.get("cycles", 0))
    elif isinstance(entry, (int, float)):
        retired_pnl, retired_on, retired_cycles = float(entry), None, 0
    else:
        retired_pnl, retired_on, retired_cycles = None, None, 0

    return {
        "symbol": symbol,
        "key": sym_key(symbol),
        "base": symbol.split("/")[0],
        "active": False,
        "price_prec": cfg["price_prec"],
        "enabled": bool(cfg.get("enabled", True)),
        "realized": state.realized_pnl_usd if state else None,
        "cycle": state.cycle_count if state else None,
        "levels": len(state.positions) if state else 0,
        "qty": state.total_qty_coin if state else 0.0,
        "avg_entry": state.avg_entry_price if state else None,
        "paused": state.paused if state else None,
        "retired_pnl": retired_pnl,
        "retired_on": retired_on,
        "retired_cycles": retired_cycles,
        "config": {key: cfg.get(key) for key in FIELD_SPECS if key in cfg or key == "enabled"},
        "config_text": {key: render(cfg[key]) for key in FIELD_SPECS if key in cfg},
    }


def build_snapshot(ctx: DashboardContext) -> dict:
    running = {cfg["symbol"] for cfg in ctx.cfgs}
    coins = [_coin_snapshot(ctx, cfg, i) for i, cfg in enumerate(ctx.cfgs, start=1)]

    retired_ledger = load_retired()
    inactive = [_inactive_snapshot(cfg, retired_ledger) for cfg in COINS
                if cfg["symbol"] not in running]

    icon_map = _icon_map([c["base"] for c in coins] + [c["base"] for c in inactive])
    for entry in (*coins, *inactive):
        entry["has_icon"] = bool(icon_map.get(entry["base"]))

    realized_active = sum(c["realized"] for c in coins)
    unrealized = sum(c["unrealized"] or 0.0 for c in coins)
    position_value = sum(c["position_value"] or 0.0 for c in coins)
    cost_basis = sum(c["cost_usd"] for c in coins)
    retired_total = total_retired()

    # --- what everything is worth right now --------------------------------
    # Two different quantities, and conflating them would misreport real money:
    # `position_value` is the bot's TRACKED grid positions, while the exchange
    # balance is everything actually held (prior holdings, manual buys, dust
    # left by a clear-stats). Prefer the real balance and say which was used.
    wallet_value = 0.0
    untracked_value = 0.0
    valued_from_wallet = 0
    for c in coins:
        if c["price"] is None:
            continue
        if c["wallet_qty"] is not None:
            wallet_value += c["wallet_qty"] * c["price"]
            untracked_value += (c["untracked_qty"] or 0.0) * c["price"]
            valued_from_wallet += 1
        else:
            wallet_value += c["position_value"] or 0.0

    holdings_basis = "wallet" if valued_from_wallet else "tracked"
    holdings_value = wallet_value if valued_from_wallet else position_value

    # Cash: the live Kraken USD balance, or the shared paper pool in paper mode.
    # None in live until the first balance poll lands -- reported as unknown
    # rather than as zero, which would understate the portfolio.
    cash = ctx.balance_holder.get("usd") if ctx.mode == "live" else ctx.wallet.usd

    balance_ts = ctx.balance_holder.get("ts") or 0.0
    return {
        "ts": time.time(),
        "mode": ctx.mode,
        "dry_run": ctx.dry_run,
        "uptime_sec": time.time() - ctx.started_ts,
        "fees": {"taker": TAKER_FEE_PCT, "maker": MAKER_FEE_PCT},
        "wallet": {
            "paper_usd": ctx.wallet.usd,
            "paper_enabled": PAPER_STARTING_USD != 0 and ctx.mode == "paper",
            "kraken_usd": ctx.balance_holder.get("usd"),
            "kraken_age_sec": (time.time() - balance_ts) if balance_ts else None,
        },
        "totals": {
            "realized_active": realized_active,
            "retired": retired_total,
            "realized_total": realized_active + retired_total,
            "unrealized": unrealized,
            "position_value": position_value,
            "cost_basis": cost_basis,
            "cash_usd": cash,                      # None = not fetched yet
            "holdings_value": holdings_value,      # coins, at the last tick price
            "holdings_basis": holdings_basis,      # "wallet" (real balances) | "tracked"
            "untracked_value": untracked_value,    # held but outside any grid
            "total_value": None if cash is None else cash + holdings_value,
            "coins_valued_from_wallet": valued_from_wallet,
            "coins_no_price": sum(1 for c in coins if c["price"] is None),
            "inactive_holding": sum(1 for c in inactive if (c.get("qty") or 0) > 0),
            "open_levels": sum(c["levels"] for c in coins),
            "coins_active": len(coins),
            "coins_holding": sum(1 for c in coins if c["levels"]),
            "coins_paused": sum(1 for c in coins if c["paused"]),
        },
        "coins": coins,
        "inactive": inactive,
        "retired_ledger": [
            {"symbol": symbol,
             "realized_pnl_usd": (float(v.get("realized_pnl_usd", 0.0))
                                  if isinstance(v, dict) else float(v)),
             "cycles": int(v.get("cycles", 0)) if isinstance(v, dict) else 0,
             "retired": v.get("retired") if isinstance(v, dict) else None}
            for symbol, v in retired_ledger.items()
            if isinstance(v, (dict, int, float))
        ],
    }


def build_meta() -> dict:
    """Static catalogs the UI needs once: actions and editable config fields."""
    return {
        "actions": [
            {"key": a.key, "label": a.label,
             # Buttons get the label up to the parenthetical; the full text
             # becomes the tooltip. Keeps the CLI menu verbose and the GUI tidy
             # without maintaining two copies of every label.
             "short": a.label.split(" (")[0],
             "kind": a.kind, "danger": a.danger,
             "takes_value": a.takes_value, "needs_confirm": a.needs_confirm,
             "local": a.kind in LOCAL_KINDS}
            for a in ACTIONS
        ],
        "fields": [
            {"key": key, "kind": spec.kind, "label": spec.label, "hint": spec.hint,
             "min": spec.minimum, "max": spec.maximum, "choices": list(spec.choices),
             "as_pct": spec.as_pct, "structural": spec.structural, "group": spec.group}
            for key, spec in FIELD_SPECS.items()
        ],
        "value_kinds": sorted(VALUE_KINDS),
        "new_coin_defaults": {k: render(v) for k, v in NEW_COIN_DEFAULTS.items()},
    }


# --- command + config plumbing ----------------------------------------------

def queue_action(ctx: DashboardContext, symbol: str, kind: str,
                 value: Optional[float] = None) -> dict:
    """Validate and enqueue one manual action. Returns a result dict.

    Deliberately thin: the checks here are the advisory ones from actions.py so
    the UI can say "no open position" instantly; coin_runner.apply_manual is
    what actually decides, on the tick that runs the command.
    """
    if kind not in QUEUEABLE_KINDS:
        return {"ok": False, "error": f"unknown action {kind!r}"}
    if symbol not in ctx.cmd_queues:
        return {"ok": False, "error": f"{symbol} is not running"}

    state = ctx.states[symbol]
    if kind in VALUE_KINDS:
        if value is None:
            return {"ok": False, "error": f"{kind} needs a value"}
        problem = check_value(kind, value, state, ctx.last_price.get(symbol))
        if problem:
            return {"ok": False, "error": f"{symbol}: {problem}"}
        payload = normalize_value(kind, value)
    else:
        payload = None

    ctx.cmd_queues[symbol].put_nowait(
        PendingAction(kind, 0, REASONS[kind], value=payload)
    )
    logging.info("DASHBOARD: queued %s -> %s%s", symbol, kind,
                 f" value={payload}" if payload is not None else "")
    return {"ok": True, "symbol": symbol, "kind": kind, "value": payload,
            "message": f"queued {kind} for {symbol}"}


async def apply_config_edit(ctx: DashboardContext, symbol: str, fields: dict) -> dict:
    """Write a coin's tunables to config.py, then hot-apply them if it's running.

    Two phases with different failure modes, reported separately: the file write
    is validated and atomic (config.py is either fully updated or untouched);
    the hot-apply only reaches coins that are currently running and skips
    structural keys, which need a restart.
    """
    try:
        edits, backup = set_fields(symbol, fields)
    except ConfigEditError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:                       # unreadable/unparseable config.py
        logging.error("DASHBOARD: config edit failed for %s: %s", symbol, exc)
        return {"ok": False, "error": f"could not edit config.py: {exc}"}

    detail = [{"key": e.key, "old": e.old, "new": e.new, "added": e.was_absent,
               "structural": e.key in STRUCTURAL_FIELDS} for e in edits]
    logging.info("DASHBOARD: config.py updated for %s -- %s", symbol,
                 ", ".join(f"{e.key}: {format_value(e.old)} -> {format_value(e.new)}"
                           for e in edits))

    live_cfg = next((c for c in ctx.cfgs if c["symbol"] == symbol), None)
    applied: list = []
    notes: list = []
    if live_cfg is None:
        notes.append(f"{symbol} is not running -- the file is updated; restart to start it")
    else:
        try:
            new_coins = parse_config_coins()
            plan = diff_coins(new_coins, [live_cfg])
        except Exception as exc:
            notes.append(f"saved to config.py, but the hot-apply check failed ({exc}) "
                         f"-- values take effect on restart")
            plan = None
        if plan is not None:
            if plan.changes:
                for line in apply_changes(ReloadPlan(plan.changes, [])):
                    logging.info(line)
                applied = [key for change in plan.changes for key, _o, _n in change.field_changes]
            skipped = sorted({e.key for e in edits} - set(applied))
            if skipped:
                notes.append("restart required for: " + ", ".join(skipped))

    # Enabling a coin used to mean "edit the file, then restart". Now it can
    # start straight away, using the same path as "+ Add coin".
    started = False
    if any(e.key == "enabled" and e.new for e in edits) and symbol not in ctx.states:
        parsed = next((c for c in parse_config_coins() if c["symbol"] == symbol), None)
        if parsed is not None:
            entry = register_entry(parsed)
            started, start_notes = await _maybe_start(ctx, entry)
            notes.extend(start_notes)
            if started:
                # Drop the "restart to start it" advice this function added
                # before we knew we could just start it.
                notes = [n for n in notes
                         if "restart required for" not in n
                         and "restart to start it" not in n]

    return {"ok": True, "symbol": symbol, "edits": detail, "applied_live": applied,
            "started": started, "notes": notes,
            "backup": str(backup) if backup else None}


def reload_preview(ctx: DashboardContext) -> dict:
    """Same diff the 'c' key previews: config.py vs every running coin.

    The plan is frozen here and handed back under an id, matching the CLI's
    rule that Y applies exactly what was shown even if config.py changes again
    before the confirm. Only the newest preview stays applicable.
    """
    try:
        plan = diff_coins(parse_config_coins(), ctx.cfgs)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    plan_id = secrets.token_hex(8)
    _reload_plans.clear()
    _reload_plans[plan_id] = plan
    return {
        "ok": True,
        "id": plan_id,
        "changes": [
            {"symbol": change.symbol,
             "fields": [{"key": key, "old": format_value(old), "new": format_value(new)}
                        for key, old, new in change.field_changes]}
            for change in plan.changes
        ],
        "warnings": list(plan.warnings),
    }


def reload_apply(ctx: DashboardContext, plan_id: str) -> dict:
    """Apply the exact plan that `plan_id` previewed (single-use)."""
    plan = _reload_plans.pop(plan_id, None)
    if plan is None:
        return {"ok": False,
                "error": "that reload preview is stale -- preview again before applying"}
    if not plan.changes:
        return {"ok": True, "applied": [], "message": "no tunable changes to apply"}
    lines = apply_changes(plan)
    for line in lines:
        logging.info(line)
    logging.info("CONFIG RELOAD applied from dashboard -- %d coin(s) re-tuned", len(plan.changes))
    return {"ok": True, "applied": lines,
            "message": f"applied changes to {len(plan.changes)} coin(s)"}


async def add_coin_api(ctx: DashboardContext, symbol: str, fields: Optional[dict]) -> dict:
    """Append a new pair to COINS in config.py.

    File-only by design: the set of running coin tasks is fixed at startup, so
    a new pair cannot be hot-applied and the caller is told a restart is needed.
    The entry is written DISABLED, so even that restart won't start trading it
    until you look at the numbers and turn it on.
    """
    try:
        symbol = normalize_symbol(symbol)
    except ConfigEditError as exc:
        return {"ok": False, "error": str(exc)}

    # If the exchange's markets are loaded, refuse a pair it doesn't list --
    # otherwise the mistake only surfaces as a stream error after a restart.
    markets = getattr(ctx.exchange, "markets", None) or {}
    if markets and symbol not in markets:
        base = symbol.split("/")[0]
        alts = sorted(m for m in markets if m.split("/")[0] == base)
        hint = f" Pairs for {base}: {', '.join(alts[:6])}." if alts else ""
        return {"ok": False,
                "error": f"{EXCHANGE_ID} does not list {symbol}.{hint}"}

    try:
        entry, backup = add_coin(symbol, fields or {})
    except ConfigEditError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        logging.error("DASHBOARD: add coin %s failed: %s", symbol, exc)
        return {"ok": False, "error": f"could not edit config.py: {exc}"}

    entry = register_entry(entry)      # visible to the dashboard right away
    started, notes = await _maybe_start(ctx, entry)
    if not started:
        notes.append(f"{symbol} was added DISABLED -- set Enabled to True to start it.")
    logging.warning("DASHBOARD: added %s to COINS (%s)", symbol,
                    "started" if started else "disabled")
    return {"ok": True, "symbol": symbol, "started": started,
            "entry": {k: render(v) for k, v in entry.items()},
            "backup": str(backup) if backup else None, "notes": notes}


def register_entry(entry: dict) -> dict:
    """Put a coin into the in-memory COINS list, replacing any same-symbol entry.

    The snapshot builds its inactive list from COINS, so without this a coin
    added while the bot is running would be written to config.py and then be
    invisible until a restart -- including invisible to the toggle that would
    enable it. Returns the object now held in COINS, so callers share one dict
    per symbol rather than accumulating copies.
    """
    for i, existing in enumerate(COINS):
        if existing.get("symbol") == entry["symbol"]:
            if existing is entry:
                return existing
            COINS[i] = entry
            return entry
    COINS.append(entry)
    return entry


def unregister_entry(symbol: str) -> None:
    """Drop a coin from the in-memory COINS list."""
    for i, existing in enumerate(COINS):
        if existing.get("symbol") == symbol:
            del COINS[i]
            return


async def _maybe_start(ctx: DashboardContext, entry: dict) -> tuple[bool, list]:
    """Start a coin now if it is enabled and not already running.

    This is what makes adding or enabling a coin take effect without a restart:
    conductor.spawn_coin does exactly what the startup path does for one coin
    (state file, queue, price slot, live reconcile, task) and registers it with
    the same supervision. Failure here is never fatal -- the config.py edit has
    already landed, so the worst case degrades to the old behaviour: restart.
    """
    symbol = entry["symbol"]
    if not entry.get("enabled", False):
        return False, []
    if ctx.spawn_coin is None:
        return False, [f"{symbol} is enabled but this build cannot start a coin "
                       f"without a restart."]
    if symbol in ctx.states:
        return False, [f"{symbol} is already running."]
    try:
        note = await ctx.spawn_coin(entry)
    except Exception as exc:
        logging.error("DASHBOARD: could not start %s: %s", symbol, exc)
        return False, [f"{symbol} was saved to config.py but could not be started "
                       f"({exc}) -- restart the bot to pick it up."]
    return True, [note]


def remove_coin_api(ctx: DashboardContext, symbol: str, force: bool = False) -> dict:
    """Delete a pair's entry from COINS in config.py.

    Refuses by default on anything that would lose track of real money: an open
    position (the coins stay on the exchange, unmanaged), a resting order, or
    realized PnL that was never booked to the retired ledger and would simply
    vanish from the totals. RETIRE handles all three properly, which is why the
    error points at it. `force` overrides -- the UI makes the user type the
    ticker for that.

    The state file and the retired ledger row are left on disk either way:
    removing a config entry is an edit, not a decision to destroy history.
    """
    try:
        symbol = normalize_symbol(symbol)
    except ConfigEditError as exc:
        return {"ok": False, "error": str(exc)}

    running = symbol in ctx.states
    state = ctx.states.get(symbol) or _inactive_state(symbol)
    base = symbol.split("/")[0]

    blockers = []
    if state is not None:
        if state.positions or state.total_qty_coin > 0:
            blockers.append(f"holds {state.total_qty_coin:.8f} {base} across "
                            f"{len(state.positions)} level(s) -- they stay on the exchange, "
                            f"untracked")
        if state.pending_buy_order is not None or state.pending_sell_order is not None:
            blockers.append("has a resting order that would be orphaned")
        if state.realized_pnl_usd and symbol not in load_retired():
            blockers.append(f"has {state.realized_pnl_usd:+,.2f} realized PnL that was never "
                            f"booked to the retired ledger -- it would disappear from the totals")

    if blockers and not force:
        return {"ok": False, "blockers": blockers, "can_force": True,
                "error": f"{symbol} still {'; '.join(blockers)}. Retire it first (that books "
                         f"the PnL and clears the position), or confirm removing it anyway."}

    try:
        removed, backup = remove_coin(symbol)
    except ConfigEditError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        logging.error("DASHBOARD: remove coin %s failed: %s", symbol, exc)
        return {"ok": False, "error": f"could not edit config.py: {exc}"}

    notes = [f"data/state_{sym_key(symbol)}.json was left on disk; delete it by hand if you "
             f"want the history gone."]
    if not running:
        unregister_entry(symbol)       # a running coin keeps its slot until restart
    if running:
        notes.insert(0, f"{symbol} is still RUNNING -- it keeps trading until the bot is "
                        f"restarted. Pause it now if that matters.")
    logging.warning("DASHBOARD: removed %s from COINS%s", symbol,
                    " (still running until restart)" if running else "")
    return {"ok": True, "symbol": symbol, "was_running": running, "forced": bool(blockers),
            "removed": {k: render(v) for k, v in removed.items()},
            "backup": str(backup) if backup else None, "notes": notes}


# --- HTTP -------------------------------------------------------------------

def _make_app(ctx: DashboardContext):
    from aiohttp import web

    routes = web.RouteTableDef()

    def _symbol_from(request) -> str:
        key = request.match_info["key"]
        wanted = key.replace("_", "/")
        for cfg in (*ctx.cfgs, *COINS):
            if cfg["symbol"] == wanted or sym_key(cfg["symbol"]) == key:
                return cfg["symbol"]
        raise web.HTTPNotFound(
            text=json.dumps({"ok": False, "error": f"unknown coin {key!r}"}),
            content_type="application/json")

    async def _json_body(request) -> dict:
        try:
            body = await request.json()
        except Exception:
            raise web.HTTPBadRequest(text="expected a JSON body")
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text="expected a JSON object")
        return body

    @web.middleware
    async def auth_middleware(request, handler):
        supplied = (request.headers.get("X-Dashboard-Token")
                    or request.query.get("token")
                    or request.cookies.get(COOKIE_NAME)
                    or "")
        if not hmac.compare_digest(supplied.encode(), DASHBOARD_TOKEN.encode()):
            if request.path.startswith("/api/"):
                return web.json_response({"ok": False, "error": "bad or missing token"}, status=401)
            return web.Response(status=401, content_type="text/html", text=_TOKEN_PAGE)
        response = await handler(request)
        if request.query.get("token") and hasattr(response, "set_cookie"):
            response.set_cookie(COOKIE_NAME, DASHBOARD_TOKEN, max_age=COOKIE_MAX_AGE,
                                httponly=True, samesite="Lax")
        return response

    @web.middleware
    async def errors_middleware(request, handler):
        """A dashboard bug must never take the bot down, or leak a traceback."""
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logging.exception("DASHBOARD: %s %s failed", request.method, request.path)
            return web.json_response({"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                                     status=500)

    @routes.get("/")
    async def index(request):
        return web.FileResponse(WEB_DIR / "index.html",
                                headers={"Cache-Control": "no-store"})

    @routes.get("/app.js")
    async def appjs(request):
        return web.FileResponse(WEB_DIR / "app.js", headers={"Cache-Control": "no-store"})

    @routes.get("/style.css")
    async def appcss(request):
        return web.FileResponse(WEB_DIR / "style.css", headers={"Cache-Control": "no-store"})

    @routes.get("/icons/{base}")
    async def icon(request):
        """Serve a cached logo. The name is restricted to a bare ticker and the
        file is resolved through the manifest, so nothing outside data/icons/
        can be reached, and the type is sniffed from the bytes rather than
        trusted from the extension."""
        base = request.match_info["base"].upper()
        if not base.isalnum() or len(base) > 16:
            raise web.HTTPNotFound(text="")
        path = icons.icon_file(base)
        if path is None:
            raise web.HTTPNotFound(text="")
        return web.FileResponse(path, headers={
            "Content-Type": icons.content_type(path),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "public, max-age=86400",
        })

    @routes.post("/api/icons/refresh")
    async def icons_refresh(request):
        """Fetch logos for any coin that doesn't have one yet.

        Explicitly user-triggered and run in a thread: the bot never reaches
        out for icons on its own, and the download loop (several HTTP calls
        with pauses) must not sit on the event loop the coins trade from.
        """
        body = await _json_body(request) if request.can_read_body else {}
        symbols = [c["symbol"] for c in COINS]
        report = await asyncio.to_thread(icons.fetch_missing, symbols,
                                         bool(body.get("force")))
        _icon_cache["ts"] = 0.0          # surface new icons on the next poll
        logging.info("DASHBOARD: icon refresh -- %s", report.get("message") or report)
        return web.json_response(report)

    @routes.get("/api/meta")
    async def meta(request):
        return web.json_response(build_meta())

    @routes.get("/api/snapshot")
    async def snapshot(request):
        return web.json_response(build_snapshot(ctx))

    @routes.post("/api/coin/{key}/action")
    async def action(request):
        symbol = _symbol_from(request)
        body = await _json_body(request)
        kind = str(body.get("kind", ""))
        raw = body.get("value")
        value = None
        if raw not in (None, ""):
            try:
                value = float(raw)
            except (TypeError, ValueError):
                return web.json_response({"ok": False, "error": f"invalid number {raw!r}"},
                                         status=400)
        result = queue_action(ctx, symbol, kind, value)
        return web.json_response(result, status=200 if result["ok"] else 400)

    @routes.post("/api/coin/{key}/config")
    async def coin_config(request):
        symbol = _symbol_from(request)
        body = await _json_body(request)
        fields = body.get("fields")
        if not isinstance(fields, dict) or not fields:
            return web.json_response({"ok": False, "error": "no fields supplied"}, status=400)
        result = await apply_config_edit(ctx, symbol, fields)
        return web.json_response(result, status=200 if result["ok"] else 400)

    @routes.post("/api/coins")
    async def coin_add(request):
        body = await _json_body(request)
        result = await add_coin_api(ctx, str(body.get("symbol", "")), body.get("fields") or {})
        return web.json_response(result, status=200 if result["ok"] else 400)

    @routes.post("/api/coins/{key}/remove")
    async def coin_remove(request):
        body = await _json_body(request) if request.can_read_body else {}
        symbol = request.match_info["key"].replace("_", "/")
        result = remove_coin_api(ctx, symbol, bool(body.get("force")))
        return web.json_response(result, status=200 if result["ok"] else 400)

    @routes.get("/api/config/preview")
    async def config_preview(request):
        result = reload_preview(ctx)
        return web.json_response(result, status=200 if result["ok"] else 400)

    @routes.post("/api/config/apply")
    async def config_apply(request):
        body = await _json_body(request)
        result = reload_apply(ctx, str(body.get("id", "")))
        return web.json_response(result, status=200 if result["ok"] else 400)

    @routes.get("/api/history")
    async def history(request):
        symbol = request.query.get("symbol") or None
        if symbol:
            symbol = symbol.replace("_", "/")
        try:
            limit = min(1000, max(1, int(request.query.get("limit", 200))))
        except ValueError:
            limit = 200
        # The first parse walks the whole multi-MB log (~0.8s today, growing).
        # Off the event loop, so a dashboard refresh can't stall the coin tasks.
        return web.json_response(await asyncio.to_thread(trade_history.summary, symbol, limit))

    @routes.get("/api/log")
    async def log(request):
        symbol = request.query.get("symbol") or None
        if symbol:
            symbol = symbol.replace("_", "/")
        try:
            limit = min(1000, max(1, int(request.query.get("limit", 200))))
        except ValueError:
            limit = 200
        return web.json_response({
            "lines": trade_history.tail_log(limit, symbol, request.query.get("contains") or None),
        })

    app = web.Application(middlewares=[errors_middleware, auth_middleware])
    app.add_routes(routes)
    return app


_TOKEN_PAGE = """<!doctype html><meta charset=utf-8>
<title>gridBot dashboard</title>
<style>body{background:#0e1116;color:#c9d1d9;font:15px/1.6 system-ui,sans-serif;
padding:3rem;max-width:34rem;margin:auto}code{background:#1a1f27;padding:.15rem .4rem;
border-radius:4px;color:#7ee787}h1{font-size:1.2rem}</style>
<h1>gridBot dashboard &mdash; token required</h1>
<p>Append your dashboard token to the URL once; it is then stored in a cookie for
this browser:</p>
<p><code>http://&lt;host&gt;:&lt;port&gt;/?token=YOUR_TOKEN</code></p>
<p>The token is <code>DASHBOARD_TOKEN</code> in the project&rsquo;s <code>.env</code>.</p>
"""


async def start(ctx: DashboardContext, host: str = DASHBOARD_HOST,
                port: int = DASHBOARD_PORT):
    """Start the dashboard. Returns an AppRunner to clean up, or None.

    Every failure path here is non-fatal and returns None: the dashboard is an
    accessory, and a missing dependency or a busy port must never stop the bot
    from trading.
    """
    try:
        from aiohttp import web
    except ImportError:
        logging.warning("Dashboard disabled -- aiohttp is not installed "
                        "(pip install aiohttp; it ships with ccxt.pro)")
        return None

    loopback = host in ("127.0.0.1", "localhost", "::1")
    if not DASHBOARD_TOKEN and not loopback:
        logging.error(
            "Dashboard NOT started: DASHBOARD_TOKEN is empty and host is %s. "
            "Set DASHBOARD_TOKEN in .env, or set DASHBOARD_HOST=127.0.0.1 to bind "
            "loopback only. Refusing to expose money actions unauthenticated.", host)
        return None
    if not WEB_DIR.is_dir():
        logging.error("Dashboard NOT started: %s is missing", WEB_DIR)
        return None

    runner = web.AppRunner(_make_app(ctx), access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, host, port).start()
    except OSError as exc:
        logging.error("Dashboard NOT started on %s:%s -- %s", host, port, exc)
        await runner.cleanup()
        return None

    # Warm the trade-history cache now, in a thread: pay the one big log parse
    # during startup instead of inside the first dashboard request.
    asyncio.create_task(asyncio.to_thread(trade_history.summary))

    shown = "localhost" if loopback else host
    suffix = f"/?token={DASHBOARD_TOKEN[:4]}..." if DASHBOARD_TOKEN else "/"
    logging.info("Dashboard on http://%s:%s%s", shown, port, suffix)
    if not DASHBOARD_TOKEN:
        logging.warning("Dashboard has NO token (loopback-only bind)")
    return runner
