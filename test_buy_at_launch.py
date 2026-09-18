#!/usr/bin/env python3
"""
Would buying at LAUNCH beat buying at the CALL?

The channel posts a median 9 minutes after the pool is created (AD-023). The
obvious idea is to detect launches ourselves and skip that delay. Before
building it, check whether those 9 minutes are worth having -- they might be
where the money is, or they might be where the rugs are.

No new API calls: the cached OHLCV series already start before each launch, so
the first candle in a series is the token's first traded minute.

    python test_buy_at_launch.py

Both entries are priced the same way (close of the entry minute) so the
comparison is like for like.
"""
import json
import os
import statistics as st
import sys

from analyse_channel import WINDOW_MIN

HERE = os.path.dirname(os.path.abspath(__file__))
PRICES = os.path.join(HERE, "cache", "channel_prices.json")
OFFSET = WINDOW_MIN * 60 + 120

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def pairs():
    """(launch_entry, call_entry, forward_candles_from_each) per token."""
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
            rows = sorted(candles, key=lambda r: r[0])
            if len(rows) < 10:
                break
            first = rows[0]
            # Only count it as a launch if the series actually starts BEFORE the
            # call; otherwise we are just re-measuring the call.
            age_min = (call_ts - first[0]) / 60
            if age_min < 1:
                break
            after_call = [r for r in rows if r[0] >= call_ts - 60]
            if len(after_call) < 2:
                break
            out.append({
                "age": age_min,
                "launch_entry": first[4] or first[1],
                "launch_fwd": rows[1:],
                "call_entry": after_call[0][4] or after_call[0][1],
                "call_fwd": after_call[1:],
            })
            break
    return out


def stats(entry, fwd, horizon_min):
    if not entry or not fwd:
        return None
    win = fwd[:horizon_min]
    if not win:
        return None
    return {
        "peak": max(r[2] for r in win) / entry,
        "trough": min(r[3] for r in win) / entry,
    }


def summarise(label, rows, key_entry, key_fwd, horizon):
    vals = [stats(r[key_entry], r[key_fwd], horizon) for r in rows]
    vals = [v for v in vals if v]
    if not vals:
        print(f"  {label:<28} no data")
        return
    peaks = sorted(v["peak"] for v in vals)
    troughs = sorted(v["trough"] for v in vals)
    n = len(vals)
    print(f"  {label:<28} n={n:>3}   med peak {st.median(peaks):>5.2f}x   "
          f"2x {sum(p >= 2 for p in peaks)/n:>4.0%}   "
          f"5x {sum(p >= 5 for p in peaks)/n:>4.0%}   "
          f"-50% {sum(t <= 0.5 for t in troughs)/n:>4.0%}")


def main() -> int:
    rows = pairs()
    n = len(rows)
    if n < 30:
        print(f"only {n} tokens with pre-call history - not enough")
        return 1
    ages = sorted(r["age"] for r in rows)
    print(f"n = {n} tokens whose price history starts before the call")
    print(f"median history before the call: {st.median(ages):.1f} min\n")

    for horizon, label in ((60, "first hour"), (360, "six hours")):
        print(f"{label.upper()} after entry:")
        summarise("buy at LAUNCH", rows, "launch_entry", "launch_fwd", horizon)
        summarise("buy at the CALL", rows, "call_entry", "call_fwd", horizon)
        print()

    # How much of the move is already gone by the time the call lands?
    moved = []
    for r in rows:
        if r["launch_entry"]:
            moved.append(r["call_entry"] / r["launch_entry"])
    moved.sort()
    print("Price at the call, relative to the launch price:")
    print(f"   median {st.median(moved):.2f}x    "
          f"p25 {moved[len(moved)//4]:.2f}x    p75 {moved[3*len(moved)//4]:.2f}x")
    print(f"   already ABOVE launch when called : {sum(m > 1 for m in moved)/len(moved):.0%}")
    print(f"   already 2x+ when called          : {sum(m >= 2 for m in moved)/len(moved):.0%}")
    print(f"   already BELOW launch when called : {sum(m < 1 for m in moved)/len(moved):.0%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
