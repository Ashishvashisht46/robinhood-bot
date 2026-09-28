#!/usr/bin/env python3
"""
Did last week's 10x coins show anything in their first minute a bot could act on?

Ashish's point: coins are doing 10-20x. True -- a Pons coin that graduates to
Uniswap V4 lists at ~12x its launch price (AD-038). What matters is whether a
winner can be told apart from the losers early enough to buy it.

Design (case-control, because there are ~80k launches a week):
  - every Pons launch 1-8 days old (so each has 24h of history)
  - EVERY one that graduated, plus a random sample of the ones that did not,
    weighted back up by the sampling rate
  - features only from the first 60s after launch; the simulated buy happens
    at 60s, after they are known -- no hindsight
  - rules are chosen on the first 4 days and scored on the last 3, which the
    search never saw

    python winners_first_minute.py            # collect (resumable), then report
    python winners_first_minute.py --report

Read-only. Sends nothing.
"""
import argparse
import json
import os
import random
import statistics as st
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from eth_utils import keccak

import curve_backtest as cb
import launch_universe as lu
import v4_backtest as vb

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
META = os.path.join(HERE, "cache", "winners_meta.json")
CURVES = os.path.join(HERE, "cache", "winners_curves.jsonl")
SWAPS = os.path.join(HERE, "cache", "winners_v4.jsonl")

V4_SWAP = "0x" + keccak(text="Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)").hex()
PAIRS = {"0x" + "0" * 40: "ETH", "0x0bd7d308f8e1639fab988df18a8011f41eacad73": "ETH",
         "0x92fd66527192e3e61d4ddd13322aa222de86f9b5": "SGOV",
         "0x5fc5360d0400a0fd4f2af552add042d716f1d168": "USDG"}

BLOCK_S = 0.1007                  # measured; the nominal 0.10 drifts (AD-032)
DAY = int(86_400 / BLOCK_S)
ENTRY_S = 60
NON_GRAD_SAMPLE = 2_000
WORKERS = 3
STAKE, GAS_TX = cb.STAKE, cb.GAS_TX
CURVE_FEE, V4_EXIT_COST = cb.CURVE_FEE, cb.V4_EXIT_COST


def a40(word):
    return "0x" + word[-40:].lower()


# --- collect -------------------------------------------------------------------

