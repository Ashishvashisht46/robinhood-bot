#!/usr/bin/env python3
"""
Is there ANY take-profit / stop-loss pair that makes these calls profitable?

Pure computation over cache/channel_prices.json -- no API calls, no Telegram.
Sweeps TP1 and SL across a grid and reports gross return per $1 staked.

    python sweep_ladder.py

Same conservatism as the scorer: if a candle's high clears TP and its low clears
SL, the stop is taken, because minute data cannot order them. Returns are GROSS
of gas, so subtract roughly $0.12 per round trip at any realistic stake.
"""
import json
import os
import statistics as st
import sys

from analyse_channel import WINDOW_MIN

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "cache", "channel_prices.json")
OFFSET = WINDOW_MIN * 60 + 120

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def series_from_cache():
    cache = json.load(open(CACHE, encoding="utf-8"))
    pools = {k.split(":", 1)[1]: v for k, v in cache.items() if k.startswith("pool:")}
    out = []
    for ca, pool in pools.items():
        if not pool:
            continue
        for k, candles in cache.items():
            if not k.startswith(f"ohlcv:{pool}:"):
                continue
            call_ts = int(k.rsplit(":", 1)[1]) - OFFSET
            after = [r for r in candles if r[0] >= call_ts - 60]
            if len(after) < 2:
                break
            entry = after[0][4] or after[0][1]
            window = [r for r in after[1:] if r[0] <= call_ts + WINDOW_MIN * 60]
            if entry and window:
                out.append((entry, window))
            break
    return out


def simulate(series, tp, sl, ratio=0.40):
    """One tranche of `ratio` at tp, remainder exits at the stop or at window end."""
    rets = []
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
            rets.append(sl - 1)
        elif hit == "tp":
            # take the tranche, ride the rest to the window close
            rest = (window[-1][4] / entry) - 1
            rets.append(ratio * (tp - 1) + (1 - ratio) * rest)
        else:
            rets.append((window[-1][4] / entry) - 1)
    return st.mean(rets), sum(r > 0 for r in rets) / len(rets)


def main() -> int:
    series = series_from_cache()
    if not series:
        print("no cached price series yet")
        return 1
    print(f"n = {len(series)} calls\n")
    tps = [1.10, 1.20, 1.30, 1.50, 2.00, 3.00]
    sls = [0.50, 0.65, 0.80, 0.90]

    print("gross return per $1 staked   (rows = stop, cols = take-profit)")
    print("        " + "".join(f"{(t-1)*100:>9.0f}%" for t in tps))
    best = None
    for sl in sls:
        cells = []
        for tp in tps:
            mean, wr = simulate(series, tp, sl)
            cells.append(mean)
            if best is None or mean > best[0]:
                best = (mean, tp, sl, wr)
        print(f"  -{(1-sl)*100:>2.0f}%  " + "".join(f"{c:>+9.2f}" for c in cells))

    mean, tp, sl, wr = best
    print()
    print(f"best cell: TP +{(tp-1)*100:.0f}%  SL -{(1-sl)*100:.0f}%  "
          f"-> {mean:+.2f} per $1 gross, {wr:.0%} of trades positive")
    print(f"           at a $2.50 stake that is {mean*2.5:+.2f} per trade gross,")
    print(f"           {mean*2.5 - 0.12:+.2f} after ~$0.12 round-trip gas")
    print()
    if mean * 2.5 - 0.12 <= 0:
        print("  NO configuration in this grid is profitable after gas.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
