# Validation — 2026-09-15

Executed in an isolated source copy with the project's Python 3.13 environment.
No credentials, Telegram sessions, live logs, or live state were copied into the
test workspace. External sockets were blocked by the test runner.

Command: `.venv\Scripts\python.exe -B run_offline_tests.py`

| Suite | Checks | Result |
|---|---:|---|
| Core reliability, risk, listener, replay, and monitor tests | 45 | PASS |
| Actual sender boundaries, protected routes, fees, RPC rotation | 12 | PASS |
| Held-stock sizing, V3/V4 execution, protection, recovery and cost basis | 24 | PASS |
| Contract-address resolution | 12 | PASS |
| Fill measurement | 8 | PASS |
| Position persistence | 16 | PASS |
| Dashboard collector compatibility | 36 | PASS |
| **Total** | **153** | **PASS** |

All Python source files also passed syntax parsing. The source snapshot matched
the original project before applying changes; pre-existing edits were preserved.

The held-stock extension was tested in a fresh isolated source copy after the
initial reliability upgrade. Checks include a native balance that can cover the
floor and gas reserve but cannot fund another ETH stake: the inventory entry
succeeds through the mocked stock router, while the ETH-funded attempt is blocked.
For a modeled $1 stock input and $0.02 receipt gas, entry accounting records a
$1 stake and $0.02 gas rather than treating the inventory as free. Amounts in
six- and eighteen-decimal raw units remain exact. Unknown broadcasts remain
journaled across restart, and the normal ETH purchase is never invoked after
an inventory swap attempt.

The read-only historical report found 26 usable observed price paths. The three
documented scenarios all had negative modeled realized P&L after the stated cost
assumptions. This is not a validated backtest: it excludes missed calls, has
rounded/possibly stale prices, and contains censored paths.

No live transaction, funded-wallet test, Telegram login, or bot restart was
performed. Profitability, current contract compatibility, and improved live fill
rates remain unproven.
