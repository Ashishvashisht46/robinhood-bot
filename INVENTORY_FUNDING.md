# Reuse held stock tokens for entries

When a signal's token has a supported pool against a stock token already in the
wallet, the bot can fund that entry directly from the stock.

| Funding available | Entry path |
| --- | --- |
| Enough eligible stock for the configured stake | Held stock → called token |
| No eligible stock or no usable quote | Existing ETH-funded route |

This skips the stock acquisition swap. If that acquisition would have used
ETH → USDG → stock, both acquisition hops are skipped. The final stock → called
token swap still costs gas and pool fees. First use may require approvals; the
V4 route uses Permit2 without an additional direct Universal Router approval.
Savings depend on the route and existing allowances and have not been measured
on the live wallet. Fewer swaps do not guarantee a profitable trade.

## Sizing and protection

- The source must match a stock address in `stock_v4_routes.STOCK_LIST`, and a
  V3 or V4 quoter must return a usable output for the exact input and pool key.
  A matching ticker alone is insufficient.
- A fresh ETH-to-stock quote sizes the raw stock amount for the configured
  stake. It is a read-only valuation query; it does not buy stock. For example,
  if the quote returns 0.25 stock for a $1 stake, the entry uses 0.25 stock even
  if the wallet holds 10. Token decimals are preserved through integer units.
- The full quoted amount must be available. There is no partial inventory
  spend or stock top-up. Insufficient inventory keeps the existing ETH route.
- `RESERVED_TOKENS`, tokens in open bot positions, and tokens associated with
  unresolved operations are excluded from automatic inventory funding.
- The existing strategy budget, capital floor, position limit and loss limits
  still apply. The native wallet admission check requires the configured floor
  plus gas reserve; an inventory entry does not also reserve an ETH stake.
- Missing balances and missing quotes make inventory unavailable. Discovery
  tries supported keys within a bounded scan window, so unsupported or slow
  pools can fall back to the regular route before any inventory submission.

## Transaction handling and accounting

The wallet lock covers selection and submission. The bot persists the source
address, exact raw input, replacement value and route before submitting. It
rechecks the balance and chain, simulates the swap, and enforces a quoted
minimum output. Native ETH swap value is zero for an inventory-funded entry.
Quotes expire after 15 seconds; expiry also prevents a swap after slow approvals.

Once an inventory route is selected, that attempt does not switch to an ETH
purchase. A failure before any broadcast is retryable while the signal is fresh.
After any broadcast, an ambiguous result is retained for reconciliation and
blocks another entry. Restarts retain the funding evidence and intended cost.

The consumed stock has an economic cost even though no ETH was spent buying it
in this entry. The entry cost includes its quoted replacement value, and actual
receipt gas is added separately. The journal records measured raw stock spent
when balance reads succeed. `entry_funding` persists with the position and its
ledger event. Returns use an estimated cost basis, not a claim of measured
historical profit on that stock holding; its original purchase price is unknown.

Exits continue to use the existing sell-to-ETH/WETH strategy. This change does
not automatically rebuild stock reserves or retain stock from sales.

## Configuration and use

`REUSE_STOCK_INVENTORY=true` is the new default. Set it to `false` to keep
ETH-funded entries only. Add exact stock addresses to `RESERVED_TOKENS` to
protect deliberate long-term holdings. Both settings are read on startup.

This upgrade changes source code; it does not restart the bot or change the
private `.env`. A running process picks it up on its next restart. Paper mode
continues with isolated ETH-funded simulation and never consumes live stock.

Run the offline validation from the project directory:

```powershell
.\.venv\Scripts\python.exe -B run_offline_tests.py
```

`test_inventory_funding.py` covers integer sizing, both router paths, surplus
and insufficient balances, exclusions, simulation failures, gas admission,
stale quotes, timeouts, recovery, and inventory cost accounting with mocked I/O.
