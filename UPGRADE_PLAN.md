# Reliability and strategy upgrade

## Evidence before changes (2026-09-15)

Local logs dated September 12–14 contain 185 incoming signals, 103 buy attempts,
27 position opens, 73 zero-fill reports, 498 lines mentioning HTTP 429, 14 missing
contract addresses, 25 unsupported Longxyz skips, 21 capital-floor skips, and 17
capacity skips. These are log occurrences, not deduplicated executions. The 24
closed ledger rows report -$4.11 gross; they are reconstructed estimates, not
verified wallet P&L. Saved state covers a different subset: 18 trades and $6.84
balance. The configured $6 floor leaves less than the $1 entry stake available.

No strategy can promise positive returns. More fills can increase losses. The
objective is complete signal accounting, bounded retries, reliable holdings
management, and evidence suitable for evaluating a strategy in paper mode.

## Implementation order

1. Correct token-address selection; accept message edits and recover recent
   missed messages. Persist signal states, deduplicate delivery, and retry only
   fresh signals with a known pre-broadcast failure.
2. Serialize wallet operations, journal transaction hashes before submission,
   preserve uncertain operations across restart, and prevent fallback routes
   from repeating a transaction whose outcome is unknown.
3. Reserve exposure when checking the capital floor, check native wallet funds,
   isolate paper state, validate risk settings, and persist loss controls.
4. Keep monitors alive after errors, account for partial fills, persist stop
   ratchets, and remove false claims of guaranteed or risk-free profit.
5. Add offline regression coverage plus signal diagnostics and a transparent
   historical strategy replay with fees/slippage and explicit data limitations.
6. Review the diff and apply only changed source files after checking that the
   original files still match the staged snapshot. Preserve local edits and all
   credentials, sessions, logs, balances, and open-position state.

## Acceptance checks

- One logical signal cannot buy twice through edits, reconnects, or retries.
- A full position book defers a fresh signal instead of silently dropping it.
- Expired signals never turn into late entries.
- No uncertain broadcast is automatically repeated.
- A failed monitor or partial close never deletes a funded position.
- All tests use isolated files and mocked I/O; no wallet signing with a real key,
  no Telegram login, no transaction broadcast, and no live bot restart.
- Do not lower the user's capital floor or claim backtested profitability.

## Held-stock funding addition

1. Discover a quoted V3 or V4 pool by exact token addresses, using available
   stock inventory before native-ETH routing. Size in raw token units from a
   fresh ETH-to-stock replacement quote for the existing configured stake.
2. Exclude reserved tokens, open positions, and unresolved wallet operations.
   Use inventory only when it covers the full stake; otherwise retain the ETH
   path without buying a stock top-up. Keep the capital floor and gas reserve.
3. Journal the funding asset and exact input before submission, allow only one
   inventory swap, and block fallbacks after any broadcast. Include the consumed
   stock's quoted value in entry cost and label its valuation as estimated.
4. Test V3/V4 routing, integer sizing, protection, failures, restart recovery,
   and accounting entirely offline. Apply verified source changes only.

## Original audit sources

- [Telethon client documentation](https://docs.telethon.dev/en/stable/modules/client.html)
  for event handlers and catch-up after registering handlers.
- [FINRA order types](https://www.finra.org/investors/investing/investment-products/stocks/order-types)
  for execution uncertainty and the limits of stop orders (general context; this
  bot uses DEX swaps, not brokerage stop orders).
