#!/usr/bin/env python3
"""
Is the positive cell in the TP/SL sweep a real edge, or a few lucky tokens?

A grid search over 40 samples will always produce a best cell. The question is
whether it survives (a) removing its biggest winner, (b) bootstrap resampling,
and (c) realistic costs. If it does not, it is noise with a pleasing shape and
must not be traded.

Pure computation over the cached prices. No API, no Telegram.

    python robustness.py
"""
import json
import os
import random
import statistics as st
import sys

from analyse_channel import WINDOW_MIN
from sweep_ladder import series_from_cache, simulate

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Costs per round trip, as fractions of the stake.
GAS_USD = 0.12
SLIPPAGE = 0.15          # SLIPPAGE_PCT=15 -- paid on entry AND exit, worst case


def net_returns(series, tp, sl, stake, ratio=0.40, slip=SLIPPAGE):
    """Per-trade return in USD after slippage both ways and gas."""
    out = []
    for entry, window in series:
        hit = None
        for r in window:
            if r[3] <= entry * sl:
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
        # slippage: you buy above mid and sell below it
        gross = (1 + gross) * (1 - slip / 2) ** 2 - 1
        out.append(gross * stake - GAS_USD)
    return out


def main() -> int:
    series = series_from_cache()
    n = len(series)
    if n < 10:
        print("not enough cached data")
        return 1
    print(f"n = {n} calls\n")

    # 1. does the best cell survive removing its best winner?
    print("1. LEAVE-ONE-OUT  (drop the single biggest winner, per config)")
    print(f"   {'config':<18}{'all':>9}{'minus top':>12}{'verdict':>12}")
    configs = [(1.20, 0.50, "TP+20 SL-50 (live)"), (2.00, 0.80, "TP+100 SL-20"),
               (3.00, 0.90, "TP+200 SL-10"), (1.50, 0.80, "TP+50 SL-20")]
    for tp, sl, label in configs:
        rets = net_returns(series, tp, sl, 2.50)
        without = sorted(rets)[:-1]
        a, b = st.mean(rets), st.mean(without)
        verdict = "holds" if b > 0 else ("fragile" if a > 0 else "negative")
        print(f"   {label:<18}{a:>+9.3f}{b:>+12.3f}{verdict:>12}")

    # 2. bootstrap: how often is the mean positive at all?
    print("\n2. BOOTSTRAP  (2000 resamples, net of slippage + gas, $2.50 stake)")
    print(f"   {'config':<18}{'mean':>9}{'5th pct':>10}{'95th pct':>10}{'P(profit)':>11}")
    random.seed(7)
    for tp, sl, label in configs:
        rets = net_returns(series, tp, sl, 2.50)
        means = []
        for _ in range(2000):
            sample = [random.choice(rets) for _ in range(n)]
            means.append(st.mean(sample))
        means.sort()
        p = sum(m > 0 for m in means) / len(means)
        print(f"   {label:<18}{st.mean(rets):>+9.3f}{means[100]:>+10.3f}"
              f"{means[1900]:>+10.3f}{p:>10.0%}")

    # 3. what does stake size do? gas is fixed, so it only shrinks as a fraction
    print("\n3. STAKE SIZE  (TP+100 SL-20, net per trade)")
    for stake in (1, 2.5, 5, 10, 25):
        rets = net_returns(series, 2.00, 0.80, stake)
        gas_pct = GAS_USD / stake
        print(f"   ${stake:>5.2f} stake   {st.mean(rets):>+7.3f} per trade"
              f"   gas is {gas_pct:>5.1%} of stake")

    # 4. how much does slippage matter?
    print("\n4. SLIPPAGE SENSITIVITY  (TP+100 SL-20, $10 stake)")
    for slip in (0.02, 0.05, 0.10, 0.15):
        rets = net_returns(series, 2.00, 0.80, 10, slip=slip)
        print(f"   {slip:>4.0%} slippage   {st.mean(rets):>+7.3f} per trade")
    return 0


if __name__ == "__main__":
    sys.exit(main())
