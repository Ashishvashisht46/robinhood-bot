#!/usr/bin/env python3
"""
Self-check for open-position persistence and restart recovery.

Open positions used to live only in memory, so a crash left the tokens on-chain
with no stop loss and no take-profit. These cover the round trip and the two
resume behaviours that silently lose money if they regress. No network:

    python test_position_persistence.py
"""
import asyncio, json, os, sys, tempfile
from datetime import datetime

from config import Config
from models import Position, PositionStatus
from strategy_engine import StrategyEngine
from position_monitor import PositionMonitor

fails = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'' if cond else f'  <- {detail}'}")
    if not cond:
        fails.append(name)


def mid_ladder_position():
    """A position that has taken TP1 and is riding to TP2 -- the risky resume case."""
    return Position(
        ticker="PEPE", contract_address="0xTOKEN",
        entry_price_eth=1.5e-9, entry_price_usd=0.000004,
        tokens_bought=1_000_000.0, stake_usd=2.0, stake_eth=0.0008,
        tx_hash_buy="0xabc", status=PositionStatus.OPEN,
        tp1_hit=True, tp1_sold_tokens=400_000.0, tp1_pnl_usd=0.16,
        peak_multiplier=1.31, trailing_stop_multiplier=0.95,
        remaining_tokens=600_000.0, accumulated_pnl_usd=0.16,
    )


def engine_on(path):
    cfg = Config()
    eng = StrategyEngine(cfg, load_state=False)
    eng._state_file = path
    return eng


def test_roundtrip():
    print("\nSerialisation round trip:")
    p = mid_ladder_position()
    back = Position.from_dict(json.loads(json.dumps(p.to_dict())))
    check("survives JSON", back.id == p.id and back.ticker == p.ticker)
    check("entry price preserved", back.entry_price_eth == p.entry_price_eth)
    check("tp1_hit preserved", back.tp1_hit is True)
    check("remaining_tokens preserved", back.remaining_tokens == 600_000.0)
    check("trailing stop preserved", back.trailing_stop_multiplier == 0.95)
    check("enum restored", back.status is PositionStatus.OPEN)
    check("datetime restored", isinstance(back.entry_time, datetime))


def test_persist_across_restart():
    print("\nPersistence across a restart:")
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "state.json")
        a = engine_on(path)
        a.add_position(mid_ladder_position())
        check("written to disk", os.path.exists(path))

        b = engine_on(path)
        b._load_state()
        check("one position restored", len(b.open_positions) == 1, f"got {len(b.open_positions)}")
        if b.open_positions:
            r = b.open_positions[0]
            check("mid-ladder state intact", r.tp1_hit and r.remaining_tokens == 600_000.0)
            check("not re-sellable as fresh", r.tp1_sold_tokens == 400_000.0)

        # Closing it must clear it, or the next restart resurrects a dead position.
        b.remove_position(b.open_positions[0].id)
        c = engine_on(path)
        c._load_state()
        check("closed position does not come back", len(c.open_positions) == 0)


def test_corrupt_entry_is_skipped():
    print("\nA corrupt entry must not take down startup:")
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "state.json")
        json.dump({"balance_usd": 100.0, "state": "BASELINE",
                   "open_positions": [{"ticker": "GOOD", "contract_address": "0x1",
                                       "remaining_tokens": 5.0},
                                      {"entry_time": "not-a-date"}]},
                  open(path, "w"))
        e = engine_on(path)
        e._load_state()
        check("good entry kept, bad one skipped", len(e.open_positions) == 1,
              f"got {len(e.open_positions)}")


async def test_resume_keeps_ratcheted_stop():
    print("\nResuming must not un-ratchet the stop:")
    cfg = Config()
    cfg.PRICE_POLL_SECONDS = 0

    class FakeDex:
        async def get_token_price_eth(self, _addr):
            return 1.5e-9 * 1.30          # 1.30x: above the 0.95x stop, below TP2
        async def sell_token(self, *a, **k):
            raise AssertionError("resume must not trigger a sell")

    pos = mid_ladder_position()
    mon = PositionMonitor(cfg, FakeDex(), None, on_position_closed=lambda *a: None)
    task = asyncio.create_task(mon._monitor(pos))
    await asyncio.sleep(0.2)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    check("stop not reset to 0.50x", pos.trailing_stop_multiplier >= 0.95,
          f"stop is {pos.trailing_stop_multiplier}")
    check("tp1 still marked hit", pos.tp1_hit is True)
    check("peak not reset below entry", pos.peak_multiplier >= 1.30,
          f"peak is {pos.peak_multiplier}")


async def main():
    test_roundtrip()
    test_persist_across_restart()
    test_corrupt_entry_is_skipped()
    await test_resume_keeps_ratcheted_stop()
    print("\nFAILED: " + ", ".join(fails) if fails else "\nAll position-persistence checks passed")
    return 1 if fails else 0


sys.exit(asyncio.run(main()))
