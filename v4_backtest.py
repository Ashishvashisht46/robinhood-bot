#!/usr/bin/env python3
"""
Out-of-sample test of the only lead left: buy Uniswap V4 launches at the
launch print, sell 50% at 2x and 50% at 5x, -50% stop, $5 a trade.

The adversarial debate (AD-036) measured uni_v4 at +$4.62/trade -- on n=12,
from one overnight window. Twelve trades is a hint. This takes 60 more from a
window that does not overlap it and asks whether the hint survives.

The population is fixed BEFORE any price is looked at:
    every Uniswap V4 pool initialised 12-30 hours ago,
    minus Pons tokens (Pons factory launch in the prior 72h, or the Pons hook),
    minus the tokens the debate already used,
    shuffled with a fixed seed and taken strictly in that order.
Nothing is picked. Tokens that die are losses, not exclusions.

Every position gets the same 12h horizon, so none is judged on less time.

    python v4_backtest.py            # collect (resumable), then report
    python v4_backtest.py --report   # re-simulate from cache, no API calls

Read-only. Sends nothing.
"""
import argparse
import collections
import json
import os
import random
import statistics as st
import sys
import time
import urllib.error
import urllib.request

from contract_resolution import SYSTEM
from launch_universe import _rpc, block_ts, _load, _save

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "cache", "v4_backtest.json")
DEBATE = os.path.join(
    r"C:\Users\Ashish\AppData\Local\Temp\claude"
    r"\C--Users-Ashish-OneDrive-Desktop-Lux-dental-marketing---GHL"
    r"\ba6d32a4-542b-44d7-ab97-fa3a8ef6c660\scratchpad", "hist.json")

PONS_FACTORY = "0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e"
PONS_LAUNCHED = "0x8d4aad4953d0ca700d468f3753aa14432d1b35b43ec6409f051fb6aa43a89607"
PONS_HOOK = "0xe5e702641ea86f4ae6cc3cdaed2b886f976be044"
BAGS_HOOK = "0x2380abf72c17aabab76480244759ac7e2932eecc"
V4_PM = "0x8366a39CC670B4001A1121B8F6A443A643e40951"
V4_INIT = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"
ZERO = "0x" + "0" * 40
SEC_PER_BLOCK = 0.1007      # measured average; sizes windows, never targets a block

TARGET = 60
MAX_CHECK = 700
HORIZON = 12 * 3600
STAKE = 5.0
GAS_TX = 0.0625             # measured per transaction in the debate (AD-036)

GT = "https://api.geckoterminal.com/api/v2/networks/robinhood"
GT_HDR = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
          "Accept": "application/json"}
GT_PACE = 2.6

STRATS = {
    "PROPOSAL  50%@2x, 50%@5x, stop 0.5x": dict(rungs=[(2, .5), (5, .5)], be=False),
    "  same, stop -> breakeven after 2x ": dict(rungs=[(2, .5), (5, .5)], be=True),
    "  simple: 100% at 2x, stop 0.5x    ": dict(rungs=[(2, 1.0)], be=False),
    "  original 6-rung ladder           ": dict(
        rungs=[(2, .5), (3, .1), (4, .1), (5, .1), (7, .1), (10, .1)], be=False),
}
MAIN = "PROPOSAL  50%@2x, 50%@5x, stop 0.5x"


# ---- data collection --------------------------------------------------------

def get_logs(addr, topic, lo, hi):
    """Splits on refusal rather than skipping: a dropped chunk is lost data."""
    r = _rpc("eth_getLogs", [{"fromBlock": hex(lo), "toBlock": hex(hi),
                              "address": addr, "topics": [topic]}])
    if "error" not in r:
        return r.get("result") or []
    if hi - lo < 2_000:
        raise RuntimeError(f"eth_getLogs {lo}-{hi}: {r['error'].get('message')}")
    mid = (lo + hi) // 2
    return get_logs(addr, topic, lo, mid) + get_logs(addr, topic, mid + 1, hi)


def scan(addr, topic, lo, hi, chunk=250_000):
    out = []
    for s in range(lo, hi + 1, chunk):
        e = min(s + chunk - 1, hi)
        out += get_logs(addr, topic, s, e)
        print(f"   {e - lo:>10,}/{hi - lo:,} blocks", end="\r", flush=True)
    print(" " * 50, end="\r")
    return out


