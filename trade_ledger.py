#!/usr/bin/env python3
"""
Append-only event ledger.

The bot's closed-trade history only ever existed in memory: `strategy_engine`
appends every result to a `trade_history` list that `_save_state()` never writes,
so each restart erased it. What survived was text in `bot.log`, which rotates at
5MB x 5 and eventually deletes itself.

This writes one JSON object per line to `logs/events.jsonl`, which is never
rotated. Only events whose data exists *solely in memory* belong here -- the
Position object at open, at close, and at each tranche exit. Signals, skips,
buys and swaps are already parseable in `bot.log` and are read from there rather
than duplicated, which keeps this out of `dex_trader.py`.

Read it with `dashboard_data.py`.
"""
import json
import logging
import os
from datetime import datetime

LEDGER_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "logs", "events.jsonl"
)

# Its own logger, with propagate=False so these lines never reach the console or
# bot.log. This is why the original TradeFilter approach was dropped: a logging
# Filter selects records FOR a handler, it does not hide them FROM the others, so
# filtering on the trade handler alone would have echoed every JSON blob into
# both the console and bot.log.
_log = logging.getLogger("copytrader.ledger")
_log.propagate = False
_log.setLevel(logging.INFO)


def _sink() -> logging.Logger:
    """Attach the file handler on first use, so importing this costs nothing."""
    if not _log.handlers:
        os.makedirs(os.path.dirname(LEDGER_PATH), exist_ok=True)
        handler = logging.FileHandler(LEDGER_PATH, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        _log.addHandler(handler)
    return _log


def log_event(kind: str, **fields) -> None:
    """
    Append one event as a single JSON line.

    Never raises. Bookkeeping must not be able to kill a live trade -- every call
    site here sits on the execution path, and a ledger is not worth a position.
    """
    try:
        payload = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "kind": kind,
        }
        payload.update(fields)
        # default=str for datetimes; ensure_ascii=False because tickers on this
        # channel are routinely CJK and \uXXXX escapes make the file unreadable.
        _sink().info(json.dumps(payload, default=str, ensure_ascii=False))
    except Exception as exc:
        logging.getLogger("copytrader").warning(
            f"ledger write failed for {kind}: {exc}"
        )


def read_events(path: str = LEDGER_PATH) -> list:
    """Every event, oldest first. Skips corrupt lines rather than failing."""
    if not os.path.exists(path):
        return []
    events = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
    return events
