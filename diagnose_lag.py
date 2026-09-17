#!/usr/bin/env python3
"""
Why are calls showing up late?

Four different faults look identical from the outside -- "the call arrived ten
minutes after the channel posted it". This separates them.

    python diagnose_lag.py

Most checks need no Telegram access, so it is safe to run while the bot is up.
The last one compares Telegram's own post times and needs the bot STOPPED,
because two clients cannot share one session.

The key idea for check 4: if messages arrive in BURSTS after long silences, the
live update stream is not working and you are only seeing catch-up/backfill.
If they arrive evenly spaced, the stream is fine and any lag is a clock offset.
"""
import glob
import os
import re
import statistics as st
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

RECV = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ .*Received message (\d+)")
# Newer logs carry the real number: "(posted HH:MM:SSZ, lag 12.3s)".
LAG = re.compile(r"Received message \d+ from channel \(posted \S+, lag ([\d.]+)s\)")
START = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ .*Telegram listener active")


def ok(msg):
    print(f"   OK    {msg}")


def bad(msg):
    print(f"   BAD   {msg}")


def warn(msg):
    print(f"   WARN  {msg}")


def check_clock():
    print("\n1. MACHINE CLOCK")
    try:
        req = urllib.request.Request("https://www.cloudflare.com/cdn-cgi/trace",
                                     headers={"User-Agent": "clock/1"})
        with urllib.request.urlopen(req, timeout=15) as r:
            server = parsedate_to_datetime(r.headers["Date"])
        local = datetime.now(timezone.utc)
        skew = (local - server).total_seconds()
        if abs(skew) < 5:
            ok(f"clock is accurate ({skew:+.0f}s vs internet time)")
        elif abs(skew) < 120:
            warn(f"clock is {skew:+.0f}s off internet time - fix with: w32tm /resync /force")
        else:
            bad(f"clock is {skew:+.0f}s ({skew/60:+.1f} min) OFF. This alone can look")
            print("         like a delivery delay. Fix first:  w32tm /resync /force")
        return skew
    except Exception as e:
        warn(f"could not reach a time source ({type(e).__name__})")
        return None


def check_instances():
    print("\n2. BOT INSTANCES")
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "@(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             "Where-Object { $_.CommandLine -like '*main.py*' }).Count"],
            capture_output=True, text=True, timeout=30)
        n = int((out.stdout or "0").strip() or 0)
        if n == 0:
            warn("bot is not running")
        elif n <= 2:
            ok(f"{n} python process(es) - normal (parent + child)")
        else:
            bad(f"{n} processes. Two bot INSTANCES fight over one Telegram")
            print("         session and neither gets reliable updates. Kill all, start one.")
        return n
    except Exception:
        warn("could not check processes")
        return None


