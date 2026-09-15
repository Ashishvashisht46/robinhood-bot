#!/usr/bin/env python3
"""
Collect everything the bot did into one JSON blob.

Sources, in order of trust:

  1. logs/events.jsonl  -- written by trade_ledger.py. Complete and never rotated.
  2. logs/bot.log*      -- ALL rotations. Text, but it is the only record of
                           signals, skips, detection and swaps.
  3. cache/strategy_state.json -- aggregate counters and open positions.

Rotation ordering is load-bearing and easy to get backwards: RotatingFileHandler
writes the NEWEST lines to bot.log and shifts older ones outward, so bot.log.5 is
the OLDEST file. Reading them in glob order interleaves history at random.

    python dashboard_data.py              # print the blob
    python dashboard_data.py --backfill   # recover pre-ledger trades
"""
import glob
import json
import math
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
LOGS = os.path.join(HERE, "logs")
STATE = os.path.join(HERE, "cache", "strategy_state.json")

# "2026-09-14 10:40:43,488 - copytrader - INFO - message"
LINE = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}),\d+ - \S+ - (\w+) - (.*)$")
ANSI = re.compile(r"\x1b\[[0-9;]*m")

# Each entry: kind -> pattern. Groups are named so the record is self-describing.
PATTERNS = {
    "startup": re.compile(r"Config loaded from .+ \| DRY_RUN=(?P<paper>True|False) \|"),
    "heartbeat": re.compile(r"Telegram connection heartbeat ping OK"),
    "listener_active": re.compile(r"Telegram listener active"),
    "wallet_balance": re.compile(r"Wallet Balance: (?P<eth>[\d.]+) ETH \(\$(?P<usd>[\d.]+) USD\)"),
    "signal": re.compile(r"==> Incoming Signal: \$(?P<ticker>\S+) \(DEX: (?P<dex>[^)]*)\)"),
    "skip_no_ca": re.compile(r"Skipping \$(?P<ticker>\S+): No contract address identified\."),
    "skip": re.compile(r"Skipping trade for \$(?P<ticker>[^:]+): (?P<reason>.+)"),
    "buy_attempt": re.compile(
        r"Executing BUY for \$(?P<ticker>\S+): Stake \$(?P<stake_usd>[\d.]+) "
        r"\((?P<stake_eth>[\d.]+) ETH\) \| State: (?P<state>\S+)"),
    "buy_failed": re.compile(r"Buy failed for \$(?P<ticker>[^:]+): (?P<reason>.+)"),
    "opened": re.compile(
        r"Position opened: (?P<pid>\S+) for \$(?P<ticker>\S+) \((?P<tokens>[\d,.]+) tokens\)"),
    "closed": re.compile(
        r"Position closed: \$(?P<ticker>\S+) \| Win: (?P<win>\w+) \| PnL: \$(?P<pnl>[+-][\d.]+)"
        r" \| New Balance: \$(?P<balance>[\d.-]+)"),
    "tracking": re.compile(
        r"Tracking \$(?P<ticker>\S+): Multiplier: (?P<mult>[\d.]+)x \(Peak: (?P<peak>[\d.]+)x\)"
        r".*Time: (?P<minutes>[\d.]+)m"),
    "tp_hit": re.compile(r"(?P<rung>TP\d) HIT for \$(?P<ticker>\S+) \((?P<mult>[\d.]+)x"),
    "detection": re.compile(
        r"Token: (?P<address>0x[a-fA-F0-9]{40}) \| Venue: (?P<venue>\S+) \| Target: (?P<target>\S+)"
        r" \| QuoteAsset: (?P<quote>\S+) \| Fee: (?P<fee>\S+) \| Detection time: (?P<seconds>[\d.]+)s"),
    # The swap summary line. Already machine-readable, and the only place a tx
    # hash appears -- which is why dex_trader.py needs no changes.
    "swap": re.compile(
        r"^(?P<ms>\d+)ms \| (?P<address>0x[a-fA-F0-9]{40}) \| (?P<venue>[^|]+?) \| (?P<target>\S+)"
        r" \| sent=(?P<sent>\S+(?: \(DRY_RUN\))?) \| tx=(?P<tx>\S+) \| status=(?P<status>\S+)"
        r" \| token_delta=(?P<token_delta>[+-][\d,.]+) \| quote_delta=(?P<quote_delta>[+-][\d.]+)"),
    "sim_ok": re.compile(r"SIMULATION OK \| Venue: (?P<venue>\S+) \| To: (?P<to>\S+) \| gas: (?P<gas>\d+)"),
    "code_version": re.compile(r"CODE VERSION: (?P<file>\S+) last modified (?P<mtime>.+)"),
    "no_ca_dump": re.compile(r"Msg (?P<msg_id>\d+) NO-CA dump: (?P<detail>.+)"),
    "rpc_limited": re.compile(r"(?P<code>429) Client Error: Too Many Requests"),
    "channel_msg": re.compile(r"Received message (?P<msg_id>\d+) from channel"),
}

