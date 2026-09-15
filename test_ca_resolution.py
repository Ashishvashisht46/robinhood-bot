#!/usr/bin/env python3
"""
Self-check for contract-address resolution.

The channel posts calls with no CA in the text, so where the address comes from
decides which token gets bought. A live search for "ROACH" on this chain returns
SIX different tokens, so resolving by ticker can buy a clone. No network here --
the DexScreener response is stubbed.

    python test_ca_resolution.py
"""
import io
import json
import sys
import urllib.request
from datetime import datetime

from message_parser import MessageParser

REAL = "0x1A3Db13103F811D01ebfd19aEd1DC64Ffd1Ba8F5"
CLONE = "0xdEAdBeEf00000000000000000000000000000001"
OTHER = "0xdEAdBeEf00000000000000000000000000000002"

CALL = """EARLY CALL - $ROACH . robinhood
called at $25k
Pool Info
DEX: Pons V2
Mcap: $29k
Liq: $14k | 47.8%
Tax: B 0% | S 0%
Token Info
Age: 4m . pons_v2
Holders: 173
Vol 24h: $102k
205 swaps (5m)"""

fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"        got  {got}")
        print(f"        want {want}")
        fails.append(name)


def pair(symbol, address, liq, chain="robinhood"):
    return {"chainId": chain,
            "baseToken": {"symbol": symbol, "address": address},
            "liquidity": {"usd": liq}}


def stub(pairs):
    urllib.request.urlopen = lambda *a, **k: io.BytesIO(
        json.dumps({"pairs": pairs}).encode())


class FakeBtn:
    def __init__(self, text, url):
        self.text, self.url = text, url


def resolve(allow=True, buttons=None, reply_markup=None):
    sig = MessageParser(allow_ticker_fallback=allow).parse(
        text=CALL, entities=None, message_id=1, timestamp=datetime.now(),
        buttons=buttons, reply_markup=reply_markup)
    return sig.contract_address if sig else None


class FakeRow:
    def __init__(self, buttons):
        self.buttons = buttons


class FakeMarkup:
    def __init__(self, rows):
        self.rows = rows


def main():
    original = urllib.request.urlopen
    try:
        print("")
        print("Ticker lookup, when explicitly enabled:")

        stub([pair("ROACH", REAL, 14000)])
        check("single exact match", resolve(), REAL)

        # A clone with the same ticker must not win on ordering alone.
        stub([pair("ROACH", CLONE, 50), pair("ROACH", REAL, 14000)])
        check("ambiguous same-ticker tokens rejected", resolve(), None)

        # The dangerous old behaviour: no symbol match, so it fell back to "any
        # robinhood pair" and bought a completely different token.
        stub([pair("PEPE", OTHER, 900000)])
        check("different ticker is NOT substituted", resolve(), None)

        stub([pair("ROACH", OTHER, 500000, chain="ethereum")])
        check("other chains ignored", resolve(), None)

        # Not indexed yet -- a 4-minute-old token. Skip rather than guess.
        stub([])
        check("no results -> no address", resolve(), None)

        stub([pair("ROACH", "0x1234", 99999)])
        check("malformed address rejected", resolve(), None)

        print("")
        print("The safe default -- ticker lookup disabled:")
        stub([pair("ROACH", REAL, 14000)])
        check("disabled -> no address, call is skipped", resolve(allow=False), None)

        print("")
        print("Buttons are the real path, and work with the fallback off:")
        stub([])
        btns = [[FakeBtn("GMGN", "https://gmgn.ai/robinhood/token/" + REAL)]]
        check("address read from a button URL", resolve(allow=False, buttons=btns), REAL)

        # Documents real behaviour rather than ideal behaviour: the parser takes
        # the FIRST address it sees, so button ORDER decides whether you get the
        # token or a DexScreener pool address.
        btns = [[FakeBtn("DexS", "https://dexscreener.com/robinhood/" + OTHER),
                 FakeBtn("GMGN", "https://gmgn.ai/robinhood/token/" + REAL)]]
        check("token link outranks chart pool regardless of order", resolve(allow=False, buttons=btns), REAL)

        print("")
        print("Telethon populates message.buttons from an entity cache and returns")
        print("None on a miss, so reply_markup is the path that must not break:")

        # Live evidence, 2026-09-14 msg 7707: buttons=NO but reply_markup=yes.
        # If reply_markup is the only source, it has to resolve.
        rm = FakeMarkup([FakeRow([FakeBtn("GMGN", "https://gmgn.ai/robinhood/token/" + REAL)])])
        check("address read from reply_markup when buttons is None",
              resolve(allow=False, reply_markup=rm), REAL)

        # rows/buttons can be present but None. This raised TypeError, and the
        # outer handler swallowed it as a parse failure -- which also skipped the
        # entities and raw-text fallbacks underneath.
        check("reply_markup.rows = None does not crash",
              resolve(allow=False, reply_markup=FakeMarkup(None)), None)
        check("row.buttons = None does not crash",
              resolve(allow=False, reply_markup=FakeMarkup([FakeRow(None)])), None)

        print("")
        if fails:
            print("FAILED: " + ", ".join(fails))
        else:
            print("All contract-address resolution checks passed")
        return 1 if fails else 0
    finally:
        urllib.request.urlopen = original


sys.exit(main())
