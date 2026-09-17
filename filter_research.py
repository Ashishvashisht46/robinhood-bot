#!/usr/bin/env python3
"""
Can anything in a call message predict whether the token pumps?

Joins the call metadata the channel publishes (mcap, liquidity, tax, holders,
token age, wallet counts) to the price outcomes already cached by
analyse_channel.py, then tests whether any of it separates winners from losers.

    python filter_research.py           # collect metadata, then test
    python filter_research.py --test    # re-test from cache, no Telegram

TWO THINGS THIS IS CAREFUL ABOUT, because both are how backtests lie:

  LOOKAHEAD. Only fields present in the message at call time are used. Current
  pool liquidity is deliberately NOT a feature: a token still having liquidity
  today is a consequence of surviving, so it would "predict" survival perfectly
  and mean nothing.

  MULTIPLE COMPARISONS. Testing 10 features on 75 samples will surface something
  that looks good by chance alone. Every split reports its bucket sizes, and a
  random control feature is included -- if a real feature cannot beat noise,
  it is noise.
"""
import json
import os
import random
import statistics as st
import sys

from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))

from analyse_channel import ADDR, WINDOW_MIN, evaluate  # noqa: E402

META = os.path.join(HERE, "cache", "channel_meta.json")
PRICES = os.path.join(HERE, "cache", "channel_prices.json")
OFFSET = WINDOW_MIN * 60 + 120

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

FEATURES = [
    ("mcap_usd", "market cap $"),
    ("liquidity_usd", "liquidity $"),
    ("liquidity_pct", "liq % of mcap"),
    ("token_age_minutes", "token age (min)"),
    ("holders", "holders"),
    ("volume_24h", "24h volume $"),
    ("swaps_5m", "swaps in 5m"),
    ("elite_wallets", "elite wallets"),
    ("good_wallets", "good wallets"),
    ("buy_tax", "buy tax"),
]


async def collect():
    from telethon import TelegramClient
    from config import Config
    from message_parser import MessageParser

    config = Config()
    client = TelegramClient(os.path.join(HERE, "robinhood_copy_trader_session"),
                            config.TELEGRAM_API_ID, config.TELEGRAM_API_HASH)
    await client.start(phone=config.TELEGRAM_PHONE or None)
    chan = await client.get_entity(config.TELEGRAM_CHANNEL)
    parser = MessageParser(allow_ticker_fallback=False)

    out = {}
    async for msg in client.iter_messages(chan, limit=400):
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
        if not ca:
            continue
        try:
            sig = parser.parse(text, msg.entities, msg.id, msg.date,
                               msg.buttons, msg.reply_markup)
        except Exception:
            sig = None
        rec = {"ts": int(msg.date.timestamp()), "hour": msg.date.hour}
        for field, _ in FEATURES:
            rec[field] = getattr(sig, field, None) if sig else None
        rec["ticker"] = getattr(sig, "ticker", "?") if sig else "?"
        out[ca] = rec
    await client.disconnect()
    json.dump(out, open(META, "w", encoding="utf-8"))
    print(f"collected metadata for {len(out)} calls -> {META}")
    return out


def outcomes():
    """contract -> {reached2x, ladder_win, peak} from the cached price series."""
    cache = json.load(open(PRICES, encoding="utf-8"))
    pools = {k.split(":", 1)[1]: v for k, v in cache.items() if k.startswith("pool:")}
    res = {}
    for ca, pool in pools.items():
        if not pool:
            continue
        for k, candles in cache.items():
            if not k.startswith(f"ohlcv:{pool}:"):
                continue
            ev = evaluate(candles, int(k.rsplit(":", 1)[1]) - OFFSET)
            if ev:
                res[ca] = {"peak": ev["peak"], "trough": ev["trough"],
                           "reached2x": ev["peak"] >= 2.0,
                           "ladder_win": ev["outcome"] in ("tp1", "tp2")}
            break
    return res


def split_test(rows, key, label, target):
    vals = [(r[key], r[target]) for r in rows
            if r.get(key) is not None and isinstance(r.get(key), (int, float))]
    if len(vals) < 20:
        return f"  {label:<20}  only {len(vals)} calls have this field - skipped"
    med = st.median(v for v, _ in vals)
    lo = [w for v, w in vals if v <= med]
    hi = [w for v, w in vals if v > med]
    if len(lo) < 8 or len(hi) < 8:
        return f"  {label:<20}  degenerate split (all one value) - skipped"
    a, b = sum(lo) / len(lo), sum(hi) / len(hi)
    gap = b - a
    flag = "  <-- look" if abs(gap) >= 0.20 else ""
    return (f"  {label:<20}  <= {med:>12,.0f}: {a:>5.0%} (n={len(lo):>2})   "
            f"> : {b:>5.0%} (n={len(hi):>2})   gap {gap:>+5.0%}{flag}")


def main() -> int:
    if "--test" not in sys.argv:
        import asyncio
        asyncio.run(collect())
    meta = json.load(open(META, encoding="utf-8"))
    outs = outcomes()

    rows = []
    for ca, m in meta.items():
        if ca in outs:
            rows.append({**m, **outs[ca]})
    n = len(rows)
    print(f"\njoined metadata + outcome for {n} calls")
    if n < 20:
        print("not enough overlap to test")
        return 1

    random.seed(3)
    for r in rows:
        r["_random"] = random.random()          # the control

    for target, tlabel in (("reached2x", "reached 2x"),
                           ("ladder_win", "+20% before -50%")):
        base = sum(r[target] for r in rows) / n
        print("\n" + "=" * 78)
        print(f"TARGET: {tlabel}    base rate {base:.0%}  (n={n})")
        print("=" * 78)
        for key, label in FEATURES:
            print(split_test(rows, key, label, target))
        print(split_test(rows, "hour", "hour of day (UTC)", target))
        print(split_test(rows, "_random", "RANDOM (control)", target))

    print("\n" + "=" * 78)
    print("How to read this: a feature only matters if its gap is LARGE, its")
    print("buckets are big, and it clearly beats the random control. With 11")
    print("features and one sample, a 15-20% gap is what noise looks like.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
