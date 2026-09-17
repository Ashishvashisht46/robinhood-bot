#!/usr/bin/env python3
"""
Did the CHANNEL's calls actually work?

Independent of our bot. For each call we take the timestamp and contract address
straight from the message buttons, pull minute OHLCV from GeckoTerminal, and
measure what the price did afterwards.

    python analyse_channel.py            # last 150 messages
    python analyse_channel.py 400        # go deeper

Method, and where it is conservative or optimistic:

  ENTRY  = close of the minute candle containing the call. You cannot buy at the
           pre-call price, and a call that pumps within its own minute gives a
           worse (higher) entry -- so this is the conservative choice.
  WIN    = high reaches +20% (TP1) BEFORE low reaches -50% (SL), walking candles
           forward in order. If both happen inside the SAME candle we count it a
           LOSS, because minute OHLCV cannot tell us which came first and the
           pessimistic reading is the honest one.
  PEAK   = best high after entry, within the window.

Prices are USD from GeckoTerminal. Tokens with no indexed pool are reported as
uncovered rather than silently dropped -- coverage is stated with every result.

Read-only. Touches Telegram (read) and GeckoTerminal. Sends nothing, buys nothing.
"""
import asyncio
import json
import os
import re
import statistics as st
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))

from telethon import TelegramClient  # noqa: E402

from config import Config  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

GT = "https://api.geckoterminal.com/api/v2"
NETWORK = "robinhood"
CACHE = os.path.join(HERE, "cache", "channel_prices.json")
ADDR = re.compile(r"0x[a-fA-F0-9]{40}")

TP1, TP2, SL = 1.20, 1.40, 0.50
WINDOW_MIN = 360           # how long after the call we follow the price


def api(path: str, tries: int = 4):
    """GeckoTerminal is free and rate limited; back off rather than hammer it."""
    for attempt in range(tries):
        try:
            req = urllib.request.Request(
                GT + path, headers={"User-Agent": "channel-research/1",
                                    "Accept": "application/json"})
            return json.loads(urllib.request.urlopen(req, timeout=25).read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503, 504):
                time.sleep(3 * (attempt + 1))
                continue
            return None
        except Exception:
            time.sleep(2)
    return None


def load_cache() -> dict:
    try:
        return json.load(open(CACHE, encoding="utf-8"))
    except Exception:
        return {}


def save_cache(c: dict) -> None:
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    tmp = CACHE + ".tmp"
    json.dump(c, open(tmp, "w", encoding="utf-8"))
    os.replace(tmp, CACHE)


def best_pool(ca: str, cache: dict):
    key = f"pool:{ca}"
    if key in cache:
        return cache[key]
    d = api(f"/networks/{NETWORK}/tokens/{ca}/pools")
    pool = None
    if d and d.get("data"):
        # deepest pool: thin ones give noisy prices
        best = max(d["data"],
                   key=lambda p: float(p["attributes"].get("reserve_in_usd") or 0))
        pool = best["id"].split("_", 1)[-1]
    cache[key] = pool
    return pool


def candles(pool: str, before_ts: int, cache: dict):
    key = f"ohlcv:{pool}:{before_ts}"
    if key in cache:
        return cache[key]
    d = api(f"/networks/{NETWORK}/pools/{pool}/ohlcv/minute"
            f"?aggregate=1&limit=1000&currency=usd&before_timestamp={before_ts}")
    rows = []
    if d:
        rows = d.get("data", {}).get("attributes", {}).get("ohlcv_list", []) or []
    rows = sorted(rows, key=lambda r: r[0])       # oldest first
    cache[key] = rows
    return rows


def evaluate(rows, call_ts: int):
    """Return entry, peak multiple, trough multiple, and the ladder outcome."""
    after = [r for r in rows if r[0] >= call_ts - 60]
    if len(after) < 2:
        return None
    entry = after[0][4] or after[0][1]             # close, else open
    if not entry:
        return None
    window = [r for r in after[1:] if r[0] <= call_ts + WINDOW_MIN * 60]
    if not window:
        return None

    peak = max(r[2] for r in window) / entry
    trough = min(r[3] for r in window) / entry
    outcome, mins_to_tp1 = "none", None
    for r in window:
        hit_tp1 = r[2] >= entry * TP1
        hit_sl = r[3] <= entry * SL
        if hit_tp1 and hit_sl:
            outcome = "sl"                         # same candle -> pessimistic
            break
        if hit_sl:
            outcome = "sl"
            break
        if hit_tp1:
            outcome = "tp2" if max(x[2] for x in window) >= entry * TP2 else "tp1"
            mins_to_tp1 = int((r[0] - call_ts) / 60)
            break
    return {"entry": entry, "peak": peak, "trough": trough,
            "outcome": outcome, "mins_to_tp1": mins_to_tp1,
            "candles": len(window)}


