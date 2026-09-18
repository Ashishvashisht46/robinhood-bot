#!/usr/bin/env python3
"""
"Don't buy a finished wick" -- is it true here?

The claim: if the price has ALREADY run by the time you would buy, the move is
over and you are buying the top. Testable against the cached call prices.

Proxy for "already moved": within the minute the call landed, how far did price
travel from that candle's open to its close. A call whose own minute closes 12%+
above its open has already been bought by someone faster.

    python test_wick_filter.py

Outcomes are measured from the SAME entry price in both groups (the call-minute
close), so this compares like with like -- it asks whether an already-moved call
goes on to do worse, not whether you paid more.
"""
import json
import os
import statistics as st
import sys

from analyse_channel import WINDOW_MIN, evaluate

HERE = os.path.dirname(os.path.abspath(__file__))
PRICES = os.path.join(HERE, "cache", "channel_prices.json")
OFFSET = WINDOW_MIN * 60 + 120

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def rows():
    cache = json.load(open(PRICES, encoding="utf-8"))
    pools = {k.split(":", 1)[1]: v for k, v in cache.items() if k.startswith("pool:")}
    out = []
    for ca, pool in pools.items():
        if not pool:
            continue
        for k, candles in cache.items():
            if not k.startswith(f"ohlcv:{pool}:"):
                continue
            call_ts = int(k.rsplit(":", 1)[1]) - OFFSET
            ev = evaluate(candles, call_ts)
            after = [r for r in candles if r[0] >= call_ts - 60]
            if ev and len(after) >= 2:
                c = after[0]                       # the call-minute candle
                o, hi, lo, cl = c[1], c[2], c[3], c[4]
                if o:
                    out.append({
                        "run": cl / o,             # how far it moved in that minute
                        "wick": hi / o,            # how far it spiked
                        "peak": ev["peak"], "trough": ev["trough"],
                        "win": ev["outcome"] in ("tp1", "tp2"),
                        "x2": ev["peak"] >= 2.0,
                    })
            break
    return out


def summarise(label, group, total):
    if not group:
        print(f"  {label:<34} n=0")
        return
    print(f"  {label:<34} n={len(group):>3} ({len(group)/total:>3.0%})   "
          f"2x {sum(g['x2'] for g in group)/len(group):>4.0%}   "
          f"win {sum(g['win'] for g in group)/len(group):>4.0%}   "
          f"med peak {st.median(g['peak'] for g in group):>5.2f}x")


def main() -> int:
    data = rows()
    n = len(data)
    print(f"n = {n} calls with a usable call-minute candle\n")
    if n < 30:
        print("not enough data")
        return 1

    print("Grouped by how far price moved DURING the call minute (close/open):")
    print("-" * 74)
    for lo, hi, label in ((0.0, 1.00, "fell or flat        (<1.00x)"),
                          (1.00, 1.06, "drifted up      (1.00-1.06x)"),
                          (1.06, 1.12, "moving          (1.06-1.12x)"),
                          (1.12, 1.30, "already run     (1.12-1.30x)"),
                          (1.30, 99.0, "finished wick       (>1.30x)")):
        summarise(label, [d for d in data if lo <= d["run"] < hi], n)

    print("\nThe proposed rule, as a binary split:")
    print("-" * 74)
    fresh = [d for d in data if d["run"] < 1.12]
    late = [d for d in data if d["run"] >= 1.12]
    summarise("BUY     (moved <1.12x)", fresh, n)
    summarise("SKIP    (moved >=1.12x)", late, n)

    if fresh and late:
        d2 = sum(f["x2"] for f in fresh)/len(fresh) - sum(l["x2"] for l in late)/len(late)
        dw = sum(f["win"] for f in fresh)/len(fresh) - sum(l["win"] for l in late)/len(late)
        print(f"\n  advantage of skipping the runners:  2x {d2:>+5.0%}   win {dw:>+5.0%}")
        print(f"  trades dropped: {len(late)} of {n} ({len(late)/n:.0%})")
        if d2 > 0.08 or dw > 0.08:
            print("\n  -> the rule helps on this sample")
        elif d2 < -0.08 or dw < -0.08:
            print("\n  -> BACKWARDS on this sample: the already-moving calls did BETTER")
        else:
            print("\n  -> no meaningful difference; the rule costs trades for nothing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
