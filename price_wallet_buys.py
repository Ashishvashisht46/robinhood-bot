#!/usr/bin/env python3
"""
Price what the scout wallets actually bought -- the test the channel list
cannot do, because it only shows a wallet when its buy became a call.

This deliberately tests the STRONGEST case: the most selective wallets, the
ones that convert the largest share of their buys into channel calls. If their
buys do not make money, no wallet lower down the list will.

Baseline to beat, from the same pricing code over 213 channel calls (AD-024):

    median peak 1.33x    reached 2x 21%    touched -50% 73%

    python price_wallet_buys.py            # 4 most selective wallets
    python price_wallet_buys.py --wallets 6

Read-only. Reuses analyse_channel's pool/OHLCV cache, so reruns are cheap.
"""
import argparse
import collections
import json
import os
import statistics as st
import sys

from analyse_channel import best_pool, candles, evaluate, load_cache, save_cache
from score_wallets import rpc, topic_addr, WINDOW, CHUNK

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
SCORES = os.path.join(HERE, "cache", "wallet_scores.json")
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def buys_with_blocks(wallets, head):
    """{wallet: {token: first_block_seen}} -- the first receipt is the entry."""
    topics = [topic_addr(w) for w in wallets]
    out = collections.defaultdict(dict)
    start = head - WINDOW
    for lo in range(start, head, CHUNK):
        hi = min(lo + CHUNK - 1, head)
        r = rpc("eth_getLogs", [{"fromBlock": hex(lo), "toBlock": hex(hi),
                                 "topics": [TRANSFER, None, topics]}])
        for log in r.get("result") or []:
            t = log.get("topics", [])
            if len(t) < 3:
                continue
            w = "0x" + t[2][-40:]
            tok = (log.get("address") or "").lower()
            bn = int(log["blockNumber"], 16)
            if tok not in out[w] or bn < out[w][tok]:
                out[w][tok] = bn
        print(f"  scanned {hi - start:>9,}/{WINDOW:,}", end="\r")
    print(" " * 50, end="\r")
    return out


def block_times(blocks):
    """Exact timestamps. Two anchors would drift over 1.2M blocks; don't guess."""
    ts = {}
    for i, bn in enumerate(sorted(set(blocks)), 1):
        r = rpc("eth_getBlockByNumber", [hex(bn), False])
        res = r.get("result")
        if res:
            ts[bn] = int(res["timestamp"], 16)
        print(f"  block times {i}/{len(set(blocks))}", end="\r")
    print(" " * 40, end="\r")
    return ts


def summarise(label, results):
    if not results:
        print(f"  {label:<34} no priced buys")
        return
    peaks = sorted(r["peak"] for r in results)
    troughs = sorted(r["trough"] for r in results)
    n = len(peaks)
    print(f"  {label:<34} n={n:>4}  med peak {st.median(peaks):>5.2f}x  "
          f"2x {sum(p >= 2 for p in peaks)/n:>4.0%}  "
          f"5x {sum(p >= 5 for p in peaks)/n:>4.0%}  "
          f"-50% {sum(t <= 0.5 for t in troughs)/n:>4.0%}  "
          f"tp1 {sum(r['outcome'].startswith('tp') for r in results)/n:>4.0%}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wallets", type=int, default=4)
    ap.add_argument("--min-tokens", type=int, default=12)
    args = ap.parse_args()

    scores = json.load(open(SCORES, encoding="utf-8"))
    pool = [s for s in scores if s["tokens"] >= args.min_tokens]
    pool.sort(key=lambda s: -(s["calls"] / s["tokens"]))
    picked = pool[:args.wallets]
    if not picked:
        print("no wallets meet the threshold; run score_wallets.py first")
        return 1

    print("Most selective recovered wallets (highest call-conversion):")
    for s in picked:
        print(f"  {s['trunc']}  {s['tier']:<5} {s['calls']:>2} calls / "
              f"{s['tokens']:>3} tokens = {s['calls']/s['tokens']:.0%}")
    print()

    head = int(rpc("eth_blockNumber", [])["result"], 16)
    addrs = [s["addr"] for s in picked]
    got = buys_with_blocks(addrs, head)
    all_blocks = [bn for toks in got.values() for bn in toks.values()]
    print(f"token-buys to price: {len(all_blocks)}")
    ts = block_times(all_blocks)

    cache = load_cache()
    per_wallet, every = {}, []
    try:
        for s in picked:
            res = []
            toks = got.get(s["addr"], {})
            for i, (tok, bn) in enumerate(sorted(toks.items(), key=lambda kv: kv[1]), 1):
                buy_ts = ts.get(bn)
                if not buy_ts:
                    continue
                pool_id = best_pool(tok, cache)
                if not pool_id:
                    continue
                rows = candles(pool_id, buy_ts + 360 * 60 + 120, cache)
                ev = evaluate(rows, buy_ts)
                if ev:
                    res.append(ev)
                print(f"  {s['trunc']}  priced {len(res):>3}/{i} of {len(toks)}",
                      end="\r")
            print(" " * 60, end="\r")
            per_wallet[s["trunc"]] = res
            every.extend(res)
    finally:
        save_cache(cache)

    print("\nOutcome of every buy these wallets made, priced from their own entry:")
    print(f"  {'':<34} {'':>6}  {'':>11}  {'':>7} {'':>7} {'':>9} reached +20%")
    for s in picked:
        summarise(f"{s['trunc']} ({s['calls']}/{s['tokens']} called)",
                  per_wallet[s["trunc"]])
    print()
    summarise("ALL selective-wallet buys", every)
    print()
    print("  Channel calls, same pricing code (AD-024):"
          "   n= 213  med peak  1.33x  2x  21%  5x   7%  -50%  73%")
    print("\nA wallet is only worth watching if its line beats that one.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