# A "no quote" refusal is the bot working correctly (AD-005), not a defect. Kept
# separate from failures so the dashboard does not read as broken when it is safe.
CORRECT_REFUSAL = ("no quote", "PoolNotInitialized", "0x486aa307", "refusing")


def log_files() -> list:
    """Oldest first. bot.log.5 ... bot.log.1, then bot.log."""
    rotated = []
    for path in glob.glob(os.path.join(LOGS, "bot.log.*")):
        suffix = path.rsplit(".", 1)[-1]
        if suffix.isdigit():
            rotated.append((int(suffix), path))
    ordered = [p for _, p in sorted(rotated, reverse=True)]  # .5 is oldest
    current = os.path.join(LOGS, "bot.log")
    if os.path.exists(current):
        ordered.append(current)
    return ordered


def parse_logs(paths=None) -> list:
    """
    Every recognised event, oldest first.

    Lines without a timestamp are continuation lines from `exc_info=True`
    tracebacks; they are skipped rather than mis-parsed.
    """
    records = []
    for path in (paths if paths is not None else log_files()):
        with open(path, encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                m = LINE.match(ANSI.sub("", raw.rstrip("\n")))
                if not m:
                    continue
                day, clock, level, msg = m.groups()
                for kind, pattern in PATTERNS.items():
                    found = pattern.search(msg)
                    if found:
                        rec = {"day": day, "time": clock, "level": level, "kind": kind}
                        rec.update(found.groupdict())
                        records.append(rec)
                        break
                else:
                    if level in ("ERROR", "CRITICAL"):
                        records.append({"day": day, "time": clock, "level": level,
                                        "kind": "error", "message": redact(msg[:300])})
    return records


# Error text is copied verbatim into the dashboard, which gets published. A URL
# with an embedded API key inside a 429 message is one bad day away from shipping,
# so scrub before it ever reaches the page rather than hoping it does not appear.
SECRET = re.compile(
    r"(https?://[^\s\"']*?/)[A-Za-z0-9_-]{16,}(/?)"      # keyed RPC endpoints
    r"|0x[a-fA-F0-9]{64}"                                  # private-key-shaped
    r"|[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{20,}",  # JWT
)


def redact(text: str) -> str:
    """Replace anything that looks like a credential. Cheap, and never wrong to do."""
    return SECRET.sub(lambda m: (m.group(1) + "<redacted>" + (m.group(2) or "")
                                 if m.group(1) else "<redacted>"), text or "")


def _num(value, default=0.0):
    try:
        return float(str(value).replace(",", "").replace("+", ""))
    except (TypeError, ValueError):
        return default


def _match_ledger(pool: list, rec: dict):
    """
    Find the ledger row for THIS close, and consume it so it cannot be reused.

    Matching on ticker + day alone reused a single event for every close of that
    ticker that day. OPENFANG traded twice on 2026-09-14 and both rows rendered
    with the first close's time and P&L, so the listed trades summed to -$3.26
    against a counter total of -$2.71. The counters were right; the trade list
    was quietly wrong, which is the worse direction.
    """
    for i, ev in enumerate(pool):
        if (ev.get("closed_at") == rec["time"]
                and ev.get("closed_day", rec["day"]) == rec["day"]):
            return pool.pop(i)
    for i, ev in enumerate(pool):
        if str(ev.get("ts", "")).startswith(rec["day"]):
            return pool.pop(i)
    return None


def build_trades(records: list, events: list) -> list:
    """
    Correlate a trade's life: buy attempt -> swap -> open -> tranche exits -> close.

    Ledger events win where they exist, because they carry the entry price, both
    tx hashes and the tranche breakdown that the text log never had. Anything only
    recoverable from text is marked reconstructed so the UI can say so.
    """
    by_ticker = defaultdict(list)
    for rec in records:
        if rec.get("ticker"):
            by_ticker[rec["ticker"]].append(rec)

    ledger_closed = {}
    for ev in events:
        if ev.get("kind") == "position_closed":
            ledger_closed.setdefault(ev.get("ticker"), []).append(ev)

    trades, open_attempts = [], {}
    for rec in records:
        ticker = rec.get("ticker")
        if rec["kind"] == "buy_attempt":
            open_attempts[ticker] = {
                "ticker": ticker, "day": rec["day"], "opened_at": rec["time"],
                "stake_usd": _num(rec.get("stake_usd")),
                "stake_eth": _num(rec.get("stake_eth")),
                "state": rec.get("state"), "outcome": "pending",
                "tranches": [], "source": "reconstructed",
            }
        elif rec["kind"] == "buy_failed" and ticker in open_attempts:
            t = open_attempts.pop(ticker)
            t.update(outcome="buy_failed", failure_reason=rec.get("reason", "").strip())
            trades.append(t)
        elif rec["kind"] == "opened" and ticker in open_attempts:
            t = open_attempts[ticker]
            t.update(outcome="open", position_id=rec.get("pid"),
                     tokens_bought=_num(rec.get("tokens")))
        elif rec["kind"] == "tp_hit" and ticker in open_attempts:
            open_attempts[ticker]["tranches"].append(
                {"rung": rec.get("rung"), "at": rec["time"], "multiplier": _num(rec.get("mult"))})
        elif rec["kind"] == "closed" and ticker in open_attempts:
            t = open_attempts.pop(ticker)
            # closed_day, not day: a position bought on the 13th and sold on the
            # 14th belongs to the 14th's realised PnL. Grouping the list by open
            # day while counting PnL by close day makes a day's trades fail to sum
            # to its own total.
            t.update(outcome="closed", closed_at=rec["time"], closed_day=rec["day"],
                     is_win=rec.get("win") == "True", pnl_usd=_num(rec.get("pnl")),
                     balance_after=_num(rec.get("balance")))
            ev = _match_ledger(ledger_closed.get(ticker, []), rec)
            if ev:
                t.update({k: v for k, v in ev.items() if k not in ("kind", "ts", "source")})
                # A backfilled row is still reconstructed, however it reached us.
                t["source"] = ev.get("source") or "ledger"
            trades.append(t)

    trades.extend(open_attempts.values())  # still open, or never resolved
    trades.sort(key=lambda t: (t.get("day", ""), t.get("opened_at", "")))
    return trades


def day_counters(records: list) -> dict:
    """Per-day totals. Keys chosen to match what the report already shows."""
    days = defaultdict(lambda: defaultdict(int))
    for rec in records:
        d = days[rec["day"]]
        k = rec["kind"]
        d["channel_messages"] += k == "channel_msg"
        d["signals"] += k == "signal"
        d["buys_attempted"] += k == "buy_attempt"
        d["buys_failed"] += k == "buy_failed"
        d["opened"] += k == "opened"
        d["closed"] += k == "closed"
        d["skipped"] += k in ("skip", "skip_no_ca")
        d["tp1"] += k == "tp_hit" and rec.get("rung") == "TP1"
        d["tp2"] += k == "tp_hit" and rec.get("rung") == "TP2"
        d["rpc_limited"] += k == "rpc_limited"
        d["errors"] += k == "error"
        if k == "closed":
            d["pnl_usd"] = round(d.get("pnl_usd", 0.0) + _num(rec.get("pnl")), 4)
            d["wins" if rec.get("win") == "True" else "losses"] += 1
    return {day: dict(counts) for day, counts in sorted(days.items())}


def bot_process() -> dict:
    """
    Is the bot running? Matches ExecutablePath as well as CommandLine, and forces
    an array -- PowerShell returns a bare object for a single match and .Count on
    that is empty, which once reported a healthy bot as stopped (AD-013).
    """
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "$ErrorActionPreference = 'Stop'\n"
             "@(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             "Where-Object { $_.CommandLine -like '*main.py*' -and "
             "($_.ExecutablePath -like '*robinhood-bot*' -or "
             "$_.CommandLine -like '*robinhood-bot*') }).Count"],
            capture_output=True, text=True, timeout=30)
        if out.returncode != 0 or not (out.stdout or "").strip().isdigit():
            return {"running": None, "error": "Process status unavailable"}
        return {"running": int((out.stdout or "0").strip() or 0) > 0,
                "processes": int((out.stdout or "0").strip() or 0)}
    except Exception as exc:
        return {"running": None, "error": type(exc).__name__}


