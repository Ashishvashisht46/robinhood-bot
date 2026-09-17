#!/usr/bin/env python3
"""
How late does this bot see channel messages?

Compares Telegram's server-side post time (msg.date, UTC) with the local time
the bot logged "Received message N from channel". That separates two very
different problems:

  delivery delay  -- Telegram/Telethon got it to us late, our problem to fix
  call delay      -- the channel posted late relative to the token launching,
                     which no amount of infrastructure can fix

Also reports how old each token already was when the call went out, using the
pool creation time from GeckoTerminal.

Read-only.

    python check_delay.py
"""
import asyncio
import json
import os
import re
import statistics as st
import sys
import urllib.request
from datetime import datetime, timezone

from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))

from telethon import TelegramClient  # noqa: E402

from config import Config  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

RECV = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ .*Received message (\d+) from channel")


def log_receipts() -> dict:
    """msg_id -> local datetime the bot logged it."""
    out = {}
    import glob
    for path in sorted(glob.glob(os.path.join(HERE, "logs", "bot.log*"))):
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = RECV.match(line)
                if m:
                    out[int(m.group(2))] = datetime.strptime(m.group(1),
                                                             "%Y-%m-%d %H:%M:%S")
    return out


def pool_created(ca: str):
    try:
        req = urllib.request.Request(
            f"https://api.geckoterminal.com/api/v2/networks/robinhood/tokens/{ca}/pools",
            headers={"User-Agent": "delay-check/1", "Accept": "application/json"})
        d = json.loads(urllib.request.urlopen(req, timeout=20).read())
        rows = d.get("data") or []
        if not rows:
            return None
        ts = min(r["attributes"].get("pool_created_at") for r in rows
                 if r["attributes"].get("pool_created_at"))
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


async def main() -> int:
    receipts = log_receipts()
    print(f"log has receipt times for {len(receipts)} messages")
    if not receipts:
        return 1

    # local clock is IST here; Telegram gives UTC
    offset = datetime.now().astimezone().utcoffset()
    print(f"local timezone offset: {offset}\n")

    config = Config()
    client = TelegramClient(os.path.join(HERE, "robinhood_copy_trader_session"),
                            config.TELEGRAM_API_ID, config.TELEGRAM_API_HASH)
    await client.start(phone=config.TELEGRAM_PHONE or None)
    chan = await client.get_entity(config.TELEGRAM_CHANNEL)

    delays, ages, checked = [], [], 0
    print(f"{'msg':>6}  {'posted (UTC)':<17}{'received (UTC)':<17}{'delay':>9}   token age at call")
    print("-" * 78)
    # Fetch the exact ids the bot logged. iter_messages(limit=N) only covers the
    # newest N, which no longer overlaps a bot that last ran two days ago.
    wanted = sorted(receipts)[-150:]
    fetched = []
    for i in range(0, len(wanted), 100):
        fetched += await client.get_messages(chan, ids=wanted[i:i + 100])
    for msg in [m for m in fetched if m is not None]:
        if msg.id not in receipts:
            continue
        posted = msg.date.astimezone(timezone.utc).replace(tzinfo=None)
        got = receipts[msg.id] - offset          # local -> UTC
        delay = (got - posted).total_seconds()
        delays.append(delay)

        age_txt = ""
        if checked < 12 and "EARLY CALL" in (msg.message or "").upper():
            urls = []
            for row in getattr(getattr(msg, "reply_markup", None), "rows", None) or []:
                for b in getattr(row, "buttons", None) or []:
                    u = getattr(getattr(b, "type", None), "url", None)
                    if u:
                        urls.append(u)
            ca = next((m.group(0).lower() for u in urls
                       for m in [re.search(r"0x[a-fA-F0-9]{40}", u)] if m), None)
            if ca:
                created = pool_created(ca)
                checked += 1
                if created:
                    mins = (msg.date.astimezone(timezone.utc) - created).total_seconds() / 60
                    ages.append(mins)
                    age_txt = f"{mins:>8.1f} min"
        if len(delays) <= 25:
            print(f"{msg.id:>6}  {posted:%Y-%m-%d %H:%M}  {got:%Y-%m-%d %H:%M}"
                  f"{delay:>8.0f}s   {age_txt}")
    await client.disconnect()

    if delays:
        d = sorted(delays)
        print(f"\nDELIVERY DELAY  (post -> bot received)   n={len(d)}")
        print(f"   median {st.median(d):>7.1f}s    p90 {d[int(len(d)*.9)]:>7.1f}s"
              f"    max {d[-1]:>7.1f}s")
        print(f"   over 60s: {sum(x > 60 for x in d)}   over 600s: {sum(x > 600 for x in d)}")
    if ages:
        a = sorted(ages)
        print(f"\nTOKEN AGE WHEN CALLED   n={len(a)}")
        print(f"   median {st.median(a):>7.1f} min    min {a[0]:>6.1f}    max {a[-1]:>7.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
