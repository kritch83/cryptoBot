"""Local cache of coin logos for the dashboard.

Logos are fetched ONCE per coin into data/icons/ and served from there
afterwards, so the dashboard stays self-contained: no per-page-load request to
a third party, and no leaking which coins you trade every time you open it.

Nothing here runs on its own. The bot never reaches out for icons as a side
effect of trading -- a fetch happens only when you ask for one, either from the
dashboard's "Fetch icons" button or by running this file directly:

    python helpers/icons.py

Matching is by ticker against CoinGecko's top coins by market cap, which is a
guess: tickers are not unique (M, AI, SYN, MON, PUMP all collide with other
tokens). The highest-cap match wins, the chosen CoinGecko id is recorded in the
manifest, and the refresh report names it so a wrong pick is visible. To correct
one, put the right id in data/icons/overrides.json:

    {"M": "some-coingecko-id"}

and refresh again. Coins with no match keep the dashboard's generated monogram
badge, which is also what every coin falls back to if you never fetch at all.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterable, Optional

ICON_DIR = Path("data/icons")
MANIFEST = ICON_DIR / "_manifest.json"
MARKET_CACHE = ICON_DIR / "_markets.json"
OVERRIDES = ICON_DIR / "overrides.json"

MARKET_URL = ("https://api.coingecko.com/api/v3/coins/markets"
              "?vs_currency=usd&order=market_cap_desc&per_page=250&page={page}&sparkline=false")
MARKET_PAGES = 4                 # top 1000 by market cap -- plenty for a Kraken pair list
MARKET_TTL_SEC = 7 * 24 * 3600   # the symbol -> logo mapping barely moves

HTTP_TIMEOUT = 15.0
MAX_ICON_BYTES = 512 * 1024
MAX_FETCH_PER_CALL = 60          # bound one refresh, so a typo can't spider the API
USER_AGENT = "gridBot-dashboard/1.0 (local dashboard icon cache)"

# Accepted image types, by magic bytes. SVG is deliberately NOT accepted: it can
# carry script, and these files are served back to the browser.
_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (b"\xff\xd8\xff", "jpg", "image/jpeg"),
    (b"GIF87a", "gif", "image/gif"),
    (b"GIF89a", "gif", "image/gif"),
)


def base_of(symbol: str) -> str:
    """VVV/USD -> VVV."""
    return symbol.split("/")[0].strip().upper()


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def load_manifest() -> dict:
    data = _read_json(MANIFEST, {})
    return data if isinstance(data, dict) else {}


def icon_file(base: str, manifest: Optional[dict] = None) -> Optional[Path]:
    """Cached icon path for a base asset, or None if we don't have one."""
    entry = (manifest if manifest is not None else load_manifest()).get(base.upper())
    if not isinstance(entry, dict):
        return None
    path = ICON_DIR / str(entry.get("file", ""))
    return path if path.name and path.is_file() else None


def have_icons(bases: Iterable[str]) -> dict:
    """{BASE: bool} -- which of these already have a cached icon."""
    manifest = load_manifest()
    return {b: icon_file(b, manifest) is not None for b in {x.upper() for x in bases}}


def content_type(path: Path) -> str:
    """Sniff the stored file rather than trusting its extension."""
    try:
        head = path.open("rb").read(12)
    except OSError:
        return "application/octet-stream"
    for magic, _ext, mime in _MAGIC:
        if head.startswith(magic):
            return mime
    return "application/octet-stream"


def _get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status}")
        return response.read(MAX_ICON_BYTES + 1)


def _market_map(force: bool = False) -> dict:
    """{ticker_lowercase: {id, name, image}} for the top coins by market cap.

    Cached on disk; the mapping is stable enough that re-fetching it on every
    refresh would just burn CoinGecko's rate limit.
    """
    cached = _read_json(MARKET_CACHE, None)
    if (not force and isinstance(cached, dict)
            and time.time() - float(cached.get("fetched", 0)) < MARKET_TTL_SEC
            and isinstance(cached.get("coins"), dict)):
        return cached["coins"]

    coins: dict = {}
    for page in range(1, MARKET_PAGES + 1):
        raw = json.loads(_get(MARKET_URL.format(page=page)).decode())
        if not isinstance(raw, list) or not raw:
            break
        for entry in raw:
            ticker = str(entry.get("symbol", "")).upper()
            image = entry.get("image")
            # First hit wins: the pages arrive in market-cap order, so the
            # biggest coin with a given ticker is the one we keep.
            if ticker and image and ticker not in coins:
                coins[ticker] = {"id": entry.get("id"), "name": entry.get("name"),
                                 "image": str(image).split("?")[0]}
        time.sleep(1.2)          # be polite to the free tier

    ICON_DIR.mkdir(parents=True, exist_ok=True)
    MARKET_CACHE.write_text(json.dumps({"fetched": time.time(), "coins": coins}))
    return coins


