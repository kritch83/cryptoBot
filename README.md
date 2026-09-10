# gridBot
<<<<<<< Updated upstream
a bot that trades crypto based on grids
=======

A multi-coin grid / DCA trading bot for Kraken, built on CCXT Pro WebSockets.
Each coin runs as its own asyncio task with its own price stream, state file and
trailing entry/exit state machines. Drive it from the terminal menu or from the
built-in web dashboard — both issue the same commands through the same queues,
so the two can never disagree about what the bot is doing.

> ### ⚠️ This places real orders with real money when run with `--live`
> It ships in paper mode and stays there unless you pass `--live`. Understand the
> strategy below, read `helpers/config.py`, and paper-trade it first. Provided
> as-is with no warranty — you are responsible for what it trades and what it
> loses.

---

## How it trades

One cycle, per coin:

1. **Wait for a dip.** Track the running high; arm once price falls `drop_pct` below it.
2. **Buy the rebound.** Follow the low down and buy when price bounces `trail_buy_pct`
   off it — a trailing entry, so it doesn't catch the first touch of a falling knife.
3. **Add levels.** Each further buy arms `drop_pct` below the *last* buy, up to
   `max_grid_levels` levels of `usd_per_buy` each.
4. **Take profit.** Once price reaches `avg_entry × (1 + take_profit_pct)`, arm a
   sell-trail; sell the **whole** position when price pulls back `trail_sell_pct`
   off the high.
5. Book realized PnL, tick the cycle counter, start over.

Buys and sells are market or post-only limit per coin (`buy_order_type` / `order_type`).

Beyond the base loop: hard stop-loss, breakeven exit, pause-after-sell, manual
target sell, manual buy/sell trails, and a ledger that keeps a retired coin's
lifetime PnL in the totals after its state is wiped.

## Quick start

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r helpers/requirements.txt
cp env.example .env       # then fill it in
```

Edit the `COINS` list in `helpers/config.py`, then run **from the project root**
(all paths are relative to it):

```bash
python conductor.py
```

| Flag | Effect |
| --- | --- |
| *(none)* | Paper trading against live prices. The default. |
| `--live` | **Sends real orders.** Needs `KRAKEN_API_KEY` / `KRAKEN_API_SECRET`. |
| `--dry-run` | With `--live`: logs the orders it would send, sends nothing. |
| `--simulate FILE.csv` | Replay prices from a CSV (first enabled coin only). |
| `--no-dashboard` | Don't start the web dashboard. |
| `--dashboard-host` / `--dashboard-port` | Override the bind address / port. |

## Controls

Both front-ends put a `PendingAction` on the coin's queue; it is applied on that
coin's next price tick. Every action asks for confirmation first.

**Terminal.** Type a coin's number then <kbd>Enter</kbd> to open its action menu;
`a` for the all-coin summary, `c` to hot-reload `config.py` tunables, `?` for help.

| Key | Action | Key | Action |
| --- | --- | --- | --- |
| `1` | live stats | `9` | sell ALL now (market) |
| `2` | config | `p` | sell ALL at my price (post-only limit) |
| `3` | set/clear stop-loss | `a` | toggle sell-trail (arm / disarm) |
| `4` | toggle pause | `b` | toggle pause buying (sells carry on) |
| | | `t` | clear targets |
| `5` | toggle breakeven-exit | `r` | **retire coin** (book PnL, full reset) |
| `6` | toggle pause-after-sell | `c` | **clear stats** (full reset) |
| `7` | force BUY now (market) | | |
| `8` | arm buy-trail (trailing dip-buy) | | |

**Web dashboard** — `http://<host>:8787/?token=<DASHBOARD_TOKEN>`; the token is
kept in a cookie after the first visit.

- Account total (cash + coins at market — real assets, no unrealized PnL),
  realized PnL, position value and unrealized as separate cards.
- Per-coin table: price, levels, avg entry, position value, unrealized, realized,
  cycles, next buy/sell with distance from current price, and status badges.
  Click a header to sort, drag one to reorder; the layout is remembered per browser.
- Click a row for its detail panel — position breakdown, open levels, the next
  buy/sell math, and the full control set. The list stays put; the panel opens
  below it.
