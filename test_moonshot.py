#!/usr/bin/env python3
"""
Does a 2-3x target beat the +20% ladder?

Corrects an error in the earlier model: it charged 15% slippage because that is
SLIPPAGE_PCT, but that value is the MAXIMUM tolerated, not the expected cost.
Real price impact is roughly trade_size / pool_depth -- $5 into a $12.8k pool is
about 0.04%. The earlier penalty was ~300x too harsh.

Tests full-position exits (sell everything at the target) as well as tranched,
across targets up to 5x and stops from -10% to none.

    python test_moonshot.py

Still conservative where the data is ambiguous: if a candle's high clears the
target and its low clears the stop, the STOP is taken.
"""
import json
import os
import statistics as st
import sys

from analyse_channel import WINDOW_MIN
from sweep_ladder import series_from_cache

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

GAS = 0.12          # per round trip, USD


def run(series, tp, sl, stake, slip=0.01, ratio=1.0):
    """ratio=1.0 sells the whole position at tp; <1 leaves a runner."""
    out = []
    for entry, window in series:
        hit = None
        for r in window:
            if sl and r[3] <= entry * sl:
                hit = "sl"
                break
            if r[2] >= entry * tp:
                hit = "tp"
                break
        if hit == "sl":
            gross = sl - 1
        elif hit == "tp":
            rest = (window[-1][4] / entry) - 1
            gross = ratio * (tp - 1) + (1 - ratio) * rest
        else:
            gross = (window[-1][4] / entry) - 1
        gross = (1 + gross) * (1 - slip) ** 2 - 1        # in and out
        out.append(gross * stake - GAS)
    return out


def main() -> int:
    series = series_from_cache()
    n = len(series)
    print(f"n = {n} calls   |   costs: 1% slippage each way + ${GAS} gas\n")

    print("How far do these calls actually run?")
    peaks = sorted(max(r[2] for r in w) / e for e, w in series)
    for mult in (1.2, 1.5, 2.0, 3.0, 5.0, 10.0):
        c = sum(p >= mult for p in peaks)
        print(f"   ever reached {mult:>4.1f}x   {c:>3}/{n}   {c/n:>4.0%}")
    print(f"   median peak      {st.median(peaks):>5.2f}x")
    print()

    print("FULL-POSITION exit at target, $10 stake, net per trade")
    print("        " + "".join(f"{t:>9.1f}x" for t in (1.5, 2.0, 3.0, 5.0)))
    rows = [("-10%", 0.90), ("-20%", 0.80), ("-30%", 0.70),
            ("-50%", 0.50), ("none", None)]
    best = None
    for label, sl in rows:
        cells = []
        for tp in (1.5, 2.0, 3.0, 5.0):
            rets = run(series, tp, sl, 10)
            m = st.mean(rets)
            cells.append(m)
            if best is None or m > best[0]:
                best = (m, tp, label, sum(r > 0 for r in rets) / n, rets)
        print(f"  {label:<6}" + "".join(f"{c:>+9.2f}" for c in cells))

    m, tp, sl, wr, rets = best
    print()
    print(f"best: sell everything at {tp}x, stop {sl}")
    print(f"      {m:+.2f} per trade on a $10 stake, {wr:.0%} of trades positive")
    print()

    # Does it survive losing its best trade, and how wide is the uncertainty?
    import random
    random.seed(11)
    without = sorted(rets)[:-1]
    means = []
    for _ in range(3000):
        means.append(st.mean([random.choice(rets) for _ in range(n)]))
    means.sort()
    print(f"      minus its single best trade : {st.mean(without):+.2f}")
    print(f"      bootstrap 5th-95th pct      : {means[150]:+.2f} to {means[2850]:+.2f}")
    print(f"      P(profitable)               : {sum(x>0 for x in means)/len(means):.0%}")
    print()
    print("      slippage sensitivity at this config:")
    for slip in (0.005, 0.01, 0.02, 0.05):
        r = run(series, tp, 0.90 if sl == "-10%" else
                {"-20%": 0.80, "-30%": 0.70, "-50%": 0.50, "none": None}[sl], 10, slip)
        print(f"        {slip:>5.1%} each way   {st.mean(r):>+7.2f} per trade")
    return 0


if __name__ == "__main__":
    sys.exit(main())
