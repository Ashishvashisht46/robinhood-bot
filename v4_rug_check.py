#!/usr/bin/env python3
"""
Re-run the V4 backtest with the rugs put back in.

The first pass marked every open position at its last trade price. But pulling
liquidity is not a trade -- it leaves no candle. So a pool drained mid-hold
looked like a token that simply went quiet, and a position that was really a
total loss got booked at roughly its entry price. On the first pass 42 of 60
trades ended "held to 12h, no target" at an average of -$0.21, which is not how
memecoins behave, and 45% of those pools are empty today.

Uniswap V4 logs every liquidity change as ModifyLiquidity(poolId, ...). This
finds the moment each pool was drained and re-runs the trade: anything still
held at that moment is lost. Fills taken before it are kept.

It also settles the 77 pools that never traded: a pool that never had liquidity
could not have been bought (no cost), but one that did would have been bought
blind -- and if it was later drained, that is a total loss too.

    python v4_rug_check.py

Read-only. Sends nothing.
"""
import json
import os
import random
import statistics as st
import sys

from eth_utils import keccak

import v4_backtest as vb
from launch_universe import _rpc, _load, _save

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
LIQ_CACHE = os.path.join(HERE, "cache", "v4_liquidity.json")
MODIFY = "0x" + keccak(text="ModifyLiquidity(bytes32,address,int24,int24,int256,bytes32)").hex()
DRAINED = 0.10          # cumulative liquidity below 10% of its peak = drained


def signed(x):
    return x - (1 << 256) if x >= (1 << 255) else x


def liquidity_history(pool_id, lo, hi):
    """Every liquidity change on one pool, oldest first: [(block, delta)]."""
    out = []
    for s in range(lo, hi + 1, 400_000):
        e = min(s + 399_999, hi)
        r = _rpc("eth_getLogs", [{"fromBlock": hex(s), "toBlock": hex(e),
                                  "address": vb.V4_PM, "topics": [MODIFY, pool_id]}])
        if "error" in r:
            raise RuntimeError(r["error"].get("message"))
        for log in r.get("result") or []:
            w = log["data"][2:]
            out.append((int(log["blockNumber"], 16), signed(int(w[128:192], 16))))
    return sorted(out)


def drain_block(hist):
    """Block after which net liquidity stays below DRAINED of its peak FOR GOOD.

    The first version returned the first dip, which flagged 11 pools as
    "drained before entry" even though they kept trading afterwards -- a
    creator pulling liquidity and re-adding it in a new range is a reposition,
    not a rug, and cannot have been drained if people traded through it.
    """
    cum, peak, series = 0, 0, []
    for blk, d in hist:
        cum += d
        peak = max(peak, cum)
        series.append((blk, cum))
    if peak <= 0 or series[-1][1] >= DRAINED * peak:
        return None                          # still funded at the end
    drain = None
    for blk, c in series:
        if c < DRAINED * peak:
            if drain is None:
                drain = blk                  # start of a below-the-line stretch
        else:
            drain = None                     # refilled: that dip was not a rug
    return drain


def simulate_with_rugs(rec, drain_ts, strat, slip=0.02, entry_min=1):
    """vb.simulate, except a drain while holding zeroes what is still held."""
    rows = rec["candles"]
    T = rec["created"] + entry_min * 60
    before_T = [r for r in rows if r[0] + 60 <= T]
    E = (before_T[-1][4] if before_T else rows[0][1]) or 0
    if E <= 0:
        return None
    end = T + vb.HORIZON
    if drain_ts is not None and drain_ts <= T:
        # An empty pool cannot be quoted, so the buy is never sent. It is not a
        # $5 loss -- the first version booked it as one.
        return {"pnl": None, "outcome": "drained before you could buy"}
    fwd = [r for r in rows if T <= r[0] <= end]
    ef = E * (1 + slip)
    stop, remaining, proceeds, sells = 0.5, 1.0, 0.0, 0
    fired = [False] * len(strat["rungs"])
    tp1 = False
    for r in fwd:
        if drain_ts is not None and r[0] >= drain_ts:
            break
        hi_, lo_, cl = r[2], r[3], r[4]
        if lo_ <= E * stop:
            proceeds += remaining * min(E * stop, cl) * (1 - slip) / ef
            remaining, sells = 0.0, sells + 1
            break
        for i, (m, frac) in enumerate(strat["rungs"]):
            if not fired[i] and hi_ >= E * m and remaining > 1e-9:
                take = min(frac, remaining)
                proceeds += take * E * m * (1 - slip) / ef
                remaining -= take
                fired[i], sells = True, sells + 1
                if i == 0:
                    tp1 = True
                    if strat["be"]:
                        stop = max(stop, 1.0)
        if remaining <= 1e-9:
            break
    rugged = drain_ts is not None and drain_ts < end and remaining > 1e-9
    if remaining > 1e-9:
        if rugged:
            outcome = "RUGGED while holding" + (" (after 2x)" if tp1 else "")
        else:
            last = [r for r in fwd if drain_ts is None or r[0] < drain_ts]
            proceeds += remaining * (last[-1][4] if last else E) * (1 - slip) / ef
            sells += 1
            outcome = "held to 12h" + (" after 2x" if tp1 else ", no target")
    else:
        outcome = "hit every target" if all(fired) else ("stopped after 2x" if tp1 else "stopped out")
    txs = 2 + sells
    return {"pnl": vb.STAKE * (proceeds - 1) - txs * vb.GAS_TX, "outcome": outcome}


