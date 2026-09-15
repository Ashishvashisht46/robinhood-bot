"""Stake-limited entry from stock tokens already held in the wallet.

All discovery is read-only. Amounts stay in raw ERC20 units; a quote for the
configured ETH stake supplies their replacement value, not historical cost.
"""
import asyncio
from decimal import Decimal
import logging
import time

from execution_guard import ExecutionUncertain, PreflightFailure
from stock_v4_routes import (
    STOCK_LIST, WETH, USDG, V3_FEES, V4_FEE_TICKS_EXTENDED, V4_HOOKS,
    _V4_PARAM_CACHE,
)

logger = logging.getLogger("copytrader")


def raw_budget(eth_amount):
    value = Decimal(str(eth_amount))
    if not value.is_finite() or value <= 0:
        raise PreflightFailure("invalid inventory stake")
    return int(value * 10**18)


class InventoryFunding:
    DISCOVERY_SECONDS = 12
    QUOTE_SECONDS = 15

    def __init__(self, config, chain, dex):
        self.config, self.chain, self.dex = config, chain, dex
        self.router = dex.stock_v4
        self.stocks = tuple(dict.fromkeys(STOCK_LIST))

    async def balance_raw(self, token):
        return int(await asyncio.to_thread(self.router._erc20_bal, token))

    def _quote(self, source, target, amount, route):
        if route["kind"] == "v3":
            return self.dex._quote_v3(source, target, amount, route["fee"])
        return self.dex._quote_v4(source, target, amount, route["fee"],
                                  route["tick"], route["hook"])

    def _find_quote(self, source, target, amount, deadline):
        """Try known exact keys first, then the supported on-chain keys."""
        pair = {source.lower(), target.lower()}
        routes = [dict(kind="v4", fee=p["fee"], tick=p["tick"], hook=p["hook"])
                  for p in _V4_PARAM_CACHE.values()
                  if {p["c0"].lower(), p["c1"].lower()} == pair]
        routes += [dict(kind="v3", fee=f) for f in V3_FEES]
        routes += [dict(kind="v4", fee=f, tick=t, hook=h)
                   for f, t in V4_FEE_TICKS_EXTENDED for h in V4_HOOKS]
        seen = set()
        for route in routes:
            key = tuple(route.items())
            if key in seen:
                continue
            seen.add(key)
            if time.monotonic() >= deadline:
                return None
            output = self._quote(source, target, amount, route)
            if output and int(output) > 1:
                return route, int(output)
        return None

    def _replacement_amount(self, stock, budget, deadline):
        # Reserve time for the USDG alternative and the actual entry quote.
        direct = self._find_quote(WETH, stock, budget, min(deadline, time.monotonic() + 2))
        if direct:
            return direct[1]
        if time.monotonic() >= deadline:
            return None
        first = self.dex._quote_v3(WETH, USDG, budget, 500)
        if not first or time.monotonic() >= deadline:
            return None
        second = self._find_quote(USDG, stock, int(first), deadline)
        return second[1] if second else None

    def _plan(self, token, stock, balance, budget, slippage, deadline):
        amount = self._replacement_amount(stock, budget, min(deadline, time.monotonic() + 4))
        if not amount or amount > balance:
            return None
        quoted = self._find_quote(stock, token, amount, deadline)
        if not quoted:
            return None
        route, output = quoted
        minimum = int(Decimal(output) * (1 - Decimal(str(slippage)) / 100))
        if minimum <= 1:
            return None
        return dict(kind="stock_inventory", token=stock, target=token,
                    amount_raw=str(amount), balance_before_raw=str(balance),
                    value_eth=budget / 1e18, valuation="replacement_quote_estimate",
                    route=route, expected_out_raw=str(output), min_out_raw=str(minimum),
                    quoted_at=time.time())

    async def prepare(self, token, eth_amount, slippage, excluded=()):
        if self.config.DRY_RUN or not self.config.REUSE_STOCK_INVENTORY:
            return None
        budget = raw_budget(eth_amount)
        if not 0 <= slippage < 100:
            raise PreflightFailure("invalid inventory slippage")
        blocked = {a.lower() for a in excluded} | {token.lower()}
        blocked |= {a.lower() for a in self.config.RESERVED_TOKENS}
        candidates = [s for s in self.stocks if s.lower() not in blocked]
        slots = asyncio.Semaphore(self.config.RPC_SCAN_WORKERS)

        async def holding(stock):
            async with slots:
                try:
                    return stock, await self.balance_raw(stock)
                except Exception:
                    # An unreadable balance is never treated as available stock.
                    return stock, 0

        balances = [(stock, balance) for stock, balance in
                    await asyncio.gather(*(holding(s) for s in candidates)) if balance > 0]
        known_pairs = [{p["c0"].lower(), p["c1"].lower()} for p in _V4_PARAM_CACHE.values()]
        balances.sort(key=lambda item: {item[0].lower(), token.lower()} not in known_pairs)
        deadline = time.monotonic() + self.DISCOVERY_SECONDS
        for index, (stock, balance) in enumerate(balances):
            now = time.monotonic()
            if now >= deadline:
                continue
            # One unrelated stock must not consume every other holding's scan time.
            candidate_deadline = now + (deadline - now) / (len(balances) - index)
            try:
                plan = await asyncio.to_thread(
                    self._plan, token, stock, balance, budget, slippage, candidate_deadline)
                if plan:
                    logger.info("Using held stock %s -> %s, raw input %s; skipping stock purchase",
                                stock, token, plan["amount_raw"])
                    return plan
            except (ExecutionUncertain, PreflightFailure):
                raise
            except Exception:
                logger.debug("Inventory quote unavailable for %s", stock, exc_info=True)
        logger.info("No fully funded, quoted stock entry; checking native ETH route")
        return None

    async def execute(self, plan, guard):
        if self.config.DRY_RUN:
            raise PreflightFailure("paper mode cannot spend wallet inventory")
        if time.time() - plan["quoted_at"] > self.QUOTE_SECONDS:
            raise PreflightFailure("inventory quote expired before execution")
        await self.dex.verify_chain_id()
        amount = int(plan["amount_raw"])
        current = await self.balance_raw(plan["token"])
        if current < amount:
            raise PreflightFailure("stock inventory changed before execution")
        plan["balance_before_raw"] = str(current)
        guard.claim_inventory_input(plan["token"], amount)
        route = plan["route"]
        # A selected inventory entry has one swap path and no ETH fallback.
        if route["kind"] == "v3":
            tx = await asyncio.to_thread(self.router.sell_v3_path,
                [plan["token"], plan["target"]], [route["fee"]], amount,
                int(plan["min_out_raw"]), simulate=True)
        else:
            tx = await asyncio.to_thread(self.router.buy_v4_erc20_in,
                plan["token"], plan["target"], amount, int(plan["min_out_raw"]),
                route["fee"], route["tick"], route["hook"])
        await asyncio.to_thread(self.router._wait_receipt, tx)
        after = await self.chain.get_token_balance(plan["target"])
        filled = after - guard.active["before"]
        if filled <= 0:
            raise ExecutionUncertain("inventory swap has no verified output balance increase")
        return tx, filled
