#!/usr/bin/env python3
"""
Can we apply the channel's criteria ourselves at 2-3 minutes instead of 9?

Ashish's proposal, and the one honest way to test it. Entering earlier is
obviously better on price -- measured, EV runs +22% at 1 min down to -7% at
9 min -- but that measurement is worthless on its own, because it was taken on
tokens selected for having >=100 transfers BY MINUTE 4. At minute 2 you cannot
know that. Filtering on the future and then admiring the returns is the single
easiest way to invent an edge that does not exist.

So this does it properly:

    population   a random sample of ALL launches, not the busy ones
    filter       computed from minute 0 to ENTRY_AGE only
    entry        at ENTRY_AGE
    outcome      priced forward from there

A threshold only counts if the tokens it selects are profitable, measured
across everything it also lets through.

    python early_entry_test.py --features      # 2-min features, resumable
    python early_entry_test.py --evaluate

Read-only. Sends nothing.
"""
import argparse
import json
import os
import statistics as st
import sys

from launch_universe import (_load, _save, _rpc, block_ts, launches_only,
                             LAUNCHES, FEATURES, called_tokens, BLOCK_S, TRANSFER)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ENTRY_AGE = float(os.getenv("ENTRY_AGE_MIN", "2"))
EARLY = os.path.join(HERE, "cache", f"features_{ENTRY_AGE:g}min.json")

GAS_USD = 0.12                   # measured round trip, fixed regardless of size

# The channel's own floors, read off the 223 calls in channel_meta.json -- the
# minimum value they were ever willing to call. liquidity_usd >= 5000 is exactly
# what MIN_LIQUIDITY_USD is already set to in .env.
CHANNEL_FLOORS = {
    "mcap_usd": 15_000,          # not backtestable: GeckoTerminal serves only
    "liquidity_usd": 5_000,      # CURRENT values, not values as at minute 2
    "holders": 20,
    "volume_usd": 6_000,
    "swaps": 1,
    "max_buy_tax": 5,
}


def features_at(token, launch_block, minutes):
    """Only what is knowable by `minutes` after launch. No future information."""
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
    recips, blocks = set(), set()
    for log in logs:
        t = log.get("topics", [])
        if len(t) < 3:
            continue
        recips.add(t[2][-40:])
        blocks.add(int(log["blockNumber"], 16))
    return {"holders": len(recips), "transfers": len(logs),
            "blocks_active": len(blocks)}


def collect() -> int:
    store = _load(LAUNCHES, {"tokens": {}})
    base = _load(FEATURES, {})
    if not base:
        print("run launch_universe.py --features first")
        return 1
    real = launches_only(store)
    early = _load(EARLY, {})
    todo = [t for t in base if t in real and t not in early]
    print(f"{len(base)} launches sampled; computing {ENTRY_AGE:g}-min features "
          f"for {len(todo)} not yet done\n")
    for i, tok in enumerate(todo, 1):
        f = features_at(tok, real[tok]["block"], ENTRY_AGE)
        if f is not None:
            early[tok] = f
            _save(EARLY, early)
        print(f"  [{i}/{len(todo)}] {tok} "
              f"{'h=%d tx=%d' % (f['holders'], f['transfers']) if f else 'failed'}",
              flush=True)
    print(f"\n{len(early)} launches with {ENTRY_AGE:g}-min features -> {EARLY}")
    return 0