def _coin_by_id(coin_id: str) -> Optional[dict]:
    """Look one coin up directly by CoinGecko id.

    Overrides exist precisely for coins the ticker match gets wrong, and those
    are often outside the top-1000 slice we cache -- so an override must not be
    limited to that slice. One extra request, only for overridden coins.
    """
    url = (f"https://api.coingecko.com/api/v3/coins/{coin_id}"
           "?localization=false&tickers=false&market_data=false"
           "&community_data=false&developer_data=false")
    raw = json.loads(_get(url).decode())
    image = (raw.get("image") or {}).get("large") or (raw.get("image") or {}).get("small")
    if not image:
        return None
    return {"id": raw.get("id"), "name": raw.get("name"), "image": str(image).split("?")[0]}


def _download_icon(base: str, url: str) -> Optional[str]:
    """Save one icon; returns the stored filename, or None if unusable."""
    data = _get(url)
    if len(data) > MAX_ICON_BYTES:
        raise RuntimeError(f"icon larger than {MAX_ICON_BYTES} bytes")
    ext = mime = None
    for magic, candidate_ext, candidate_mime in _MAGIC:
        if data.startswith(magic):
            ext, mime = candidate_ext, candidate_mime
            break
    if ext is None:
        raise RuntimeError("not a PNG/JPEG/GIF image")
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{base}.{ext}"
    (ICON_DIR / name).write_bytes(data)
    logging.debug("icon cached: %s (%s, %d bytes)", name, mime, len(data))
    return name


def fetch_missing(symbols: Iterable[str], force: bool = False) -> dict:
    """Download logos for any of `symbols` we don't already have. BLOCKING.

    Call it off the event loop (asyncio.to_thread) -- it does several HTTP
    requests with deliberate pauses between them. Never raises: every failure
    is collected into the report so one dead URL can't stop the rest, and a
    total outage just means no icons.
    """
    bases = sorted({base_of(s) for s in symbols if s})
    manifest = load_manifest()
    overrides = _read_json(OVERRIDES, {})
    overrides = overrides if isinstance(overrides, dict) else {}

    def needs_fetch(base: str) -> bool:
        if force or icon_file(base, manifest) is None:
            return True
        # An override only means work when it points somewhere new -- otherwise
        # every refresh would re-download the coins you have already corrected.
        override = overrides.get(base)
        return bool(override) and (manifest.get(base) or {}).get("id") != override

    wanted = [b for b in bases if needs_fetch(b)]
    report = {"ok": True, "requested": len(bases), "fetched": [], "skipped": [],
              "failed": [], "unmatched": [], "already": len(bases) - len(wanted)}
    if not wanted:
        report["message"] = "every coin already has an icon"
        return report

    if len(wanted) > MAX_FETCH_PER_CALL:
        report["skipped"] = wanted[MAX_FETCH_PER_CALL:]
        wanted = wanted[:MAX_FETCH_PER_CALL]

    try:
        markets = _market_map()
    except Exception as exc:
        report.update(ok=False, error=f"could not load the coin list: {exc}")
        return report

    by_id = {c["id"]: c for c in markets.values() if c.get("id")}
    for base in wanted:
        override = overrides.get(base)
        if override:
            match = by_id.get(override)
            if match is None:
                try:
                    match = _coin_by_id(str(override))
                except Exception as exc:
                    report["failed"].append({"base": base,
                                             "error": f"override id {override!r}: {exc}"})
                    continue
        else:
            match = markets.get(base)
        if not match or not match.get("image"):
            report["unmatched"].append(base)
            continue
        try:
            name = _download_icon(base, match["image"])
        except Exception as exc:
            report["failed"].append({"base": base, "error": str(exc)})
            continue
        manifest[base] = {"file": name, "id": match.get("id"), "name": match.get("name"),
                          "source": match["image"], "fetched": int(time.time()),
                          "via_override": bool(override)}
        report["fetched"].append({"base": base, "id": match.get("id"),
                                  "name": match.get("name")})
        time.sleep(0.4)

    try:
        ICON_DIR.mkdir(parents=True, exist_ok=True)
        MANIFEST.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    except OSError as exc:
        report.update(ok=False, error=f"could not write the manifest: {exc}")

    bits = [f"{len(report['fetched'])} fetched"]
    if report["unmatched"]:
        bits.append(f"{len(report['unmatched'])} with no ticker match "
                    f"({', '.join(report['unmatched'])})")
    if report["failed"]:
        bits.append(f"{len(report['failed'])} failed")
    if report["skipped"]:
        bits.append(f"{len(report['skipped'])} left for the next run")
    report["message"] = "; ".join(bits)
    return report


if __name__ == "__main__":                     # python helpers/icons.py
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    from helpers.config import COINS       # noqa: E402  (path set up above)

    result = fetch_missing(c["symbol"] for c in COINS)
    print(json.dumps(result, indent=2))
    for entry in result.get("fetched", []):
        print(f"  {entry['base']:8} -> {entry['name']} ({entry['id']})")
    if result.get("unmatched"):
        print("\nNo ticker match for: " + ", ".join(result["unmatched"]))
        print(f"Add the right CoinGecko id to {OVERRIDES} and run again, e.g.")
        print('  {"%s": "coingecko-id-here"}' % result["unmatched"][0])
