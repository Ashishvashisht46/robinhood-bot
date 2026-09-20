#!/usr/bin/env python3
"""
The negative class: launches the channel did NOT call.

Every question about "reverse engineer their filter" has been unanswerable for
one reason -- we only ever see the ~0.6% of launches that became a call. You
cannot learn a threshold from the positive class alone. This collects the other
99.4% from factory events, so the two can finally be compared.

    python launch_universe.py --collect --hours 72
    python launch_universe.py --features --sample 200
    python launch_universe.py --compare

Pacing matters: the official RPC rate-limits hard and takes ~60s to forgive, so
every call goes through _rpc() which backs off and the work is resumable. Safe
to run while the bot trades -- it is read-only and on a different endpoint from
the bot's QuickNode.
"""
import argparse
import json
import os
import random
import statistics as st
import sys
import time
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHES = os.path.join(HERE, "cache", "launches.json")
FEATURES = os.path.join(HERE, "cache", "launch_features.json")
META = os.path.join(HERE, "cache", "channel_meta.json")

RPC = "https://rpc.mainnet.chain.robinhood.com"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
BLOCK_S = 0.10                       # measured chain block time

TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# Measured launch rates over a 2h window, which is how we know V2/V3 alone
# would have been a 2% sample and a badly biased negative class:
#     Pons V2   321/h     Uniswap V4  288/h     V2+V3  16/h
# (name, contract, topic0, which topic slots can hold the new token)
SOURCES = (
    ("pons_v2", "0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e",
     "0x8d4aad4953d0ca700d468f3753aa14432d1b35b43ec6409f051fb6aa43a89607", (1,)),
    ("uni_v4", "0x8366a39CC670B4001A1121B8F6A443A643e40951",
     "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438", (2, 3)),
    ("uni_v2", "0x8bcEaA40B9AcdfAedF85AdF4FF01F5Ad6517937f",
     "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9", (1, 2)),
    ("uni_v3", "0x1f7d7550B1b028f7571E69A784071F0205FD2EfA",
     "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118", (1, 2)),
)

CHUNK = 250_000                      # ~7h; ~3.6k events/source, under the 10k log cap
FEATURE_AGE_MIN = 4                  # how old a token is when we judge it
PACE = 1.2                           # seconds between calls, politeness


def _rpc(method, params, timeout=120):
    """One RPC call with 429 backoff. The endpoint forgives after ~60s."""
    delay = 5
    for attempt in range(6):
        try:
            req = urllib.request.Request(
                RPC, json.dumps({"jsonrpc": "2.0", "id": 1,
                                 "method": method, "params": params}).encode(),
                {"Content-Type": "application/json", "User-Agent": UA})
            out = json.load(urllib.request.urlopen(req, timeout=timeout))
            time.sleep(PACE)
            return out
        except urllib.error.HTTPError as exc:
            if exc.code != 429 or attempt == 5:
                raise
            time.sleep(delay)
            delay = min(90, delay * 2)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            if attempt == 5:
                raise
            time.sleep(delay)
            delay = min(90, delay * 2)
    raise RuntimeError("rpc exhausted")


def head():
    return int(_rpc("eth_blockNumber", [])["result"], 16)


def block_ts(bn):
    r = _rpc("eth_getBlockByNumber", [hex(bn), False]).get("result")
    return int(r["timestamp"], 16) if r else None


def block_at_time(target_ts, hi=None, hi_ts=None):
    """The block at a timestamp, by binary search.

    Do NOT extrapolate from a nominal block time. Measured over 3 days the
    0.10s figure drifts 17,394 blocks (~29 min) -- enough to look outside any
    sane search window and report a token as 'not launched here'. That is how
    a correct dataset gets thrown away.
    """
    if hi is None:
        hi = head()
    if hi_ts is None:
        hi_ts = block_ts(hi)
    lo = max(1, hi - int((hi_ts - target_ts) / BLOCK_S * 1.5) - 100_000)
    while lo < hi:
        mid = (lo + hi) // 2
        t = block_ts(mid)
        if t is None:
            break
        if t < target_ts:
            lo = mid + 1
        else:
            hi = mid
    return hi