def main() -> int:
    store = _load(vb.CACHE, {})
    meta = store["meta"]
    head = int(_rpc("eth_blockNumber", [])["result"], 16)
    liq = _load(LIQ_CACHE, {})
    todo = [(t, r) for t, r in store["done"].items() if r["status"] in ("B", "C")]
    print(f"reading liquidity history for {len(todo)} pools...")
    for i, (tok, r) in enumerate(todo, 1):
        pid = store["cands"][tok]["pool"]
        if pid in liq:
            continue
        lo = store["cands"][tok]["block"]
        liq[pid] = liquidity_history(pid, lo, head)
        _save(LIQ_CACHE, liq)
        print(f"  {i}/{len(todo)}", end="\r", flush=True)
    print(" " * 30)

    def ts(b):
        return vb.interp(meta["anchors"], b)

    res, notes = [], {"never funded": 0, "funded, never traded": 0,
                      "funded, never traded, then drained": 0}
    for tok, r in todo:
        pid = store["cands"][tok]["pool"]
        hist = liq.get(pid, [])
        added = any(d > 0 for _, d in hist)
        db = drain_block(hist)
        dts = ts(db) if db else None
        if r["status"] == "B":
            if not added:
                notes["never funded"] += 1          # no liquidity: no quote, no buy
                continue
            if dts is not None:
                notes["funded, never traded, then drained"] += 1
                res.append({"pnl": -vb.STAKE - 2 * vb.GAS_TX, "outcome": "bought blind, then drained",
                            "blind": True})
            else:
                notes["funded, never traded"] += 1
                res.append({"pnl": vb.STAKE * ((1 - .02) ** 2 - 1) - 3 * vb.GAS_TX,
                            "outcome": "bought blind, sold back", "blind": True})
            continue
        x = simulate_with_rugs(r, dts, vb.STRATS[vb.MAIN])
        if x and x["pnl"] is None:
            notes["drained before you could buy (no trade)"] = \
                notes.get("drained before you could buy (no trade)", 0) + 1
        elif x:
            x["blind"] = False
            res.append(x)

    def summary(label, rows):
        p = [x["pnl"] for x in rows]
        if not p:
            return
        n = len(p)
        rng = random.Random(4663)
        m = sorted(sum(rng.choice(p) for _ in range(n)) / n for _ in range(20_000))
        print(f"\n{label}")
        print(f"  trades {n}   win rate {sum(1 for v in p if v > 0) / n:.0%}   "
              f"EV ${st.mean(p):+.2f}/trade   95% CI [${m[500]:+.2f}, ${m[19_499]:+.2f}]")
        print(f"  total ${sum(p):+.2f}   P(profit) {sum(1 for v in m if v > 0) / len(m):.0%}")

    print("=" * 76)
    print("THE 60 TRADEABLE TOKENS, WITH RUGS COUNTED")
    print("=" * 76)
    traded = [x for x in res if not x["blind"]]
    for k in sorted({x["outcome"] for x in traded}):
        g = [x["pnl"] for x in traded if x["outcome"] == k]
        print(f"  {k:<32} {len(g):>3}   avg ${st.mean(g):+.2f}")
    summary("RESULT, rugs counted (was +$1.77/trade before):", traded)

    print("\n" + "=" * 76)
    print("THE 77 THAT NEVER TRADED -- what buying every launch blind costs")
    print("=" * 76)
    for k, v in notes.items():
        print(f"  {k:<40} {v:>3}")
    summary("BUY EVERY V4 LAUNCH BLIND (traded + funded-but-untraded):", res)
    return 0


if __name__ == "__main__":
    sys.exit(main())
