#!/usr/bin/env python3
"""
Encoder verification harness.

Answers one question: is the chain knowledge in this repo -- the hardcoded
addresses, the V4 pool keys, the Universal Router calldata encoding -- still
valid against live chain 4663?

Four tiers, cheapest first. Only tier 3 costs money.

  0  addresses   eth_getCode on every hardcoded contract    free, no wallet
  1  pool keys   does the pool the BUY path targets exist?  free, no wallet
  2  calldata    eth_estimateGas on real encoder output     free, needs --from (an
                                                            ADDRESS only, no key)
  3  live swap   broadcast a real swap                      SPENDS REAL ETH

Tiers 0-2 send nothing and need no private key. Run those first; if they fail,
tier 3 only burns gas confirming what you already know.

Usage:
    python verify_encoders.py                             # tiers 0-1
    python verify_encoders.py --from 0xYourWallet         # tiers 0-2
    python verify_encoders.py --from 0xYou --tokens 0xA,0xB
    python verify_encoders.py --broadcast --i-understand  # tier 3, spends ETH

Exit code is 0 only if every attempted check passed.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any, Optional

from web3 import Web3

import dex_trader as dt
from chain_client import ERC20_ABI, ChainClient
from config import Config
from dex_trader import DexTrader
from stock_v4_routes import (
    _V4_PARAM_CACHE,
    STOCK_TOKENS,
    V4_FEE_TICKS_EXTENDED,
    V4_HOOKS,
    pool_id_from_key,
)

PASS, FAIL, WARN, SKIP, INFO = "[PASS]", "[FAIL]", "[WARN]", "[SKIP]", "  ... "
NATIVE = "0x0000000000000000000000000000000000000000"

# Every address the bot will call on chain 4663. If one of these has no bytecode
# the chain layer is stale and nothing downstream can work.
CONTRACTS = {
    "WETH": dt.WETH_ADDRESS,
    "USDG": dt.USDG_ADDRESS,
    "PERMIT2": dt.PERMIT2_ADDRESS,
    "UNIVERSAL_ROUTER": dt.UNIVERSAL_ROUTER_ADDR,
    "UNISWAP_V2_FACTORY": dt.UNISWAP_V2_FACTORY,
    "UNISWAP_V2_ROUTER": dt.UNISWAP_V2_ROUTER,
    "UNISWAP_V3_FACTORY": dt.UNISWAP_V3_FACTORY,
    "UNISWAP_V3_SWAPROUTER02": dt.UNISWAP_V3_SWAPROUTER02,
    "UNISWAP_V3_QUOTER_V2": dt.UNISWAP_V3_QUOTER_V2,
    "UNISWAP_V4_POOL_MGR": dt.UNISWAP_V4_POOL_MGR,
    "UNISWAP_V4_STATE_VIEW": dt.UNISWAP_V4_STATE_VIEW,
    "UNISWAP_V4_QUOTER": dt.UNISWAP_V4_QUOTER,
    "PONS_V2_FACTORY": dt.PONS_V2_FACTORY,
    "PONS_V2_HOOK": dt.PONS_V2_HOOK,
    "BAGS_FACTORY": dt.BAGS_FACTORY,
    "BAGS_LENS": dt.BAGS_LENS,
    "BAGS_HOOK": dt.BAGS_HOOK,
}

# config.py declares its own copies of three addresses that disagree with
# dex_trader.py's. Nothing reads the config.py ones today, but the drift is a
# signal that one of the two sets was updated and the other was not.
CONFLICTS = {
    "PONS_V2_FACTORY": (Config.PONS_V2_FACTORY, dt.PONS_V2_FACTORY),
    "UNIVERSAL_ROUTER": (Config.UNISWAP_ROUTER, dt.UNIVERSAL_ROUTER_ADDR),
    "WETH": (Config.WETH_ADDRESS, dt.WETH_ADDRESS),
}


class Report:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.warned = 0

    def ok(self, msg: str) -> None:
        self.passed += 1
        print(f"{PASS} {msg}")

    def bad(self, msg: str) -> None:
        self.failed += 1
        print(f"{FAIL} {msg}")

    def warn(self, msg: str) -> None:
        self.warned += 1
        print(f"{WARN} {msg}")

    @staticmethod
    def skip(msg: str) -> None:
        print(f"{SKIP} {msg}")

    @staticmethod
    def note(msg: str) -> None:
        print(f"{INFO}{msg}")


def banner(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def short(reason: Any, limit: int = 110) -> str:
    text = " ".join(str(reason).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


# --------------------------------------------------------------------------
# Tier 0: are the hardcoded addresses real contracts on this chain?
# --------------------------------------------------------------------------
async def tier0_addresses(w3: Web3, rep: Report) -> None:
    banner("TIER 0  Hardcoded addresses (free, no wallet)")

    try:
        chain_id = await asyncio.to_thread(lambda: w3.eth.chain_id)
    except Exception as exc:
        rep.bad(f"RPC unreachable: {short(exc)}")
        return
    if chain_id == 4663:
        rep.ok(f"chain_id == 4663")
    else:
        rep.bad(f"chain_id == {chain_id}, expected 4663 -- wrong network, stop here")
        return

    for name, addr in CONTRACTS.items():
        try:
            code = await asyncio.to_thread(w3.eth.get_code, w3.to_checksum_address(addr))
        except Exception as exc:
            rep.bad(f"{name:<26} {addr}  lookup failed: {short(exc)}")
            continue
        if len(code) > 0:
            rep.ok(f"{name:<26} {addr}  ({len(code)} bytes)")
        else:
            rep.bad(f"{name:<26} {addr}  NO BYTECODE -- address is wrong or stale")

    for name, (cfg_val, dex_val) in CONFLICTS.items():
        if cfg_val.lower() != dex_val.lower():
            rep.warn(
                f"{name} disagrees between files: config.py={cfg_val} "
                f"dex_trader.py={dex_val} (dex_trader.py is the one actually used)"
            )


# --------------------------------------------------------------------------
# Tier 1: does the pool the buy path targets actually exist?
# --------------------------------------------------------------------------
def v4_pool_id(currency_a: str, currency_b: str, fee: int, tick: int, hook: str) -> str:
    a, b = Web3.to_checksum_address(currency_a), Web3.to_checksum_address(currency_b)
    c0, c1 = (a, b) if int(a, 16) < int(b, 16) else (b, a)
    return pool_id_from_key(c0, c1, fee, tick, hook)


async def v4_liquidity(trader: DexTrader, pool_id: str) -> Optional[int]:
    try:
        return int(await asyncio.to_thread(trader.state_view.functions.getLiquidity(pool_id).call))
    except Exception:
        return None


async def check_v4_token(trader: DexTrader, token: str, quote: str, fee: int, rep: Report) -> None:
    """
    The heart of the harness.

    build_v4_swap_tx() -> make_v4_swap_input() builds its PoolKey from
    self.weth_address (dex_trader.py:813). But Uniswap V4 addresses native ETH
    as address(0), and every native entry in _V4_PARAM_CACHE uses c0 = NATIVE.
    If the live pool is native-keyed, the WETH-keyed PoolKey hashes to a
    different poolId, that pool does not exist, and the swap reverts.

    So: derive both, ask the chain which one is real.
    """
    weth = trader.weth_address
    quote_is_ethish = quote.lower() in (weth.lower(), NATIVE.lower())

    # Ask the code what key it will actually use. _v4_params_for is now the single
    # resolver for buys, sells and pricing, so this is what all three will encode.
    guess_fee, guess_tick, guess_hook = await asyncio.to_thread(
        trader._v4_params_for, token, quote, fee
    )
    # The quote the resolver settled on, which legitimately differs from the one
    # detection reported when a deeper native pool exists.
    resolved_quote = trader.v4_resolved_quote(token, quote)
    rep.note(f"resolved key: fee={guess_fee} tick={guess_tick} hook={guess_hook} quote={resolved_quote}")

    if quote_is_ethish:
        # The native-vs-WETH question only exists for ETH-quoted pools.
        weth_liq = await v4_liquidity(trader, v4_pool_id(weth, token, guess_fee, guess_tick, guess_hook))
        native_liq = await v4_liquidity(trader, v4_pool_id(NATIVE, token, guess_fee, guess_tick, guess_hook))
        chosen, wants_weth = await asyncio.to_thread(
            trader._resolve_v4_currency_in, token, guess_fee, guess_tick, guess_hook
        )
        chosen_liq = weth_liq if wants_weth else native_liq

        if chosen_liq:
            rep.ok(
                f"encoder keys on {'WETH' if wants_weth else 'native address(0)'} and that "
                f"pool is live (liquidity={chosen_liq})"
            )
        elif weth_liq or native_liq:
            rep.bad(
                f"encoder keys on {'WETH' if wants_weth else 'native'} but the LIVE pool is "
                f"{'native' if native_liq else 'WETH'}-keyed -- buy will revert"
            )
        else:
            rep.warn("neither WETH- nor native-keyed pool exists at the resolved key")
    else:
        liq = await v4_liquidity(trader, v4_pool_id(resolved_quote, token, guess_fee, guess_tick, guess_hook))
        if liq:
            rep.ok(f"pool at the resolved key is live against {resolved_quote} (liquidity={liq})")
        else:
            rep.warn(f"no live pool at the resolved key against {resolved_quote}")

    # What does the SELL path find? It brute-forces the key; the buy path guesses.
    # A mismatch here is the asymmetry that makes buys fail while sells work.
    try:
        probed = await asyncio.to_thread(trader.stock_v4.probe_v4_key, token, resolved_quote)
    except Exception as exc:
        probed = None
        rep.note(f"probe_v4_key errored: {short(exc)}")

    if probed:
        same = (
            int(probed["fee"]) == int(guess_fee)
            and int(probed["tick"]) == int(guess_tick)
            and probed["hook"].lower() == str(guess_hook).lower()
        )
        if same:
            rep.ok("buy, sell and pricing all resolve to the same live key as the probe")
        else:
            rep.bad(
                f"resolver returned fee={guess_fee} tick={guess_tick} hook={guess_hook} but the "
                f"probe finds fee={probed['fee']} tick={probed['tick']} hook={probed['hook']} live"
            )
    else:
        rep.warn(
            f"probe_v4_key found no live pool across "
            f"{len(V4_FEE_TICKS_EXTENDED) * len(V4_HOOKS)} fee/tick/hook combos"
        )

    # The sell encoder has the same native-vs-WETH exposure and cannot be
    # estimateGas'd without holding the token, so verify the half that is
    # checkable: does the sell target a pool that actually exists?
    #
    # Only meaningful for ETH-quoted pools -- a stock- or USDG-quoted token exits
    # through a different route, so the single-hop-to-ETH branch never runs for it.
    if not quote_is_ethish:
        rep.skip(f"sell check skipped: quote is {quote}, not ETH -- different exit route")
        return

    try:
        sell_tx, sell_cmds = await asyncio.to_thread(
            trader.build_v4_sell_tx, token, 10**18, 1,
            trader.chain.account.address if trader.chain.account else weth,
            quote, guess_fee, guess_tick, guess_hook,
        )
    except Exception as exc:
        rep.bad(f"build_v4_sell_tx raised: {short(exc)}")
        return

    currency_out, wants_weth = await asyncio.to_thread(
        trader._resolve_v4_currency_in, token, guess_fee, guess_tick, guess_hook
    )
    sell_liq = await v4_liquidity(trader, v4_pool_id(currency_out, token, guess_fee, guess_tick, guess_hook))
    # Native: V4_SWAP only (TAKE_ALL credits ETH to the caller). WETH: V4_SWAP +
    # UNWRAP_WETH. Neither carries a PERMIT2_TRANSFER_FROM -- SETTLE_ALL pulls via
    # Permit2 itself, and adding 0x02 made the router pull twice.
    expected = "10" if not wants_weth else "100c"
    if sell_liq and sell_cmds == expected:
        rep.ok(
            f"sell encodes 0x{sell_cmds} "
            f"({'native, no Permit2 pull' if not wants_weth else 'UNWRAP_WETH'}) against a live pool"
        )
    elif not sell_liq:
        rep.bad(f"sell targets a pool with no liquidity (currency_out={currency_out})")
    else:
        rep.warn(f"sell commands 0x{sell_cmds}, expected 0x{expected}")


V3_POOL_ABI = [
    {"inputs": [], "name": "slot0",
     "outputs": [{"name": "sqrtPriceX96", "type": "uint160"}, {"name": "tick", "type": "int24"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "liquidity", "outputs": [{"type": "uint128"}],
     "stateMutability": "view", "type": "function"},
]
V2_PAIR_ABI = [
    {"inputs": [], "name": "getReserves",
     "outputs": [{"type": "uint112"}, {"type": "uint112"}, {"type": "uint32"}],
     "stateMutability": "view", "type": "function"},
]


async def target_is_a_pool(trader: DexTrader, target: str) -> bool:
    """
    get_token_price_eth() calls slot0() directly on whatever `target` detection
    handed back (dex_trader.py:1612-1617). Some detection branches return the
    ROUTER there instead of the pool. When that happens every price poll
    reverts, get_token_price_eth falls through to 0.0, and position_monitor
    skips the tick entirely (position_monitor.py:78-80) -- so TP/SL never fire.
    """
    pool = trader.w3.eth.contract(address=Web3.to_checksum_address(target), abi=V3_POOL_ABI)
    try:
        return (await asyncio.to_thread(pool.functions.slot0().call))[0] > 0
    except Exception:
        return False


async def check_v3_token(trader: DexTrader, token: str, target: str, quote: str,
                         fee: int, rep: Report) -> None:
    # Don't trust `target` -- resolve the pool from the factory ourselves, then
    # separately report whether `target` was usable for price polling.
    pool_addr = None
    for candidate_fee in [fee, 3000, 10000, 500, 100]:
        if candidate_fee is None:
            continue
        try:
            found = await asyncio.to_thread(
                trader.v3_factory.functions.getPool(
                    Web3.to_checksum_address(quote), Web3.to_checksum_address(token),
                    int(candidate_fee)).call
            )
        except Exception:
            continue
        if found and int(found, 16) != 0:
            pool_addr, fee = found, candidate_fee
            break

    if not pool_addr:
        rep.bad(f"no V3 pool exists for this token/quote pair at any standard fee tier")
        return

    pool = trader.w3.eth.contract(address=Web3.to_checksum_address(pool_addr), abi=V3_POOL_ABI)
    try:
        sqrt_price = (await asyncio.to_thread(pool.functions.slot0().call))[0]
        liq = await asyncio.to_thread(pool.functions.liquidity().call)
    except Exception as exc:
        rep.bad(f"V3 pool {pool_addr} does not answer slot0()/liquidity(): {short(exc)}")
        return

    if sqrt_price > 0 and liq > 0:
        rep.ok(f"V3 pool {pool_addr} live at fee={fee} (liquidity={liq})")
    elif sqrt_price > 0:
        rep.warn(f"V3 pool {pool_addr} initialised but has zero liquidity")
    else:
        rep.bad(f"V3 pool {pool_addr} exists but was never initialised (sqrtPriceX96=0)")

    if not await target_is_a_pool(trader, target):
        rep.warn(
            f"detection returned target={target}, which is a router, not a pool -- "
            f"`target` means different things depending on which detection branch "
            f"won (stock_v4_routes.py:256-261 vs check_v3_pool). Pricing no longer "
            f"trusts it, but anything else reading `target` as a pool will break"
        )


async def check_v2_token(trader: DexTrader, token: str, quote: str, rep: Report) -> None:
    try:
        pair = await asyncio.to_thread(
            trader.v2_factory.functions.getPair(
                Web3.to_checksum_address(quote), Web3.to_checksum_address(token)).call
        )
    except Exception as exc:
        rep.bad(f"getPair failed: {short(exc)}")
        return
    if not pair or int(pair, 16) == 0:
        rep.bad("no V2 pair exists for this token/quote pair")
        return
    contract = trader.w3.eth.contract(address=Web3.to_checksum_address(pair), abi=V2_PAIR_ABI)
    try:
        reserves = await asyncio.to_thread(contract.functions.getReserves().call)
    except Exception as exc:
        rep.bad(f"V2 pair {pair} does not answer getReserves(): {short(exc)}")
        return
    if reserves[0] > 0 and reserves[1] > 0:
        rep.ok(f"V2 pair {pair} live (reserves={reserves[0]}, {reserves[1]})")
    else:
        rep.bad(f"V2 pair {pair} has empty reserves")


async def check_onchain_price(trader: DexTrader, token: str, rep: Report) -> None:
    """
    Can the monitor actually price this token from chain state?

    get_token_price_eth() falls back to a DexScreener HTTP call on any failure,
    which masks a broken on-chain path behind a 3s network round trip on every
    2s poll -- and returns nothing at all for a token too fresh to be indexed.
    Stub the fallback out so this measures the on-chain path only.

    A 0.0 here means position_monitor.py:78-80 hits `continue` every tick and
    TP/SL never fire, no matter how well the buy executed.
    """
    original = dt.get_price_eth_dexscreener
    dt.get_price_eth_dexscreener = lambda *a, **k: 0.0
    try:
        price = await trader.get_token_price_eth(token)
    except Exception as exc:
        rep.bad(f"on-chain pricing raised: {short(exc)}")
        return
    finally:
        dt.get_price_eth_dexscreener = original

    if price > 0:
        rep.ok(f"on-chain price resolves ({price:.12g} ETH) -- monitor can evaluate TP/SL")
    else:
        rep.bad(
            "on-chain price is 0.0 -- every poll falls through to DexScreener, and "
            "if that misses too the monitor skips the tick, so TP/SL never fire"
        )


async def tier1_routes(trader: DexTrader, tokens: list[str], rep: Report) -> None:
    banner("TIER 1  Route + pool-key resolution (free, no wallet)")

    for token in tokens:
        print(f"\n--- {token}")
        try:
            venue, target, quote, fee = await trader.detect_venue_and_route(token)
        except Exception as exc:
            rep.bad(f"detect_venue_and_route raised: {short(exc)}")
            continue

        # A token with no venue has no market to price — not a pricing bug.
        if venue != "NONE":
            await check_onchain_price(trader, token, rep)

        rep.note(f"venue={venue} target={target} quote={quote} fee={fee}")
        if venue == "NONE":
            rep.warn("no venue detected -- buy_token would return 0 tokens for this token")
            continue

        if "V4" in venue or venue == "STOCK_PAIR":
            await check_v4_token(trader, token, quote, fee, rep)
        elif "UNISWAP_V3" in venue:
            await check_v3_token(trader, token, target, quote, fee, rep)
        elif "UNISWAP_V2" in venue:
            await check_v2_token(trader, token, quote, rep)
        elif "CURVE" in venue:
            code = await asyncio.to_thread(trader.w3.eth.get_code, Web3.to_checksum_address(target))
            (rep.ok if code else rep.bad)(f"curve contract {target} bytecode={len(code)} bytes")
        else:
            rep.skip(f"no pool check implemented for venue {venue}")


# --------------------------------------------------------------------------
# Tier 2: does the real encoder produce calldata the chain accepts?
# --------------------------------------------------------------------------
class SkipRoute(Exception):
    """This venue is not reachable as a single estimable transaction."""


def build_with_min_out(trader, venue, token, quote, fee, amount_wei, target, sender, min_out):
    """
    Build the buy tx the bot would actually send for this venue, at a given floor.
    Mirrors buy_token's branching so the harness tests the encoder really used.
    """
    if "UNISWAP_V3" in venue:
        return trader.build_v3_swap_tx(token, amount_wei, min_out, sender, quote, fee or 3000)

    if "V4" in venue or venue == "STOCK_PAIR":
        # Resolve first: a token detected as USDG- or stock-quoted is now routed
        # through its native pool when one exists, which makes it a single-hop
        # swap this harness CAN estimate. Only fall back to SkipRoute when no
        # native pool was found and the multi-hop path is genuinely required.
        v4_fee, v4_tick, v4_hook = trader._v4_params_for(token, quote, fee)
        resolved = trader.v4_resolved_quote(token, quote)
        if resolved.lower() == trader.usdg_address.lower():
            raise SkipRoute("USDG-quoted V4 with no native pool -- buy_token uses the "
                            "2-tx buy_usdg_two_tx path (atomic encoder is known-broken)")
        if resolved.lower() not in (trader.weth_address.lower(), NATIVE.lower()):
            raise SkipRoute(f"stock-quoted V4 ({resolved}) with no native pool -- "
                            f"buy_token uses the 2-tx StockV4Router path")
        return trader.build_v4_swap_tx(token, amount_wei, min_out, sender, resolved,
                                       fee=v4_fee, tick=v4_tick, hook=v4_hook)

    if "CURVE" in venue:
        return trader.build_curve_buy_tx(target, amount_wei, min_out, sender, quote)

    raise SkipRoute(f"no builder wired for venue {venue}")


async def try_native_v4(trader: DexTrader, token: str, amount_wei: int, min_out: int,
                        sender: str, fee: int) -> Optional[int]:
    """
    The candidate fix, run as an experiment: key the pool on address(0) instead of
    WETH, and drop the WRAP_ETH command (a native pool settles ETH directly, there
    is nothing to wrap). Returns the gas estimate if the chain accepts it.
    """
    import time as _time

    cached = trader._v4_pool_params_cache.get(Web3.to_checksum_address(token), {})
    for tick, hook in {
        (cached.get("tick", 200), cached.get("hook", dt.PONS_V2_HOOK)),
        (200, dt.PONS_V2_HOOK),
        (60, dt.ZERO),
    }:
        try:
            v4_input = dt.make_v4_swap_input(
                NATIVE, token, amount_wei, min_out, sender,
                fee=int(fee) if fee is not None and int(fee) >= 0 else 0,
                tick_spacing=int(tick), hook_addr=hook,
            )
            data = trader.uni_router.functions.execute(
                bytes([0x10]), [v4_input], int(_time.time()) + 300
            )._encode_transaction_data()
            tx = {"from": sender, "to": trader.uni_router_address,
                  "value": amount_wei, "data": data, "chainId": 4663}
            return int(await asyncio.to_thread(trader.w3.eth.estimate_gas, tx))
        except Exception:
            continue
    return None


async def tier2_calldata(trader: DexTrader, tokens: list[str], sender: str,
                         amount_eth: float, rep: Report) -> None:
    banner(f"TIER 2  Calldata simulation from {sender} (free -- estimateGas only)")

    w3 = trader.w3
    try:
        balance = await asyncio.to_thread(w3.eth.get_balance, sender)
    except Exception as exc:
        rep.bad(f"cannot read balance of {sender}: {short(exc)}")
        return

    amount_wei = w3.to_wei(amount_eth, "ether")
    print(f"{INFO}balance {w3.from_wei(balance, 'ether')} ETH, simulating {amount_eth} ETH swaps")
    if balance < amount_wei:
        rep.warn(
            f"{sender} holds less than {amount_eth} ETH -- value-bearing estimateGas "
            f"calls will fail on funds, not on encoding. Fund it or lower --amount-eth."
        )

    slippage = getattr(trader.config, "SLIPPAGE_PCT", 15.0)

    for token in tokens:
        print(f"\n--- {token}")
        try:
            venue, target, quote, fee = await trader.detect_venue_and_route(token)
        except Exception as exc:
            rep.bad(f"detect failed: {short(exc)}")
            continue

        if venue == "NONE":
            rep.skip(f"venue NONE -- nothing to encode")
            continue

        # The bot shipped every swap with min_out = 1 wei, i.e. no slippage
        # protection at all. Confirm it now quotes a real floor.
        min_out = await trader.min_out_for(venue, token, quote, fee, amount_wei, target, slippage)
        if min_out is None:
            rep.bad(f"{venue}: route cannot be quoted -- the bot will refuse to trade this token")
            continue
        if min_out <= 1:
            rep.bad(f"{venue}: min_out is {min_out} -- swap would go out UNPROTECTED")
            continue
        rep.ok(f"{venue}: min_out = {min_out:,} at {slippage}% slippage")

        try:
            tx, label = await asyncio.to_thread(
                build_with_min_out, trader, venue, token, quote, fee,
                amount_wei, target, sender, min_out)
        except SkipRoute as skip:
            rep.skip(str(skip))
            continue
        except Exception as exc:
            rep.bad(f"{venue}: encoder itself raised: {short(exc)}")
            continue

        rep.note(f"{venue} -> {tx['to']} ({label}), {len(tx['data']) // 2} bytes calldata")

        try:
            gas = await asyncio.to_thread(w3.eth.estimate_gas, tx)
            rep.ok(f"{venue}: calldata ACCEPTED with the real floor, estimated gas {gas:,}")
            # A floor the pool ignores is no floor. Ten times the quote must revert.
            try:
                strict = await asyncio.to_thread(
                    build_with_min_out, trader, venue, token, quote, fee, amount_wei,
                    target, sender, min_out * 10)
                await asyncio.to_thread(w3.eth.estimate_gas, strict)
                rep.bad(f"{venue}: 10x min_out still accepted -- the floor is NOT enforced")
            except Exception:
                rep.ok(f"{venue}: 10x min_out reverts -- slippage floor is enforced on-chain")
            continue
        except Exception as exc:
            rep.bad(f"{venue}: REVERTED -- {short(exc)}")

        # A V4 revert is the expected failure. Re-run the identical swap with the
        # PoolKey on address(0) and no WRAP_ETH; if that estimates, the revert was
        # the WETH/native key mismatch and this is the fix.
        if "V4" in venue or venue == "STOCK_PAIR":
            gas = await try_native_v4(trader, token, amount_wei, min_out, sender, fee)
            if gas:
                rep.note(
                    f"SAME swap with PoolKey on address(0) and no WRAP_ETH: "
                    f"ACCEPTED, gas {gas:,} -- confirms the WETH-key mismatch"
                )
            else:
                rep.note("native-keyed variant also reverts -- revert is not the key mismatch")


# --------------------------------------------------------------------------
# Tier 3: real broadcast. Spends money. Never runs without both flags.
# --------------------------------------------------------------------------
async def tier3_live(trader: DexTrader, config: Config, tokens: list[str],
                     amount_eth: float, rep: Report, sell_only: bool = False) -> None:
    banner("TIER 3  LIVE BROADCAST -- THIS SPENDS REAL ETH")

    if not trader.chain.account:
        rep.bad("no PRIVATE_KEY in .env -- cannot broadcast")
        return

    sender = trader.chain.account.address
    w3 = trader.w3
    token = tokens[0]          # one round trip, not one per harvested token
    slippage = config.SLIPPAGE_PCT

    eth_before = float(w3.from_wei(await asyncio.to_thread(w3.eth.get_balance, sender), "ether"))
    venue, target, quote, fee = await trader.detect_venue_and_route(token)
    quoted = await trader.min_out_for(venue, token, quote, fee,
                                      w3.to_wei(amount_eth, "ether"), target, slippage)

    print(f"{INFO}wallet      {sender}")
    print(f"{INFO}ETH balance {eth_before:.6f}")
    print(f"{INFO}token       {token}")
    print(f"{INFO}venue       {venue}")
    held = await trader._safe_token_balance(token) or 0.0
    if sell_only:
        print(f"{INFO}holding     {held:,.6f} tokens")
        print(f"{INFO}action      SELL ONLY - no buy, exiting the whole balance")
        if held <= 0:
            rep.skip("wallet holds none of this token - nothing to sell")
            return
    else:
        print(f"{INFO}spending    {amount_eth} ETH, then selling the whole fill straight back")
    print(f"{INFO}min_out     {quoted if quoted is not None else 'UNQUOTABLE'} at {slippage}% slippage")
    if quoted is None:
        rep.bad("route is unquotable -- the bot would refuse this trade; nothing sent")
        return
    if amount_eth > 0.002:
        print(f"{INFO}NOTE: {amount_eth} ETH is well above the ~$1 test size")

    answer = input("\nType LIVE (capitals) to broadcast two real transactions, anything else aborts: ").strip()
    if answer != "LIVE":
        if answer.upper() == "LIVE":
            rep.skip(
                f"you typed '{answer}' -- the confirmation is case-sensitive on purpose. "
                f"Nothing was sent; re-run and type LIVE in capitals."
            )
        else:
            rep.skip("aborted at confirmation prompt -- nothing was sent")
        return

    config.DRY_RUN = False

    if sell_only:
        real = held
        rep.note(f"skipping the buy; selling the {real:,.6f} tokens already held")
    else:
        # ---- leg 1: buy ---------------------------------------------------
        before = await trader._safe_token_balance(token)
        try:
            tx_hash, received = await trader.buy_token(token, amount_eth, slippage)
        except Exception as exc:
            rep.bad(f"buy_token raised: {short(exc)}")
            return
        if not tx_hash:
            rep.bad("buy returned no tx hash -- every route failed, nothing spent beyond gas")
            return

        real = await trader._measure_fill(token, before)
        rep.note(f"buy tx {tx_hash}")
        if real <= 0:
            rep.bad("buy transaction sent but no tokens arrived")
            return
        rep.ok(f"BUY landed: {real:,.6f} tokens")
        if received > 0 and abs(real - received) > max(real, received) * 0.01:
            rep.bad(f"reported {received} but wallet gained {real} -- fill measurement is wrong")
        else:
            rep.ok("reported fill matches the wallet delta")

    # ---- leg 2: sell it straight back -------------------------------------
    # This is the half that estimateGas cannot reach, including the native-pool
    # SWEEP path, so it is the whole reason to spend money here.
    print(f"\n{INFO}selling {real:,.6f} tokens back to ETH...")
    try:
        sell_tx, sold = await trader.sell_token(token, real, slippage)
    except Exception as exc:
        rep.bad(f"sell_token raised: {short(exc)} -- YOU ARE STILL HOLDING {real} tokens")
        return
    if not sell_tx or sold <= 0:
        rep.bad(f"sell did not fill -- YOU ARE STILL HOLDING {real} tokens, exit manually")
        return
    rep.ok(f"SELL landed: {sold:,.6f} tokens sold, tx {sell_tx}")

    left = await trader._safe_token_balance(token)
    if left is not None and left > real * 0.01:
        rep.warn(f"{left:,.6f} tokens still in the wallet after the sell")

    # ---- what the round trip actually cost --------------------------------
    eth_after = float(w3.from_wei(await asyncio.to_thread(w3.eth.get_balance, sender), "ether"))
    cost = eth_before - eth_after
    eth_usd = await trader.chain.get_eth_price_usd()
    if sell_only:
        rep.note(f"sell leg netted {-cost:+.8f} ETH (${-cost * eth_usd:+.4f}) after gas; "
                 f"this is one leg, not a round trip")
    else:
        rep.note(f"round-trip cost {cost:.8f} ETH (${cost * eth_usd:.4f}) on a {amount_eth} ETH trade")
    rep.note(
        f"TP1 at {getattr(config, 'TP1_MULTIPLIER', 1.2)}x sells "
        f"{getattr(config, 'TP1_RATIO', 0.4) * 100:.0f}% of a ${config.BASELINE_STAKE_USD:.2f} stake "
        f"= ${(getattr(config, 'TP1_MULTIPLIER', 1.2) - 1) * config.BASELINE_STAKE_USD * getattr(config, 'TP1_RATIO', 0.4):.4f} gross"
    )
    if not sell_only:
        rep.note("if that gross is below the round-trip cost, the ladder cannot pay for itself")


# --------------------------------------------------------------------------
def resolve_tokens(arg: Optional[str]) -> list[str]:
    """
    Default test set comes from the repo's own hardcoded caches. Those pools were
    hand-verified by whoever wrote this, so they are the friendliest possible
    case. If the encoders cannot handle these, they cannot handle anything.
    """
    if arg:
        return [Web3.to_checksum_address(a.strip()) for a in arg.split(",") if a.strip()]

    seen: dict[str, None] = {}
    for entry in _V4_PARAM_CACHE.values():
        for key in ("c0", "c1"):
            addr = entry.get(key, "")
            if addr and int(addr, 16) != 0:
                seen[Web3.to_checksum_address(addr)] = None
    for addr in list(STOCK_TOKENS.values())[:2]:
        seen[Web3.to_checksum_address(addr)] = None
    return list(seen)


async def run(args: argparse.Namespace) -> int:
    config = Config()
    config.DRY_RUN = True  # nothing here may broadcast unless tier 3 flips it
    chain = ChainClient(config)
    trader = DexTrader(chain, config)
    rep = Report()

    tokens = resolve_tokens(args.tokens)
    print(f"RPC      : {chain.rpc_pool[0]}")
    print(f"Tokens   : {len(tokens)} under test")
    if not args.tokens:
        print("           (harvested from the repo's own _V4_PARAM_CACHE + STOCK_TOKENS)")

    await tier0_addresses(chain.w3, rep)
    if rep.failed:
        banner("STOPPING: tier 0 failed -- the address table is wrong, nothing below can pass")
        return 1

    await tier1_routes(trader, tokens, rep)

    sender = args.sender
    if not sender and chain.account:
        sender = chain.account.address
    if sender:
        await tier2_calldata(trader, tokens, Web3.to_checksum_address(sender),
                             args.amount_eth, rep)
    else:
        banner("TIER 2  skipped")
        print("Pass --from 0xYourFundedAddress to simulate calldata. An address is")
        print("enough -- estimateGas needs no private key, and nothing is broadcast.")

    if args.broadcast:
        if not args.i_understand:
            banner("TIER 3  refused")
            print("--broadcast also requires --i-understand. It spends real ETH.")
            return 1
        await tier3_live(trader, config, tokens, args.amount_eth, rep, args.sell_only)

    banner("SUMMARY")
    print(f"passed {rep.passed}   failed {rep.failed}   warnings {rep.warned}")
    if rep.failed:
        print("\nThe chain layer has real breakage. Read the [FAIL] lines above --")
        print("they tell you whether it is the addresses, the pool keys, or the encoding.")
    else:
        print("\nChain layer looks sound. The bugs are in the orchestration layer,")
        print("which is the part worth rewriting.")
    return 1 if rep.failed else 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--from", dest="sender", metavar="0xADDR",
                   help="funded address to simulate from (address only, no private key)")
    p.add_argument("--tokens", help="comma-separated token addresses to test")
    p.add_argument("--amount-eth", type=float, default=0.0004,
                   help="swap size for simulation/broadcast (default 0.0004)")
    p.add_argument("--broadcast", action="store_true",
                   help="run tier 3 and send REAL transactions")
    p.add_argument("--sell-only", action="store_true",
                   help="tier 3: skip the buy and just sell the full balance of the token "
                        "(use this to exit a position left over from a failed run)")
    p.add_argument("--i-understand", action="store_true",
                   help="required alongside --broadcast; confirms you accept the cost")
    args = p.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
