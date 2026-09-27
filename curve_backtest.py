#!/usr/bin/env python3
"""
Buy a Pons token ON its bonding curve shortly after launch; sell when it
graduates to Uniswap V4, or at a time stop if it never does.

What the curve research established (AD-038):
  - every Pons token starts with 1,000,000,000 tokens on a constant-product
    curve against a virtual quote reserve (~41.13 SGOV / ~1.70 ETH, ~$4.1-4.6k)
  - graduation lists it on Uniswap V4 at ~12x the launch price, every time
  - ~1.1% of launches graduate
  - the curve price can NEVER fall below the launch price: all supply starts on
    the curve, so the worst case is everyone else selling back to it

So the downside is bounded by how far above launch you bought, and the upside
is the graduation multiple. Whether that nets out depends on how early you can
get in, which is what this measures -- from the curve's own trade events,
because this RPC ignores the block number on historical eth_call (measured).

    python curve_backtest.py            # collect (resumable), then report
    python curve_backtest.py --report

Read-only. Sends nothing.
"""
import argparse
import collections
import os
import random
import statistics as st
import sys

from launch_universe import _rpc, BLOCK_S, _load, _save

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "cache", "curve_backtest.json")

PONS_FACTORY = "0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e"
LAUNCHED = "0x8d4aad4953d0ca700d468f3753aa14432d1b35b43ec6409f051fb6aa43a89607"
BUY, SELL = "0xec36bf57", "0x8113d738"
GRADUATED_SEL = "0xe7c2b772"
SUPPLY = 1_000_000_000

SAMPLE = 400
STAKE = 5.0
GAS_TX = 0.0625
CURVE_FEE = 0.01            # measured on every curve trade
V4_EXIT_COST = 0.02         # slippage + pool fee selling on V4 after graduation
PER_H = int(3600 / 0.1007)


def get_logs(addr, lo, hi, topics=None):
    q = {"fromBlock": hex(lo), "toBlock": hex(hi), "address": addr}
    if topics:
        q["topics"] = topics
    r = _rpc("eth_getLogs", [q])
    if "error" not in r:
        return r.get("result") or []
    if hi - lo < 2_000:
        raise RuntimeError(r["error"].get("message"))
    mid = (lo + hi) // 2
    return get_logs(addr, lo, mid, topics) + get_logs(addr, mid + 1, hi, topics)


def words(data):
    return [int(data[2 + i:66 + i], 16) for i in range(0, len(data) - 2, 64)]


def collect():
    store = _load(CACHE, {})
    if "sample" not in store:
        head = int(_rpc("eth_blockNumber", [])["result"], 16)
        lo, hi = head - 48 * PER_H, head - 24 * PER_H      # every launch has 24h+
        print(f"head {head:,}; sampling launches from {lo:,}-{hi:,} (24-48h old)")
        launches = []
        for s in range(lo, hi, 250_000):
            launches += get_logs(PONS_FACTORY, s, min(s + 249_999, hi), [LAUNCHED])
            print(f"   {len(launches):,} launches", end="\r", flush=True)
        print()
        random.Random(4663).shuffle(launches)
        store.update({"head": head, "n_launches": len(launches),
                      "sample": [{"token": "0x" + l["topics"][1][-40:],
                                  "curve": "0x" + l["topics"][2][-40:],
                                  "pair": "0x" + l["data"][2:66][-40:],
                                  "block": int(l["blockNumber"], 16)}
                                 for l in launches[:SAMPLE]],
                      "done": {}})
        _save(CACHE, store)

    done = store["done"]
    fails = 0
    for i, s in enumerate(store["sample"], 1):
        if s["curve"] in done:
            continue
        try:
            ev = get_logs(s["curve"], s["block"], min(s["block"] + 48 * PER_H, store["head"]))
            g = _rpc("eth_call", [{"to": s["curve"], "data": GRADUATED_SEL}, "latest"])
            grad = int((g.get("result") or "0x0"), 16) == 1 if "error" not in g else None
        except Exception:
            fails += 1
            continue
        trades = []
        for e in sorted(ev, key=lambda e: (int(e["blockNumber"], 16), int(e["logIndex"], 16))):
            t0 = e["topics"][0][:10]
            if t0 in (BUY, SELL):
                trades.append([t0, int(e["blockNumber"], 16), words(e["data"])[:3]])
        done[s["curve"]] = {"graduated": grad, "trades": trades, **s}
        if i % 10 == 0:
            _save(CACHE, store)
        print(f"  {len(done):>3}/{len(store['sample'])} curves read   (failed {fails})",
              end="\r", flush=True)
    _save(CACHE, store)
    print()
    if fails:
        print(f"  {fails} curves failed to read and were NOT recorded; rerun to retry")


def path(rec):
    """Exact curve price after every trade, rebuilt from events.

    Initial virtual quote reserve comes from the first buy against a full
    1e9-token reserve: (r0 + dq)(1e9 - dt) = r0 * 1e9. Every later trade moves
    the reserves by exactly what it moved in and out.
    """
    tr = rec["trades"]
    first = next((t for t in tr if t[0] == BUY), None)
    if not first:
        return None, []
    dq = (first[2][0] - first[2][2]) / 1e18
    dt = first[2][1] / 1e18
    if dt <= 0:
        return None, []
    r0 = dq * (SUPPLY - dt) / dt
    p0 = r0 / SUPPLY
    r1 = float(SUPPLY)
    out = []
    for kind, blk, w in tr:
        if kind == BUY:
            r0 += (w[0] - w[2]) / 1e18
            r1 -= w[1] / 1e18
        else:
            r1 += w[0] / 1e18
            r0 -= (w[1] + w[2]) / 1e18
        if r1 > 0:
            out.append((blk, r0 / r1))
    return p0, out