def gt(path):
    """(json, None), (None, '404') or (None, 'fail').

    Only a 404 is an answer. Anything else is a failed measurement and must not
    be recorded -- a cached failure looks exactly like a dead token (AD-033).
    """
    delay = 5
    for _ in range(7):
        try:
            req = urllib.request.Request(GT + path, headers=GT_HDR)
            data = json.load(urllib.request.urlopen(req, timeout=25))
            time.sleep(GT_PACE)
            return data, None
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                time.sleep(GT_PACE)
                return None, "404"
            time.sleep(delay)
            delay = min(90, delay * 2)
        except Exception:
            time.sleep(delay)
            delay = min(90, delay * 2)
    return None, "fail"


def hook_class(h):
    h = (h or ZERO).lower()
    if h == ZERO:
        return "plain V4 (no hook)"
    if h == PONS_HOOK:
        return "Pons hook"
    if h == BAGS_HOOK:
        return "Bags hook"
    return "other hook"


def interp(anchors, b):
    a = sorted(anchors)
    for (b0, t0), (b1, t1) in zip(a, a[1:]):
        if b <= b1:
            return t0 + (b - b0) * (t1 - t0) / (b1 - b0)
    (b0, t0), (b1, t1) = a[-2], a[-1]
    return t0 + (b - b0) * (t1 - t0) / (b1 - b0)