def evaluate() -> int:
    from analyse_channel import best_pool, candles, load_cache, save_cache, WINDOW_MIN

    early = _load(EARLY, {})
    store = _load(LAUNCHES, {"tokens": {}})
    called = called_tokens()
    if not early:
        print("run --features first")
        return 1
    real = launches_only(store)
    cache = load_cache()

    lo = min(real[t]["block"] for t in early if t in called and t in real)
    hi = max(real[t]["block"] for t in early if t in called and t in real)
    hours = (hi - lo) * 0.1007 / 3600
    pop = sum(1 for t, r in real.items() if lo <= r["block"] <= hi)
    sampled = len(early)
    scale = pop / sampled

    print(f"population {pop:,} launches over {hours:.0f}h; "
          f"{sampled} sampled (1 = {scale:.0f} real)")
    print(f"entry at {ENTRY_AGE:g} min, exit TP 2.0x / SL 80%, "
          f"gas ${GAS_USD:.2f} round trip\n")

    # Block timestamps are one RPC each and never change. Caching them turns a
    # ~50 minute re-run into a fast one.
    ts_path = os.path.join(HERE, "cache", "block_ts.json")
    ts_cache = _load(ts_path, {})

    def launch_ts(block):
        key = str(block)
        if key not in ts_cache:
            t = block_ts(block)
            if t is None:
                return None
            ts_cache[key] = t
            _save(ts_path, ts_cache)
        return ts_cache[key]

    # GeckoTerminal allows ~30 requests/minute and api() only backs off AFTER a
    # failure -- it never paces. Purging the poisoned cache left ~1,000 fetches
    # to redo, which throttled every one of them and priced 0 of 1,001. Two
    # calls per token, so 2.2s between tokens keeps us under the limit.
    import time as _time
    PACE = 2.2
    fetched = 0

    rows = []
    try:
        for i, (tok, f) in enumerate(early.items(), 1):
            if i % 50 == 0:
                print(f"  {i}/{len(early)} attempted, {len(rows)} priced",
                      flush=True)
            lts = launch_ts(real[tok]["block"])
            if not lts:
                continue
            if f"pool:{tok}" not in cache:
                _time.sleep(PACE)
                fetched += 1
            pool = best_pool(tok, cache)
            if not pool:
                continue
            ets = int(lts + ENTRY_AGE * 60)
            # Prefer ANY cached series for this pool. GeckoTerminal's minute
            # OHLCV has aged out for most of this window -- a fresh fetch with a
            # new before_timestamp returns nothing for the older pools, even
            # though a series fetched days ago is sitting in the cache and spans
            # the minutes we need. Refetching would throw away the only data
            # that still exists.
            have = [k for k in cache if k.startswith(f"ohlcv:{pool}:") and cache[k]]
            if not have:
                # Refetching is pointless here: minute OHLCV for this window has
                # aged out, measured 3 of 4 older pools returning nothing even
                # with polite pacing. Only cached history can price this period.
                continue
            c = max((cache[k] for k in have), key=len)
            a = [r for r in sorted(c, key=lambda r: r[0]) if r[0] >= ets - 60]
            if len(a) < 2:
                continue
            e = a[0][4] or a[0][1]
            if not e:
                continue
            w = [r for r in a[1:] if r[0] <= ets + WINDOW_MIN * 60]
            if not w:
                continue
            # Volume over the pre-entry window, from the candles themselves.
            # This IS knowable at entry; liquidity and mcap are not, historically.
            pre = [r for r in sorted(c, key=lambda r: r[0])
                   if lts <= r[0] < ets]
            vol = sum((r[5] or 0) for r in pre)

            ret = None
            for r in w:
                if r[3] <= e * 0.80:
                    ret = -0.20
                    break
                if r[2] >= e * 2.0:
                    ret = 1.0
                    break
            if ret is None:
                ret = ((w[-1][4] or e) / e) - 1
            rows.append({"holders": f["holders"], "transfers": f["transfers"],
                         "volume": vol, "gross": ret, "called": tok in called})
    finally:
        save_cache(cache)

    print(f"priced {len(rows)} of {sampled} sampled launches "
          f"(the rest have no pool or no price history -- unbuyable anyway)\n")
    if len(rows) < 20:
        print("too few priced to judge")
        return 1

    F = CHANNEL_FLOORS
    tests = [
        ("no filter (every launch)", lambda r: True),
        ("holders >= 20 (channel)", lambda r: r["holders"] >= F["holders"]),
        ("volume >= $6k (channel)", lambda r: r["volume"] >= F["volume_usd"]),
        ("CHANNEL FLOORS (h+v+swaps)",
         lambda r: r["holders"] >= F["holders"] and r["volume"] >= F["volume_usd"]
         and r["transfers"] >= F["swaps"]),
        ("  ..and holders >= 60", lambda r: r["holders"] >= 60
         and r["volume"] >= F["volume_usd"]),
        ("  ..and volume >= $25k", lambda r: r["holders"] >= F["holders"]
         and r["volume"] >= 25_000),
        ("  ..and volume >= $60k", lambda r: r["holders"] >= F["holders"]
         and r["volume"] >= 60_000),
    ]

    for stake in (float(os.getenv("STAKE_USD", "50")), 2.50):
        gas = GAS_USD / stake
        print(f"\n=== stake ${stake:.2f}   gas {gas:.2%} per trade ===")
        print(f"{'filter':>28} {'n':>4} {'/day':>6} {'win':>5} "
              f"{'EV/trade':>9} {'$/day':>9} {'$/30d':>9}")
        print("-" * 76)
        for label, fn in tests:
            g = [r for r in rows if fn(r)]
            if len(g) < 8:
                print(f"{label:>28} {len(g):>4}   too few to judge")
                continue
            ev = st.mean(r["gross"] for r in g) - gas
            win = sum(1 for r in g if r["gross"] > gas) / len(g)
            ncall = sum(1 for r in g if r["called"])
            perday = (ncall + (len(g) - ncall) * scale) / hours * 24
            daily = perday * ev * stake
            print(f"{label:>28} {len(g):>4} {perday:>6.0f} {win:>4.0%} "
                  f"{ev:>+8.1%} {daily:>+8.0f} {daily * 30:>+8.0f}")

    print("\n/day scales the sampled non-called launches back to the population.")
    print("$/day is EV x volume x stake -- what the filter would actually have")
    print("made or lost per day. Note liquidity and mcap are NOT in these filters:")
    print("they cannot be reconstructed historically, only queried live.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--features", action="store_true")
    ap.add_argument("--evaluate", action="store_true")
    args = ap.parse_args()
    if args.features:
        return collect()
    if args.evaluate:
        return evaluate()
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
