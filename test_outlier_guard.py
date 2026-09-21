#!/usr/bin/env python3
"""
A position that cannot be priced must not be left open.

Live, a $50 entry was quoted against the wrong Uniswap V4 pool key. Every later
price read then looked ~50x off, the monitor rejected each one as an outlier and
returned. It did that 1,717 times across 110 minutes. For those 110 minutes the
position had no stop-loss, no take-profit, and nothing above WARNING to say so.
It ended +9x, which is the only reason it looked like good news; the identical
failure on a token going the other way holds it to zero.

These drive the REAL PositionMonitor._tick. A test that reimplements the logic
it is checking only proves the same bug can be written twice.

    python test_outlier_guard.py
"""
import asyncio
import sys
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"          got {got!r} want {want!r}")
        fails.append(name)


class Pos:
    def __init__(self):
        self.id = "p1"
        self.ticker = "TEST"
        self.contract_address = "0x" + "ab" * 20
        self.entry_price_eth = 1e-10        # what the (wrong) fill implied
        self.tokens_bought = 1000.0
        self.remaining_tokens = 1000.0
        self.peak_multiplier = 1.0
        self.trailing_stop_multiplier = 0.5
        self.pending_exit = None
        self.tp1_hit = self.tp2_hit = False
        self.completed_sell_operations = []
        self.tx_hash_sell = ""
        self.entry_time = datetime.now(timezone.utc)
        self.accumulated_pnl_usd = 0.0
        self.gas_cost_usd = 0.0
        self.pnl_usd = 0.0
        self.status = None


class Cfg:
    SL_MULTIPLIER = 0.5
    TRAILING_STOP_DELTA = 0.2
    RUNNER_TIMEOUT_MINUTES = 60
    STAGNANT_TIMEOUT_MINUTES = 30
    TP1_MULTIPLIER = 1.2
    TP2_MULTIPLIER = 1.4
    TP1_SELL_PCT = 40
    TP2_SELL_PCT = 40
    FINAL_TARGET_MULTIPLIER = 5.0


BAD_PRICE = 1e-8            # 100x the entry -> always an "outlier" at peak 1.0
GOOD_PRICE = 1.1e-10        # 1.1x the entry -> perfectly normal


def build(price_source):
    """A monitor with the chain and the exit path stubbed, everything else real."""
    from position_monitor import PositionMonitor
    mon = PositionMonitor.__new__(PositionMonitor)
    mon._outlier_streak = {}
    mon._stopping = False
    mon.config = Cfg()
    mon._persist = lambda position: None
    exits = []

    async def fake_exit(position):
        exits.append(dict(position.pending_exit))
        position.pending_exit = None
        position.remaining_tokens = 0.0
        return True

    mon._execute_exit = fake_exit

    closed = []

    async def fake_close(position, reason, multiplier):
        closed.append(reason)
        return True

    mon._close = fake_close

    class FakeTrader:                       # no .operations -> guard stays None
        async def get_token_price_eth(self, _addr):
            return price_source()

    mon.dex_trader = FakeTrader()
    return mon, exits


def main() -> int:
    from position_monitor import OUTLIER_STREAK_LIMIT

    print(f"the guard gives up after {OUTLIER_STREAK_LIMIT} bad reads, not never:")
    mon, exits = build(lambda: BAD_PRICE)
    pos = Pos()
    res = [asyncio.run(mon._tick(pos)) for _ in range(OUTLIER_STREAK_LIMIT)]
    check(f"first {OUTLIER_STREAK_LIMIT - 1} reads rejected, position untouched",
          res[:OUTLIER_STREAK_LIMIT - 1], [False] * (OUTLIER_STREAK_LIMIT - 1))
    check("the streak limit triggers an exit", len(exits), 1)
    check("it closes the WHOLE remaining size", exits[-1]["tokens"], 1000.0)
    check("and records why", "unpriceable" in exits[-1]["reason"], True)

    print("\nit does not loop forever (the actual bug):")
    mon, exits = build(lambda: BAD_PRICE)
    pos = Pos()
    res = [asyncio.run(mon._tick(pos)) for _ in range(50)]
    check("50 bad reads do NOT yield 50 silent rejections",
          res.count(False) < 50, True)
    check("the position was exited", len(exits) >= 1, True)
    check("exit happened at the limit, not later",
          res.index(True), OUTLIER_STREAK_LIMIT - 1)

    print("\na good price is still handled normally:")
    mon, exits = build(lambda: GOOD_PRICE)
    pos = Pos()
    asyncio.run(mon._tick(pos))
    check("no exit on a sane price", len(exits), 0)
    check("peak updated from the real reading",
          round(pos.peak_multiplier, 2), 1.1)

    print("\nan intermittent bad tick must NOT close the position:")
    seq = [BAD_PRICE, GOOD_PRICE, BAD_PRICE, GOOD_PRICE, BAD_PRICE, GOOD_PRICE]
    it = iter(seq)
    mon, exits = build(lambda: next(it))
    pos = Pos()
    for _ in seq:
        asyncio.run(mon._tick(pos))
    check("alternating good/bad never reaches the streak limit", len(exits), 0)

    print()
    if fails:
        print(f"FAILED: {', '.join(fails)}")
        return 1
    print("All outlier-guard checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
