#!/usr/bin/env python3
"""
Recover full addresses for the scout wallets the channel names only in
truncated form (`0x2a4d...21d7`).

The channel posts 4+4 hex characters. That is 32 bits, so a prefix+suffix match
against one token's few hundred recipients is effectively unique -- a false
match needs ~4.3e9 candidates. Validated 7/7 on NEUR0 with one match each.

    ticker -> pool (GeckoTerminal) -> token CA -> Transfer logs -> recipients
                                                                  -> match 4+4

Ticker resolution is ambiguous on this chain (20 pools are called "GROKBOOK"),
so candidates are not guessed: each one is tried and scored by how many of the
wallets the channel SAYS bought that token actually appear in its recipients.
The wrong token scores ~0. That makes resolution self-verifying.

    python recover_wallets.py --self-test     # no network beyond one token
    python recover_wallets.py --min-calls 3   # pin every wallet with >=3 calls
    python recover_wallets.py --all

Writes cache/scout_wallets.json. Read-only against the chain; sends nothing.
"""
import argparse
import collections
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "cache", "scout_wallets.json")
# Lives in cache/ because that is gitignored: the list itself is not ours to
# publish, and a source file should not carry anyone's desktop path.
CSV_DEFAULT = os.path.join(HERE, "cache", "scout_channel_only_wallets.csv")

# The official endpoint is the only one that serves eth_getLogs on chain 4663
# (AD-028). It also 403s a bare urllib User-Agent, hence the browser UA.
RPC = "https://rpc.mainnet.chain.robinhood.com"
GT = "https://api.geckoterminal.com/api/v2"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

LOOKBACK = 2_000_000          # ~55h at the chain's 0.10s block time
MAX_CANDIDATES = 8            # per ticker, best-scoring wins
GT_SLEEP = 4.0                # GeckoTerminal rate limit is ~30/min


def _post(payload, timeout=90):
    req = urllib.request.Request(
        RPC, json.dumps(payload).encode(),
        {"Content-Type": "application/json", "User-Agent": UA})
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def rpc(method, params):
    return _post({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})


_GT_CACHE = os.path.join(HERE, "cache", "gt_search.json")


def _gt_cache():
    try:
        return json.load(open(_GT_CACHE, encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def gt(path):
    """GeckoTerminal, cached on disk. Its 429 is aggressive and a rerun of this
    script must not re-pay for searches it already did."""
    cache = _gt_cache()
    if path in cache:
        return cache[path]
    req = urllib.request.Request(GT + path, headers={"User-Agent": UA,
                                                     "Accept": "application/json"})
    delay = GT_SLEEP
    for attempt in range(6):
        try:
            data = json.load(urllib.request.urlopen(req, timeout=30))
            cache[path] = data
            json.dump(cache, open(_GT_CACHE, "w", encoding="utf-8"))
            return data
        except urllib.error.HTTPError as exc:
            if exc.code != 429 or attempt == 5:
                raise
            time.sleep(delay)
            delay *= 2
    raise RuntimeError("unreachable")


def recipients(ca, head, span=LOOKBACK):
    """Distinct addresses that RECEIVED this token. Halves the span on refusal."""
    while span >= 25_000:
        try:
            r = rpc("eth_getLogs", [{"fromBlock": hex(max(0, head - span)),
                                     "toBlock": hex(head), "address": ca,
                                     "topics": [TRANSFER]}])
            if "error" not in r:
                logs = r.get("result") or []
                return {("0x" + l["topics"][2][-40:]).lower()
                        for l in logs if len(l.get("topics", [])) > 2}, len(logs)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            pass
        span //= 2
    return set(), 0


def load_rows(path):
    rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))
    for r in rows:
        r["_tokens"] = {t.strip().upper()
                        for t in r["Tokens_Called_In_Channel"].split(",")
                        if t.strip() and t.strip().upper() != "UNKNOWN"}
        r["_calls"] = int(r["Calls_Triggered"])
    return rows


def cover(rows, min_calls):
    """Greedy set cover: fewest tickers that touch every wallet worth scoring."""
    remaining = {r["Address_From_Channel"]: r["_tokens"]
                 for r in rows if r["_calls"] >= min_calls and r["_tokens"]}
    order = []
    while remaining:
        cnt = collections.Counter(t for ts in remaining.values() for t in ts)
        if not cnt:
            break
        best = cnt.most_common(1)[0][0]
        order.append(best)
        remaining = {w: ts for w, ts in remaining.items() if best not in ts}
    return order


