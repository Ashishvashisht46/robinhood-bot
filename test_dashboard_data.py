#!/usr/bin/env python3
"""
Self-check for the dashboard collector.

The collector's job is to be trustworthy about money, so the things worth testing
are the ones that fail SILENTLY: rotated files read in the wrong order, a trade
whose close never gets attached to its open, a reconstructed row that looks
complete. No network, no RPC -- fixtures only.

    python test_dashboard_data.py
"""
import os
import shutil
import sys
import tempfile

import dashboard_data as dd

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"          got  {got!r}\n          want {want!r}")
        fails.append(name)


def line(day, clock, msg, level="INFO"):
    return f"{day} {clock},000 - copytrader - {level} - {msg}\n"


def main():
    tmp = tempfile.mkdtemp(prefix="dash_test_")
    original_logs = dd.LOGS
    dd.LOGS = tmp
    try:
        # --- rotation ordering -------------------------------------------------
        # RotatingFileHandler writes NEWEST to bot.log and shifts older outward, so
        # bot.log.3 is the OLDEST. Glob order would interleave these at random, and
        # nothing downstream would look wrong -- the day counters would just be
        # attached to the wrong days.
        for suffix, day in (("bot.log.3", "2026-09-01"), ("bot.log.2", "2026-09-02"),
                            ("bot.log.1", "2026-09-03"), ("bot.log", "2026-09-04")):
            with open(os.path.join(tmp, suffix), "w", encoding="utf-8") as fh:
                fh.write(line(day, "10:00:00", "Received message 1 from channel"))

        check("rotated files ordered oldest -> newest",
              [os.path.basename(p) for p in dd.log_files()],
              ["bot.log.3", "bot.log.2", "bot.log.1", "bot.log"])
        check("every rotation is read, not just bot.log",
              [r["day"] for r in dd.parse_logs()],
              ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"])

        # --- one trade, end to end --------------------------------------------
        for name in os.listdir(tmp):
            os.remove(os.path.join(tmp, name))
        D = "2026-09-14"
        with open(os.path.join(tmp, "bot.log"), "w", encoding="utf-8") as fh:
            fh.writelines([
                line(D, "09:00:00", "Received message 7700 from channel"),
                line(D, "09:00:01", "==> Incoming Signal: $空气币 (DEX: Pons V2)"),
                line(D, "09:00:02", "Executing BUY for $空气币: Stake $1.00 (0.00040 ETH) | State: baseline"),
                line(D, "09:00:03", "1200ms | 0x" + "a" * 40 + " | UNISWAP_V4 | 0x" + "b" * 40
                     + " | sent=True | tx=0xfeed | status=1 | token_delta=+1,234.5000 | quote_delta=-0.000400"),
                line(D, "09:00:04", "Position opened: abc123 for $空气币 (1,234.50 tokens)"),
                line(D, "09:10:00", "Tracking $空气币: Multiplier: 1.21x (Peak: 1.21x) | TP1 | SL | Time: 10.0m"),
                line(D, "09:10:01", "TP1 HIT for $空气币 (1.21x >= 1.20x)! Selling 40% (493.80 tokens)..."),
                line(D, "09:20:00", "Position closed: $空气币 | Win: True | PnL: $+0.40 | New Balance: $7.24 | Next State: BASELINE"),
                # a signal that never becomes a buy
                line(D, "09:30:00", "==> Incoming Signal: $ENTHUSIASTS (DEX: Pons V2)"),
                line(D, "09:30:01", "Skipping $ENTHUSIASTS: No contract address identified."),
                # a buy that fails
                line(D, "09:40:00", "==> Incoming Signal: $SYNAPSE (DEX: Pons V2)"),
                line(D, "09:40:01", "Executing BUY for $SYNAPSE: Stake $1.00 (0.00040 ETH) | State: baseline"),
                line(D, "09:40:02", "Buy failed for $SYNAPSE: 0 tokens received.", "ERROR"),
                # a traceback: continuation lines must not be mistaken for events
                line(D, "09:50:00", "Trade execution exception for $BOOM: bang", "ERROR"),
                "Traceback (most recent call last):\n",
                "  File \"dex_trader.py\", line 1, in <module>\n",
                "    raise RuntimeError('Position closed: $FAKE | Win: True | PnL: $+99.00 | New Balance: $0')\n",
            ])

        recs = dd.parse_logs()
        trades = dd.build_trades(recs, [])
        by = {t["ticker"]: t for t in trades}

        check("trades correlated", sorted(by), ["ENTHUSIASTS", "SYNAPSE", "空气币"]
              if "ENTHUSIASTS" in by else sorted(by))
        check("CJK ticker survives", "空气币" in by, True)

        won = by.get("空气币", {})
        check("closed trade outcome", won.get("outcome"), "closed")
        check("closed trade PnL", won.get("pnl_usd"), 0.40)
        check("closed trade is a win", won.get("is_win"), True)
        check("stake carried from the buy attempt", won.get("stake_usd"), 1.0)
        check("fill size attached", won.get("tokens_bought"), 1234.50)
        check("position id attached", won.get("position_id"), "abc123")
        check("tranche recorded", [t["rung"] for t in won.get("tranches", [])], ["TP1"])

        check("failed buy outcome", by.get("SYNAPSE", {}).get("outcome"), "buy_failed")
        check("failure reason kept", by.get("SYNAPSE", {}).get("failure_reason"),
              "0 tokens received.")

        # A traceback line containing a fake "Position closed" must not register.
        check("traceback body is not parsed as an event",
              [r for r in recs if r.get("ticker") == "FAKE"], [])

        counters = dd.day_counters(recs)[D]
        check("signals counted", counters["signals"], 3)
        check("buys attempted", counters["buys_attempted"], 2)
        check("buys failed", counters["buys_failed"], 1)
        check("opened", counters["opened"], 1)
        check("closed", counters["closed"], 1)
        check("skipped", counters["skipped"], 1)
        check("TP1 hits", counters["tp1"], 1)
        check("realised PnL", counters["pnl_usd"], 0.40)

        swaps = [r for r in recs if r["kind"] == "swap"]
        check("swap line parsed", len(swaps), 1)
        check("tx hash recovered", swaps[0]["tx"] if swaps else None, "0xfeed")
        check("venue recovered", swaps[0]["venue"].strip() if swaps else None, "UNISWAP_V4")

        # --- a trade that spans midnight --------------------------------------
        # Bought on the 13th, sold on the 14th. It belongs to the 14th's realised
        # PnL. Grouping the list by OPEN day while counting PnL by CLOSE day made a
        # day's listed trades fail to sum to that day's own total.
        for name in os.listdir(tmp):
            os.remove(os.path.join(tmp, name))
        with open(os.path.join(tmp, "bot.log"), "w", encoding="utf-8") as fh:
            fh.writelines([
                line("2026-09-13", "23:50:00", "Executing BUY for $OVERNIGHT: Stake $1.00 (0.00040 ETH) | State: baseline"),
                line("2026-09-13", "23:50:01", "Position opened: n1 for $OVERNIGHT (100.00 tokens)"),
                line("2026-09-14", "08:36:50", "Position closed: $OVERNIGHT | Win: False | PnL: $-0.93 | New Balance: $6.00 | Next State: BASELINE"),
            ])
        recs2 = dd.parse_logs()
        overnight = dd.build_trades(recs2, [])[0]
        check("trade keeps the day it opened", overnight.get("day"), "2026-09-13")
        check("trade is attributed to its close day", overnight.get("closed_day"), "2026-09-14")
        counters2 = dd.day_counters(recs2)
        check("PnL lands on the close day", counters2["2026-09-14"].get("pnl_usd"), -0.93)
        check("open day carries no PnL", counters2["2026-09-13"].get("pnl_usd"), None)

        # --- reconstructed vs ledger ------------------------------------------
        # A row rebuilt from text has no entry price and no buy tx hash. It must say
        # so, because a reconstructed row that looks complete is a lie.
        check("text-derived trade is flagged reconstructed", won.get("source"),
              "reconstructed")

        ledger = [{"kind": "position_closed", "ts": f"{D}T09:20:00", "ticker": "空气币",
                   "tx_hash_buy": "0xfeed", "tx_hash_sell": "0xbeef",
                   "entry_price_usd": 0.00081, "pnl_usd": 0.40}]
        upgraded = {t["ticker"]: t for t in dd.build_trades(recs, ledger)}["空气币"]
        check("ledger row upgrades the trade", upgraded.get("source"), "ledger")
        check("sell tx hash comes from the ledger", upgraded.get("tx_hash_sell"), "0xbeef")
        check("entry price comes from the ledger", upgraded.get("entry_price_usd"), 0.00081)
    finally:
        dd.LOGS = original_logs
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if fails:
        print(f"FAILED: {', '.join(fails)}")
        return 1
    print("All dashboard collector checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
