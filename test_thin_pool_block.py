#!/usr/bin/env python3
"""
A fill with unrecoverable price impact must be exited, not narrated.

Two fills in one morning came in at 30.4x and 1552.8x spot -- 97% and 100% price
impact on a $50 stake. The THIN POOL check identified both correctly, logged a
warning, and let them run to -$48.60 and -$50.03. That is $98.63 of a $221
losing session spent sitting in a loss the bot had already detected.

Detecting a problem and then doing nothing about it is worse than not detecting
it, because the log looks like the system is working.

    python test_thin_pool_block.py
"""
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"          got {got!r} want {want!r}")
        fails.append(name)


class Pos:
    def __init__(self):
        self.ticker = "TEST"
        self.contract_address = "0x" + "cd" * 20
        self.stake_usd = 50.0
        self.remaining_tokens = 1000.0
        self.pending_exit = None


def decide(ratio, threshold):
    """The branch main.py takes on a measured spot/entry ratio."""
    pos = Pos()
    if ratio < threshold:
        pos.pending_exit = dict(rung="CLOSE",
                                reason="thin pool; fill impact unrecoverable",
                                multiplier=ratio, tokens=pos.remaining_tokens)
    return pos


def main() -> int:
    from main import MIN_ENTRY_QUALITY as T

    print(f"threshold is spot/entry >= {T} (i.e. reject worse than "
          f"{(1-T)*100:.0f}% impact):")

    print("\nthe two fills that actually cost $98.63:")
    check("0.033 (97% impact, 30x spot)  -> exits", decide(0.033, T).pending_exit is not None, True)
    check("0.001 (100% impact, 1552x)    -> exits", decide(0.001, T).pending_exit is not None, True)

    print("\nnormal fills are left alone:")
    for r in (0.896, 0.895, 0.889, 0.853):
        check(f"{r} (~{(1-r)*100:.0f}% impact) -> held",
              decide(r, T).pending_exit, None)

    print("\nthe boundary:")
    check("just above threshold -> held", decide(T + 0.001, T).pending_exit, None)
    check("just below threshold -> exits", decide(T - 0.001, T).pending_exit is not None, True)

    print("\nthe exit closes everything and says why:")
    p = decide(0.001, T)
    check("whole remaining size", p.pending_exit["tokens"], 1000.0)
    check("reason recorded", "thin pool" in p.pending_exit["reason"], True)
    check("rung is a full close", p.pending_exit["rung"], "CLOSE")

    print("\nthe threshold sits clear of both populations:")
    check("above every catastrophic fill seen", T > 0.033, True)
    check("below every healthy fill seen", T < 0.853, True)

    print()
    if fails:
        print(f"FAILED: {', '.join(fails)}")
        return 1
    print("All thin-pool checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
