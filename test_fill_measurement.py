#!/usr/bin/env python3
"""
Self-check for buy fill measurement (dex_trader._safe_token_balance / _measure_fill).

These decide the token count that becomes a position's entry-price denominator, so
getting them wrong reintroduces the instant-stop-loss bug. No network, no framework:
    python test_fill_measurement.py
"""
import asyncio, sys, types

from config import Config
from dex_trader import DexTrader


def trader_with(balances):
    """DexTrader with only the bits _measure_fill touches; `balances` is popped per call."""
    t = object.__new__(DexTrader)
    t.config = Config()
    seq = list(balances)

    class FakeChain:
        async def get_token_balance(self, _addr):
            v = seq.pop(0)
            if isinstance(v, Exception):
                raise v
            return v

    t.chain = FakeChain()
    return t


async def main():
    fails = []

    def check(name, got, want):
        ok = abs(got - want) < 1e-9
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got}, want {want}")
        if not ok:
            fails.append(name)

    # Normal fill: bought 250 on top of an empty wallet.
    t = trader_with([250.0])
    check("empty wallet, 250 filled", await t._measure_fill("0xT", 0.0), 250.0)

    # Pre-existing balance must not be counted as part of this fill.
    t = trader_with([1000.0])
    check("held 900, bought 100", await t._measure_fill("0xT", 900.0), 100.0)

    # A reverted buy that delivered nothing.
    t = trader_with([5.0])
    check("no tokens delivered", await t._measure_fill("0xT", 5.0), 0.0)

    # Never negative, even if the balance moved down between reads.
    t = trader_with([3.0])
    check("balance shrank", await t._measure_fill("0xT", 10.0), 0.0)

    # Transient RPC error is retried, not fatal.
    t = trader_with([RuntimeError("rpc"), 42.0])
    check("retries a flaky read", await t._measure_fill("0xT", 0.0), 42.0)

    # Post-trade read fails entirely -> 0.0, and the caller must surface it.
    t = trader_with([RuntimeError("x")] * 3)
    check("unreadable after trade", await t._measure_fill("0xT", 0.0), 0.0)

    # Pre-trade balance unknown -> size from the full balance rather than
    # discarding a position the wallet already paid for.
    t = trader_with([77.0])
    check("unknown pre-trade balance", await t._measure_fill("0xT", None), 77.0)

    # _safe_token_balance must never raise.
    t = trader_with([RuntimeError("a"), RuntimeError("b"), RuntimeError("c")])
    got = await t._safe_token_balance("0xT")
    ok = got is None
    print(f"  {'PASS' if ok else 'FAIL'}  never raises, returns None after 3 tries: {got}")
    if not ok:
        fails.append("safe_balance")

    print("\nFAILED: " + ", ".join(fails) if fails else "\nAll fill-measurement checks passed")
    return 1 if fails else 0


sys.exit(asyncio.run(main()))
