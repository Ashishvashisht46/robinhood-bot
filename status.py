#!/usr/bin/env python3
"""
Is anything unmanaged right now?

The dangerous state for this bot is not a crash -- it is holding tokens while
nothing is watching them. That happened overnight on 2026-09-12: the terminal
closed, the bot stopped, and a position sat unsold with no stop-loss running.

Answers three questions:
  1. Is the bot process actually running?
  2. Does the saved state think positions are open?
  3. Does the wallet hold tokens the bot is not tracking?

Read-only. Sells nothing.

    python status.py
"""
import asyncio
import json
import os
import re
import subprocess
import sys

from chain_client import ChainClient, ERC20_ABI
from config import Config
from dex_trader import DexTrader

STATE = os.path.join(os.path.dirname(__file__), "cache", "strategy_state.json")
LOG = os.path.join(os.path.dirname(__file__), "logs", "bot.log")
DUST = 1e-6


def bot_is_running() -> int:
    """
    Count running bot processes.

    The @() is load-bearing: PowerShell returns a bare object rather than an array
    when exactly one process matches, and .Count on that is empty. run_bot.bat
    launches exactly one python process, so without @() this reported NOT RUNNING
    for a perfectly healthy bot.
    """
    try:
        # Match on ExecutablePath as well as CommandLine. run_bot.bat cd's into the
        # project and launches a RELATIVE path, so the command line reads
        # '".venv\Scripts\python.exe"  main.py' with no project name in it. Matching
        # CommandLine alone reported NOT RUNNING while the bot was running -- which
        # invites starting a second instance, and two instances fight over the
        # Telegram session so neither receives calls.
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


def tokens_seen_in_log() -> set:
    """Every token the bot has ever touched -- the candidates for a stranded balance."""
    try:
        with open(LOG, encoding="utf-8", errors="ignore") as f:
            return set(re.findall(r"0x[a-fA-F0-9]{40}", f.read()))
    except Exception:
        return set()


async def main() -> int:
    config = Config()
    chain = ChainClient(config)
    trader = DexTrader(chain, config)
    problems = []

    running = bot_is_running()
    print(f"bot process      : {'RUNNING' if running > 0 else 'NOT RUNNING'}"
          f"{'' if running < 0 else f' ({running} procs)'}")

    tracked = []
    if os.path.exists(STATE):
        try:
            data = json.load(open(STATE, encoding="utf-8"))
            tracked = data.get("open_positions", [])
            print(f"saved state      : balance ${data.get('balance_usd', 0):.2f} | "
                  f"{data.get('total_trades', 0)} trades | {data.get('state')} | "
                  f"{len(tracked)} open position(s)")
        except Exception as e:
            print(f"saved state      : UNREADABLE ({e})")
            problems.append("state file is corrupt")
    else:
        print("saved state      : none (clean)")

    if not chain.account:
        print("\nNo usable private key -- cannot check wallet balances.")
        return 1

    eth = await chain.get_eth_balance()
    usd = await chain.get_eth_price_usd()
    print(f"wallet           : {chain.account.address}")
    print(f"ETH              : {eth:.6f} (${eth * usd:.2f})")

    tracked_addrs = {p.get("contract_address", "").lower() for p in tracked}
    reserved = getattr(config, "RESERVED_TOKENS", set())
    held = []
    for addr in tokens_seen_in_log():
        try:
            cs = chain.w3.to_checksum_address(addr)
            c = chain.w3.eth.contract(address=cs, abi=ERC20_ABI)
            raw = c.functions.balanceOf(chain.account.address).call()
            if raw <= 0:
                continue
            dec = c.functions.decimals().call()
            amount = raw / 10 ** dec
            if amount < DUST:
                continue
            held.append((cs, amount, cs.lower() in tracked_addrs, cs.lower() in reserved))
        except Exception:
            continue

    print()
    if not held:
        print("tokens held      : none")
    else:
        print("tokens held:")
        for cs, amount, is_tracked, is_reserved in sorted(held, key=lambda x: -x[1]):
            try:
                price = await trader.get_token_price_eth(cs)
                value = f"${price * amount * usd:.2f}"
            except Exception:
                value = "?"
            tag = "tracked" if is_tracked else ("reserved" if is_reserved else "UNTRACKED")
            print(f"   {cs}  {amount:>16,.6f}  {value:>8}  {tag}")
            if not is_tracked and not is_reserved:
                problems.append(f"{cs} held but not in the bot's open positions")

    print()
    if tracked and running <= 0:
        problems.append(f"{len(tracked)} position(s) open but the bot is NOT running "
                        f"-- no stop-loss, no take-profit")

    if problems:
        print("PROBLEMS:")
        for p in problems:
            print(f"   - {p}")
        print()
        print("To exit a token the bot is not managing:")
        print("   .venv\\Scripts\\python.exe verify_encoders.py --broadcast "
              "--i-understand --sell-only --tokens <address>")
        return 1

    print("OK: nothing unmanaged.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
