#!/usr/bin/env python3
"""
Derive a wallet watchlist from measured outcomes, not from someone's label.

The channel's own 519-wallet "scout" list failed because it had no denominator:
a wallet appeared in it only when its buy became a call, so a bot buying 449
tokens a day looked like a genius (AD-030). This builds the list the other way
round -- find tokens that actually went up, see who bought them early, then
measure EVERY token those wallets bought, winners and losers alike.

    1. winners      tokens whose price >= WIN_MULT from their first candle
    2. early buyers wallets that received a winner inside EARLY_MIN
    3. candidates   wallets appearing across >= MIN_WINS different winners
    4. SCORE        every token each candidate bought in the window

Step 4 is the one that matters. A wallet that bought 3 winners out of 400 buys
is a sprayer; one that bought 3 out of 12 is a signal. The old list could not
tell those apart.

    python derive_wallets.py

Read-only. Sends nothing.
"""
import collections
import json
import os
import statistics as st
import sys

from contract_resolution import SYSTEM
from launch_universe import _rpc, TRANSFER, BLOCK_S, _load, _save

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
SCRATCH = (r"C:\Users\Ashish\AppData\Local\Temp\claude"
           r"\C--Users-Ashish-OneDrive-Desktop-Lux-dental-marketing---GHL"
           r"\ba6d32a4-542b-44d7-ab97-fa3a8ef6c660\scratchpad")
OUT = os.path.join(HERE, "cache", "derived_wallets.json")

WIN_MULT = 2.0          # what counts as a winner
EARLY_MIN = 10          # "bought early" window after launch
MIN_WINS = 2            # appear in this many winners to be a candidate
SCORE_BLOCKS = 900_000  # ~25h of history to score a candidate against


def peak_multiple(candles):
    rows = sorted(candles, key=lambda r: r[0])
    if len(rows) < 3:
        return None
    entry = rows[0][4] or rows[0][1]
    if not entry:
        return None
    return max(r[2] for r in rows[1:]) / entry


def receivers(token, from_block, to_block):
    r = _rpc("eth_getLogs", [{"fromBlock": hex(from_block), "toBlock": hex(to_block),
                              "address": token, "topics": [TRANSFER]}])
    if "error" in r:
        return set()
    out = set()
    for log in r.get("result") or []:
        t = log.get("topics", [])
        if len(t) > 2:
            out.add("0x" + t[2][-40:])
    return out


