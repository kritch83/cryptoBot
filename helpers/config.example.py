# TEMPLATE -- copy to helpers/config.py and edit that copy:
#     cp helpers/config.example.py helpers/config.py
# helpers/config.py is git-ignored so your live coin settings stay private.

"""User-editable configuration for the crypto grid/DCA bot.

Edit values here, then run `python conductor.py` (paper) or
`python conductor.py --live`.

Multi-coin: each entry in COINS is one tracked pair with its own tunables.
The selector digit in manual controls is the entry's index + 1 (so the
first coin is `1`, the second is `2`, etc.).

Optional per-coin fields (omit to keep current behavior):
    "order_type":            "market" | "limit"   (default "market"; sells only)
    "limit_sell_offset_pct": 0.001                 (default 0.001; only used
                                                    when order_type == "limit".
                                                    Limit price = last * (1+offset).
                                                    Make it larger than the
                                                    typical bid/ask spread so
                                                    the order rests as a maker;
                                                    otherwise it may match
                                                    immediately as a taker.)
The manual 'sell ALL now' (menu 9) always market-sells regardless of order_type.
Automatic trail-fires and the arm-sell-trail flow (menu a) honor order_type.

Secrets (API keys/tokens) are NOT stored here -- they load from a project-root
`.env` file (see `.env.example`). Copy `.env.example` to `.env` and fill it in.
"""

import os
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Minimal `.env` loader -- stdlib only, no extra dependency (matching the
    rest of this project). Parses `KEY=VALUE` lines and pushes them into
    os.environ. Real environment variables always win (setdefault), so you can
    override any value per-shell without editing the file. Missing file is a
    silent no-op."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, sep, val = line.partition("=")
        if not sep:
            continue
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]          # strip matching surrounding quotes
        os.environ.setdefault(key, val)


# Load the project-root .env (one level up from this helpers/ dir) before the
# credential lookups below.
_load_dotenv(Path(__file__).resolve().parent.parent / ".env")




# --- Coins ------------------------------------------------------------------
COINS = [
    # One dict per coin. Copy a block to add a coin (or use "+ Add coin" in the
    # dashboard), set "enabled": False to stop trading it without deleting it.
    {
        "symbol":               "BTC/USD",
        "price_prec":           1,       # decimals Kraken quotes this pair in
        "drop_pct":             0.02,    # buy another level every 2% down
        "trail_buy_pct":        0.005,   # buy trail: rebound needed to fire
        "trail_sell_pct":       0.006,   # sell trail: pullback needed to fire
        "buy_order_type":       "limit",
        "limit_buy_offset_pct": 0.001,
        "order_type":           "limit",
        "limit_sell_offset_pct":0.001,
        "take_profit_pct":      0.05,    # sell the whole grid 5% above avg entry
        "usd_per_buy":          100.0,
        "max_grid_levels":      10,
        "enabled":              True,
        "blynk_pin":            "V1",
    },
    {
        "symbol":               "ETH/USD",
        "price_prec":           2,
        "drop_pct":             0.025,
        "trail_buy_pct":        0.005,
        "trail_sell_pct":       0.006,
        "buy_order_type":       "limit",
        "limit_buy_offset_pct": 0.001,
        "order_type":           "limit",
        "limit_sell_offset_pct":0.001,
        "take_profit_pct":      0.05,
        "usd_per_buy":          100.0,
        "max_grid_levels":      10,
        "enabled":              False,
        "blynk_pin":            "V2",
    },
]

# --- Exchange / paths -------------------------------------------------------
EXCHANGE_ID = "kraken"
STATE_DIR    = "data"
WALLET_FILE  = "data/wallet.json"
RETIRED_FILE = "data/retired_pnl.json"   # retired-coin realized-PnL ledger (human-editable)
LOG_FILE     = "data/grid_bot.log"

# --- Fees -------------------------------------------------------------------
TAKER_FEE_PCT = 0.004           # taker fee for paper-mode fee math (market orders)
MAKER_FEE_PCT = 0.002          # maker fee for paper-mode fee math (limit-sell fills)

# --- Paper wallet + pacing --------------------------------------------------
PAPER_STARTING_USD  = 1800.0    # shared paper USD across all coins
ACTION_COOLDOWN_SEC = 10.0       # min gap between order events per coin (bypassed under --simulate)
LIVE_BUY_FILL_TIMEOUT_SEC = 20.0  # max wait for market-buy fill confirmation via fetch_order polling

# --- Kraken API credentials (only needed for --live) -----------------------
# Secrets load from the project-root .env file (see .env.example).
KRAKEN_API_KEY    = os.getenv("KRAKEN_API_KEY", "")
KRAKEN_API_SECRET = os.getenv("KRAKEN_API_SECRET", "")

# --- Pushover notifications ------------------------------------------------
PUSHOVER_ENABLED = True
PUSHOVER_TOKEN   = os.getenv("PUSHOVER_TOKEN", "")
PUSHOVER_USER    = os.getenv("PUSHOVER_USER", "")
PUSHOVER_SOUND   = os.getenv("PUSHOVER_SOUND", "cashregister")

# --- Web dashboard ----------------------------------------------------------
# Runs inside the bot process (helpers/dashboard.py) and serves live stats plus
# the same manual controls as the terminal menu. Because those controls move
# real money, the dashboard refuses to start on a non-loopback host without a
# token: set DASHBOARD_TOKEN in .env, or bind 127.0.0.1 and reach it over an
# SSH tunnel. This is plain HTTP for a trusted LAN -- do not port-forward it.
DASHBOARD_ENABLED = True
DASHBOARD_HOST    = os.getenv("DASHBOARD_HOST", "0.0.0.0")   # 127.0.0.1 = loopback only
DASHBOARD_PORT    = int(os.getenv("DASHBOARD_PORT", "8787"))
DASHBOARD_TOKEN   = os.getenv("DASHBOARD_TOKEN", "")         # shared secret; required off-loopback
# Extra hostnames the dashboard answers to (comma-separated), e.g. a reverse
# proxy domain. localhost, this machine's own hostname and any IP address are
# always accepted; anything else is refused (guards against DNS rebinding).
DASHBOARD_ALLOWED_HOSTS = [h.strip() for h in os.getenv("DASHBOARD_ALLOWED_HOSTS", "").split(",") if h.strip()]

# --- Blynk (push realized PnL to virtual pins) ------------------------------
BLYNK_ENABLED       = False                     # master switch -- set False to disable all Blynk pushes
BLYNK_TOKEN         = os.getenv("BLYNK_TOKEN", "")  # Blynk Cloud token (set in .env)
BLYNK_TOTAL_PNL_PIN = "V0"                     # pin for sum-of-all-coins realized PnL
BLYNK_HEARTBEAT_SEC = 120                       # also push every N seconds even with no trades
BLYNK_BASE_URL      = "https://blynk.cloud"