def _load(path, default):
    try:
        return json.load(open(path, encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _save(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    json.dump(obj, open(tmp, "w", encoding="utf-8"))
    os.replace(tmp, path)


def collect(hours):
    """Factory events -> {token: {pool, block}}. Resumable across runs."""
    store = _load(LAUNCHES, {"tokens": {}, "scanned": []})
    hi = head()
    lo = hi - int(hours * 3600 / BLOCK_S)
    print(f"head {hi:,}   scanning back {hours}h to {lo:,}")

    done = {tuple(x) for x in store["scanned"]}
    for start in range(lo, hi, CHUNK):
        end = min(start + CHUNK - 1, hi)
        if any(a <= start and end <= b for a, b in done):
            continue
        found = 0
        for name, addr, topic, slots in SOURCES:
            try:
                r = _rpc("eth_getLogs", [{"fromBlock": hex(start), "toBlock": hex(end),
                                          "address": addr, "topics": [topic]}])
            except Exception as exc:
                print(f"  {start:,}-{end:,} {name} failed: {exc}")
                continue
            if "error" in r:
                print(f"  {start:,}-{end:,} {name}: {r['error'].get('message')}")
                continue
            for log in r.get("result") or []:
                t = log.get("topics", [])
                bn = int(log["blockNumber"], 16)
                for i in slots:
                    if i >= len(t):
                        continue
                    tok = "0x" + t[i][-40:]
                    prev = store["tokens"].get(tok)
                    if prev is None:
                        store["tokens"][tok] = {"block": bn, "pools": 1, "venue": name}
                    else:
                        prev["pools"] = prev.get("pools", 1) + 1
                        prev["block"] = min(prev["block"], bn)
                found += 1
        store["scanned"].append([start, end])
        _save(LAUNCHES, store)
        print(f"  {start:,}-{end:,}  +{found} pools   total tokens {len(store['tokens']):,}",
              flush=True)

    real = launches_only(store)
    print(f"\ncollected {len(store['tokens']):,} addresses, "
          f"{len(real):,} look like launches (rest pair with everything = quotes)")
    return store


MAX_POOLS_FOR_A_LAUNCH = 8      # WETH/USDG sit in hundreds; a memecoin in 1-3


def launches_only(store):
    """Drop quote tokens. Nothing to hardcode -- they identify themselves by
    appearing on one side of hundreds of pairs."""
    return {t: r for t, r in store["tokens"].items()
            if r.get("pools", 1) <= MAX_POOLS_FOR_A_LAUNCH}


def early_features(token, launch_block, minutes=FEATURE_AGE_MIN):
    """What is knowable about a token `minutes` after it launched."""
    span = int(minutes * 60 / BLOCK_S)
    try:
        r = _rpc("eth_getLogs", [{"fromBlock": hex(launch_block),
                                  "toBlock": hex(launch_block + span),
                                  "address": token, "topics": [TRANSFER]}])
    except Exception:
        return None
    if "error" in r:
        return None
    logs = r.get("result") or []
    if not logs:
        return {"transfers": 0, "holders": 0, "senders": 0,
                "first_trade_blocks": None, "blocks_active": 0}
    recips, senders, blocks = set(), set(), set()
    for log in logs:
        t = log.get("topics", [])
        if len(t) < 3:
            continue
        senders.add(t[1][-40:])
        recips.add(t[2][-40:])
        blocks.add(int(log["blockNumber"], 16))
    first = min(blocks) - launch_block if blocks else None
    return {"transfers": len(logs), "holders": len(recips), "senders": len(senders),
            "first_trade_blocks": first, "blocks_active": len(blocks)}


def called_tokens():
    """The positive class, with the launch time the channel itself implied."""
    meta = _load(META, {})
    out = {}
    for ca, m in meta.items():
        ts, age = m.get("ts"), m.get("token_age_minutes")
        if ts and age is not None:
            out[ca.lower()] = {"call_ts": ts, "launch_ts": ts - age * 60,
                               "ticker": m.get("ticker")}
    return out


def outcomes(min_transfers):
    """The question that actually matters.

    'Will the channel call it' was only ever a proxy, and a bad one: called
    tokens have no profitable exit (AD-023/024). So predicting calls better
    predicts badness better. This prices the tokens an early-activity filter
    would ACTUALLY buy -- called or not -- entered at FEATURE_AGE_MIN, which is
    when the filter could fire.
    """
    from analyse_channel import best_pool, candles, evaluate, load_cache, save_cache

    feats = _load(FEATURES, {})
    store = _load(LAUNCHES, {"tokens": {}})
    called = called_tokens()
    picks = [t for t, f in feats.items()
             if f.get("transfers", 0) >= min_transfers and t in store["tokens"]]
    print(f"{len(picks)} launches pass >={min_transfers} transfers in the first "
          f"{FEATURE_AGE_MIN} min")
    if not picks:
        return 1

    cache = load_cache()
    rows = []
    try:
        for i, tok in enumerate(picks, 1):
            bn = store["tokens"][tok]["block"] + int(FEATURE_AGE_MIN * 60 / BLOCK_S)
            ts = block_ts(bn)
            if not ts:
                continue
            pool = best_pool(tok, cache)
            if not pool:
                continue
            ev = evaluate(candles(pool, ts + 360 * 60 + 120, cache), ts)
            if ev:
                ev["called"] = tok in called
                rows.append(ev)
            print(f"  [{i}/{len(picks)}] priced {len(rows)}", end="\r", flush=True)
    finally:
        save_cache(cache)
    print(" " * 50, end="\r")

    def show(label, rs):
        if not rs:
            print(f"  {label:<26} no data")
            return
        peaks = sorted(r["peak"] for r in rs)
        tr = sorted(r["trough"] for r in rs)
        n = len(peaks)
        print(f"  {label:<26} n={n:>4}  med peak {st.median(peaks):>5.2f}x  "
              f"2x {sum(p >= 2 for p in peaks)/n:>4.0%}  "
              f"-50% {sum(t <= 0.5 for t in tr)/n:>4.0%}  "
              f"+20% first {sum(r['outcome'].startswith('tp') for r in rs)/n:>4.0%}")

    print(f"\nEntered at {FEATURE_AGE_MIN} min old, filter = >={min_transfers} transfers:")
    show("ALL that pass the filter", rows)
    show("  of those, CALLED later", [r for r in rows if r["called"]])
    show("  of those, never called", [r for r in rows if not r["called"]])
    print("\n  Channel calls, same code (AD-024):"
          "  n= 213  med peak  1.33x  2x  21%  -50%  73%  +20% first  58%")
    print("\nThis filter is only worth building if its line beats that one.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--collect", action="store_true")
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--features", action="store_true")
    ap.add_argument("--sample", type=int, default=150)
    ap.add_argument("--compare", action="store_true")
    ap.add_argument("--outcomes", action="store_true",
                    help="price high-activity launches: do they actually go UP?")
    ap.add_argument("--min-transfers", type=int, default=400)
    args = ap.parse_args()

    if args.outcomes:
        return outcomes(args.min_transfers)

    if args.collect:
        collect(args.hours)
        return 0

    if args.features:
        store = _load(LAUNCHES, {"tokens": {}})
        if not store["tokens"]:
            print("run --collect first")
            return 1
        real = launches_only(store)
        called = called_tokens()
        pos = [t for t in real if t in called]
        if not pos:
            print("no called tokens in the collected window")
            return 1
        # Positives span ~51h of a 135h collection. Sampling negatives from the
        # whole window would compare two different periods of the chain, so the
        # negatives are drawn from the SAME block range as the positives.
        lo = min(real[t]["block"] for t in pos)
        hi = max(real[t]["block"] for t in pos)
        neg = [t for t in real if t not in called and lo <= real[t]["block"] <= hi]
        print(f"{len(real):,} launches; {len(pos)} called, "
              f"{len(neg):,} not-called inside the same block range "
              f"{lo:,}-{hi:,}")
        feats = _load(FEATURES, {})
        random.seed(4663)                       # reproducible sample
        random.shuffle(neg)
        pool = [t for t in pos + neg[:args.sample] if t not in feats]
        print(f"computing early features for {len(pool)} launches "
              f"({FEATURE_AGE_MIN} min old)\n")
        for i, tok in enumerate(pool, 1):
            f = early_features(tok, real[tok]["block"])
            if f is not None:
                feats[tok] = f
                _save(FEATURES, feats)
            print(f"  [{i}/{len(pool)}] {tok} "
                  f"{'holders %d, tx %d' % (f['holders'], f['transfers']) if f else 'failed'}",
                  flush=True)
        print(f"\n{len(feats)} launches with features -> {FEATURES}")
        return 0

    if args.compare:
        feats = _load(FEATURES, {})
        called = called_tokens()
        if not feats:
            print("run --features first")
            return 1
        pos = {t: f for t, f in feats.items() if t in called}
        neg = {t: f for t, f in feats.items() if t not in called}
        print(f"launches with features : {len(feats)}")
        print(f"  of which CALLED      : {len(pos)}")
        print(f"  never called         : {len(neg)}\n")
        if len(pos) < 10:
            print("Too few called tokens overlap the collected window.")
            print("Collect a window that covers the calls, or collect forward")
            print("from now and compare against live calls.")
            return 1

        store = _load(LAUNCHES, {"tokens": {}})
        blocks = {t: store["tokens"][t]["block"] for t in feats
                  if t in store["tokens"]}
        random.seed(4663)
        for f in feats.values():
            f["_random"] = random.random()       # the control, same as AD-024

        keys = ("holders", "transfers", "senders", "blocks_active",
                "first_trade_blocks", "_random")

        def auc(items_pos, items_neg, key):
            """P(a random called token scores above a random uncalled one).
            0.50 is a coin flip; the random control shows what noise looks like."""
            a = [f[key] for f in items_pos if f.get(key) is not None]
            b = [f[key] for f in items_neg if f.get(key) is not None]
            if not a or not b:
                return None
            wins = ties = 0
            for x in a:
                for y in b:
                    if x > y:
                        wins += 1
                    elif x == y:
                        ties += 1
            return (wins + 0.5 * ties) / (len(a) * len(b))

        print(f"{'feature':<20} {'ALL':>7} {'train':>7} {'holdout':>8}   median called / not")
        print("-" * 74)
        mid = st.median(blocks.values()) if blocks else 0
        tr_p = [f for t, f in pos.items() if blocks.get(t, 0) <= mid]
        tr_n = [f for t, f in neg.items() if blocks.get(t, 0) <= mid]
        ho_p = [f for t, f in pos.items() if blocks.get(t, 0) > mid]
        ho_n = [f for t, f in neg.items() if blocks.get(t, 0) > mid]
        for key in keys:
            a = auc(list(pos.values()), list(neg.values()), key)
            t_ = auc(tr_p, tr_n, key) if tr_p and tr_n else None
            h_ = auc(ho_p, ho_n, key) if ho_p and ho_n else None
            p = [f[key] for f in pos.values() if f.get(key) is not None]
            n = [f[key] for f in neg.values() if f.get(key) is not None]
            fmt = lambda v: f"{v:>7.2f}" if v is not None else "      -"
            med = (f"{st.median(p):>8.0f} / {st.median(n):<8.0f}"
                   if p and n and key != "_random" else "")
            mark = "  <- CONTROL" if key == "_random" else ""
            print(f"{key:<20}{fmt(a)}{fmt(t_)}{fmt(h_) :>9}   {med}{mark}")

        print(f"\ntrain n={len(tr_p)}+{len(tr_n)}   holdout n={len(ho_p)}+{len(ho_n)}")
        print("A feature is only real if its holdout AUC stays well clear of the")
        print("control AND of 0.50. Train-only lift is what overfitting looks like.")
        return 0

    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