def candidates(ticker):
    """Distinct base-token addresses for a ticker, most liquid first."""
    try:
        data = gt(f"/search/pools?query={ticker}&network=robinhood").get("data", [])
    except Exception as exc:
        print(f"    search failed: {exc}")
        return []
    seen = {}
    for pool in data:
        attrs = pool.get("attributes", {})
        name = (attrs.get("name") or "").upper()
        # "GROKBOOK / WETH" -- only accept the ticker as the BASE side
        if name.split("/")[0].strip().split()[0:1] != [ticker]:
            continue
        bt = (pool.get("relationships", {}).get("base_token", {})
                  .get("data", {}).get("id") or "")
        ca = bt.split("_", 1)[1].lower() if "_" in bt else ""
        if not ca:
            continue
        liq = float(attrs.get("reserve_in_usd") or 0)
        if ca not in seen or liq > seen[ca]:
            seen[ca] = liq
    return sorted(seen, key=lambda c: -seen[c])[:MAX_CANDIDATES]


def match(truncated, pool_addrs):
    pre, _, suf = truncated.lower().partition("...")
    return [a for a in pool_addrs if a.startswith(pre) and a.endswith(suf)]


def resolve(ticker, expect, head):
    """Pick the CA whose recipients actually contain the expected wallets."""
    best = (0, None, {}, 0)
    for ca in candidates(ticker):
        addrs, n_logs = recipients(ca, head)
        if not addrs:
            continue
        found = {}
        for w in expect:
            m = match(w, addrs)
            if len(m) == 1:
                found[w] = m[0]
        if len(found) > best[0]:
            best = (len(found), ca, found, n_logs)
        if len(found) == len(expect):       # perfect, stop paying for candidates
            break
    return best


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=CSV_DEFAULT)
    ap.add_argument("--min-calls", type=int, default=3)
    ap.add_argument("--all", action="store_true", help="every ticker, not a cover")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    rows = load_rows(args.csv)
    by_ticker = collections.defaultdict(list)
    for r in rows:
        for t in r["_tokens"]:
            by_ticker[t].append(r["Address_From_Channel"])

    tickers = (sorted(by_ticker, key=lambda t: -len(by_ticker[t]))
               if args.all else cover(rows, args.min_calls))
    head = int(rpc("eth_blockNumber", [])["result"], 16)
    print(f"head block {head:,}   tickers to resolve: {len(tickers)}\n")

    found, resolved = {}, {}
    for i, tk in enumerate(tickers, 1):
        expect = by_ticker[tk]
        n, ca, hits, n_logs = resolve(tk, expect, head)
        flag = "OK " if n else "MISS"
        print(f"  [{i:>2}/{len(tickers)}] {flag} {tk:<14} {n:>3}/{len(expect):<3} "
              f"wallets  {ca or '-'}  ({n_logs} logs)")
        if ca:
            resolved[tk] = ca
        found.update(hits)
        time.sleep(GT_SLEEP)

    scored = {r["Address_From_Channel"]: r for r in rows}
    out = {
        "recovered": found,
        "tickers": resolved,
        "head_block": head,
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(out, open(OUT, "w", encoding="utf-8"), indent=1)

    top = [w for w in found if scored[w]["_calls"] >= args.min_calls]
    want = sum(1 for r in rows if r["_calls"] >= args.min_calls)
    print(f"\nrecovered {len(found)} addresses total")
    print(f"  of the >={args.min_calls}-call group: {len(top)}/{want}")
    print(f"  written to {OUT}")
    return 0


def self_test() -> int:
    """NEUR0 is the fixture: 7 wallets named, all 7 must resolve uniquely."""
    fails = []

    def check(name, got, want):
        ok = got == want
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
        if not ok:
            print(f"          got {got!r} want {want!r}")
            fails.append(name)

    print("matching is exact and unique:")
    pool = ["0x2a4d34cd09a36f59ae3bedc0880cd5da929321d7",
            "0x2a4d000000000000000000000000000000ff21d7",   # same 4+4, different body
            "0xdeadbeef00000000000000000000000000000000"]
    check("prefix+suffix collision is DETECTED, not silently taken",
          len(match("0x2a4d...21d7", pool)), 2)
    check("a non-matching address is excluded", match("0xbeef...9999", pool), [])
    check("prefix right but suffix wrong is NOT a match",
          match("0xdead...1234", pool), [])
    check("case is normalised", match("0X2A4D...21D7", pool[:1]),
          ["0x2a4d34cd09a36f59ae3bedc0880cd5da929321d7"])

    print("\nlive fixture -- NEUR0 names 7 wallets:")
    head = int(rpc("eth_blockNumber", [])["result"], 16)
    addrs, n_logs = recipients("0x05a7a8ab2996ca400738bb4c751ebe176a651fe2", head)
    check("NEUR0 returns transfer logs", n_logs > 100, True)
    expect = ["0x2a4d...21d7", "0xe7db...e602", "0x173f...27da", "0xd94a...0296",
              "0x820c...3514", "0x6e57...9bed", "0x14a8...05e6"]
    hits = {w: match(w, addrs) for w in expect}
    check("all 7 recovered", sum(1 for m in hits.values() if len(m) == 1), 7)
    check("none ambiguous", [w for w, m in hits.items() if len(m) > 1], [])

    print()
    if fails:
        print(f"FAILED: {', '.join(fails)}")
        return 1
    print("All recovery checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
