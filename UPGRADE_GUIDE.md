# Upgrade guide

## What changed

- Persistent signal inbox, duplicate protection, edited-message handling,
  reconnect catch-up, and bounded recent-message recovery.
- Fresh capacity/funding failures wait and retry. Unfilled signals expire after
  120 seconds; missing or ambiguous identities and unsupported venues have an
  explicit outcome. A corrected message can repair a missing contract address.
- Contract tags and token links take precedence over chart pool IDs. Exact
  linked pools can be resolved through DexScreener; ambiguous ticker searches
  are refused even when ticker fallback is enabled.
- Wallet operations are serialized. Transaction hashes reach disk before
  broadcast. Receipt timeouts and interrupted operations cannot trigger a blind
  second buy. Confirmed token deltas can recover a purchased position on restart.
- Every production broadcast goes through the journal, including stock routes
  and the former forced-buy path. Fallback V4 entries are simulated. Both legs
  of stock/USDG routes require quotes, and the ERC20 V4 leg no longer includes a
  second funding transfer before SETTLE_ALL.
- RPC scans use a configurable concurrency cap. Provider rotation preserves the
  Web3 object referenced by existing contract calls; token decimals are cached.
- Open stakes reserve capital above the configured floor. Native funds and a gas
  reserve are checked too. Compounding requires explicit opt-in. Optional daily
  loss and consecutive-loss limits persist across restarts.
- Partial exits retain the remaining tokens. Stops survive restarts and take
  priority over the next profit tranche. Exceptions keep the monitor alive.
  Closing a position and updating its P&L commit together and are idempotent.
- Receipt gas and ETH/WETH/USDG balance changes support cash-flow P&L for
  uninterrupted live operations. Recovered or unreadable cash flows remain
  explicitly estimated. Use an exclusive bot wallet: unrelated deposits,
  withdrawals, or external swaps during a measurement can distort wallet deltas.
- Paper entries require route quotes and include configurable slippage and fee
  assumptions. Paper files are isolated from live files.

## Verify offline

From the project directory:

```powershell
.\.venv\Scripts\python.exe -B run_offline_tests.py
```

The runner blocks external sockets and uses fake chain/Telegram clients. It never
constructs a live trading client or uses your key to sign. The old random-win
`test_dry_run.py` now runs deterministic regressions. `test_gas_pricing.py` now
uses a fake sender instead of loading a real wallet and making live RPC calls.

## Paper evaluation

```powershell
.\.venv\Scripts\python.exe main.py --paper
```

This explicitly forces paper mode even when `.env` says `DRY_RUN=false`, and
evaluates every eligible signal while retaining tax, identity, freshness, capital,
and position limits. It still needs Telegram credentials/channel membership and
working read-only market endpoints. The process lock permits one bot per project,
so stop an existing bot before starting a paper run. Do not interrupt a live bot
with holdings without arranging continued monitoring.

`--paper` does not edit `.env`. Paper state starts from `INITIAL_CAPITAL_USD` and
persists separately; it does not copy the live balance or reset the live floor.

| Data | Live | Paper |
|---|---|---|
| Positions | `cache/strategy_state.json` | `cache/strategy_state_paper.json` |
| Signal inbox | `cache/signals_live.sqlite3` | `cache/signals_paper.sqlite3` |
| Execution journal | `cache/execution_live.json` | `cache/execution_paper.json` |
| Logs/ledger | `logs/` | `logs/paper/` |

The existing dashboard reads the live logs. Use the inbox report below for both
modes. Paper prices/costs are a model; they do not establish executable live P&L.

## Diagnose missed trades

```powershell
.\.venv\Scripts\python.exe strategy_report.py --output AUDIT_REPORT.md
```

The report reads logs, the ledger, and both inboxes without network calls. It
separates legacy estimates from measured results and gives each queued signal's
current outcome/reason. Historical replay includes cost assumptions and flags
unsold/censored paths. It is not an optimizer or proof of profitability.

## New settings

See `.env.example`. Defaults are:

| Setting | Default | Meaning |
|---|---:|---|
| `MAX_SIGNAL_AGE_SECONDS` | 120 | Maximum entry age, including queue time |
| `MAX_SIGNAL_ATTEMPTS` | 6 | Retry budget for known pre-broadcast failures |
| `SIGNAL_RETRY_SECONDS` | 5 | Initial backoff, capped at 30 seconds |
| `SIGNAL_BACKFILL_LIMIT` | 100 | Maximum recent messages inspected on reconnect |
| `RPC_SCAN_WORKERS` | 4 | Maximum workers per venue scan |
| `GAS_RESERVE_USD` | 0.25 | Additional entry funding reserve, not a measured gas cost |
| `ENABLE_COMPOUNDING` | false | Opt-in to larger post-win stakes |
| `MAX_DAILY_LOSS_USD` | 0 | 0 disables the additional UTC-day loss limit |
| `MAX_CONSECUTIVE_LOSSES` | 0 | 0 disables the additional streak limit |
| `PAPER_FEE_PER_SWAP_USD` | 0.03 | Assumed paper fee per entry/exit |
| `PAPER_SLIPPAGE_PCT` | 2 | Assumed adverse paper slippage |

`BUY_EVERY_SIGNAL=true` bypasses the optional holder/liquidity filters. Tax limits,
valid token identity, quotes, capital limits, and supported routes still apply.
It cannot make Longxyz signature-gated tokens tradeable. Existing `.env` values
are preserved; this upgrade does not automatically enable live trading or lower
the floor. A larger stake is not a remedy for a losing strategy.

## When an operation needs review

Read the corresponding `execution_live.json` entry and its recorded hashes. The
bot attempts read-only recovery of entry fills every 15 seconds. An unknown
receipt blocks new submissions using that nonce; a known failed multi-hop route
may leave stock/USDG in the wallet and stays visible for review. The code does
not automatically spend those stranded assets or erase the journal to resume
trading. Resolve the transaction/holdings first; deleting the journal blindly can
permit a duplicate buy. Existing position exits keep running when the wallet has
no unconfirmed transaction blocking them.

## Limits of this delivery

The upgraded code was tested offline, not against a funded wallet. Current chain
deployments, liquidity, token safety, gas costs, and real fill rates were not
revalidated through live transactions. A quoted/simulated swap can still revert,
and a stop can fail to fill. Historical data here does not demonstrate a profitable
strategy. No live bot, scheduled task, transaction, or Telegram login was started
as part of this upgrade.

## Held-stock entries

The bot now prefers a stake-sized amount of eligible stock already held in the
wallet when a supported stock/called-token pool can be quoted. See
[inventory funding](INVENTORY_FUNDING.md) for protection rules, accounting and
configuration. `REUSE_STOCK_INVENTORY` defaults to `true`; `.env` and the running
process are not changed by the source upgrade.
