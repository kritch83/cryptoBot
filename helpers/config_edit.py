"""Write coin tunables back into helpers/config.py, preserving the file.

config_reload.py READS config.py (parse -> diff -> apply in memory). This
module is the write half: it edits one coin's values in the source text so a
change made in the dashboard survives a restart, instead of living only in the
running process.

The edit is surgical, not a re-serialization: config.py is hand-maintained --
tab-aligned values, per-coin comments recording why a number was changed,
whole commented-out coin blocks -- and regenerating it from parsed data would
throw all of that away. So we locate the exact source span of the value we're
replacing via the AST and splice the new literal into it. Every other byte of
the file, comments included, is left untouched.

Safety, in order:
  1. the new value is type/range checked against FIELD_SPECS,
  2. the edited source is written to a sibling temp file and parsed with
     config_reload.parse_config_coins() -- if the result doesn't load, or the
     coin doesn't come back with the intended values, the temp file is removed
     and config.py is never touched,
  3. a timestamped copy of the previous config.py is kept under
     data/config_backups/,
  4. only then does os.replace() swap the new file in (atomic on POSIX).

Nothing here touches the running process. Hot-applying the change to the live
coin dicts is the caller's job (see dashboard.apply_config_edit), which reuses
the same config_reload.apply_changes() path the 'c' key uses.
"""
from __future__ import annotations

import ast
import os
import shutil
import time
from pathlib import Path
from typing import Any, NamedTuple, Optional

from .config_reload import DEFAULT_CONFIG_PATH, parse_config_coins

BACKUP_DIR = Path("data/config_backups")
BACKUP_KEEP = 30                    # newest N kept; older pruned after each write


class FieldSpec(NamedTuple):
    kind: str                       # "int" | "float" | "bool" | "choice" | "str"
    label: str                      # form label in the dashboard
    hint: str = ""                  # short help under the input
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    choices: tuple = ()
    as_pct: bool = False            # value is a fraction; UI shows a % hint too
    structural: bool = True         # True = file-only, needs a restart to take effect
    group: str = "buy"              # form section: buy | sell | general


# Everything a user may edit from the dashboard. Anything not listed here is
# refused outright -- 'symbol' above all, because it keys states/cmd_queues/
# last_price and renaming it at runtime would orphan the running task.
FIELD_SPECS: dict[str, FieldSpec] = {
    "enabled": FieldSpec(
        "bool", "Enabled",
        "Structural: a restart is required to start or stop the coin's task.",
        structural=True, group="general"),
    "price_prec": FieldSpec(
        "int", "Price precision", "Decimals used to display this pair's price.",
        minimum=0, maximum=12, structural=False, group="general"),
    "blynk_pin": FieldSpec(
        "str", "Blynk pin", "Virtual pin this coin's realized PnL is pushed to (e.g. V7).",
        structural=False, group="general"),

    "usd_per_buy": FieldSpec(
        "float", "USD per buy", "Quote spent on each grid level.",
        minimum=0.0, maximum=1_000_000.0, structural=False, group="buy"),
    "max_grid_levels": FieldSpec(
        "float", "Max grid levels", "Cap on simultaneously open levels.",
        minimum=1, maximum=200, structural=False, group="buy"),
    "drop_pct": FieldSpec(
        "float", "Drop pct", "Spacing between grid buys, as a fraction.",
        minimum=0.0, maximum=1.0, as_pct=True, structural=False, group="buy"),
    "trail_buy_pct": FieldSpec(
        "float", "Trail buy pct", "Rebound off the low that fires the buy.",
        minimum=0.0, maximum=1.0, as_pct=True, structural=False, group="buy"),
    "buy_order_type": FieldSpec(
        "choice", "Buy order type", "'force BUY now' is always market regardless.",
        choices=("market", "limit"), structural=False, group="buy"),
    "limit_buy_offset_pct": FieldSpec(
        "float", "Buy limit offset", "limit_price = bid * (1 - offset).",
        minimum=0.0, maximum=1.0, as_pct=True, structural=False, group="buy"),

    "take_profit_pct": FieldSpec(
        "float", "Take profit pct", "Above avg entry before the sell-trail can arm.",
        minimum=0.0, maximum=10.0, as_pct=True, structural=False, group="sell"),
    "trail_sell_pct": FieldSpec(
        "float", "Trail sell pct", "Pullback off the high that fires the sell.",
        minimum=0.0, maximum=1.0, as_pct=True, structural=False, group="sell"),
    "order_type": FieldSpec(
        "choice", "Sell order type", "'sell ALL now' is always market regardless.",
        choices=("market", "limit"), structural=False, group="sell"),
    "limit_sell_offset_pct": FieldSpec(
        "float", "Sell limit offset", "limit_price = last * (1 + offset).",
        minimum=0.0, maximum=1.0, as_pct=True, structural=False, group="sell"),
}

# Fields whose edit only reaches a running coin after a restart.
STRUCTURAL_FIELDS = frozenset(k for k, s in FIELD_SPECS.items() if s.structural)