def risk_limits() -> dict:
    """Read only non-secret configuration used in the entry budget calculation."""
    defaults = dict(SAFETY_FLOOR_USD="40", BASELINE_STAKE_USD="2",
                    COMPOUND_STAKE_USD="5", GAS_RESERVE_USD="0.25",
                    BUFFER_GATE_USD="60", ENABLE_COMPOUNDING="false")
    values = {k: os.environ.get(k, v) for k, v in defaults.items()}
    try:
        from dotenv import dotenv_values
        configured = dotenv_values(os.path.join(HERE, ".env"))
        values.update({k: configured[k] for k in defaults if configured.get(k) is not None})
    except OSError:
        pass
    return values


def entry_budget(state, limits=None) -> dict:
    """Diagnostic only: never changes the balance, stake or trading settings."""
    values = risk_limits() if limits is None else limits
    try:
        balance = float(state["balance_usd"])
        floor = float(values["SAFETY_FLOOR_USD"])
        gas = float(values["GAS_RESERVE_USD"])
        baseline = float(values["BASELINE_STAKE_USD"])
        compound = float(values["COMPOUND_STAKE_USD"])
        buffer = float(values["BUFFER_GATE_USD"])
        stakes = [float(p["stake_usd"]) for p in state.get("open_positions", [])]
        numbers = [balance, floor, gas, baseline, compound, buffer] + stakes
        if state.get("error") or any(not math.isfinite(n) or n < 0 for n in numbers):
            raise ValueError("invalid state or limits")
        use_compound = (str(values["ENABLE_COMPOUNDING"]).lower() == "true"
                        and str(state.get("state", "BASELINE")).upper() == "COMPOUND"
                        and not (buffer > 0 and balance < buffer))
        stake = compound if use_compound else baseline
        reserved = sum(stakes)
        required = floor + gas + stake + reserved
        return dict(known=True, balance_usd=balance, safety_floor_usd=floor,
                    gas_reserve_usd=gas, next_stake_usd=stake, reserved_stake_usd=reserved,
                    required_balance_usd=required, shortfall_usd=max(0, required - balance),
                    below_requirement=balance < required,
                    halted=str(state.get("state", "")).upper() == "HALTED")
    except (KeyError, TypeError, ValueError):
        return dict(known=False)


