"""Shared catalog of the manual coin actions + their pre-flight validators.

Both front-ends drive the SAME commands: conductor.py's keypress menu and the
web dashboard (helpers/dashboard.py) each build a PendingAction from an entry
here and drop it on the coin's cmd_queue. Keeping the catalog in one module is
what stops the two UIs drifting apart when an action is added or renumbered.

Authority note: coin_runner.apply_manual re-validates every command on the tick
that executes it (with a fresh price and post-fill state). The checks here are
therefore *advisory* -- they exist so a UI can refuse an obviously-wrong entry
immediately with a readable message instead of queueing a no-op the user only
finds out about by reading the log. Never treat them as the safety net.
"""
from __future__ import annotations

from typing import NamedTuple, Optional

from .state import State


class Action(NamedTuple):
    key: str              # single-char selector in conductor's action menu
    label: str            # menu/button text
    kind: str             # PendingAction.kind
    needs_confirm: bool   # CLI asks Y; GUI shows a confirm dialog
    danger: bool = False  # GUI: red button, spells out the consequences
    takes_value: bool = False   # a number is typed before the confirm


# Order here IS the CLI menu order -- renumbering the menu means editing this
# list and nothing else. Keys 1-9 are the common actions; the tail uses letters.
ACTIONS: tuple[Action, ...] = (
    Action("1", "live stats",                 "stats",                False),
    Action("2", "config",                     "config",               False),
    Action("3", "set/clear stop-loss (% below avg entry)", "set_stop_loss", True,
           takes_value=True),
    Action("4", "toggle pause",               "pause_toggle",         True),
    Action("5", "toggle breakeven-exit",      "arm_breakeven_exit",   True),
    Action("6", "toggle pause-after-sell",    "arm_pause_after_sell", True),
    Action("7", "force BUY now (market)",     "buy",                  True, danger=True),
    Action("8", "arm buy-trail (trailing dip-buy; fires ONE buy on the rebound)",
           "arm_buy_trail", True),
    Action("9", "sell ALL now (market)",      "sell_all",             True, danger=True),
    Action("p", "sell ALL at MY price (post-only limit; pauses after fill)",
           "set_target_sell", True, danger=True, takes_value=True),
    Action("a", "arm sell-trail",             "arm_sell_trail",       True),
    Action("t", "clear targets (reset first-buy to -drop_pct)", "clear_targets", True),
    Action("r", "RETIRE coin (book PnL to ledger + FULL reset)", "retire", True, danger=True),
    Action("c", "clear stats (FULL reset)",   "clear_stats",          True, danger=True),
)

ACTION_BY_KEY = {a.key: a for a in ACTIONS}
ACTION_BY_KIND = {a.kind: a for a in ACTIONS}

# Kinds that are printed locally by the CLI rather than queued for the coin task.
LOCAL_KINDS = frozenset({"stats", "config"})

# Kinds that take a typed number + ENTER before the Y-confirm.
VALUE_KINDS = frozenset(a.kind for a in ACTIONS if a.takes_value)

# Per-kind labels: (echo-line unit, short name for cancel/invalid messages)
VALUE_LABELS = {"set_stop_loss":   ("stop-loss %",   "stop-loss"),
                "set_target_sell": ("target sell $", "target")}

REASONS = {
    "pause_toggle":         "manual pause toggle",
    "arm_sell_trail":       "manual arm-sell-trail",
    "arm_buy_trail":        "manual arm-buy-trail",
    "arm_breakeven_exit":   "manual breakeven-exit toggle",
    "arm_pause_after_sell": "manual pause-after-sell toggle",
    "buy":                  "manual force-buy",
    "sell_all":             "manual force-sell-all",
    "clear_stats":          "manual clear-stats",
    "clear_targets":        "manual clear-targets",
    "retire":               "manual retire-coin",
    "set_stop_loss":        "manual set-stop-loss",
    "set_target_sell":      "manual target-sell",
}

# Queueable kinds -- anything a UI may legitimately send to a coin task.
QUEUEABLE_KINDS = frozenset(REASONS)


def check_stop_loss(pct: float, state: State) -> Optional[str]:
    """Validate a stop-loss entry in PERCENT units (10 => -10% below avg).

    Returns an error message, or None if the entry is acceptable. 0 means
    "clear", which is only meaningful when a stop-loss is actually set.
    Mirrors conductor._commit_stop_loss / coin_runner.apply_manual.
    """
    if pct < 0 or pct >= 100:
        return f"{pct:g} out of range (need 0 < pct < 100; 0 clears)"
    if pct == 0 and state.stop_loss_pct is None:
        return "no stop-loss set -- nothing to clear"
    return None


def check_target_sell(price: float, state: State, last_price: Optional[float]) -> Optional[str]:
    """Validate a manual target-sell price in quote currency (absolute $).

    Returns an error message, or None. 0 means "clear", which only applies to a
    MANUAL target -- the bot's own auto limit-sell is never cleared this way.
    Mirrors conductor._commit_target_sell / coin_runner.apply_manual.
    """
    pso = state.pending_sell_order
    has_target = pso is not None and pso.manual
    if price < 0:
        return f"{price:g} out of range (need a price above current; 0 clears)"
    if price == 0:
        if has_target:
            return None
        if pso is not None:
            return ("the resting limit sell is the bot's own (auto) order, "
                    "not a manual target -- nothing to clear")
        return "no target sell set -- nothing to clear"
    if not state.positions or state.total_qty_coin <= 0:
        return "no open position -- nothing to sell"
    if last_price is None:
        return "no price tick yet -- can't validate the target; try again in a moment"
    if price <= last_price:
        return (f"target {price:g} is at/below current {last_price:g} -- a post-only sell "
                f"would be rejected. Use 'sell ALL now' to sell at market")
    return None


def check_value(kind: str, value: float, state: State,
                last_price: Optional[float]) -> Optional[str]:
    """Dispatch to the right validator for a value-taking kind."""
    if kind == "set_stop_loss":
        return check_stop_loss(value, state)
    if kind == "set_target_sell":
        return check_target_sell(value, state, last_price)
    return None


def normalize_value(kind: str, value: float) -> float:
    """Convert a UI-entered number into the unit PendingAction.value carries.

    Stop-loss is typed as a percent but stored as a fraction; target-sell is an
    absolute price on both sides.
    """
    return value / 100.0 if kind == "set_stop_loss" else value
