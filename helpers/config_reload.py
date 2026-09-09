"""Hot-reload support for the COINS tunables in helpers/config.py.

parse_config_coins() executes a FRESH copy of config.py source into a private
namespace -- it never touches the live, already-imported helpers.config module
(importlib.reload is deliberately avoided: a failed reload can leave the live
module half-updated). diff_coins() compares the parsed entries against the
running coin dicts; apply_changes() swaps new values into the SAME dict
objects in place (cfg.clear() + cfg.update()), which every holder (conductor's
`enabled` list, each run_coin task's `cfg` param, coin_runner's module-level
COINS, blynk.push_pnl) observes immediately because they all share references.

Scope: tunables of coins that are ALREADY RUNNING. Structural changes --
coin added, coin removed, "enabled" flipped, symbol renamed -- are reported
as needs-restart warnings and never applied. Module-level constants (fees,
cooldowns, Blynk/Pushover, PAPER_STARTING_USD) are bound at import time in
their consumers and also need a restart.

All functions are pure with respect to the process (no logging, no printing,
no live-module access) so they can be unit-tested standalone against
temporary config sources.
"""
from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

# config_reload.py lives in the same helpers/ dir as config.py, so this is the
# exact file the running process imported -- independent of the bot's cwd.
DEFAULT_CONFIG_PATH = Path(__file__).resolve().with_name("config.py")

# Sentinel for "key absent on this side of the diff".
_MISSING = object()

# Keys that define coin identity/lifecycle -- never diffed as tunables,
# never applied. "symbol" is also the key of states/cmd_queues/last_price,
# so renaming it at runtime would orphan the running task's state.
STRUCTURAL_KEYS = frozenset({"symbol", "enabled"})

# Keys read with cfg[...] (no default) in hot paths -- a missing one would
# KeyError a live coin task mid-tick, so an entry about to be applied must
# have them all. (Optional keys -- order types, offsets, blynk_pin -- are
# read with .get() and may come and go freely.)
REQUIRED_KEYS = (
    "symbol", "price_prec", "drop_pct", "trail_buy_pct", "trail_sell_pct",
    "take_profit_pct", "usd_per_buy", "max_grid_levels",
)
_NUMERIC_KEYS = (
    "drop_pct", "trail_buy_pct", "trail_sell_pct",
    "take_profit_pct", "usd_per_buy", "max_grid_levels",
)


class CoinChange(NamedTuple):
    """One running coin's pending re-tune. `target` is the LIVE cfg dict
    (mutated on apply); `new_entry` is the freshly parsed replacement --
    private to this plan, so applying later applies exactly what was
    previewed even if config.py changes again in between."""
    symbol: str
    target: dict
    new_entry: dict
    field_changes: list          # [(key, old_value, new_value)]; _MISSING marks absence


class ReloadPlan(NamedTuple):
    changes: list                # [CoinChange] -- only coins with >=1 field change
    warnings: list               # [str] -- structural needs-restart notes


def format_value(v) -> str:
    """Render a diff value; the _MISSING sentinel prints as '(unset)'."""
    return "(unset)" if v is _MISSING else repr(v)


def parse_config_coins(path: str | Path = DEFAULT_CONFIG_PATH) -> list:
    """Exec a fresh copy of config.py source and return its COINS list.

    Runs in a throwaway namespace with __file__ set so config.py's
    _load_dotenv(Path(__file__)...) resolves the real project .env
    (os.environ.setdefault re-runs are harmless -- existing env wins).
    Raises on ANY problem (unreadable file, SyntaxError, runtime error,
    missing/malformed COINS, duplicate symbols); callers abort the reload
    and nothing in the live process has changed.
    """
    path = Path(path)
    src = path.read_text()
    code = compile(src, str(path), "exec")   # real filename -> readable tracebacks
    ns: dict = {"__file__": str(path), "__name__": "helpers._config_reload"}
    exec(code, ns)

    coins = ns.get("COINS")
    if not isinstance(coins, list) or not coins:
        raise ValueError("COINS missing, not a list, or empty after exec")
    seen: set = set()
    for i, entry in enumerate(coins):
        if not isinstance(entry, dict):
            raise ValueError(f"COINS[{i}] is not a dict")
        sym = entry.get("symbol")
        if not isinstance(sym, str) or not sym:
            raise ValueError(f"COINS[{i}] has no valid 'symbol'")
        if sym in seen:
            raise ValueError(f"duplicate symbol {sym!r} in COINS -- fix config.py")
        seen.add(sym)
    return coins


