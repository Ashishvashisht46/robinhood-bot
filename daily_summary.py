#!/usr/bin/env python3
"""
What did the bot do, per day?

A thin CLI over dashboard_data.collect(). All the parsing lives there, so this
and the dashboard can never disagree about the numbers -- and it picks up the
rotation fix for free: the old version read only bot.log and bot.log.1, which
silently dropped history once the log rotated past two files.

    python daily_summary.py          # last 2 days
    python daily_summary.py --all    # every day in the logs
"""
import sys

from dashboard_data import collect

# Tickers are routinely CJK on this channel. Piping flips stdout to cp1252,
# which cannot encode them, so this died only when redirected.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROWS = [
    ("channel messages", "channel_messages"), ("signals parsed", "signals"),
    ("buys attempted", "buys_attempted"), ("positions opened", "opened"),
    ("positions closed", "closed"), ("buys failed", "buys_failed"),
    ("skipped", "skipped"), ("TP1 hits", "tp1"), ("TP2 hits", "tp2"),
    ("RPC rate limits", "rpc_limited"), ("errors", "errors"),
]


def main() -> int:
    data = collect(check_process=False)
    days = data["days"]
    for day in (list(days) if "--all" in sys.argv else list(days)[-2:]):
        counts = days[day]
        print("=" * 62)
        print(day)
        print("=" * 62)
        for label, key in ROWS:
            print(f"  {label:<22}{counts.get(key, 0):>6,}")

        closed = [t for t in data["trades"]
                  if t.get("closed_day") == day and t.get("outcome") == "closed"]
        if closed:
            print()
            for t in sorted(closed, key=lambda x: x.get("closed_at", "")):
                mark = "" if t.get("source") == "ledger" else "  ~"
                print(f"    {t.get('closed_at','')}  {t['ticker'][:16]:<18}"
                      f"{'WIN ' if t.get('is_win') else 'LOSS'}  "
                      f"${t.get('pnl_usd', 0):+.2f}{mark}")
        print()
        print(f"  {'REALISED PnL':<22}{counts.get('pnl_usd', 0):>+6.2f}   "
              f"({counts.get('wins', 0)}W / {counts.get('losses', 0)}L)")
        print()

    if any(t.get("source") != "ledger" for t in data["trades"]):
        print("~ reconstructed from text logs: no entry price or tx hashes.")
    if data["code_version"]:
        print(f"code version: dex_trader.py modified {data['code_version']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
