#!/usr/bin/env python3
"""
Read-only Telegram channel inspector.

Answers the question the execution tests cannot: does the parser actually find
the right contract address in this channel's current message format?

It reads recent messages, shows every button URL, shows what MessageParser
extracts, and then checks on-chain whether that address is a tradeable token or
(the likely failure) a pool address from a DexScreener / GeckoTerminal link.

Sends nothing. Buys nothing. Needs TELEGRAM_API_ID / _HASH / _PHONE in .env.

    python inspect_channel.py            # last 20 messages
    python inspect_channel.py 50         # last 50
"""
import asyncio
import sys

from telethon import TelegramClient

from chain_client import ChainClient, ERC20_ABI
from config import Config
from dex_trader import DexTrader
from message_parser import MessageParser

OK, BAD, WARN = "  [OK]  ", "  [BAD] ", "  [WARN]"


async def describe_address(trader: DexTrader, addr: str) -> str:
    """Is this a tradeable token, or did we pick up a pool address?"""
    w3 = trader.w3
    try:
        code = await asyncio.to_thread(w3.eth.get_code, w3.to_checksum_address(addr))
    except Exception as e:
        return f"{BAD}cannot read {addr}: {e}"
    if not code:
        return f"{BAD}{addr} has no bytecode -- not a contract at all"

    token = w3.eth.contract(address=w3.to_checksum_address(addr), abi=ERC20_ABI)
    try:
        decimals = await asyncio.to_thread(token.functions.decimals().call)
        supply = await asyncio.to_thread(token.functions.totalSupply().call)
    except Exception:
        return (f"{BAD}{addr} is a contract but not an ERC20 -- this is almost "
                f"certainly a POOL address from a DexScreener/Gecko link, not the token")

    venue, target, quote, fee = await trader.detect_venue_and_route(addr)
    if venue == "NONE":
        return (f"{WARN}{addr} is an ERC20 (decimals={decimals}, supply={supply}) "
                f"but NO tradeable pool was found -- the bot would skip this call")
    return (f"{OK}{addr} is a token, venue={venue} quote={quote} fee={fee} "
            f"-- the bot could trade this")


async def main(limit: int) -> int:
    config = Config()
    if not config.TELEGRAM_API_ID or not config.TELEGRAM_API_HASH:
        print("TELEGRAM_API_ID / TELEGRAM_API_HASH missing from .env -- fill those in first.")
        return 1

    parser = MessageParser()
    trader = DexTrader(ChainClient(config), config)

    client = TelegramClient("robinhood_copy_trader_session",
                            config.TELEGRAM_API_ID, config.TELEGRAM_API_HASH)
    await client.start(phone=config.TELEGRAM_PHONE or None)
    channel = await client.get_entity(config.CHANNEL_USERNAME)
    print(f"Reading the last {limit} messages from @{config.CHANNEL_USERNAME}\n")

    seen = parsed = tradeable = 0
    async for message in client.iter_messages(channel, limit=limit):
        text = message.message or ""
        if not text:
            continue
        seen += 1

        signal = parser.parse(
            text=text, entities=message.entities, message_id=message.id,
            timestamp=message.date, buttons=getattr(message, "buttons", None),
            reply_markup=getattr(message, "reply_markup", None),
        )
        if not signal:
            continue  # celebration post, or a format the parser does not match

        parsed += 1
        print("=" * 78)
        print(f"msg {message.id}  ${signal.ticker}   dex={signal.dex}")
        print(f"  liq=${signal.liquidity_usd}  holders={signal.holders}  "
              f"age={signal.token_age_minutes}m  tax B{signal.buy_tax}/S{signal.sell_tax}")

        # Every button URL, in the order the parser scans them.
        print("  buttons (scanned in this order):")
        rows = getattr(message, "buttons", None) or []
        for row in rows:
            for btn in (row if isinstance(row, list) else [row]):
                label = getattr(btn, "text", "?")
                url = getattr(btn, "url", None) or getattr(btn, "data", None)
                print(f"    {label:<14} {url}")

        if not signal.contract_address:
            print(f"{BAD}no contract address found -- the bot would skip this call")
            continue

        print(f"  parser picked: {signal.contract_address}")
        verdict = await describe_address(trader, signal.contract_address)
        print(verdict)
        if verdict.startswith(OK):
            tradeable += 1

    print("\n" + "=" * 78)
    print(f"messages read: {seen}   parsed as calls: {parsed}   actually tradeable: {tradeable}")
    if parsed == 0:
        print("\nNothing parsed. The channel's format no longer matches message_parser.py --")
        print("no execution fix will make the bot trade until that is corrected.")
    elif tradeable < parsed:
        print(f"\n{parsed - tradeable} call(s) parsed but were not tradeable. Check whether the")
        print("parser grabbed a pool address from a DexScreener/Gecko button instead of the token.")
    await client.disconnect()
    return 0


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    sys.exit(asyncio.run(main(n)))
