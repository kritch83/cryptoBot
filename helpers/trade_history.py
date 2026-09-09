"""Closed-cycle trade history, recovered from data/grid_bot.log.

The bot keeps only a running total per coin (State.realized_pnl_usd) plus the
retired ledger -- there is no per-trade store. But every booked sell already
writes one fully-structured line to the log:

    ... [VVV/USD] [LIVE] LIMIT-SELL FILL price=$20.243 qty=5.28317000
        fee=$0.160 net=$106.787 avg_entry=$18.955 levels=1
        realized=$6.642 (+6.63263%) cycle=21 (limit-sell filled)

so the history the dashboard charts is parsed back out of those lines rather
than invented from a new write path. That deliberately keeps this module
READ-ONLY with respect to the bot: nothing here can affect trading, and a
parsing bug costs a chart, never a position.

Parsing is incremental. The live log is appended to constantly (4.7 MB and
growing), so each refresh seeks to where the last parse stopped and reads only
the new bytes; rotation (or any shrink/inode change) triggers a full re-read.
Rotated logs are immutable, so they're parsed once and cached on size+mtime.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import LOG_FILE

# One booked sell. Matches both tags that reach _finalize_sell ("SELL-ALL" for
# market exits, "LIMIT-SELL FILL" for resting limit fills) and their PARTIAL
# variants, whose qty field is "qty=<sold>/<tracked>".
_SELL_RE = re.compile(
    r"^(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+\s+\w+\s+"
    r"\[(?P<symbol>[^\]]+)\]\s+\[(?P<mode>[A-Z]+)\]\s+(?P<tag>[A-Z][A-Z -]*?)\s+"
    r"price=\$(?P<price>[\d,.]+)\s+"
    r"qty=(?P<qty>[\d.]+)(?:/(?P<tracked>[\d.]+))?\s+"
    r"fee=\$(?P<fee>[-\d,.]+)\s+net=\$(?P<net>[-\d,.]+)\s+"
    r"avg_entry=\$(?P<avg_entry>[\d,.]+)\s+levels=(?P<levels>\d+)\s+"
    r"realized=\$(?P<realized>[-\d,.]+)\s+\((?P<pct>[-+\d.]+)%\)\s+"
    r"cycle=(?P<cycle>\d+)\s+\((?P<reason>.*)\)\s*$"
)

# path -> {"key": (inode, size), "offset": int, "trades": [...]}
_cache: dict[str, dict] = {}


def _num(text: str) -> float:
    return float(text.replace(",", ""))


def _parse_lines(lines) -> list[dict]:
    out: list[dict] = []
    for line in lines:
        if "realized=" not in line:
            continue
        m = _SELL_RE.match(line)
        if m is None:
            continue
        try:
            stamp = datetime.strptime(m["ts"], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        out.append({
            "ts":       stamp.replace(tzinfo=timezone.utc).timestamp(),
            "time":     m["ts"],
            "symbol":   m["symbol"],
            "mode":     m["mode"].lower(),
            "tag":      m["tag"].strip(),
            "partial":  m["tracked"] is not None,
            "price":    _num(m["price"]),
            "qty":      _num(m["qty"]),
            "fee_usd":  _num(m["fee"]),
            "net_usd":  _num(m["net"]),
            "avg_entry": _num(m["avg_entry"]),
            "levels":   int(m["levels"]),
            "realized": _num(m["realized"]),
            "pct":      float(m["pct"]),
            "cycle":    int(m["cycle"]),
            "reason":   m["reason"],
        })
    return out


def _read_file(path: Path) -> list[dict]:
    """Trades from one log file, re-reading only what was appended since last time."""
    key_path = str(path)
    try:
        stat = path.stat()
    except OSError:
        _cache.pop(key_path, None)
        return []

    entry = _cache.get(key_path)
    key = (stat.st_ino, stat.st_size)
    if entry is not None and entry["key"][0] == key[0] and stat.st_size >= entry["offset"]:
        if stat.st_size == entry["offset"]:
            return entry["trades"]
        start, trades = entry["offset"], entry["trades"]
    else:
        start, trades = 0, []

    try:
        with path.open("r", errors="replace") as handle:
            handle.seek(start)
            fresh = _parse_lines(handle)
            offset = handle.tell()
    except OSError as exc:
        logging.warning("trade history: could not read %s: %s", path, exc)
        return trades

    trades = trades + fresh
    _cache[key_path] = {"key": key, "offset": offset, "trades": trades}
    return trades


def _log_files() -> list[Path]:
    """Live log plus its rotations, oldest first."""
    live = Path(LOG_FILE)
    rotated = sorted(
        (p for p in live.parent.glob(live.name + ".*") if p.suffix.lstrip(".").isdigit()),
        key=lambda p: int(p.suffix.lstrip(".")),
        reverse=True,
    )
    return [*rotated, live]


def all_trades(symbol: Optional[str] = None) -> list[dict]:
    """Every booked sell across all log files, oldest first.

    Duplicates are possible in principle (a log line copied by a rotation race),
    so identical (ts, symbol, cycle, realized) tuples are collapsed.
    """
    seen: set = set()
    trades: list[dict] = []
    for path in _log_files():
        for trade in _read_file(path):
            if symbol is not None and trade["symbol"] != symbol:
                continue
            fingerprint = (trade["ts"], trade["symbol"], trade["cycle"], trade["realized"])
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            trades.append(trade)
    trades.sort(key=lambda t: t["ts"])
    return trades


def summary(symbol: Optional[str] = None, limit: int = 200) -> dict:
    """Trade list + the aggregates the dashboard charts.

    `cumulative` is realized PnL running-summed in time order -- the equity
    curve. `daily` buckets realized PnL by calendar day (UTC) for the bar
    chart. `wins`/`losses` count closed sells, not levels.

    NOTE: these totals cover only what is still IN THE LOGS. Coins retired long
    ago, or cycles whose lines have rotated out, are missing -- which is why the
    dashboard labels this "from logs" and keeps State.realized_pnl_usd plus the
    retired ledger as the authoritative totals.
    """
    trades = all_trades(symbol)

    running = 0.0
    cumulative = []
    daily: dict[str, float] = {}
    per_symbol: dict[str, dict] = {}
    wins = losses = 0
    best = worst = None

    for trade in trades:
        running += trade["realized"]
        cumulative.append({"ts": trade["ts"], "value": running,
                           "symbol": trade["symbol"], "realized": trade["realized"]})
        day = trade["time"][:10]
        daily[day] = daily.get(day, 0.0) + trade["realized"]

        bucket = per_symbol.setdefault(trade["symbol"], {
            "symbol": trade["symbol"], "realized": 0.0, "trades": 0,
            "wins": 0, "losses": 0, "fees": 0.0, "last_ts": 0.0,
        })
        bucket["realized"] += trade["realized"]
        bucket["trades"] += 1
        bucket["fees"] += trade["fee_usd"]
        bucket["last_ts"] = max(bucket["last_ts"], trade["ts"])
        if trade["realized"] >= 0:
            bucket["wins"] += 1
            wins += 1
        else:
            bucket["losses"] += 1
            losses += 1

        if best is None or trade["realized"] > best["realized"]:
            best = trade
        if worst is None or trade["realized"] < worst["realized"]:
            worst = trade

    return {
        "symbol":     symbol,
        "trades":     trades[-limit:] if limit else trades,
        "count":      len(trades),
        "realized":   running,
        "fees":       sum(t["fee_usd"] for t in trades),
        "wins":       wins,
        "losses":     losses,
        "best":       best,
        "worst":      worst,
        "cumulative": cumulative,
        "daily":      [{"day": d, "realized": v} for d, v in sorted(daily.items())],
        "per_symbol": sorted(per_symbol.values(), key=lambda b: b["realized"], reverse=True),
    }


def tail_log(limit: int = 200, symbol: Optional[str] = None,
             contains: Optional[str] = None) -> list[str]:
    """Last `limit` lines of the live log, optionally filtered to one coin.

    Reads a bounded window off the end rather than the whole file -- the live
    log is multi-megabyte and this is polled.
    """
    path = Path(LOG_FILE)
    window = max(64_000, limit * 400)
    try:
        size = os.path.getsize(path)
        with path.open("r", errors="replace") as handle:
            handle.seek(max(0, size - window))
            lines = handle.read().splitlines()
    except OSError as exc:
        return [f"(could not read {path}: {exc})"]

    if size > window and lines:
        lines = lines[1:]                       # drop the partial first line
    tag = f"[{symbol}]" if symbol else None
    if tag:
        lines = [ln for ln in lines if tag in ln]
    if contains:
        needle = contains.lower()
        lines = [ln for ln in lines if needle in ln.lower()]
    return lines[-limit:]