def _validate_tunables(entry: dict) -> None:
    """Reject an entry that would crash a RUNNING task if applied.

    Stricter than startup (which does no validation) on purpose: reload must
    not inject a KeyError/ValueError time bomb into a live coin. Raises
    ValueError naming the coin and key.
    """
    sym = entry["symbol"]
    for key in REQUIRED_KEYS:
        if key not in entry:
            raise ValueError(f"{sym}: missing required key {key!r}")
    pp = entry["price_prec"]
    if isinstance(pp, bool) or not isinstance(pp, int):
        # price_prec feeds f-string precision (f"...:.{pp}f") -- a float here
        # raises ValueError at display time, killing status output.
        raise ValueError(f"{sym}: price_prec must be an int (got {pp!r})")
    for key in _NUMERIC_KEYS:
        v = entry[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError(f"{sym}: {key} must be a number (got {v!r})")


def diff_coins(new_coins: list, running: list) -> ReloadPlan:
    """Compare freshly parsed entries against the running coin dicts.

    running: conductor's `enabled` list (the live dict objects).
    Tunable diffs -> CoinChange; structural differences -> warnings only.
    A coin disabled in the file is skipped ENTIRELY (its other edits are NOT
    applied): the user's intent is "stop this coin", and re-tuning something
    they mentally decommissioned is surprising. It also keeps the invariant
    that a running cfg never carries enabled=False.
    Raises ValueError (via _validate_tunables) if any would-be-applied entry
    is malformed -- the whole reload aborts; fix the file and press 'c' again.
    """
    new_by_symbol = {e["symbol"]: e for e in new_coins}   # dupes rejected in parse
    running_syms = {c["symbol"] for c in running}
    changes: list = []
    warnings: list = []

    for cfg in running:
        sym = cfg["symbol"]
        new = new_by_symbol.get(sym)
        if new is None:
            warnings.append(
                f"{sym}: removed from config.py -- needs restart to stop "
                f"(pause it now via menu 4 meanwhile)"
            )
            continue
        if not new.get("enabled", True):
            warnings.append(
                f"{sym}: disabled in config.py -- needs restart to stop "
                f"(pause it now via menu 4 meanwhile); its other edits NOT applied"
            )
            continue
        _validate_tunables(new)
        field_changes = []
        for key in sorted(set(cfg) | set(new)):
            if key in STRUCTURAL_KEYS:
                continue
            old_v = cfg.get(key, _MISSING)
            new_v = new.get(key, _MISSING)
            if old_v != new_v:
                field_changes.append((key, old_v, new_v))
        if field_changes:
            changes.append(CoinChange(sym, cfg, new, field_changes))

    for e in new_coins:
        sym = e["symbol"]
        if sym not in running_syms and e.get("enabled", True):
            warnings.append(f"{sym}: added/enabled in config.py but not running -- needs restart to start")

    # Reorder detection: menu numbers (and the 'a' table order) are fixed at
    # startup from the running order; warn so nobody re-tunes coin "3"
    # thinking the new file order took effect.
    run_order = [c["symbol"] for c in running if c["symbol"] in new_by_symbol]
    file_order = [e["symbol"] for e in new_coins if e["symbol"] in running_syms]
    if run_order != file_order:
        warnings.append("COINS order changed -- menu numbering keeps the RUNNING order until restart")

    return ReloadPlan(changes, warnings)


def apply_changes(plan: ReloadPlan) -> list:
    """Swap each changed coin's new entry into the live dict IN PLACE.

    clear()+update() keeps the dict object identity, so every reference
    holder sees the new values, and keys deleted from the file genuinely
    disappear (code .get() defaults apply again). Both calls are synchronous
    with no awaits possible between them, so the single-threaded event loop
    can never observe a half-updated dict. Returns one audit line per coin
    for the caller to log.
    """
    lines = []
    for change in plan.changes:
        detail = ", ".join(
            f"{key}: {format_value(old_v)} -> {format_value(new_v)}"
            for key, old_v, new_v in change.field_changes
        )
        change.target.clear()
        change.target.update(change.new_entry)
        lines.append(f"CONFIG RELOAD {change.symbol}: {detail}")
    return lines
