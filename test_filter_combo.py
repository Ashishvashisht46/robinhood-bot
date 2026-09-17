#!/usr/bin/env python3
"""
Does combining the promising features actually select better calls?

The single-feature gaps (mcap, liquidity, liq%, token age) beat the random
control at n=213. That is necessary but not sufficient: combining rules chosen
by looking at the data is exactly how a backtest fools you.

So the rules are FIXED on the first 60% of calls (chronologically) and then
applied, untouched, to the last 40% the rules have never seen. Only the holdout
number means anything.

    python test_filter_combo.py
"""
import json
import os
import statistics as st
import sys

from analyse_channel import WINDOW_MIN, evaluate
from test_moonshot import run

HERE = os.path.dirname(os.path.abspath(__file__))
META = os.path.join(HERE, "cache", "channel_meta.json")
PRICES = os.path.join(HERE, "cache", "channel_prices.json")
OFFSET = WINDOW_MIN * 60 + 120

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def dataset():
    meta = json.load(open(META, encoding="utf-8"))
    cache = json.load(open(PRICES, encoding="utf-8"))
    pools = {k.split(":", 1)[1]: v for k, v in cache.items() if k.startswith("pool:")}
    rows = []
    for ca, pool in pools.items():
        if not pool or ca not in meta:
            continue
        for k, candles in cache.items():
            if not k.startswith(f"ohlcv:{pool}:"):
                continue
            call_ts = int(k.rsplit(":", 1)[1]) - OFFSET
            ev = evaluate(candles, call_ts)
            if ev:
                after = [r for r in candles if r[0] >= call_ts - 60]
                entry = after[0][4] or after[0][1]
                window = [r for r in after[1:] if r[0] <= call_ts + WINDOW_MIN * 60]
                rows.append({**meta[ca], "ca": ca, "peak": ev["peak"],
                             "reached2x": ev["peak"] >= 2.0,
                             "series": (entry, window)})
            break
    return sorted(rows, key=lambda r: r["ts"])


def passes(r):
    """Fixed rules, chosen on the training half only. Missing field = reject."""
    try:
        return (r.get("liquidity_usd") or 0) > 18000 and \
               (r.get("mcap_usd") or 0) > 41500 and \
               (r.get("liq_pct_ok", True))
    except Exception:
        return False


def rate(rows, key="reached2x"):
    return (sum(r[key] for r in rows) / len(rows)) if rows else 0.0


def main() -> int:
    rows = dataset()
    n = len(rows)
    cut = int(n * 0.6)
    train, test = rows[:cut], rows[cut:]
    print(f"n = {n}   train = {len(train)} (older)   holdout = {len(test)} (newer)\n")

    variants = {
        "liquidity > $18k": lambda r: (r.get("liquidity_usd") or 0) > 18000,
        "mcap > $41.5k": lambda r: (r.get("mcap_usd") or 0) > 41500,
        "age <= 8 min": lambda r: (r.get("token_age_minutes") or 999) <= 8,
        "liq% <= 44": lambda r: (r.get("liquidity_pct") or 999) <= 44,
        "liq>18k AND mcap>41.5k": lambda r: (r.get("liquidity_usd") or 0) > 18000
            and (r.get("mcap_usd") or 0) > 41500,
        "liq>18k AND age<=8": lambda r: (r.get("liquidity_usd") or 0) > 18000
            and (r.get("token_age_minutes") or 999) <= 8,
        "all four": lambda r: (r.get("liquidity_usd") or 0) > 18000
            and (r.get("mcap_usd") or 0) > 41500
            and (r.get("token_age_minutes") or 999) <= 8
            and (r.get("liquidity_pct") or 999) <= 44,
    }

    print(f"{'filter':<26}{'TRAIN 2x':>12}{'HOLDOUT 2x':>14}{'kept':>8}")
    print("-" * 62)
    print(f"{'(no filter)':<26}{rate(train):>11.0%}{rate(test):>14.0%}"
          f"{len(test):>8}")
    keep = {}
    for name, fn in variants.items():
        tr = [r for r in train if fn(r)]
        te = [r for r in test if fn(r)]
        keep[name] = te
        tr_s = f"{rate(tr):.0%}" if len(tr) >= 8 else f"n={len(tr)}"
        te_s = f"{rate(te):.0%}" if len(te) >= 8 else f"n={len(te)}"
        print(f"{name:<26}{tr_s:>12}{te_s:>14}{len(te):>8}")

    print("\nOnly the HOLDOUT column is evidence. TRAIN is where the rules came")
    print("from, so it is guaranteed to look good.\n")

    # Does any surviving filter make money on the holdout?
    print("Net per trade on the HOLDOUT, 5x target / -10% stop, $10 stake:")
    print(f"{'filter':<26}{'trades':>8}{'net/trade':>12}{'total':>10}")
    print("-" * 58)
    base = run([r["series"] for r in test], 5.0, 0.90, 10)
    print(f"{'(no filter)':<26}{len(base):>8}{st.mean(base):>+12.2f}"
          f"{sum(base):>+10.2f}")
    for name, te in keep.items():
        if len(te) < 8:
            continue
        rets = run([r["series"] for r in te], 5.0, 0.90, 10)
        print(f"{name:<26}{len(rets):>8}{st.mean(rets):>+12.2f}{sum(rets):>+10.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