def activity_summary(records):
    started = next((i for i in range(len(records)-1, -1, -1)
                    if records[i]["kind"] == "startup"), None)
    session = records[started:] if started is not None else []
    stamp = lambda row: f"{row['day']} {row['time']}" if row else None
    floor_refusals = [r for r in records if r["kind"] == "skip"
                      and "balance above floor" in r.get("reason", "").lower()]
    wallet = next((r for r in reversed(records) if r["kind"] == "wallet_balance"), None)
    return dict(started_at=stamp(records[started]) if started is not None else None,
                last_activity_at=stamp(records[-1]) if records else None,
                session_signal_evaluations=sum(r["kind"] == "signal" for r in session),
                session_floor_refusals=sum(r["kind"] == "skip" and
                    "balance above floor" in r.get("reason", "").lower() for r in session),
                last_floor_refusal_at=stamp(floor_refusals[-1]) if floor_refusals else None,
                native_wallet_usd=_num(wallet["usd"]) if wallet else None,
                native_wallet_at=stamp(wallet))


def source_stamp():
    """Invalidate parsed data on state, ledger or config changes as well as logs."""
    paths = log_files() + [STATE, os.path.join(LOGS, "events.jsonl"), os.path.join(HERE, ".env")]
    result = []
    for path in paths:
        try:
            stat = os.stat(path)
            result.append((path, stat.st_mtime_ns, stat.st_size))
        except OSError:
            result.append((path, None, None))
    return tuple(result)