class FieldEdit(NamedTuple):
    key: str
    old: Any                 # None when the key was absent
    new: Any
    was_absent: bool


class ConfigEditError(Exception):
    """Refused edit -- config.py is unchanged. Message is user-facing."""


# --- value coercion / rendering ---------------------------------------------

def coerce(key: str, raw: Any) -> Any:
    """Turn a JSON-decoded value into the type config.py should hold.

    Raises ConfigEditError with a readable message on anything unusable, so a
    bad form entry can never reach the file.
    """
    spec = FIELD_SPECS.get(key)
    if spec is None:
        raise ConfigEditError(f"{key!r} is not an editable field")

    if spec.kind == "bool":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str) and raw.strip().lower() in ("true", "false"):
            return raw.strip().lower() == "true"
        raise ConfigEditError(f"{spec.label}: expected true/false (got {raw!r})")

    if spec.kind == "choice":
        val = str(raw).strip()
        if val not in spec.choices:
            raise ConfigEditError(
                f"{spec.label}: must be one of {', '.join(spec.choices)} (got {raw!r})")
        return val

    if spec.kind == "str":
        val = str(raw).strip()
        if not val:
            raise ConfigEditError(f"{spec.label}: cannot be empty")
        if len(val) > 32 or any(c in val for c in "\"'\\\n\r"):
            raise ConfigEditError(f"{spec.label}: invalid text {raw!r}")
        return val

    # numeric
    if isinstance(raw, bool):
        raise ConfigEditError(f"{spec.label}: expected a number (got {raw!r})")
    try:
        num = float(str(raw).strip())
    except (TypeError, ValueError):
        raise ConfigEditError(f"{spec.label}: expected a number (got {raw!r})") from None
    if num != num or num in (float("inf"), float("-inf")):
        raise ConfigEditError(f"{spec.label}: expected a finite number (got {raw!r})")
    if spec.kind == "int":
        if num != int(num):
            raise ConfigEditError(f"{spec.label}: must be a whole number (got {raw!r})")
        num = int(num)
    elif num.is_integer() and "." not in str(raw) and "e" not in str(raw).lower():
        # "14" stays 14, "14.0" stays 14.0 -- don't churn the file's int/float
        # spelling just because the value round-tripped through JSON.
        num = int(num)
    if spec.minimum is not None and num < spec.minimum:
        raise ConfigEditError(f"{spec.label}: must be >= {spec.minimum:g} (got {num:g})")
    if spec.maximum is not None and num > spec.maximum:
        raise ConfigEditError(f"{spec.label}: must be <= {spec.maximum:g} (got {num:g})")
    return num


def render(value: Any) -> str:
    """Source text for a literal, formatted the way config.py writes them.

    repr() is wrong for the small offsets this file is full of -- repr(0.00002)
    is '2e-05', which is correct Python but reads as a typo next to the
    hand-written '0.0001' on the line above. Floats are rendered in plain
    decimal instead.
    """
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        text = repr(value)
        if "e" in text or "E" in text:
            text = f"{value:.12f}".rstrip("0")
            if text.endswith("."):
                text += "0"
        return text
    return '"%s"' % value


# --- source splicing --------------------------------------------------------

def _line_starts(data: bytes) -> list[int]:
    """Byte offset of the first byte of each 1-based source line."""
    starts = [0, 0]
    for i, byte in enumerate(data):
        if byte == 0x0A:
            starts.append(i + 1)
    return starts


def _span(starts: list[int], node: ast.AST) -> tuple[int, int]:
    """Absolute byte span of an AST node (col offsets are UTF-8 byte offsets)."""
    return (starts[node.lineno] + node.col_offset,
            starts[node.end_lineno] + node.end_col_offset)


def _find_coin_dict(tree: ast.Module, symbol: str) -> ast.Dict:
    """The ast.Dict for `symbol` inside the top-level COINS list."""
    coins_node = None
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else []
        if any(isinstance(t, ast.Name) and t.id == "COINS" for t in targets):
            coins_node = node.value
    if not isinstance(coins_node, ast.List):
        raise ConfigEditError("COINS is missing or is not a plain list literal in config.py")

    for element in coins_node.elts:
        if not isinstance(element, ast.Dict):
            continue
        for key_node, value_node in zip(element.keys, element.values):
            if (isinstance(key_node, ast.Constant) and key_node.value == "symbol"
                    and isinstance(value_node, ast.Constant) and value_node.value == symbol):
                return element
    raise ConfigEditError(f"{symbol} not found in COINS (is it commented out?)")


def _entry_for(symbol: str, coins: list) -> dict:
    for entry in coins:
        if entry.get("symbol") == symbol:
            return entry
    raise ConfigEditError(f"{symbol} not found in COINS")