def collect():
    meta = lu._load(META, {})
    if "launches" not in meta:
        head = lu.head()
        hi = head - DAY                      # youngest study launch: 1 day old
        lo = hi - 7 * DAY                    # oldest: 8 days old
        hist = lo - 7 * DAY                  # creator track record: 7 days before that
        anchors = [(b, lu.block_ts(b)) for b in range(hist, head + 1, (head - hist) // 20)]
        anchors.append((head, lu.block_ts(head)))
        print(f"launches {hist:,}-{hi:,} (study window from {lo:,})")
        raw = vb.scan(cb.PONS_FACTORY, cb.LAUNCHED, hist, hi)
        launches = [{"token": a40(l["topics"][1]), "curve": a40(l["topics"][2]),
                     "creator": a40(l["topics"][3]), "pair": a40(l["data"][2:66]),
                     "block": int(l["blockNumber"], 16), "tx": l["transactionHash"]}
                    for l in raw]
        print(f"  {len(launches):,} launches; graduations (V4 pools with the Pons hook)...")
        inits = vb.scan(vb.V4_PM, vb.V4_INIT, hist, min(head, hi + DAY + 1_000))
        known = {l["token"] for l in launches}
        grads = {}
        for g in inits:
            d = g["data"]
            if len(d) < 258 or a40(d[130:194]) != vb.PONS_HOOK:
                continue
            c0, c1 = a40(g["topics"][2]), a40(g["topics"][3])
            tok = c0 if c0 in known else c1 if c1 in known else None
            if tok and tok not in grads:
                grads[tok] = {"block": int(g["blockNumber"], 16), "pool": g["topics"][1],
                              "sqrtP": int(d[194:258], 16), "tok_is_c0": tok == c0}
        meta.update({"head": head, "lo": lo, "hi": hi, "hist": hist, "anchors": anchors,
                     "launches": launches, "grads": grads})
        study = [l for l in launches if lo <= l["block"] <= hi]
        rng = random.Random(4663)
        non = [l for l in study if l["token"] not in grads]
        meta["sample"] = [l["curve"] for l in study if l["token"] in grads] + \
            [l["curve"] for l in rng.sample(non, min(NON_GRAD_SAMPLE, len(non)))]
        meta["n_study"], meta["n_study_non"] = len(study), len(non)
        lu._save(META, meta)
        print(f"  study window: {len(study):,} launches, "
              f"{len(study) - len(non)} graduated; sampling {len(meta['sample'])}")

    by_curve = {l["curve"]: l for l in meta["launches"]}
    done = set()
    if os.path.exists(CURVES):
        done = {json.loads(x)["curve"] for x in open(CURVES, encoding="utf-8") if x.strip()}
    todo = [by_curve[c] for c in meta["sample"] if c not in done]
    print(f"curve histories: {len(done)} cached, {len(todo)} to fetch")
    _fetch(todo, _curve_events, CURVES, len(meta["sample"]))

    gdone = set()
    if os.path.exists(SWAPS):
        gdone = {json.loads(x)["token"] for x in open(SWAPS, encoding="utf-8") if x.strip()}
    study_grads = [(t, g) for t, g in meta["grads"].items()
                   if t in {by_curve[c]["token"] for c in meta["sample"]} and t not in gdone]
    print(f"V4 after listing: {len(gdone)} cached, {len(study_grads)} to fetch")
    _fetch(study_grads, _v4_swaps, SWAPS, len(study_grads) + len(gdone))


def _fetch(items, fn, path, total):
    if not items:
        return
    n, fails = 0, 0
    with ThreadPoolExecutor(WORKERS) as pool, open(path, "a", encoding="utf-8") as out:
        futs = [pool.submit(fn, it) for it in items]
        for f in as_completed(futs):
            try:
                out.write(json.dumps(f.result()) + "\n")
                out.flush()
                n += 1
            except Exception:
                fails += 1           # not recorded: a rerun retries it (AD-033)
            if (n + fails) % 25 == 0:
                print(f"   {n + total - len(items):,}/{total:,}   (failed {fails})", flush=True)
    print(f"   done: {n} fetched, {fails} failed and will be retried on the next run")


def _curve_events(l):
    """Every buy and sell on one curve for its first 24h."""
    ev = cb.get_logs(l["curve"], l["block"], l["block"] + DAY + 700)
    out = []
    for e in sorted(ev, key=lambda e: (int(e["blockNumber"], 16), int(e["logIndex"], 16))):
        k = e["topics"][0][:10]
        if k not in (cb.BUY, cb.SELL):
            continue
        w = cb.words(e["data"])[:4]
        who = a40(e["topics"][2] if k == cb.BUY else e["topics"][1])   # buyer / seller
        out.append(["B" if k == cb.BUY else "S", int(e["blockNumber"], 16), *w, who,
                    e["transactionHash"] == l["tx"]])
    return {"curve": l["curve"], "ev": out}


def _v4_swaps(item):
    tok, g = item
    r = _swaps_for_pool(g["pool"], g["block"], g["block"] + DAY)
    # data words: amount0, amount1, sqrtPriceX96, liquidity, tick, fee
    pts = [[int(x["blockNumber"], 16), int(x["data"][2 + 128:2 + 192], 16)] for x in r]
    return {"token": tok, "pts": sorted(pts)}


def _swaps_for_pool(pool, lo, hi):
    q = {"fromBlock": hex(lo), "toBlock": hex(hi), "address": vb.V4_PM,
         "topics": [V4_SWAP, pool]}
    res = lu._rpc("eth_getLogs", [q])
    if "error" in res:
        if hi - lo < 2_000:
            raise RuntimeError(res["error"].get("message"))
        mid = (lo + hi) // 2
        return _swaps_for_pool(pool, lo, mid) + _swaps_for_pool(pool, mid + 1, hi)
    return res.get("result") or []


# --- measure -------------------------------------------------------------------

def price_path(rec):
    return cb.path({"trades": [[cb.BUY if e[0] == "B" else cb.SELL, e[1], e[2:5]]
                               for e in rec["ev"]]})


def features(rec, launch, history):
    """Only what was visible in the first ENTRY_S seconds after launch."""
    lb, cut = launch["block"], launch["block"] + int(ENTRY_S / BLOCK_S)
    ev = [e for e in rec["ev"] if e[1] <= cut]
    p0, pts = price_path({"ev": ev})
    r0 = (p0 or 0) * cb.SUPPLY
    buys = [e for e in ev if e[0] == "B"]
    dev = [e for e in buys if e[7]]
    crowd = [e for e in buys if not e[7] and e[6] != launch["creator"]]
    q = lambda e: (e[2] - e[4]) / 1e18 / r0 if r0 else 0          # buy size, share of reserve
    prior = history.get(launch["creator"], [])
    before = [b for b in prior if lb - 7 * DAY <= b[0] < lb]      # the creator's last 7 days
    return {
        "entry_x": (pts[-1][1] / p0) if (p0 and pts) else 1.0,
        "buys": len(buys), "sells": sum(1 for e in ev if e[0] == "S"),
        "buyers": len({e[6] for e in crowd}),
        "volume": sum(q(e) for e in crowd),
        "biggest_buy": max((q(e) for e in crowd), default=0.0),
        "dev_buy": sum(q(e) for e in dev),
        "dev_sold": int(any(e[0] == "S" and e[6] == launch["creator"] for e in ev)),
        "snipers": sum(1 for e in crowd if e[1] - lb <= 20),        # within ~2s
        "first_buy_s": min(((e[1] - lb) * BLOCK_S for e in crowd), default=ENTRY_S + 1),
        "creator_launches_7d": len(before),
        "creator_grads_7d": sum(1 for b in before if b[1] is not None and b[1] < lb),
        "pair": PAIRS.get(launch["pair"], "other"),
    }


def outcome(rec, launch, grad):
    """Buy at ENTRY_S; sell at graduation, or back to the curve after 24h."""
    lb = launch["block"]
    cut = lb + int(ENTRY_S / BLOCK_S)
    if grad and grad["block"] <= cut:
        return {"kind": "graduated before you could buy", "pnl": None}
    p0, pts = price_path(rec)
    if p0 is None:
        return {"kind": "never traded", "pnl": STAKE * ((1 - CURVE_FEE) ** 2 - 1) - 3 * GAS_TX,
                "x_from_entry": 1.0}
    before = [p for b, p in pts if b <= cut]
    entry = before[-1] if before else p0
    cost = entry / (1 - CURVE_FEE)
    if grad and grad["block"] <= lb + DAY and pts:
        mult = pts[-1][1] * (1 - V4_EXIT_COST) / cost
        kind = "graduated -> sold on V4"
    else:
        held = [p for b, p in pts if cut < b <= lb + DAY]
        mult = (held[-1] if held else entry) * (1 - CURVE_FEE) / cost
        kind = "no graduation -> sold back after 24h"
    peak = max([p for b, p in pts if b > cut] or [entry]) / cost
    return {"kind": kind, "pnl": STAKE * (mult - 1) - 3 * GAS_TX, "mult": mult,
            "x_from_entry": peak}


def load():
    meta = lu._load(META, {})
    curves = {}
    for x in open(CURVES, encoding="utf-8"):
        if x.strip():
            r = json.loads(x)
            curves[r["curve"]] = r
    v4 = {}
    if os.path.exists(SWAPS):
        for x in open(SWAPS, encoding="utf-8"):
            if x.strip():
                r = json.loads(x)
                v4[r["token"]] = r["pts"]
    return meta, curves, v4


def rows():
    meta, curves, v4 = load()
    grads = meta["grads"]
    history = {}
    for l in meta["launches"]:
        g = grads.get(l["token"])
        history.setdefault(l["creator"], []).append((l["block"], g["block"] if g else None))
    day0 = vb.interp(meta["anchors"], meta["lo"])
    out = []
    for l in meta["launches"]:
        rec = curves.get(l["curve"])
        if not rec or not (meta["lo"] <= l["block"] <= meta["hi"]):
            continue
        g = grads.get(l["token"])
        o = outcome(rec, l, g)
        row = {"token": l["token"], "grad": g is not None,
               "day": int((vb.interp(meta["anchors"], l["block"]) - day0) // 86_400),
               **o, **features(rec, l, history)}
        if g and l["token"] in v4:
            row["v4_peak_x"] = v4_peak(g, v4[l["token"]])
            if row.get("x_from_entry"):
                row["x_from_entry"] *= row["v4_peak_x"]      # curve run, then the V4 run
        if g:
            row["grad_min"] = (g["block"] - l["block"]) * BLOCK_S / 60
        out.append(row)
    return meta, out


def v4_peak(g, pts):
    """Highest price after listing, as a multiple of the listing price."""
    def tok_price(sq):
        p = (sq / 2 ** 96) ** 2
        return p if g["tok_is_c0"] else (1 / p if p else 0)
    listing = tok_price(g["sqrtP"])
    if not listing or not pts:
        return 1.0
    return max(tok_price(sq) for _, sq in pts) / listing


# --- report --------------------------------------------------------------------

NUMERIC = ["entry_x", "buys", "sells", "buyers", "volume", "biggest_buy", "dev_buy",
           "snipers", "first_buy_s", "creator_launches_7d", "creator_grads_7d", "dev_sold"]


def weights(meta, rs):
    n_non = sum(1 for r in rs if not r["grad"])
    w_non = meta["n_study_non"] / n_non if n_non else 1.0
    return lambda r: 1.0 if r["grad"] else w_non


def ev_of(rs, w):
    rs = [r for r in rs if r["pnl"] is not None]
    tw = sum(w(r) for r in rs)
    return (sum(w(r) * r["pnl"] for r in rs) / tw if tw else 0.0), tw


def boot(rs, w, reps=2_000):
    """Stratified: resample the graduates and the sampled non-graduates separately."""
    rng = random.Random(4663)
    g = [r for r in rs if r["grad"] and r["pnl"] is not None]
    n = [r for r in rs if not r["grad"] and r["pnl"] is not None]
    m = []
    for _ in range(reps):
        s = [rng.choice(g) for _ in g] + [rng.choice(n) for _ in n]
        m.append(ev_of(s, w)[0])
    m.sort()
    return m[int(.025 * reps)], m[int(.975 * reps)], sum(1 for x in m if x > 0) / reps


def rules(train):
    out = []
    for f in NUMERIC:
        vals = sorted(r[f] for r in train)
        if len(set(vals)) < 2:
            continue
        qs = sorted({vals[int(len(vals) * q / 10)] for q in range(1, 10)})
        for t in qs:
            out.append((f"{f} >= {t:.4g}", lambda r, f=f, t=t: r[f] >= t))
            out.append((f"{f} <= {t:.4g}", lambda r, f=f, t=t: r[f] <= t))
    for p in ("ETH", "SGOV", "USDG"):
        out.append((f"pair == {p}", lambda r, p=p: r["pair"] == p))
    return out


def report():
    meta, rs = rows()
    w = weights(meta, rs)
    days = 7
    grads = [r for r in rs if r["grad"]]
    print("\n" + "=" * 78)
    print(f"PONS LAUNCHES 1-8 DAYS OLD: {meta['n_study']:,} "
          f"({meta['n_study'] / days:,.0f}/day)")
    print("=" * 78)
    ng = len(grads)
    print(f"  graduated to Uniswap V4 (the ~12x coins)   {ng:>6}  "
          f"({ng / meta['n_study']:.2%} of launches, {ng / days:.0f}/day)")
    early = [r for r in grads if r["kind"].startswith("graduated before")]
    print(f"  ...graduated before a 60s buy was possible {len(early):>6}  "
          f"({len(early) / max(ng, 1):.0%} of them)")
    gm = sorted(r["grad_min"] for r in grads)
    if gm:
        print(f"  time to graduate: median {st.median(gm):.1f} min, "
              f"within 1 min {sum(1 for x in gm if x <= 1) / len(gm):.0%}, "
              f"within 10 min {sum(1 for x in gm if x <= 10) / len(gm):.0%}")
    pk = [r["v4_peak_x"] for r in grads if "v4_peak_x" in r]
    if pk:
        print(f"  after listing, peak within 24h: median {st.median(pk):.2f}x the listing "
              f"price;  >=2x {sum(1 for x in pk if x >= 2)},  >=5x "
              f"{sum(1 for x in pk if x >= 5)},  >=10x {sum(1 for x in pk if x >= 10)}  "
              f"(of {len(pk)}; peaks, not exits)")

    buyable = [r for r in rs if r["pnl"] is not None]
    x10 = [r for r in buyable if r.get("x_from_entry", 0) >= 10]
    wx10 = sum(w(r) for r in x10)
    print(f"  did 10x FROM THE 60s PRICE at some point      ~{wx10:,.0f}  "
          f"(~{wx10 / days:.1f}/day, estimated from the sample)")

    print("\n" + "=" * 78)
    print("FIRST 60 SECONDS: coins that went on to graduate vs coins that did not")
    print("=" * 78)
    late = [r for r in buyable if r["grad"]]
    dead = [r for r in buyable if not r["grad"]]
    print(f"  {'feature':<22}{'graduated (n=' + str(len(late)) + ')':>22}"
          f"{'did not (sample ' + str(len(dead)) + ')':>26}")
    for f in NUMERIC:
        a = st.median(r[f] for r in late) if late else 0
        b = st.median(r[f] for r in dead) if dead else 0
        print(f"  {f:<22}{a:>22.4g}{b:>26.4g}")

    print("\n" + "=" * 78)
    print(f"TRADING IT: buy at {ENTRY_S}s, sell at graduation or after 24h (${STAKE:.0f} a trade)")
    print("=" * 78)
    train = [r for r in buyable if r["day"] < 4]
    test = [r for r in buyable if r["day"] >= 4]
    for label, sub in (("buy every launch", buyable),
                       ("hindsight: only the ones that graduate", late)):
        e, n = ev_of(sub, w)
        print(f"  {label:<44} EV ${e:+.3f}/trade  over ~{n:,.0f} trades")

    cands = []
    for name, fn in rules(train):
        sel = [r for r in train if fn(r)]
        e, n = ev_of(sel, w)
        if n / 4 >= 3:                       # at least ~3 real trades a day
            cands.append((e, name, fn, n))
    cands.sort(key=lambda c: -c[0])
    top = cands[:12]
    pairs = []
    for i, (e1, n1, f1, _) in enumerate(top):
        for e2, n2, f2, _ in top[i + 1:]:
            fn = (lambda r, a=f1, b=f2: a(r) and b(r))
            e, n = ev_of([r for r in train if fn(r)], w)
            if n / 4 >= 3:
                pairs.append((e, f"{n1}  AND  {n2}", fn, n))
    best = sorted(cands + pairs, key=lambda c: -c[0])[:5]
    print(f"\n  searched {len(cands) + len(pairs)} rules on days 1-4; best five, then the")
    print("  same rules on days 5-7, which the search never saw:")
    for e, name, fn, n in best:
        te = [r for r in test if fn(r)]
        e2, n2 = ev_of(te, w)
        lo, hi, pp = boot(te, w) if len(te) >= 10 else (float("nan"),) * 3
        print(f"   {name}")
        print(f"      days 1-4: EV ${e:+.3f}/trade (~{n / 4:.0f}/day)   "
              f"days 5-7: EV ${e2:+.3f}/trade (~{n2 / 3:.0f}/day)  "
              f"95% CI [${lo:+.3f}, ${hi:+.3f}]  P(profit) {pp:.0%}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    if not ap.parse_args().report:
        collect()
    return report()


if __name__ == "__main__":
    sys.exit(main())