async def collect_calls(limit: int):
    config = Config()
    client = TelegramClient(os.path.join(HERE, "robinhood_copy_trader_session"),
                            config.TELEGRAM_API_ID, config.TELEGRAM_API_HASH)
    await client.start(phone=config.TELEGRAM_PHONE or None)
    chan = await client.get_entity(config.TELEGRAM_CHANNEL)

    calls = []
    async for msg in client.iter_messages(chan, limit=limit):
        text = msg.message or ""
        if "EARLY CALL" not in text.upper():
            continue
        urls = []
        for row in getattr(getattr(msg, "reply_markup", None), "rows", None) or []:
            for b in getattr(row, "buttons", None) or []:
                u = getattr(getattr(b, "type", None), "url", None)
                if u:
                    urls.append(u)
        ca = next((m.group(0).lower() for u in urls for m in [ADDR.search(u)] if m), None)
        tick = re.search(r"\$([^\s·]+)", text)
        if ca:
            calls.append({"id": msg.id, "ts": int(msg.date.timestamp()),
                          "when": msg.date.astimezone(timezone.utc),
                          "ticker": (tick.group(1) if tick else "?")[:14], "ca": ca})
    await client.disconnect()
    return sorted(calls, key=lambda c: c["ts"])


def main(limit: int) -> int:
    calls = asyncio.run(collect_calls(limit))
    print(f"calls with a contract address: {len(calls)}")
    if not calls:
        return 1
    print(f"range: {calls[0]['when']:%Y-%m-%d %H:%M} -> "
          f"{calls[-1]['when']:%Y-%m-%d %H:%M} UTC\n")

    cache = load_cache()
    rows, uncovered = [], []
    for i, c in enumerate(calls, 1):
        print(f"\r  pricing {i}/{len(calls)}  ${c['ticker']:<14}", end="", flush=True)
        pool = best_pool(c["ca"], cache)
        if not pool:
            uncovered.append(c)
            continue
        cs = candles(pool, c["ts"] + WINDOW_MIN * 60 + 120, cache)
        res = evaluate(cs, c["ts"]) if cs else None
        if not res:
            uncovered.append(c)
            continue
        rows.append({**c, **res})
        if i % 10 == 0:
            save_cache(cache)
        time.sleep(2.2)                            # stay under the free rate limit
    save_cache(cache)
    print("\r" + " " * 50 + "\r", end="")

    if not rows:
        print("no calls could be priced")
        return 1

    n = len(rows)
    tp1 = sum(r["outcome"] in ("tp1", "tp2") for r in rows)
    tp2 = sum(r["outcome"] == "tp2" for r in rows)
    sl = sum(r["outcome"] == "sl" for r in rows)
    none = sum(r["outcome"] == "none" for r in rows)
    peaks = sorted(r["peak"] for r in rows)
    troughs = sorted(r["trough"] for r in rows)

    print("=" * 74)
    print(f"CHANNEL CALL QUALITY   n={n} priced, {len(uncovered)} uncovered "
          f"({len(uncovered)/(n+len(uncovered)):.0%})")
    print(f"entry = close of the call minute | window = {WINDOW_MIN//60}h "
          f"| TP1 +20% | SL -50%")
    print("=" * 74)
    print(f"  reached +20% before -50%      {tp1:>4}  ({tp1/n:.0%})   <- the bot's TP1")
    print(f"     of those, also +40%        {tp2:>4}  ({tp2/n:.0%})")
    print(f"  hit -50% first                {sl:>4}  ({sl/n:.0%})")
    print(f"  neither within {WINDOW_MIN//60}h            {none:>4}  ({none/n:.0%})")
    print()
    print(f"  median peak                  {st.median(peaks):>6.2f}x")
    print(f"  median trough                {st.median(troughs):>6.2f}x")
    print(f"  calls that ever went +20%    {sum(p>=TP1 for p in peaks):>4}  "
          f"({sum(p>=TP1 for p in peaks)/n:.0%})")
    print(f"  calls that ever went +100%   {sum(p>=2 for p in peaks):>4}  "
          f"({sum(p>=2 for p in peaks)/n:.0%})")
    print(f"  calls that ever went -50%    {sum(t<=SL for t in troughs):>4}  "
          f"({sum(t<=SL for t in troughs)/n:.0%})")
    tt = [r["mins_to_tp1"] for r in rows if r["mins_to_tp1"] is not None]
    if tt:
        print(f"  median minutes to +20%       {st.median(tt):>6.0f}")
    print()
    print("  worst 5 and best 5 by peak:")
    for r in sorted(rows, key=lambda x: x["peak"])[:5]:
        print(f"    {r['when']:%m-%d %H:%M}  ${r['ticker']:<14} peak {r['peak']:>5.2f}x  "
              f"trough {r['trough']:>5.2f}x  {r['outcome']}")
    print("    ...")
    for r in sorted(rows, key=lambda x: -x["peak"])[:5]:
        print(f"    {r['when']:%m-%d %H:%M}  ${r['ticker']:<14} peak {r['peak']:>5.2f}x  "
              f"trough {r['trough']:>5.2f}x  {r['outcome']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 150))