- A config editor that writes changes back into `helpers/config.py` **and**
  hot-applies them to the running coin. Coins can be **added** (validated against
  the exchange's markets; starts trading immediately if you enable it, no restart)
  and **removed** (refused while the coin holds a position, has a resting order,
  or has unbooked PnL — retire it first).
- Realized-PnL equity curve, parsed out of the log.
- Coin logos, fetched once to `data/icons/` and served locally afterwards.

## Configuration

Tunables live per coin in `COINS` in [`helpers/config.py`](helpers/config.py):

| Key | Meaning |
| --- | --- |
| `symbol`, `price_prec` | Pair and display precision |
| `enabled` | Whether the coin runs (restart required to change) |
| `drop_pct` | Spacing between grid buys |
| `trail_buy_pct` | Rebound off the low that fires a buy |
| `trail_sell_pct` | Pullback off the high that fires the sell |
| `take_profit_pct` | Above avg entry before the sell-trail can arm |
| `usd_per_buy`, `max_grid_levels` | Size per level and the level cap |
| `buy_order_type` / `order_type` | `market` or `limit` |
| `limit_buy_offset_pct` / `limit_sell_offset_pct` | Limit price offsets |
| `blynk_pin` | Virtual pin this coin's realized PnL is pushed to |

**What can change without a restart:** every tunable except `symbol` — press `c`
in the terminal or use the dashboard's config editor. Adding a coin and turning
one **on** also take effect immediately from the dashboard.
**What needs one:** turning a coin **off**, removing one that is currently
running, and the module-level constants (fees, `ACTION_COOLDOWN_SEC`,
`PAPER_STARTING_USD`, notification and dashboard settings).

Secrets go in `.env`, never in `config.py` — see [`env.example`](env.example).

## Layout

```
conductor.py            entry point: CLI, terminal menu, task orchestration
helpers/
  coin_runner.py        per-coin tick loop, trail state machines, order execution
  config.py             COINS + all settings
  config_reload.py      hot reload, read side (parse → diff → apply in memory)
  config_edit.py        hot reload, write side (AST-surgical edits to config.py)
  actions.py            action catalog + validators shared by both front-ends
  state.py, wallet.py   per-coin persistence, shared paper USD pool
  retired.py            retired-coin PnL ledger
  dashboard.py          embedded web server + JSON API
  trade_history.py      closed-trade history, parsed back out of the log
  icons.py              one-time local cache of coin logos
  blynk.py              Blynk push
  web/                  dashboard front-end (no build step, no dependencies)
data/                   state, logs, ledgers, icons, config backups (gitignored)
```

## Data files

| File | Contents |
| --- | --- |
| `data/state_<PAIR>.json` | One per coin: positions, realized PnL, trails, pending orders |
| `data/wallet.json` | Shared paper USD pool |
| `data/retired_pnl.json` | Lifetime PnL of retired coins (hand-editable) |
| `data/grid_bot.log` | Full activity log — also the source for the dashboard's PnL history |
| `data/config_backups/` | Timestamped copies of `config.py` before each dashboard edit |
| `data/icons/` | Coin logos, fetched once via the dashboard's "Fetch icons" button (or `python helpers/icons.py`) |

Coin logos are matched by ticker, which is a guess — tickers are not unique. The
match is named in the fetch report; correct a wrong one by putting the right
CoinGecko id in `data/icons/overrides.json`.

## Notifications

Optional and independent: **Pushover** for fills, **Blynk** for realized PnL on
virtual pins (`V0` = total, one pin per coin). Both are disabled by leaving their
tokens blank in `.env`.

## Security

The dashboard can place and cancel real orders, so it refuses to start on a
non-loopback address without `DASHBOARD_TOKEN` set. It is plain HTTP intended for
a trusted LAN — **do not port-forward it**. For remote access, bind loopback and
tunnel:

```bash
python conductor.py --dashboard-host 127.0.0.1
ssh -L 8787:localhost:8787 you@your-server
```

`.env` (API keys) and `data/` (positions, balances, trade history) are gitignored.
Keep it that way.
>>>>>>> Stashed changes