def build_population(store):
    h = int(_rpc("eth_blockNumber", [])["result"], 16)
    per_h = int(3600 / SEC_PER_BLOCK)
    lo, hi = h - 30 * per_h, h - 12 * per_h
    # The window must start AFTER the debate's window ends. Excluding its tokens
    # is not enough: an overlapping window shares its market hours, so a result
    # that was really "that night was good" would replicate by construction.
    # The first run of this script got that wrong and was stopped.
    debate_hi = (_load(DEBATE.replace("hist.json", "fresh_launches.json"), {})
                 .get("hi"))
    if debate_hi:
        lo = max(lo, int(debate_hi) + 1)
    if hi - lo < 2 * per_h:
        raise SystemExit(f"only {(hi - lo) / per_h:.1f}h of non-overlapping "
                         f"launches old enough to judge; wait and rerun")
    print(f"head {h:,}   window {lo:,}-{hi:,}  "
          f"({(hi - lo) / per_h:.1f}h of launches, all 12h+ old, "
          f"all after the debate's window ended at {debate_hi:,})")
    anchors = [(b, block_ts(b)) for b in (lo, (lo + hi) // 2, hi)]

    print("scanning Pons launches (the window plus 72h before it)...")
    pons = {"0x" + l["topics"][1][-40:].lower()
            for l in scan(PONS_FACTORY, PONS_LAUNCHED, lo - 72 * per_h, hi)
            if len(l.get("topics", [])) > 1}
    print(f"  {len(pons):,} Pons tokens")

    print("scanning Uniswap V4 pool initialisations...")
    inits = scan(V4_PM, V4_INIT, lo, hi)
    print(f"  {len(inits):,} V4 pools initialised")

    freq = collections.Counter()
    parsed = []
    for l in inits:
        t = l.get("topics", [])
        if len(t) < 4:
            continue
        c0, c1 = ("0x" + t[2][-40:]).lower(), ("0x" + t[3][-40:]).lower()
        data = l.get("data") or "0x"
        hook = ("0x" + data[130:194][-40:]).lower() if len(data) >= 194 else ZERO
        parsed.append((int(l["blockNumber"], 16), t[1], c0, c1, hook))
        freq[c0] += 1
        freq[c1] += 1

    def is_quote(a):
        return a in SYSTEM or a == ZERO or freq[a] >= 5

    hooks_all = collections.Counter()
    cands = {}
    for bn, pid, c0, c1, hook in sorted(parsed):
        hooks_all[hook_class(hook)] += 1
        side = [a for a in (c0, c1) if not is_quote(a)]
        if len(side) != 1:
            continue
        tok = side[0]
        if tok not in cands:                     # first pool is the launch
            cands[tok] = {"block": bn, "pool": pid, "hook": hook}

    n0 = len(cands)
    pons_hit = [t for t, c in cands.items() if t in pons or c["hook"] == PONS_HOOK]
    for t in pons_hit:
        cands.pop(t)
    debate = {k.lower() for k in _load(DEBATE, {})}
    overlap = [t for t in cands if t in debate]
    for t in overlap:
        cands.pop(t)

    order = sorted(cands)
    random.Random(4663).shuffle(order)
    store.update({
        "meta": {"head": h, "lo": lo, "hi": hi, "anchors": anchors,
                 "n_inits": len(inits), "n_pons": len(pons), "cands": n0,
                 "pons_removed": len(pons_hit), "debate_removed": len(overlap),
                 "hooks_all": dict(hooks_all)},
        "order": order, "cands": cands, "done": {}})
    _save(CACHE, store)


def collect():
    store = _load(CACHE, {})
    if "meta" not in store:
        build_population(store)
    meta, done = store["meta"], store["done"]
    now = time.time()
    n_c = sum(1 for v in done.values() if v["status"] == "C")
    fails = 0
    print(f"\npricing in fixed random order until {TARGET} are tradeable...")
    for tok in store["order"]:
        if n_c >= TARGET or len(done) >= MAX_CHECK:
            break
        if tok in done:
            continue
        c = store["cands"][tok]
        d, err = gt(f"/pools/{c['pool']}")
        if err == "404":
            done[tok] = {"status": "A"}
            _save(CACHE, store)
            continue
        if err:
            fails += 1
            continue
        a = d["data"]["attributes"]
        base = (d["data"].get("relationships", {}).get("base_token", {})
                .get("data", {}).get("id") or "").split("_", 1)[-1].lower()
        side = "base" if base == tok else "quote"
        created = interp(meta["anchors"], c["block"])
        before = int(min(created + HORIZON + 600, now - 60))
        o, err2 = gt(f"/pools/{c['pool']}/ohlcv/minute?aggregate=1&limit=1000"
                     f"&currency=usd&token={side}&before_timestamp={before}")
        if err2 and err2 != "404":
            fails += 1
            continue
        rows = sorted(((o or {}).get("data", {}).get("attributes", {})
                       .get("ohlcv_list") or []), key=lambda r: r[0])
        tx = (a.get("transactions") or {}).get("h24") or {}
        done[tok] = {"status": "C" if rows else "B", "created": created,
                     "side": side, "reserve_now": float(a.get("reserve_in_usd") or 0),
                     "h24_trades": (tx.get("buys") or 0) + (tx.get("sells") or 0),
                     "candles": rows}
        if rows:
            n_c += 1
        _save(CACHE, store)
        print(f"  checked {len(done):>3}   tradeable {n_c:>2}/{TARGET}   "
              f"failed lookups {fails}", end="\r", flush=True)
    print()
    if fails:
        print(f"  {fails} lookups failed and were NOT recorded; rerun to retry them.")

    for tok, rec in done.items():
        if rec["status"] == "C" and "supply" not in rec:
            try:
                s = _rpc("eth_call", [{"to": tok, "data": "0x18160ddd"}, "latest"]).get("result")
                dc = _rpc("eth_call", [{"to": tok, "data": "0x313ce567"}, "latest"]).get("result")
                rec["supply"] = int(s, 16) / 10 ** int(dc, 16)
            except Exception:
                rec["supply"] = None
    _save(CACHE, store)


# ---- simulation -------------------------------------------------------------

def simulate(rec, strat, slip=0.02, entry_min=1, horizon=HORIZON):
    """One trade. Pessimistic where the candle cannot say: if a minute touches
    both the stop and a target, the stop is assumed first. A stop fills at the
    worse of the stop price and the candle close, so a rug that gaps through
    the stop is filled near zero, not politely at 0.5x."""
    rows = rec["candles"]
    if not rows:
        return None
    T = rec["created"] + entry_min * 60
    before_T = [r for r in rows if r[0] + 60 <= T]
    E = (before_T[-1][4] if before_T else rows[0][1]) or 0
    if E <= 0:
        return None
    fwd = [r for r in rows if T <= r[0] <= T + horizon]
    ef = E * (1 + slip)
    peak = max([r[2] for r in fwd], default=E) / E
    t2x = next(((r[0] - T) / 60 for r in fwd if r[2] >= 2 * E), None)

    stop, remaining, proceeds, sells = 0.5, 1.0, 0.0, 0
    fired = [False] * len(strat["rungs"])
    tp1, outcome = False, None
    for r in fwd:
        hi_, lo_, cl = r[2], r[3], r[4]
        if lo_ <= E * stop:
            proceeds += remaining * min(E * stop, cl) * (1 - slip) / ef
            remaining, sells = 0.0, sells + 1
            outcome = "stopped after taking 2x" if tp1 else "stopped out"
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
            outcome = "hit every target"
            break
    if remaining > 1e-9:
        last = fwd[-1][4] if fwd else E
        proceeds += remaining * last * (1 - slip) / ef
        sells += 1
        outcome = "held to 12h after 2x" if tp1 else "held to 12h, no target"
    txs = 2 + sells                         # buy, approve, each sell
    return {"pnl": STAKE * (proceeds - 1) - txs * GAS_TX, "outcome": outcome,
            "peak": peak, "entry": E, "t2x": t2x, "txs": txs}


def boot(p, reps=20_000):
    rng, n = random.Random(4663), len(p)
    means = sorted(sum(rng.choice(p) for _ in range(n)) / n for _ in range(reps))
    return means[int(.025 * reps)], means[int(.975 * reps)], \
        sum(1 for m in means if m > 0) / reps


def report():
    store = _load(CACHE, {})
    if "meta" not in store:
        print("no data; run without --report first")
        return 1
    meta, done = store["meta"], store["done"]
    C = [r for r in done.values() if r["status"] == "C"]
    A = sum(1 for r in done.values() if r["status"] == "A")
    B = [r for r in done.values() if r["status"] == "B"]

    print("\n" + "=" * 76)
    print("POPULATION (fixed before any price was seen)")
    print("=" * 76)
    print(f"  V4 pools initialised 12-30h ago        {meta['n_inits']:>6,}")
    print(f"  distinct launched tokens               {meta['cands']:>6,}")
    print(f"  minus Pons tokens                      {meta['pons_removed']:>6,}")
    print(f"  minus tokens the debate already used   {meta['debate_removed']:>6,}")
    print(f"  checked, in fixed random order         {len(done):>6,}")
    print(f"    no pool record anywhere (A)          {A:>6,}")
    print(f"    pool, but never traded (B)           {len(B):>6,}")
    print(f"    tradeable with price history (C)     {len(C):>6,}   <- the test")
    print(f"  hooks on every V4 pool in the window: {meta['hooks_all']}")
    if len(C) < 20:
        print("\n  too few tradeable tokens to judge")
        return 1

    res = [x for x in (simulate(r, STRATS[MAIN]) for r in C) if x]
    p = [x["pnl"] for x in res]
    n = len(p)
    lo, hi, pprof = boot(p)
    wins = [x for x in p if x > 0]
    losses = [x for x in p if x <= 0]

    print("\n" + "=" * 76)
    print(f"THE PROPOSAL on {n} trades   ($5 stake, entry 1 min, 2% slippage, "
          f"${GAS_TX}/tx gas)")
    print("=" * 76)
    print(f"  win rate              {len(wins)/n:>7.0%}   ({len(wins)} won, "
          f"{len(losses)} lost)")
    print(f"  average win           ${st.mean(wins) if wins else 0:>+7.2f}")
    print(f"  average loss          ${st.mean(losses) if losses else 0:>+7.2f}")
    print(f"  median trade          ${st.median(p):>+7.2f}")
    print(f"  EV per trade          ${st.mean(p):>+7.2f}   "
          f"({st.mean(p)/STAKE:+.1%} of stake)")
    print(f"  95% CI on EV          [${lo:+.2f}, ${hi:+.2f}]")
    print(f"  total over {n} trades   ${sum(p):>+7.2f}   "
          f"(range ${lo*n:+.0f} to ${hi*n:+.0f})")
    print(f"  P(the {n} trades end in profit)   {pprof:.0%}")
    srt = sorted(p, reverse=True)
    print(f"  without the best 1    ${st.mean(srt[1:]):>+7.2f}/trade")
    print(f"  without the best 3    ${st.mean(srt[3:]):>+7.2f}/trade")
    print(f"  best / worst trade    ${srt[0]:+.2f} / ${srt[-1]:+.2f}")

    oc = collections.Counter(x["outcome"] for x in res)
    print("\n  how the trades ended:")
    for k, v in oc.most_common():
        g = [x["pnl"] for x in res if x["outcome"] == k]
        print(f"    {k:<26} {v:>3}  avg ${st.mean(g):+.2f}")
    pk = [x["peak"] for x in res]
    print(f"\n  touched 2x within 12h   {sum(1 for x in pk if x >= 2)/n:.0%}")
    print(f"  touched 5x within 12h   {sum(1 for x in pk if x >= 5)/n:.0%}")
    t2 = [x["t2x"] for x in res if x["t2x"] is not None]
    if t2:
        print(f"  median time to 2x       {st.median(t2):.0f} min")

    fdv = sorted(x["entry"] * r["supply"] for x, r in zip(res, C)
                 if r.get("supply"))
    if fdv:
        print("\n  market cap at entry (is it really ~$5k?):")
        q = lambda f: fdv[min(len(fdv) - 1, int(f * len(fdv)))]
        print(f"    p10 ${q(.1):,.0f}   p25 ${q(.25):,.0f}   median ${st.median(fdv):,.0f}"
              f"   p75 ${q(.75):,.0f}   p90 ${q(.9):,.0f}")
        near5 = sum(1 for f in fdv if 3_000 <= f <= 8_000)
        print(f"    entered between $3k and $8k: {near5}/{len(fdv)}")
        for lab, a_, b_ in (("entry < $10k", 0, 10_000), ("entry >= $10k", 10_000, 1e18)):
            g = [x["pnl"] for x, r in zip(res, C)
                 if r.get("supply") and a_ <= x["entry"] * r["supply"] < b_]
            if len(g) >= 5:
                print(f"    {lab:<14} n={len(g):>2}  EV ${st.mean(g):+.2f}/trade  "
                      f"win {sum(1 for v in g if v > 0)/len(g):.0%}")

    print("\n" + "=" * 76)
    print("DOES THE RESULT SURVIVE WORSE ASSUMPTIONS?   EV per trade")
    print("=" * 76)
    print(f"  {'slippage':<12}{'entry 1 min':>14}{'entry 2 min':>14}{'entry 4 min':>14}")
    for s in (0.02, 0.05, 0.10):
        row = []
        for em in (1, 2, 4):
            g = [x["pnl"] for x in (simulate(r, STRATS[MAIN], s, em) for r in C) if x]
            row.append(f"${st.mean(g):+.2f}")
        print(f"  {s:<12.0%}{row[0]:>14}{row[1]:>14}{row[2]:>14}")
    for hz, lab in ((2 * 3600, "2h horizon"), (HORIZON, "12h horizon")):
        g = [x["pnl"] for x in (simulate(r, STRATS[MAIN], horizon=hz) for r in C) if x]
        print(f"  {lab:<12}  ${st.mean(g):+.2f}/trade")

    print("\n  exit variants (entry 1 min, 2% slippage):")
    for name, s in STRATS.items():
        g = [x["pnl"] for x in (simulate(r, s) for r in C) if x]
        print(f"    {name}  ${st.mean(g):>+6.2f}/trade   win {sum(1 for v in g if v > 0)/len(g):.0%}")
    g6 = [x["pnl"] for x in (simulate(r, STRATS["  original 6-rung ladder           "],
                                      horizon=2 * 3600) for r in C) if x]
    print(f"\n  like-for-like with the debate (6 rungs, 2h, 2%): ${st.mean(g6):+.2f}/trade"
          f"   <- the debate measured +$4.62 on n=12")

    if B:
        benign = -(2 * 0.02 * STAKE + 3 * GAS_TX)
        share = len(B) / (len(B) + n)
        print(f"\n  buying EVERY launch blind also buys the {len(B)} that never traded.")
        print(f"  if you can sell back at the seed price that costs ${benign:.2f} each,")
        print(f"  dragging EV to ${st.mean(p) * (1 - share) + benign * share:+.2f}/trade; if the "
              f"liquidity is pulled, to ${st.mean(p) * (1 - share) - STAKE * share:+.2f}.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    if not ap.parse_args().report:
        collect()
    return report()


if __name__ == "__main__":
    sys.exit(main())