def simulate(rec, entry_s=60, time_stop_h=24.0):
    p0, pts = path(rec)
    lb = rec["block"]
    entry_blk = lb + int(entry_s / BLOCK_S)
    if p0 is None:                       # nobody ever traded it
        return {"kind": "never traded", "pnl": STAKE * ((1 - CURVE_FEE) ** 2 - 1) - 3 * GAS_TX,
                "entry_x": 1.0, "grad": False}
    before = [p for b, p in pts if b <= entry_blk]
    after = [(b, p) for b, p in pts if b > entry_blk]
    graduated = bool(rec["graduated"])
    grad_blk = pts[-1][0] if (graduated and pts) else None
    if graduated and grad_blk is not None and grad_blk <= entry_blk:
        return {"kind": "graduated before you could buy", "pnl": None,
                "entry_x": None, "grad": True}
    entry = before[-1] if before else p0
    cost = entry / (1 - CURVE_FEE)                       # the 1% buy fee
    stop_blk = entry_blk + int(time_stop_h * 3600 / BLOCK_S)
    if graduated and grad_blk is not None and grad_blk <= stop_blk:
        exit_p = pts[-1][1] * (1 - V4_EXIT_COST)
        kind = "graduated -> sold on V4"
    else:
        held = [p for b, p in after if b <= stop_blk]
        last = held[-1] if held else entry
        exit_p = last * (1 - CURVE_FEE)
        kind = "no graduation -> sold back to curve"
    mult = exit_p / cost
    return {"kind": kind, "pnl": STAKE * (mult - 1) - 3 * GAS_TX,
            "entry_x": entry / p0, "grad": graduated, "mult": mult}


def boot(p, reps=20_000):
    rng, n = random.Random(4663), len(p)
    m = sorted(sum(rng.choice(p) for _ in range(n)) / n for _ in range(reps))
    return m[int(.025 * reps)], m[int(.975 * reps)], sum(1 for x in m if x > 0) / reps


def report():
    store = _load(CACHE, {})
    recs = list(store.get("done", {}).values())
    if not recs:
        print("no data; run without --report")
        return 1
    n = len(recs)
    g = [r for r in recs if r["graduated"]]
    traded = [r for r in recs if any(t[0] == BUY for t in r["trades"])]
    print("\n" + "=" * 76)
    print(f"POPULATION  {n} Pons launches, random, 24-48h old (of "
          f"{store['n_launches']:,} in the window)")
    print("=" * 76)
    print(f"  ever traded on the curve      {len(traded):>4}  ({len(traded)/n:.0%})")
    print(f"  graduated to Uniswap V4       {len(g):>4}  ({len(g)/n:.1%})")
    mults = []
    for r in g:
        p0, pts = path(r)
        if p0 and pts:
            mults.append(pts[-1][1] / p0)
    if mults:
        print(f"  graduation price / launch     median {st.median(mults):.1f}x   "
              f"(cross-check: the V4 listings measured 11.9-12.2x)")
    tg = []
    for r in g:
        _, pts = path(r)
        if pts:
            tg.append((pts[-1][0] - r["block"]) * BLOCK_S / 60)
    if tg:
        tg.sort()
        print(f"  time to graduate              median {st.median(tg):.0f} min;  "
              f"within 1 min: {sum(1 for t in tg if t <= 1)}/{len(tg)}")

    for entry_s in (30, 60, 120, 300):
        res = [simulate(r, entry_s) for r in recs]
        can = [x for x in res if x["pnl"] is not None]
        p = [x["pnl"] for x in can]
        if len(p) < 20:
            continue
        lo, hi, pp = boot(p)
        kinds = collections.Counter(x["kind"] for x in res)
        ex = [x["entry_x"] for x in can if x["entry_x"]]
        print("\n" + "=" * 76)
        print(f"BUY {entry_s}s AFTER LAUNCH, sell at graduation or after 24h   "
              f"(${STAKE:.0f} stake)")
        print("=" * 76)
        for k, v in kinds.most_common():
            sub = [x["pnl"] for x in res if x["kind"] == k and x["pnl"] is not None]
            tail = f"   avg ${st.mean(sub):+.2f}" if sub else ""
            print(f"    {k:<38} {v:>4}{tail}")
        print(f"  you paid (vs launch price)    median {st.median(ex):.2f}x   "
              f"p90 {sorted(ex)[int(.9 * len(ex))]:.2f}x")
        print(f"  graduated after you bought    {sum(1 for x in can if x['kind'].startswith('graduated'))}"
              f" of {len(can)}  ({sum(1 for x in can if x['kind'].startswith('graduated'))/len(can):.1%})")
        print(f"  win rate                      {sum(1 for v in p if v > 0)/len(p):.0%}")
        print(f"  EV per trade                  ${st.mean(p):+.3f}   95% CI "
              f"[${lo:+.3f}, ${hi:+.3f}]")
        print(f"  median trade                  ${st.median(p):+.3f}")
        print(f"  P(profit over {len(p)} trades)     {pp:.0%}")
        srt = sorted(p, reverse=True)
        print(f"  without the best 1 / best 3   ${st.mean(srt[1:]):+.3f} / ${st.mean(srt[3:]):+.3f}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    if not ap.parse_args().report:
        collect()
    return report()


if __name__ == "__main__":
    sys.exit(main())
