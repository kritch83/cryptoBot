"""Retired-coin realized-PnL ledger.

data/retired_pnl.json maps symbol -> {"realized_pnl_usd", "cycles", "retired"}.
The file is HUMAN-EDITABLE: hand-add coins that were removed before this
feature existed (a bare number entry like "TRX/USD": 5.23 is tolerated too).
Reads are fresh on every call so hand edits show up without a restart; on a
read error the last-good copy is served so totals never transiently drop
(e.g. mid-edit while the Blynk heartbeat fires).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from .config import RETIRED_FILE

_cache: dict = {}       # last-good parse
_warned: bool = False   # warn once per corruption, not per heartbeat


def load_retired() -> dict:
    """Fresh read of the ledger; {} if missing; last-good copy on parse error."""
    global _cache, _warned
    path = Path(RETIRED_FILE)
    if not path.exists():
        _cache = {}
        return {}
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError("ledger root must be a JSON object")
        _cache, _warned = data, False
        return data
    except Exception as e:
        if not _warned:
            logging.warning("Could not read %s: %s -- using last-good values", RETIRED_FILE, e)
            _warned = True
        return _cache


def total_retired() -> float:
    """Sum of retired realized PnL. Tolerates bare-number entries; skips junk.
    Never raises -- callers include the Blynk push, which must not break trading."""
    total = 0.0
    for sym, entry in load_retired().items():
        try:
            total += float(entry.get("realized_pnl_usd", 0.0)) if isinstance(entry, dict) else float(entry)
        except (AttributeError, TypeError, ValueError):
            logging.warning("retired ledger: unreadable entry for %r -- skipped", sym)
    return total


def add_retired(symbol: str, pnl_usd: float, cycles: int) -> bool:
    """Book (accumulate) a coin's lifetime PnL + cycles into the ledger.

    Returns False if the write failed -- the caller must NOT reset the coin's
    state in that case (never zero PnL that isn't durably booked). Fully
    synchronous, so the read-modify-write is atomic within the event loop.
    """
    global _cache
    data = dict(load_retired())
    old = data.get(symbol)
    if isinstance(old, dict):
        prev_pnl, prev_cycles = float(old.get("realized_pnl_usd", 0.0)), int(old.get("cycles", 0))
    elif isinstance(old, (int, float)):
        prev_pnl, prev_cycles = float(old), 0
    else:
        prev_pnl, prev_cycles = 0.0, 0
    data[symbol] = {
        "realized_pnl_usd": prev_pnl + float(pnl_usd),
        "cycles": prev_cycles + int(cycles),
        "retired": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }
    try:
        path = Path(RETIRED_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2))
        _cache = data
        return True
    except Exception as e:
        logging.error("Could not write %s: %s -- retire NOT booked", RETIRED_FILE, e)
        return False
