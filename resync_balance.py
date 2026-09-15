#!/usr/bin/env python3
"""
Teach the bot about money you added to (or removed from) the wallet.

    python resync_balance.py            # show what it would change
    python resync_balance.py --apply    # change it

The strategy engine does NOT read your wallet. `balance_usd` starts at
INITIAL_CAPITAL_USD and only ever moves by realised P&L, so a deposit is
invisible to it. Every trade is gated on that internal number:

    strategy_engine.py:169
    if balance_usd - reserved - stake - gas_reserve < SAFETY_FLOOR_USD:
        "Not enough balance above floor"

So after funding the wallet the bot keeps refusing calls, with a balance that
looks fine on-chain. This resyncs the internal figure to the real one.

Native ETH only. Tokens are not counted: the engine stakes ETH, and anything in
RESERVED_TOKENS is deliberately not for spending.

Refuses to run while the bot is up -- it holds this state in memory and rewrites
the file, so an edit underneath it is silently lost.
"""
import asyncio
import json
import os
import subprocess
import sys

from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))

from chain_client import ChainClient  # noqa: E402
from config import Config  # noqa: E402

STATE = os.path.join(HERE, "cache", "strategy_state.json")
ENV = os.path.join(HERE, ".env")

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def bot_running() -> int:
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "@(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             "Where-Object { $_.CommandLine -like '*main.py*' -and "
             "($_.ExecutablePath -like '*robinhood-bot*' -or "
             "$_.CommandLine -like '*robinhood-bot*') }).Count"],
            capture_output=True, text=True, timeout=30)
        return int((out.stdout or "0").strip() or 0)
    except Exception:
        return -1


def set_env_value(key: str, value: str) -> None:
    """Rewrite one key in .env, leaving comments and everything else alone."""
    lines = open(ENV, encoding="utf-8").read().splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[i] = f"{key}={value}\n"
            break
    else:
        lines.append(f"{key}={value}\n")
    tmp = ENV + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.writelines(lines)
    os.replace(tmp, ENV)


async def main() -> int:
    apply = "--apply" in sys.argv

    config = Config()
    chain = ChainClient(config)
    eth = await chain.get_eth_balance()
    price = await chain.get_eth_price_usd()
    wallet_usd = round(eth * price, 2)

    state = {}
    if os.path.exists(STATE):
        state = json.load(open(STATE, encoding="utf-8"))
    tracked = float(state.get("balance_usd", config.INITIAL_CAPITAL_USD))
    open_positions = state.get("open_positions", [])

    stake = config.BASELINE_STAKE_USD
    gas_reserve = getattr(config, "GAS_RESERVE_USD", 0) or 0
    floor = config.SAFETY_FLOOR_USD
    reserved = sum(p.get("stake_usd", 0) for p in open_positions)

    def verdict(balance):
        headroom = balance - reserved - stake - gas_reserve - floor
        return ("TRADES" if headroom >= 0 else "BLOCKED"), headroom

    was, was_room = verdict(tracked)
    now, now_room = verdict(wallet_usd)

    print(f"  wallet on-chain    {eth:.8f} ETH @ ${price:,.2f}  =  ${wallet_usd:.2f}")
    print(f"  bot thinks it has  ${tracked:.2f}")
    print(f"  difference         ${wallet_usd - tracked:+.2f}")
    print()
    print(f"  stake ${stake:.2f} | gas reserve ${gas_reserve:.2f} | "
          f"floor ${floor:.2f} | reserved by open positions ${reserved:.2f}")
    print(f"  before   {was:<8} headroom ${was_room:+.2f}")
    print(f"  after    {now:<8} headroom ${now_room:+.2f}")

    if now == "BLOCKED":
        print()
        print(f"  Resyncing alone will NOT unblock it. ${wallet_usd:.2f} is not enough")
        print(f"  for a ${stake:.2f} stake above a ${floor:.2f} floor. Add more funds, or")
        print(f"  lower SAFETY_FLOOR_USD / BASELINE_STAKE_USD.")

    if not apply:
        print("\n  dry run. Re-run with --apply to write it.")
        return 0

    running = bot_running()
    if running != 0:
        print()
        print(f"  REFUSING: the bot is running ({running} process(es)).")
        print("  It holds this state in memory and rewrites the file, so the edit")
        print("  would be silently lost. Stop it (ctrl+c in its window), re-run")
        print("  this, then start it again.")
        if open_positions:
            print(f"  NOTE: {len(open_positions)} position(s) open -- stopping leaves")
            print("  them with no stop-loss until you restart.")
        return 1

    state["balance_usd"] = wallet_usd
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
    os.replace(tmp, STATE)

    # The circuit breaker measures against this, and reset() restores to it.
    set_env_value("INITIAL_CAPITAL_USD", f"{wallet_usd:.2f}")

    print()
    print(f"  cache/strategy_state.json  balance_usd -> ${wallet_usd:.2f}")
    print(f"  .env  INITIAL_CAPITAL_USD  -> {wallet_usd:.2f}")
    print("\n  Done. Start the bot:  run_bot.bat")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