def main() -> int:
    hist = _load(os.path.join(SCRATCH, "hist.json"), {})
    launches = _load(os.path.join(SCRATCH, "fresh_launches.json"), {})
    toks = launches.get("tokens", {})
    if not hist or not toks:
        print("missing the forward-priced cohort; nothing to derive from")
        return 1

    scored = []
    for ca, candles in hist.items():
        pk = peak_multiple(candles)
        if pk is not None:
            scored.append((pk, ca))
    scored.sort(reverse=True)
    winners = [(pk, ca) for pk, ca in scored if pk >= WIN_MULT and ca in toks]
    print(f"{len(hist)} priced tokens; {len(winners)} reached {WIN_MULT:g}x "
          f"and have a launch block\n")
    if len(winners) < 3:
        print("too few winners to derive anything")
        return 1
    for pk, ca in winners[:12]:
        print(f"  {pk:>7.2f}x  {ca}  ({toks[ca].get('venue')})")

    span = int(EARLY_MIN * 60 / BLOCK_S)
    buyers = collections.defaultdict(set)      # wallet -> {winner}
    print(f"\npulling early buyers ({EARLY_MIN} min) for {len(winners)} winners...")
    for i, (pk, ca) in enumerate(winners, 1):
        bn = toks[ca]["block"]
        for w in receivers(ca, bn, bn + span):
            buyers[w].add(ca)
        print(f"  {i}/{len(winners)}", end="\r", flush=True)
    print(" " * 30)

    cands = {w: s for w, s in buyers.items() if len(s) >= MIN_WINS}
    print(f"{len(buyers):,} distinct early buyers across the winners")
    print(f"{len(cands)} appear in >= {MIN_WINS} different winners")

    # Routers and pools receive every token by construction, so they appear in
    # EVERY winner and would top any "bought the most winners" ranking. One of
    # them alone exceeded the endpoint's 10,000-log cap over 20k blocks, which is
    # how they were noticed. A watchlist of routers is not a signal.
    contracts = set()
    for w in list(cands):
        if w.lower() in SYSTEM:
            contracts.add(w)
            continue
        try:
            code = _rpc("eth_getCode", [w, "latest"]).get("result", "0x")
        except Exception:
            code = "0x"
        if code and code != "0x":
            contracts.add(w)
    cands = {w: s for w, s in cands.items() if w not in contracts}
    print(f"  minus {len(contracts)} contracts (routers/pools) -> "
          f"{len(cands)} real wallets\n")
    if not cands:
        print("No wallet bought more than one winner early.")
        print("That is itself the answer: on this sample the winners share no")
        print("common early buyer, so there is no list to watch.")
        return 0

    # THE DENOMINATOR. Without this we repeat AD-030 exactly.
    head = int(_rpc("eth_blockNumber", [])["result"], 16)
    lo = head - SCORE_BLOCKS
    addrs = sorted(cands)
    topics = ["0x" + "0" * 24 + a[2:].lower() for a in addrs]
    bought = collections.defaultdict(set)
    print(f"scoring {len(addrs)} candidates against ~{SCORE_BLOCKS*BLOCK_S/3600:.0f}h "
          f"of their own history...")
    # 20k blocks, not 100k. With ~87 addresses in the filter a 100k-block chunk
    # returns ~24,000 logs, over the endpoint's 10,000 cap -- every query errored
    # and a silent `continue` turned that into "these wallets bought nothing".
    # An error here is a broken measurement, so it is counted and reported.
    failed = 0
    for start in range(lo, head, 20_000):
        end = min(start + 19_999, head)
        r = _rpc("eth_getLogs", [{"fromBlock": hex(start), "toBlock": hex(end),
                                  "topics": [TRANSFER, None, topics]}])
        if "error" in r:
            failed += 1
            continue
        for log in r.get("result") or []:
            t = log.get("topics", [])
            if len(t) > 2:
                bought["0x" + t[2][-40:]].add((log.get("address") or "").lower())
        print(f"  {end-lo:,}/{SCORE_BLOCKS:,} blocks", end="\r", flush=True)
    print(" " * 40)
    if failed:
        print(f"WARNING: {failed} chunks failed. The denominator is incomplete and")
        print("every hit rate below is overstated. Do not act on this run.\n")

    rows = []
    for w in addrs:
        n_all = len(bought.get(w, ()))
        n_win = len(cands[w])
        rows.append((n_win / n_all if n_all else 0.0, n_win, n_all, w))
    rows.sort(reverse=True)

    print(f"{'hit rate':>9} {'winners':>8} {'total bought':>13}  wallet")
    print("-" * 74)
    for hr, nw, na, w in rows:
        flag = "  <- sprayer" if na >= 100 else ""
        print(f"{hr:>8.1%} {nw:>8} {na:>13}  {w}{flag}")

    keep = [r for r in rows if r[2] and r[2] < 100 and r[0] >= 0.05]
    print(f"\n{len(keep)} of {len(rows)} look selective rather than spraying")
    if rows:
        tot = [r[2] for r in rows if r[2]]
        if tot:
            print(f"median tokens bought per candidate: {st.median(tot):.0f}")
    _save(OUT, {"candidates": [{"wallet": w, "winners": nw, "bought": na,
                                "hit_rate": round(hr, 4)}
                               for hr, nw, na, w in rows],
                "winners_used": [c for _, c in winners]})
    print(f"written to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
