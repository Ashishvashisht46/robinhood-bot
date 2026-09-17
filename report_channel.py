#!/usr/bin/env python3
"""
Score the channel's calls from whatever analyse_channel.py has already cached.

Reads only cache/channel_prices.json, so it is safe to run while the collector
is still working and it never re-hits the API. The call timestamp is recoverable
from the cache key, which is why no Telegram access is needed here.

    python report_channel.py
"""
import json
import os
import statistics as st
import sys

from analyse_channel import SL, TP1, TP2, WINDOW_MIN, evaluate

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "cache", "channel_prices.json")
OFFSET = WINDOW_MIN * 60 + 120          # how candles() built its cache key

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    cache = json.load(open(CACHE, encoding="utf-8"))
    pool_of = {k.split(":", 1)[1]: v for k, v in cache.items() if k.startswith("pool:")}

    rows, no_pool, no_price = [], 0, 0
    for ca, pool in pool_of.items():
        if not pool:
            no_pool += 1
            continue
        series = [(k, v) for k, v in cache.items() if k.startswith(f"ohlcv:{pool}:")]
        if not series:
            continue
        key, candles = series[0]
        call_ts = int(key.rsplit(":", 1)[1]) - OFFSET
        res = evaluate(candles, call_ts)
        if not res:
            no_price += 1
            continue
        rows.append({"ca": ca, "ts": call_ts, **res})

    n = len(rows)
    if not n:
        print("nothing priced yet")
        return 1
    uncov = no_pool + no_price

    tp1 = sum(r["outcome"] in ("tp1", "tp2") for r in rows)
    tp2 = sum(r["outcome"] == "tp2" for r in rows)
    sl = sum(r["outcome"] == "sl" for r in rows)
    none = sum(r["outcome"] == "none" for r in rows)
    peaks = sorted(r["peak"] for r in rows)
    troughs = sorted(r["trough"] for r in rows)
    tt = [r["mins_to_tp1"] for r in rows if r["mins_to_tp1"] is not None]

    print("=" * 70)
    print(f"CHANNEL CALL QUALITY   n={n} priced   ({uncov} uncovered)")
    print(f"entry = close of call minute | window {WINDOW_MIN//60}h | "
          f"TP1 +{(TP1-1)*100:.0f}% | SL -{(1-SL)*100:.0f}%")
    print("=" * 70)
    print(f"  +20% before -50%   (ladder WIN)   {tp1:>4}   {tp1/n:>5.0%}")
    print(f"     also reached +40% (TP2)        {tp2:>4}   {tp2/n:>5.0%}")
    print(f"  -50% first         (ladder LOSS)  {sl:>4}   {sl/n:>5.0%}")
    print(f"  neither within {WINDOW_MIN//60}h              {none:>4}   {none/n:>5.0%}")
    print()
    print(f"  median peak                     {st.median(peaks):>6.2f}x")
    print(f"  median trough                   {st.median(troughs):>6.2f}x")
    print(f"  ever touched +20%               {sum(p>=TP1 for p in peaks):>4}   "
          f"{sum(p>=TP1 for p in peaks)/n:>5.0%}")
    print(f"  ever touched +100%              {sum(p>=2 for p in peaks):>4}   "
          f"{sum(p>=2 for p in peaks)/n:>5.0%}")
    print(f"  ever touched -50%               {sum(t<=SL for t in troughs):>4}   "
          f"{sum(t<=SL for t in troughs)/n:>5.0%}")
    if tt:
        print(f"  median minutes to +20%          {st.median(tt):>6.0f}")
    print()

    # What the actual ladder would have returned, per $1 staked, gross of gas.
    # TP1 sells 40% at +20%; TP2 sells another 40% at +40%; the rest is assumed
    # closed at the stop for a loser and left at the trough otherwise. This is a
    # rough envelope, not a backtest of the live exit logic.
    per_trade = []
    for r in rows:
        if r["outcome"] == "sl":
            per_trade.append(-0.50)
        elif r["outcome"] == "tp2":
            per_trade.append(0.40 * 0.20 + 0.40 * 0.40 + 0.20 * (r["trough"] - 1))
        elif r["outcome"] == "tp1":
            per_trade.append(0.40 * 0.20 + 0.60 * (r["trough"] - 1))
        else:
            per_trade.append(r["trough"] - 1 if r["trough"] < 1 else 0.0)
    print(f"  rough gross return per $1 staked  {st.mean(per_trade):>+6.2f}"
          f"   (median {st.median(per_trade):+.2f})")
    print(f"  ...at $2.50 stake                 {st.mean(per_trade)*2.5:>+6.2f} per trade")
    print(f"  ...minus ~$0.12 gas               {st.mean(per_trade)*2.5-0.12:>+6.2f} per trade")
    return 0


if __name__ == "__main__":
    sys.exit(main())
