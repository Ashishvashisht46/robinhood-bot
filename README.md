# Robinhood Chain sniper bot + activity dashboard

An automated sniper for **Robinhood Chain (ID 4663)**. It watches a Telegram call
channel, resolves the token's contract address, routes a buy across whichever DEX
venue actually has liquidity, and manages the exit with a 40/40/20 ladder.

Ships with a **read-only dashboard** that reconstructs every signal, swap, buy,
sell, rejection and P&L figure from the bot's own logs.

> **This trades real money on a live chain and can lose all of it.** Use a burner
> wallet. Read [Risk](#risk) before running anything with `DRY_RUN=false`.

Forked from [POOKIE0614/robinhood-bot](https://github.com/POOKIE0614/robinhood-bot);
this copy adds an execution-layer overhaul and the dashboard. History starts fresh
here deliberately — see [Credit](#credit).

---

## What actually works

Stated plainly, because the cost of being wrong here is money. Everything marked
verified was checked against live chain 4663 with `eth_estimateGas` or a view call,
not inferred from reading the code.

| Path | Status |
|---|---|
| Uniswap V4, ETH-quoted, single hop | **Verified.** The main path — ~62% of tradeable calls. |
| Uniswap V3 (WETH / USDG) | **Verified.** |
| Bonding curves (Pons / Bags) | **Verified.** |
| Stock-quoted tokens (2 transactions) | **Works via a native-pool workaround.** Tokens with no native pool still use a 2-tx route whose leg 2 sometimes reverts. |
| Atomic V4 USDG 2-hop | **Broken. Do not use.** Six encodings tried, all revert. Not wired into any live path; USDG buys take the 2-transaction route instead. |

Known-imperfect and documented rather than hidden:

- **Dashboard P&L is gross.** Only the pre-flight gas estimate is logged, not actual
  gas used, so fees are not deducted.
- **Calls sometimes arrive with no contract address.** Those are skipped rather than
  guessed — see `ALLOW_TICKER_FALLBACK` in `.env.example` for why guessing is unsafe.
- **Entry latency is bound by your RPC.** Free endpoints rate-limit under load and a
  detection timeout becomes a missed buy.

---

## Setup

Python 3.11+ and a wallet you do not care about.

```bash
git clone <this repo>
cd robinhood-bot
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env
```

Now edit `.env`. Three things are mandatory:

1. **`PRIVATE_KEY`** — a burner. The bot grants unlimited token approvals, and the
   key sits in plaintext on disk.
2. **`RPC_URL`** — get your own. This is the single biggest lever on whether you
   land trades; shared endpoints return HTTP 429 under load.
3. **Telegram** — `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` from
   [my.telegram.org](https://my.telegram.org) → *API development tools*. The account
   you use **must already have joined the channel**, or the listener reconnect-loops
   forever without ever seeing a call.

Leave `DRY_RUN=true` until you have run the checks below.

### Fund the wallet

Native ETH on chain 4663, for stake plus gas. Gas is cheap here (a round trip cost
about **$0.12** when measured), but read the economics warning in `.env.example`
before choosing a stake size — at $1 the ladder loses money even on wins.

---

## Verify before trusting it

```bash
python verify_encoders.py
```

A four-tier harness that checks contract addresses, calldata encoding, venue
detection and quoting against live chain state **without spending anything**.
Expect ~50 passed / 0 failed. Counts vary with RPC throttling, so re-run before
believing a failure.

```bash
python test_gas_pricing.py            # fee ceiling logic, broadcasts nothing
python test_ca_resolution.py          # contract-address resolution
python test_fill_measurement.py       # fill sizing
python test_position_persistence.py   # restart recovery
python test_dashboard_data.py         # log parsing and trade correlation
```

---

## Run it

```bash
python main.py
```

On Windows, `run_bot.bat` restarts it after a non-zero exit and respects Ctrl+C.
It does **not** survive the console closing — Windows kills child processes with
their console. See `SUPERVISOR.md` for Task Scheduler.

Ctrl+C stops it cleanly. Open positions are written to `cache/strategy_state.json`
and reconciled against on-chain balances on the next start, so a restart does not
orphan a bag.

### Is anything unmanaged?

```bash
python status.py
```

Answers the only question that matters when you walk away: are you holding tokens
while nothing is watching them. Read-only, exits 1 when something is unmanaged.

---

## The dashboard

```bash
python dashboard.py             # http://127.0.0.1:8787, refreshes itself
python dashboard.py --snapshot  # self-contained dashboard.html you can send
python dashboard.py --share     # reachable off-machine, password required
python daily_summary.py         # the same numbers, in a terminal
```

Every signal, every rejection with its reason, every buy attempt with its outcome,
every swap with its transaction hash, closed trades with P&L, open positions, and a
filterable feed of the lot. It flags when the bot is alive but refusing to trade —
the safety floor blocking every call is invisible in a plain log.

**It makes no RPC calls.** Open-position state is read from the tracking line the
bot already writes, because the endpoint is already the constraint on trading and a
dashboard competing for that rate limit would cost real trades.

Data comes from `logs/events.jsonl` — an append-only ledger written at position
open, close and each tranche exit — plus all `bot.log` rotations. Recover trades
that predate the ledger with:

```bash
python dashboard_data.py --backfill
```

Those rows are tagged `rebuilt`: they have no entry price or transaction hashes,
because the bot never recorded them.

### Sharing it live

`share_dashboard.bat` (Windows) starts the dashboard in share mode and opens a
[Cloudflare quick tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/trycloudflare/),
giving a public HTTPS link. Share mode **requires a password** — a fresh one per
session — because the page shows a wallet address, balances and open positions.
Requires `winget install --id Cloudflare.cloudflared`. Your machine has to stay on,
and the link dies when you close the window.

---

## How it decides

```
Telegram message
   -> parse ticker + contract address (from the message buttons)
   -> safety filters: liquidity, tax, holders, age, max concurrent
   -> detect venue: native V4 pool preferred, then V3, V2, bonding curve
   -> quote the route; REFUSE the trade if it cannot be priced
   -> buy, then measure the actual balance delta as the fill
   -> monitor every 2s:
        TP1  1.20x -> sell 40%, ratchet stop to 0.95x
        TP2  1.40x -> sell 40%, ratchet stop to 1.20x  (principal returned)
        TP3  5.00x -> sell the 20% moonbag
        SL   0.50x, trailing
```

Two behaviours worth knowing because they look like bugs and are not:

- **It refuses trades it cannot quote.** No quote means no slippage protection, and
  sending unprotected is worse than skipping.
- **It prefers a thinner native pool over a deeper multi-hop route.** Every failure
  observed in this project was on a multi-hop route; a native pool is a single hop
  and the quoted floor still rejects a bad fill.

---

## Layout

| File | Purpose |
|---|---|
| `main.py` | Orchestration, startup recovery |
| `telegram_listener.py` / `message_parser.py` | Signal intake, address extraction |
| `strategy_engine.py` | Filters, sizing, circuit breaker, persistence |
| `dex_trader.py` | Venue detection, quoting, calldata, execution |
| `stock_v4_routes.py` | Stock-quoted 2-transaction route |
| `chain_client.py` | RPC, retries and rotation, fee pricing, signing |
| `position_monitor.py` | The exit ladder |
| `trade_ledger.py` | Append-only event ledger |
| `dashboard*.py`, `daily_summary.py`, `status.py` | Observability |
| `verify_encoders.py` | Spend-nothing verification harness |

---

## Risk

- **Losing money is the expected outcome for most configurations.** Memecoin
  sniping is negative-sum after fees; the exit ladder does not change that.
- The private key is stored in plaintext and the bot grants **unlimited approvals**
  to routers and curve contracts. A malicious token contract can exploit that. Use
  a burner and keep it thinly funded.
- Never commit `.env`, `*.session`, `logs/` or `cache/`. All are gitignored.
- The generated `dashboard.html` inlines live log data. It is gitignored for that
  reason — check it before sending it anywhere.
- Nothing here is financial advice. You are responsible for what this does with
  your funds.

## Credit

Original bot by [POOKIE0614](https://github.com/POOKIE0614/robinhood-bot) — the
chain knowledge in it (contract addresses for 4663, the Uniswap V4 Universal Router
calldata encoding, the venue-detection cascade, Permit2 flows) is the expensive part
and was worth keeping.

This copy starts from a fresh commit rather than carrying upstream history, because
that history contains a live API key.
