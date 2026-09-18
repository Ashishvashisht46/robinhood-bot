#!/usr/bin/env python3
"""
The denominator the scout list does not have.

The channel names a wallet only when that wallet's buy helped trigger a call.
So the list is selected on the outcome: a wallet that buys 200 tokens a day
appears in it by arithmetic, not by skill. Before pricing anything, count how
many tokens each recovered wallet ACTUALLY bought over the same window.

    calls_triggered / tokens_bought  =  how often this wallet's buy meant anything

If that ratio is tiny, "a watched wallet bought a token" cannot be a buy signal,
because the wallet buys nearly everything.

    python score_wallets.py

Read-only: one eth_getLogs topic filter per block chunk. Sends nothing, and
makes no GeckoTerminal calls, so it is safe to run while the bot trades.
"""
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
WALLETS = os.path.join(HERE, "cache", "scout_wallets.json")
CSV_DEFAULT = os.path.join(HERE, "cache", "scout_channel_only_wallets.csv")

RPC = "https://rpc.mainnet.chain.robinhood.com"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

WINDOW = 1_200_000            # ~33h at 0.10s blocks -- covers the CSV's tokens
CHUNK = 100_000               # one eth_getLogs call per chunk


def rpc(method, params, timeout=120):
    req = urllib.request.Request(
        RPC, json.dumps({"jsonrpc": "2.0", "id": 1,
                         "method": method, "params": params}).encode(),
        {"Content-Type": "application/json", "User-Agent": UA})
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def topic_addr(a):
    return "0x" + "0" * 24 + a[2:].lower()


def incoming(wallets, head, window=WINDOW, chunk=CHUNK):
    """Every token each wallet RECEIVED in the window. One filter, many chunks."""
    topics = [topic_addr(w) for w in wallets]
    got = collections.defaultdict(set)        # wallet -> {token}
    events = collections.Counter()            # wallet -> n transfers
    start = head - window
    for lo in range(start, head, chunk):
        hi = min(lo + chunk - 1, head)
        for attempt in range(4):
            try:
                r = rpc("eth_getLogs", [{"fromBlock": hex(lo), "toBlock": hex(hi),
                                         "topics": [TRANSFER, None, topics]}])
                if "error" in r:
                    print(f"    chunk {lo}-{hi}: {r['error'].get('message')}")
                    break
                for log in r.get("result") or []:
                    t = log.get("topics", [])
                    if len(t) < 3:
                        continue
                    w = "0x" + t[2][-40:]
                    got[w].add((log.get("address") or "").lower())
                    events[w] += 1
                break
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                if attempt == 3:
                    print(f"    chunk {lo}-{hi}: gave up ({exc})")
                time.sleep(2 * (attempt + 1))
        print(f"  scanned {hi - start:>9,}/{window:,} blocks", end="\r")
    print(" " * 60, end="\r")
    return got, events


def main() -> int:
    if not os.path.exists(WALLETS):
        print("run recover_wallets.py first")
        return 1
    rec = json.load(open(WALLETS, encoding="utf-8"))["recovered"]
    rows = {r["Address_From_Channel"]: r
            for r in csv.DictReader(open(CSV_DEFAULT, encoding="utf-8-sig"))}

    head = int(rpc("eth_blockNumber", [])["result"], 16)
    addrs = sorted(set(rec.values()))
    print(f"head {head:,}   wallets {len(addrs)}   window {WINDOW:,} blocks "
          f"(~{WINDOW * 0.1 / 3600:.0f}h)\n")

    got, events = incoming(addrs, head)

    back = {v: k for k, v in rec.items()}
    table = []
    for a in addrs:
        trunc = back.get(a, a)
        row = rows.get(trunc)
        calls = int(row["Calls_Triggered"]) if row else 0
        tier = row["Scout_Tier"] if row else "?"
        n_tok = len(got.get(a, ()))
        table.append((calls, n_tok, events[a], tier, trunc, a))

    table.sort(key=lambda r: (-r[0], -r[1]))
    print(f"{'calls':>5} {'tokens':>7} {'xfers':>7} {'hit rate':>9}  {'tier':<5} wallet")
    print("-" * 78)
    ratios = []
    for calls, n_tok, xf, tier, trunc, a in table:
        hr = f"{calls / n_tok:>8.1%}" if n_tok else "       --"
        if n_tok:
            ratios.append(calls / n_tok)
        print(f"{calls:>5} {n_tok:>7} {xf:>7} {hr}  {tier:<5} {trunc}  {a}")

    json.dump([{"calls": c, "tokens": n, "xfers": x, "tier": t,
                "trunc": tr, "addr": a} for c, n, x, t, tr, a in table],
              open(os.path.join(HERE, "cache", "wallet_scores.json"), "w",
                   encoding="utf-8"), indent=1)

    print("-" * 78)
    tok_counts = [r[1] for r in table if r[1]]
    if tok_counts:
        tok_counts.sort()
        import statistics as st
        print(f"tokens bought per wallet in ~{WINDOW * 0.1 / 3600:.0f}h: "
              f"median {st.median(tok_counts):.0f}   "
              f"min {tok_counts[0]}   max {tok_counts[-1]}")
    if ratios:
        ratios.sort()
        import statistics as st
        print(f"share of a wallet's buys that became a channel call: "
              f"median {st.median(ratios):.1%}")
        print("\nRead this as: if the median is low, 'a scout wallet bought it' is")
        print("not a signal -- the wallet buys almost everything, and the channel's")
        print("filter is doing the work, not the wallet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