def collect(check_process: bool = True) -> dict:
    records = parse_logs()
    events = []
    try:
        from trade_ledger import read_events
        events = read_events()
    except Exception:
        pass

    state = {}
    if os.path.exists(STATE):
        try:
            state = json.load(open(STATE, encoding="utf-8"))
        except ValueError:
            state = {"error": "strategy_state.json is corrupt"}

    trades = build_trades(records, events)
    latest = {}
    for rec in records:
        if rec["kind"] == "tracking":
            latest[rec["ticker"]] = rec

    failures = [t for t in trades if t.get("outcome") == "buy_failed"]
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "bot": bot_process() if check_process else {},
        "code_version": next((r["mtime"] for r in reversed(records)
                              if r["kind"] == "code_version"), None),
        "totals": state,
        "entry_budget": entry_budget(state),
        "activity": activity_summary(records),
        "days": day_counters(records),
        "trades": trades,
        "open_positions": [
            {**p, "live": latest.get(p.get("ticker"), {})}
            for p in state.get("open_positions", [])
        ],
        "signals": [r for r in records if r["kind"] == "signal"],
        "rejections": [r for r in records if r["kind"] in ("skip", "skip_no_ca")],
        "failures": [
            {**t, "correct_refusal": any(
                s.lower() in json.dumps(t, default=str).lower() for s in CORRECT_REFUSAL)}
            for t in failures
        ],
        "swaps": [r for r in records if r["kind"] == "swap"],
        "detections": [r for r in records if r["kind"] == "detection"],
        "errors": [r for r in records if r["kind"] == "error"][-200:],
        "no_ca_dumps": [r for r in records if r["kind"] == "no_ca_dump"],
        "ledger_events": len(events),
        # So the page can say WHY trading stopped, not just that it did.
        "safety_floor": _num(risk_limits()["SAFETY_FLOOR_USD"]),
    }


def _env_floor() -> float:
    return _num(risk_limits()["SAFETY_FLOOR_USD"])


def backfill() -> int:
    """
    Recover pre-ledger closed trades into events.jsonl, marked reconstructed.

    Entry price, buy tx hash and the tranche breakdown were never logged for these,
    so those fields stay absent rather than being invented. Idempotent: a trade
    already in the ledger is not written twice.
    """
    from trade_ledger import LEDGER_PATH, log_event, read_events

    have = {(e.get("ticker"), e.get("closed_at"), e.get("day"))
            for e in read_events() if e.get("kind") == "position_closed"}
    written = 0
    for t in build_trades(parse_logs(), []):
        if t.get("outcome") != "closed":
            continue
        key = (t.get("ticker"), t.get("closed_at"), t.get("day"))
        if key in have:
            continue
        log_event("position_closed", source="reconstructed", **{
            k: v for k, v in t.items() if k not in ("source",)})
        have.add(key)
        written += 1
    print(f"backfilled {written} closed trade(s) into {LEDGER_PATH}")
    return written


if __name__ == "__main__":
    if "--backfill" in sys.argv:
        backfill()
    else:
        print(json.dumps(collect(check_process="--no-process" not in sys.argv),
                         indent=2, default=str, ensure_ascii=False))
