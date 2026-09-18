"""Account value over time -- the dashboard's "Account value" chart.

The bot has no record of what the account was worth yesterday: balances are
polled, used and forgotten. So while the dashboard runs it appends one line
per EQUITY_EVERY_SEC to data/equity_history.jsonl:

    {"ts": 1789700000.0, "total": 10834.2, "cash": 5146.7, "coins": 5232.8,
     "other": 451.8, "realized": 3038.4, "unrealized": -1260.0}

`realized` rides along so the chart can separate what the bot banked from
what the market did (and from deposits / withdrawals, which show up as jumps
in `total` that `realized` doesn't explain).

READ-ONLY with respect to trading: a failure here costs a chart point, never a
position. Samples are skipped rather than written when the total is known to
be incomplete (no balance yet, a coin without a price, a stale balance), since
a dip that is really missing data would be worse than a gap.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
EQUITY_FILE = DATA_DIR / "equity_history.jsonl"
EQUITY_EVERY_SEC = 600          # one sample per 10 minutes (~5 MB per year)
MAX_BALANCE_AGE_SEC = 300       # older than this and the cash side is stale


def sample(snap: dict) -> dict | None:
    """The line to record for this snapshot, or None if it isn't trustworthy."""
    t = snap["totals"]
    if t.get("total_value") is None or t.get("coins_no_price"):
        return None
    age = snap["wallet"].get("kraken_age_sec")
    if snap["mode"] == "live" and (age is None or age > MAX_BALANCE_AGE_SEC):
        return None
    return {
        "ts": round(snap["ts"], 1),
        "total": round(t["total_value"], 2),
        "cash": round(t["cash_usd"], 2),
        "coins": round(t["holdings_value"], 2),
        "other": round(t.get("other_value") or 0.0, 2),
        "realized": round(t["realized_total"], 2),
        "unrealized": round(t["unrealized"], 2),
    }


def append(row: dict, path: Path = EQUITY_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(row, separators=(",", ":")) + "\n")


def load(days: float | None = None, max_points: int = 600,
         path: Path = EQUITY_FILE) -> list[dict]:
    """Samples from the last `days` (all if None), thinned to <= max_points.

    Thinning keeps the LAST sample of each time bucket, so the final point is
    always the newest value and the period change stays exact.
    """
    if not path.exists():
        return []
    since = time.time() - days * 86400 if days else 0.0
    rows = []
    with path.open() as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except ValueError:
                continue          # a torn last line after a crash
            if row.get("ts", 0) >= since:
                rows.append(row)
    if len(rows) <= max_points:
        return rows
    t0, t1 = rows[0]["ts"], rows[-1]["ts"]
    width = (t1 - t0) / max_points or 1.0
    buckets: dict[int, dict] = {}
    for row in rows:
        buckets[int((row["ts"] - t0) / width)] = row
    thinned = [buckets[k] for k in sorted(buckets)]
    if thinned[0] is not rows[0]:
        thinned.insert(0, rows[0])  # keep the period's true starting value
    return thinned


async def recorder(build_snapshot, every: float = EQUITY_EVERY_SEC) -> None:
    """Background task: record a sample every `every` seconds, forever.

    The first sample waits a minute so the balance poller has landed.
    """
    import asyncio
    await asyncio.sleep(60)
    while True:
        try:
            row = sample(build_snapshot())
            if row is not None:
                await asyncio.to_thread(append, row)
        except Exception as exc:   # never let a chart kill the task
            logging.warning("Equity sample failed: %s", exc)
        await asyncio.sleep(every)