def _splice_field(data: bytes, starts: list[int], coin: ast.Dict,
                  key: str, value: Any) -> tuple[bytes, bool]:
    """Replace (or insert) one key's value. Returns (new_bytes, was_absent)."""
    for key_node, value_node in zip(coin.keys, coin.values):
        if isinstance(key_node, ast.Constant) and key_node.value == key:
            start, end = _span(starts, value_node)
            return data[:start] + render(value).encode() + data[end:], False

    # Absent: append a new entry, copying the indentation and the "key":-to-value
    # column of the LAST existing pair so tab-aligned blocks stay aligned.
    if not coin.keys:
        raise ConfigEditError(f"cannot add {key!r} to an empty coin entry")
    last_key = coin.keys[-1]
    key_start, _ = _span(starts, last_key)
    line_start = starts[last_key.lineno]
    indent = data[line_start:key_start].decode()
    if indent.strip():                      # more than one pair on that line
        indent = " " * 8
    # Pad so the new value lands in the same column as the one above it. The
    # file aligns with tabs in some blocks and spaces in others, so the target
    # is computed on a tab-expanded copy and padded with spaces -- which lands
    # correctly either way at the tabstop-8 these blocks are written for.
    last_start, last_end = _span(starts, coin.values[-1])
    line_start = starts[coin.values[-1].lineno]
    column = len(data[line_start:last_start].decode().expandtabs(8))
    head = f'{indent}"{key}":'
    gap = " " * max(1, column - len(head.expandtabs(8)))

    insert_at = last_end
    prefix = ""
    if data[insert_at:insert_at + 1] == b",":
        insert_at += 1
    else:
        prefix = ","
    new_line = f'{prefix}\n{head}{gap}{render(value)},'
    return data[:insert_at] + new_line.encode() + data[insert_at:], True


def _backup(path: Path) -> Optional[Path]:
    """Timestamped copy of the current config.py; None if the copy failed.

    A failed backup is not fatal -- the edit itself is already validated and
    atomic -- but the caller reports it so the user knows there's no undo.
    """
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        dest = BACKUP_DIR / f"config_{stamp}.py"
        bump = 1
        while dest.exists():          # two edits in the same second must not collide
            dest = BACKUP_DIR / f"config_{stamp}-{bump}.py"
            bump += 1
        shutil.copy2(path, dest)
        stale = sorted(BACKUP_DIR.glob("config_*.py"))[:-BACKUP_KEEP]
        for old in stale:
            old.unlink(missing_ok=True)
        return dest
    except OSError:
        return None


def set_fields(symbol: str, updates: dict, path: str | Path = DEFAULT_CONFIG_PATH
               ) -> tuple[list[FieldEdit], Optional[Path]]:
    """Apply `updates` to one coin in config.py. Returns (edits, backup_path).

    `updates` maps field name -> raw (JSON-decoded) value; values are coerced
    and range-checked via FIELD_SPECS. Fields already holding the requested
    value are dropped, so a no-op form submit doesn't rewrite the file. Raises
    ConfigEditError -- with config.py untouched -- on any validation, parse or
    write-back failure.
    """
    path = Path(path)
    if not updates:
        raise ConfigEditError("no fields to update")

    wanted = {key: coerce(key, raw) for key, raw in updates.items()}

    data = path.read_bytes()
    tree = ast.parse(data, filename=str(path))
    starts = _line_starts(data)
    coin_node = _find_coin_dict(tree, symbol)

    current = _entry_for(symbol, parse_config_coins(path))
    edits: list[FieldEdit] = []
    for key, value in wanted.items():
        old = current.get(key, None)
        absent = key not in current
        if not absent and old == value and type(old) is type(value):
            continue
        edits.append(FieldEdit(key, old, value, absent))
    if not edits:
        raise ConfigEditError("nothing to change -- values already match config.py")

    # Splice right-to-left: an earlier edit must not shift a later edit's span.
    def _pos(edit: FieldEdit) -> int:
        for key_node, value_node in zip(coin_node.keys, coin_node.values):
            if isinstance(key_node, ast.Constant) and key_node.value == edit.key:
                return _span(starts, value_node)[0]
        return 1 << 62      # insertions go last

    new_data = data
    for edit in sorted(edits, key=_pos, reverse=True):
        new_data, _ = _splice_field(new_data, starts, coin_node, edit.key, edit.new)
        # Re-parse so the next splice sees the shifted source. Cheap (one file)
        # and far safer than tracking offset deltas by hand.
        tree = ast.parse(new_data, filename=str(path))
        starts = _line_starts(new_data)
        coin_node = _find_coin_dict(tree, symbol)

    # Validate the candidate as a real config before it can become config.py.
    tmp = path.with_suffix(".py.tmp-edit")
    try:
        tmp.write_bytes(new_data)
        reparsed = _entry_for(symbol, parse_config_coins(tmp))
        for edit in edits:
            got = reparsed.get(edit.key, None)
            if got != edit.new:
                raise ConfigEditError(
                    f"write-back check failed for {edit.key}: file would hold "
                    f"{got!r}, expected {edit.new!r} -- config.py not modified")
        backup = _backup(path)
        os.replace(tmp, path)
    except ConfigEditError:
        tmp.unlink(missing_ok=True)
        raise
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        raise ConfigEditError(f"edited config.py did not load ({exc}) -- not saved") from exc

    return edits, backup
