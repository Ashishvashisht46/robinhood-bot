#!/usr/bin/env python3
"""
Self-check for the pre-flight simulation fast path.

Pre-flight simulation costs a measured 760ms median (4.3s p90) on every entry,
paid before the transaction is broadcast. The machinery to skip it existed --
`no_sim_venues` plus a kill-switch -- but nothing ever ADDED to the set, and
FAST_EXECUTION_MODE was read from config and used nowhere. So the pre-flight was
never skipped, on any venue, ever.

The risk of getting this wrong is broadcasting a doomed transaction, so the
promotion rule is tested directly rather than assumed:

    python test_fast_path.py
"""
import sys
from types import SimpleNamespace

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"          got {got!r}  want {want!r}")
        fails.append(name)


class Fake:
    """Just the state record_fill touches, so no chain or config is needed."""

    def __init__(self, fast=True, after=3):
        self.fast_execution = fast
        self.no_sim_venues = set()
        self.venue_clean_runs = {}
        self.SIM_SKIP_AFTER = after

    record_fill = None  # bound below


def main() -> int:
    from dex_trader import DexTrader
    Fake.record_fill = DexTrader.record_fill

    V = "UNISWAP_V4"

    print("promotion only after repeated clean fills:")
    d = Fake()
    d.record_fill(V, True)
    check("1 clean fill  -> still simulating", V in d.no_sim_venues, False)
    d.record_fill(V, True)
    check("2 clean fills -> still simulating", V in d.no_sim_venues, False)
    d.record_fill(V, True)
    check("3 clean fills -> fast path ON", V in d.no_sim_venues, True)

    print("\na bad fill demotes it immediately:")
    d.record_fill(V, False)
    check("bad fill -> counter reset", d.venue_clean_runs[V], 0)
    # the kill-switch in buy_token does the discard; record_fill must not re-add
    d.no_sim_venues.discard(V)
    d.record_fill(V, True)
    check("after demotion, one good fill does NOT re-promote",
          V in d.no_sim_venues, False)

    print("\nthe streak must be CONSECUTIVE:")
    d = Fake()
    d.record_fill(V, True)
    d.record_fill(V, True)
    d.record_fill(V, False)      # breaks the run
    d.record_fill(V, True)
    d.record_fill(V, True)
    check("2 + bad + 2 -> still simulating", V in d.no_sim_venues, False)
    d.record_fill(V, True)
    check("...third consecutive -> fast path ON", V in d.no_sim_venues, True)

    print("\nFAST_EXECUTION_MODE=false disables it entirely:")
    d = Fake(fast=False)
    for _ in range(10):
        d.record_fill(V, True)
    check("10 clean fills, flag off -> never skips", d.no_sim_venues, set())

    print("\nvenues are tracked independently:")
    d = Fake()
    for _ in range(3):
        d.record_fill("UNISWAP_V4", True)
    d.record_fill("LAUNCHPAD_CURVE_ETH", True)
    check("proven venue promoted", "UNISWAP_V4" in d.no_sim_venues, True)
    check("unproven venue not promoted",
          "LAUNCHPAD_CURVE_ETH" in d.no_sim_venues, False)

    print()
    if fails:
        print(f"FAILED: {', '.join(fails)}")
        return 1
    print("All fast-path checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
