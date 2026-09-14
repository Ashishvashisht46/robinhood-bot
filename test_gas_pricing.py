#!/usr/bin/env python3
"""
Self-check for transaction fee pricing.

TP1 sells were failing with "max fee per gas less than block base fee" -- the
ladder silently stopped taking profit. Two causes, both only visible at runtime:

  1. Callers priced the transaction BEFORE simulating it, and this chain's base
     fee only ever climbs (empty blocks still drift it up ~0.3% each instead of
     decaying 12.5%). A 1.25x ceiling was overtaken before the send.
  2. `send_transaction` had the correct late pricing, but skipped it whenever
     maxFeePerGas was already set -- which every caller did. Dead code.

Nothing is broadcast: see Blocked/arm() below -- the guard sits on _retry,
because patching w3 is defeated by RPC rotation.

    python test_gas_pricing.py
"""
import asyncio
import os
import sys

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from chain_client import ChainClient, eip1559_fees  # noqa: E402
from config import Config  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

fails = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{'' if ok else '  -> ' + detail}")
    if not ok:
        fails.append(name)


class Blocked(BaseException):
    """
    Deliberately a BaseException, not Exception.

    First attempt at this test raised RuntimeError from a patched
    w3.eth.send_raw_transaction. ChainClient._retry caught it as a network error,
    ROTATED TO A DIFFERENT RPC -- which rebuilds self.w3 and threw the patch away --
    and then really did try to broadcast. Only a stale nonce stopped it.

    So the block has to sit on _retry itself, which rotation does not replace, and
    it has to be something `except Exception` cannot swallow.
    """


def arm(chain):
    """Make any attempt to broadcast abort the test instead of reaching the chain."""
    original = chain._retry

    async def guarded(func, *args, **kwargs):
        if getattr(func, "__name__", "") == "send_raw_transaction":
            raise Blocked
        return await original(func, *args, **kwargs)

    chain._retry = guarded


async def price(chain, tx):
    try:
        await chain.send_transaction(tx)
    except Blocked:
        pass
    return tx


def main() -> int:
    config = Config()
    config.DRY_RUN = False  # force the real signing path, not the dry-run early return
    chain = ChainClient(config)
    if not chain.account:
        print("no private key configured -- cannot test signing")
        return 1

    arm(chain)

    base = chain.w3.eth.get_block("latest")["baseFeePerGas"]
    me = chain.account.address
    print(f"  live base fee: {base:,}\n")

    max_fee, priority = eip1559_fees(chain.w3)
    check("cap is >= 2x the base fee", max_fee >= base * 2, f"{max_fee} vs {base}")
    check("cap covers base + tip", max_fee > base + priority)
    check("tip is non-zero", priority > 0)

    # The exact production failure: a cap computed earlier, now below the base fee.
    stale = int(base * 0.98)
    tx = asyncio.run(price(chain, {
        "from": me, "to": me, "value": 0, "gas": 21000,
        "maxFeePerGas": stale, "maxPriorityFeePerGas": 1_000_000,
    }))
    check("a stale cap below the base fee is overwritten", tx["maxFeePerGas"] > stale,
          f"still {tx['maxFeePerGas']}")
    check("overwritten cap clears the base fee", tx["maxFeePerGas"] > base)

    # A caller that sets nothing must still get priced.
    tx = asyncio.run(price(chain, {"from": me, "to": me, "value": 0, "gas": 21000}))
    check("unpriced transaction gets a cap", tx.get("maxFeePerGas", 0) > base)
    check("unpriced transaction gets a tip", tx.get("maxPriorityFeePerGas", 0) > 0)
    check("no legacy gasPrice is mixed in", "gasPrice" not in tx)

    # A caller that deliberately chose legacy pricing keeps it.
    tx = asyncio.run(price(chain, {
        "from": me, "to": me, "value": 0, "gas": 21000, "gasPrice": base * 3}))
    check("explicit legacy gasPrice is respected", "maxFeePerGas" not in tx)

    print()
    if fails:
        print(f"FAILED: {', '.join(fails)}")
        return 1
    print("All gas pricing checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