def read_log():
    recv, starts = [], []
    for path in sorted(glob.glob(os.path.join(HERE, "logs", "bot.log*"))):
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = RECV.match(line)
                if m:
                    recv.append(datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
                    continue
                m = START.match(line)
                if m:
                    starts.append(datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
    return sorted(recv), sorted(starts)


def check_restarts(starts, recv):
    print("\n3. RESTARTS")
    if not starts:
        warn("no startup lines found in logs")
        return
    span_h = ((recv[-1] - recv[0]).total_seconds() / 3600) if len(recv) > 1 else 0
    rate = len(starts) / span_h if span_h else 0
    if rate < 0.5:
        ok(f"{len(starts)} starts over {span_h:.0f}h - stable")
    else:
        bad(f"{len(starts)} starts over {span_h:.0f}h ({rate:.1f}/hour)")
        print("         Every restart replays old messages via backfill, which")
        print("         arrive stamped NOW but were posted minutes ago.")


def check_burstiness(recv):
    """The decisive test: live stream, or catch-up only?"""
    print("\n4. ARE MESSAGES ARRIVING LIVE, OR IN CATCH-UP BURSTS?")
    if len(recv) < 20:
        warn(f"only {len(recv)} messages logged - need more history")
        return
    gaps = [(recv[i + 1] - recv[i]).total_seconds() for i in range(len(recv) - 1)]
    near_zero = sum(g <= 2 for g in gaps)
    big = sum(g >= 300 for g in gaps)
    print(f"   {len(recv)} messages, median gap {st.median(gaps):.0f}s")
    print(f"   arrived <2s after the previous one : {near_zero} ({near_zero/len(gaps):.0%})")
    print(f"   gaps longer than 5 min             : {big}")
    if near_zero / len(gaps) > 0.35:
        bad("a third or more arrive bunched together. That is the signature of")
        print("         catch-up/backfill, not a live stream. Live updates are not")
        print("         reaching this client -- see the fixes below.")
    else:
        ok("messages arrive spread out - the live stream looks healthy")


# Each of these faults writes a distinctive line. Finding one of these is a
# diagnosis, not a guess -- the code path that logs it is the code path that
# stalls delivery.
SIGNATURES = [
    ("Flood wait error", "TELEGRAM IS RATE-LIMITING THIS ACCOUNT",
     "The client sleeps for the ENTIRE flood-wait window and receives nothing,\n"
     "         then catches up in a burst. This is the most likely cause of a\n"
     "         consistent multi-minute lag. Use an older/established Telegram\n"
     "         account, and stop restarting the bot (each reconnect costs quota)."),
    ("database is locked", "TWO BOT INSTANCES SHARING ONE SESSION",
     "Telethon keeps its session in SQLite and only one process can hold it.\n"
     "         Kill every python running main.py, then start exactly one."),
    ("WinError 121", "THE NETWORK IS DROPPING THE CONNECTION",
     "Each drop forces reconnect-and-catch-up. A VPN, proxy or router that\n"
     "         kills idle sockets does this. Try without the VPN."),
    ("Network socket timeout", "CONNECTION DROPS",
     "Same as above - the socket is not staying open."),
]


def check_signatures():
    print("\n5. KNOWN FAULT SIGNATURES IN THE LOGS")
    text = ""
    for path in sorted(glob.glob(os.path.join(HERE, "logs", "bot.log*"))):
        try:
            text += open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            pass
    if not text:
        warn("no logs to scan")
        return
    found = False
    for needle, title, advice in SIGNATURES:
        n = text.count(needle)
        if n:
            found = True
            bad(f"{title}  ({n} occurrences of '{needle}')")
            print(f"         {advice}")
    if not found:
        ok("none of the known delivery faults appear in these logs")


def check_measured_lag():
    """If the bot logged the real lag, stop inferring and just read it."""
    print("\n6. MEASURED DELIVERY LAG (from the log itself)")
    lags = []
    for path in sorted(glob.glob(os.path.join(HERE, "logs", "bot.log*"))):
        try:
            for line in open(path, encoding="utf-8", errors="replace"):
                m = LAG.search(line)
                if m:
                    lags.append(float(m.group(1)))
        except OSError:
            pass
    if not lags:
        warn("this log predates lag recording - restart the bot, let it run")
        print("         through a few calls, then run this again for exact numbers.")
        return
    lags.sort()
    med = st.median(lags)
    p90 = lags[int(len(lags) * 0.9)]
    print(f"   n={len(lags)}   median {med:.1f}s   p90 {p90:.1f}s   max {lags[-1]:.0f}s")
    print(f"   over 2 min: {sum(x > 120 for x in lags)}   "
          f"over 10 min: {sum(x > 600 for x in lags)}")
    if med < 5:
        ok("delivery is effectively instant - the lag is NOT on this machine")
    elif med < 60:
        warn(f"median {med:.0f}s - slower than it should be, but not the 10 min reported")
    else:
        bad(f"median {med:.0f}s ({med/60:.1f} min) - CONFIRMED delivery lag here")
        print("         Now look at checks 1-5 above for which of the four causes it is.")


def main() -> int:
    print("=" * 66)
    print("  WHY ARE CALLS LATE?")
    print("=" * 66)
    check_clock()
    check_instances()
    recv, starts = read_log()
    if recv:
        check_restarts(starts, recv)
        check_burstiness(recv)
    else:
        warn("\nno 'Received message' lines in logs - set log level to DEBUG")
    check_signatures()
    check_measured_lag()

    print("\n" + "=" * 66)
    print("  IF THE LIVE STREAM IS BROKEN, TRY IN THIS ORDER")
    print("=" * 66)
    print("""
   1. Fix the clock first:            w32tm /resync /force
   2. Run exactly ONE instance. Two clients on one session is the
      single most common cause -- updates get split between them.
   3. Log out other sessions for that Telegram account (Telegram app ->
      Settings -> Devices). Another active client can absorb the updates.
   4. Delete the .session file and re-authenticate. A stale session can
      keep connecting while silently receiving no updates.
   5. Check for a proxy/VPN/firewall on the machine. MTProto needs a
      persistent connection; something that drops idle sockets forces the
      client into reconnect-and-catch-up, which is exactly a 10-min lag.

   Remember: even with everything perfect, the CHANNEL itself calls tokens
   ~9 minutes after launch (median, measured over 172 calls). A perfectly
   healthy bot still looks "late" relative to the token, just not relative
   to the message.
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
