"""Offline boundary tests for the actual senders and protected fallback routes."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from chain_client import ChainClient, eip1559_fees
from dex_trader import DexTrader
from execution_guard import ExecutionUncertain, PreflightFailure
import stock_v4_routes
from stock_v4_routes import StockV4Router


class SenderTests(unittest.IsolatedAsyncioTestCase):
    def chain(self):
        events = []
        chain = object.__new__(ChainClient)
        chain.config = SimpleNamespace(DRY_RUN=False, GAS_MULTIPLIER=1.3)
        chain.chain_id = 4663
        chain.account = SimpleNamespace(key=b"test-key")
        def sign(tx, private_key):
            events.append(("signed", tx.copy()))
            return SimpleNamespace(raw_transaction=b"dummy-signed-bytes")
        def send(raw):
            events.append(("broadcast", raw))
            return b"hash"
        chain.w3 = SimpleNamespace(
            eth=SimpleNamespace(account=SimpleNamespace(sign_transaction=sign), send_raw_transaction=send),
            to_hex=lambda _: "0xhash", keccak=lambda _: b"hash")
        async def threaded(func, *args):
            return func(*args)
        chain._run_in_thread = threaded
        chain.get_nonce = AsyncMock(return_value=17)
        chain.execution_guard = SimpleNamespace(before_send=lambda h, tx: events.append(("journaled", h)))
        return chain, events

    async def test_pending_nonce_and_late_fees_precede_journaled_broadcast(self):
        chain, events = self.chain()
        with patch("chain_client.eip1559_fees", return_value=(2000, 100)):
            await chain.send_transaction({"nonce": 2, "gas": 21000, "value": 1, "maxFeePerGas": 2})
        self.assertEqual([e[0] for e in events], ["signed", "journaled", "broadcast"])
        self.assertEqual(events[0][1]["nonce"], 17)
        self.assertEqual(events[0][1]["maxFeePerGas"], 2000)

    async def test_sender_timeout_is_uncertain_and_not_resubmitted(self):
        chain, _ = self.chain()
        chain.w3.eth.send_raw_transaction = Mock(side_effect=TimeoutError("RPC timeout"))
        with patch("chain_client.eip1559_fees", return_value=(2000, 100)):
            with self.assertRaises(ExecutionUncertain):
                await chain.send_transaction({"gas": 21000, "value": 1})
        chain.w3.eth.send_raw_transaction.assert_called_once()

    async def test_dry_run_cannot_sign(self):
        chain, events = self.chain()
        chain.config.DRY_RUN = True
        result = await chain.send_transaction({"value": 1})
        self.assertTrue(result.startswith("DRY_RUN"))
        self.assertEqual(events, [])

    async def test_failed_v4_fallback_simulation_cannot_broadcast(self):
        dex = object.__new__(DexTrader)
        dex.config = SimpleNamespace(DRY_RUN=False)
        dex.w3 = SimpleNamespace(to_checksum_address=lambda a: a, to_wei=lambda a, _: int(a * 1e18))
        dex.uni_router_address = "0x" + "1" * 40
        dex.chain = SimpleNamespace(account=SimpleNamespace(address=dex.uni_router_address),
                                    get_nonce=AsyncMock(return_value=1), send_transaction=AsyncMock())
        dex.min_out_for = AsyncMock(return_value=100)
        dex.build_v4_swap_tx = Mock(return_value=({"data": "0xdata"}, "commands"))
        dex._safe_token_balance = AsyncMock(return_value=0)
        dex.simulate_execution = AsyncMock(return_value=(False, "reverted"))
        with patch("dex_trader.eip1559_fees", return_value=(2000, 100)):
            self.assertEqual(await dex.force_buy_v4_or_stock(dex.uni_router_address, 0.001, 5), ("", 0.0))
        dex.chain.send_transaction.assert_not_awaited()

    async def test_paper_entry_uses_route_quote_and_slippage(self):
        dex = object.__new__(DexTrader)
        dex.config = SimpleNamespace(PAPER_SLIPPAGE_PCT=2)
        dex.w3 = SimpleNamespace(to_wei=lambda a, _: int(a * 1e18))
        dex.chain = SimpleNamespace(get_token_decimals=AsyncMock(return_value=6))
        dex.detect_venue_and_route = AsyncMock(return_value=("V3", "router", "WETH", 500))
        dex._quote_route = Mock(return_value=100_000_000)
        self.assertAlmostEqual(await dex._dry_run_fill("token", 0.001), 98)
        dex._quote_route.return_value = None
        self.assertEqual(await dex._dry_run_fill("token", 0.001), 0)

    async def test_guarded_unreadable_fill_cannot_fall_through_to_second_buy(self):
        dex = object.__new__(DexTrader)
        dex.chain = SimpleNamespace(execution_guard=object())
        dex._safe_token_balance = AsyncMock(return_value=None)
        with self.assertRaises(ExecutionUncertain):
            await dex._measure_fill("token", 0)


class LatencyTests(unittest.IsolatedAsyncioTestCase):
    """The three latency changes: the call's DEX steers detection, the quote runs
    alongside the simulation, and send_transaction fetches nonce+fee together."""

    ZERO = "0x" + "0" * 40
    TOKEN = "0x" + "7" * 40
    CURVE = "0x" + "c" * 40

    def trader(self):
        from web3 import Web3
        dex = object.__new__(DexTrader)
        dex.config = SimpleNamespace(DRY_RUN=False)
        dex.w3 = SimpleNamespace(to_checksum_address=Web3.to_checksum_address, keccak=Web3.keccak,
                                 to_wei=lambda a, _: int(a * 1e18))
        dex.weth_address = "0x" + "e" * 40
        dex.usdg_address = "0x" + "d" * 40
        dex.uni_router_address = "0x" + "a" * 40
        dex._curve_cache, dex._route_cache = {}, {}
        dex.verify_chain_id = AsyncMock()
        return dex

    def test_dex_labels_map_to_families(self):
        from dex_trader import dex_family
        for label, family in [("Pons V2", "pons"), ("pons_v2", "pons"), ("Pons", "pons"),
                              ("uni_v4", "uniswap"), ("Uniswap V3", "uniswap"),
                              ("Longxyz", None), ("", None), (None, None)]:
            self.assertEqual(dex_family(label), family, label)

    async def test_graduated_pons_call_goes_straight_to_its_pool(self):
        dex = self.trader()
        dex.is_contract = AsyncMock(return_value=True)
        dex.resolve_token_curve = AsyncMock(return_value=(None, self.ZERO, True))
        dex.v3_factory = None           # any V3/stock/V2 probe would crash on this
        dex._v4_pool_params_cache = {}
        with patch("dex_trader.fetch_dexscreener", return_value=None):
            route = await dex._detect_venue_and_route_uncached(self.TOKEN, "Pons V2")
        self.assertEqual(route, ("UNISWAP_V4", dex.uni_router_address, self.ZERO, 0))
        # ...with the Pons graduation key remembered, so no V4 key probing follows
        key = dex._v4_pool_params_cache[dex.w3.to_checksum_address(self.TOKEN)]
        self.assertEqual((key["fee"], key["tick"], key["verified"]), (0, 200, True))

    async def test_timed_out_pons_lookup_is_asked_again(self):
        dex = self.trader()
        dex.is_contract = AsyncMock(return_value=True)
        # First lookup timed out (answers nothing and caches nothing); second answers.
        dex.resolve_token_curve = AsyncMock(side_effect=[(None, self.ZERO, False),
                                                         (self.CURVE, self.ZERO, False)])
        with patch("dex_trader.fetch_dexscreener", return_value=None):
            venue, target, _, _ = await dex._detect_venue_and_route_uncached(self.TOKEN, "pons_v2")
        self.assertEqual((venue, target), ("LAUNCHPAD_CURVE_ETH", self.CURVE))

    async def test_uniswap_call_skips_the_curve_lookup(self):
        dex = self.trader()
        dex.is_contract = AsyncMock(return_value=True)
        dex.resolve_token_curve = AsyncMock()
        pair = {"stub": True}
        with patch("dex_trader.fetch_dexscreener", return_value=pair), \
             patch("dex_trader.map_ds_pair", return_value={"venue": "UNISWAP_V3_WETH", "target": "p",
                                                           "quote": dex.weth_address, "fee": 3000}):
            venue, *_ = await dex._detect_venue_and_route_uncached(self.TOKEN, "uni_v4")
        self.assertEqual(venue, "UNISWAP_V3_WETH")
        dex.resolve_token_curve.assert_not_awaited()

    async def test_curve_timeout_is_not_cached_but_a_revert_is(self):
        import time as _time
        from web3.exceptions import ContractLogicError
        dex = self.trader()
        dex.bags_lens = SimpleNamespace(functions=SimpleNamespace(
            getTokenState=Mock(side_effect=RuntimeError("no"))))
        dex.chain = SimpleNamespace(fallback_w3=SimpleNamespace(eth=SimpleNamespace(
            block_number=property(lambda _: 1))))
        dex.w3.eth = SimpleNamespace(call=lambda *_: _time.sleep(0.3))
        with patch("dex_trader.CURVE_LOOKUP_TIMEOUT", 0.05):
            self.assertEqual(await dex.resolve_token_curve(self.TOKEN), (None, self.ZERO, False))
        self.assertNotIn(dex.w3.to_checksum_address(self.TOKEN), dex._curve_cache)

        dex.w3.eth = SimpleNamespace(call=Mock(side_effect=ContractLogicError("reverted")))
        await dex.resolve_token_curve(self.TOKEN)
        self.assertIn(dex.w3.to_checksum_address(self.TOKEN), dex._curve_cache)

    async def test_prefetched_route_is_joined_not_searched_twice(self):
        dex = self.trader()
        route = ("LAUNCHPAD_CURVE_ETH", self.CURVE, self.ZERO, 0)
        async def slow(*_):
            await asyncio.sleep(0.05)
            return route
        dex._detect_venue_and_route_uncached = AsyncMock(side_effect=slow)
        dex.prefetch_route(self.TOKEN, "Pons V2")          # the call arrives
        # ...and its buy comes up while that search is still running
        self.assertEqual(await dex.detect_venue_and_route(self.TOKEN, "Pons V2"), route)
        self.assertEqual(dex._detect_venue_and_route_uncached.await_count, 1)
        self.assertEqual(await dex.detect_venue_and_route(self.TOKEN), route)   # now cached
        self.assertEqual(dex._detect_venue_and_route_uncached.await_count, 1)

    async def test_failed_prefetch_is_rerun_by_the_buy(self):
        dex = self.trader()
        route = ("LAUNCHPAD_CURVE_ETH", self.CURVE, self.ZERO, 0)
        dex._detect_venue_and_route_uncached = AsyncMock(side_effect=[RuntimeError("rpc down"), route])
        dex.prefetch_route(self.TOKEN)
        await asyncio.sleep(0.01)                          # the prefetch fails quietly
        self.assertEqual(await dex.detect_venue_and_route(self.TOKEN), route)

    async def test_new_calls_with_an_address_are_prefetched_on_arrival(self):
        from main import CopyTraderBot
        bot = object.__new__(CopyTraderBot)
        bot.dex_trader = SimpleNamespace(prefetch_route=Mock())
        call = SimpleNamespace(contract_address=self.TOKEN, dex="Pons V2")
        for accepted, has_ca, expect in ((True, True, 1), (False, True, 0), (True, False, 0)):
            bot.dex_trader.prefetch_route.reset_mock()
            bot.signal_queue = SimpleNamespace(submit=Mock(return_value=accepted))
            call.contract_address = self.TOKEN if has_ca else None
            await bot.enqueue_signal(call)
            self.assertEqual(bot.dex_trader.prefetch_route.call_count, expect, (accepted, has_ca))

    def buying_trader(self, quote):
        dex = self.trader()
        dex.chain = SimpleNamespace(account=SimpleNamespace(address="0x" + "1" * 40),
                                    send_transaction=AsyncMock(return_value="0xtx"),
                                    wait_for_receipt=AsyncMock(return_value={"status": 1}))
        dex.detect_venue_and_route = AsyncMock(return_value=("LAUNCHPAD_CURVE_ETH", self.CURVE, self.ZERO, 0))
        dex.min_out_for = quote
        dex.build_curve_buy_tx = lambda curve, amt, m, who, pair: ({"data": f"min={m}"}, "buy")
        dex._safe_token_balance = AsyncMock(return_value=0)
        dex._measure_fill = AsyncMock(return_value=100.0)
        dex.no_sim_venues, dex.record_fill = set(), Mock()
        return dex

    async def test_simulation_runs_while_the_quote_is_in_flight(self):
        release, order = asyncio.Event(), []
        async def quote(*_a, **_k):
            await release.wait()          # would deadlock if the sim waited for it
            order.append("quote returned")
            return 777
        dex = self.buying_trader(quote)
        async def sim(tx, *_):
            order.append(f"sim {tx['data']}")
            release.set()
            return True, "gas: 1"
        dex.simulate_execution = sim
        result = await asyncio.wait_for(dex.buy_token(self.TOKEN, 0.001, 15), timeout=5)
        self.assertEqual(result, ("0xtx", 100.0))
        self.assertEqual(order, ["sim min=1", "quote returned"])
        self.assertEqual(dex.chain.send_transaction.call_args.args[0]["data"], "min=777")

    async def test_no_quote_means_nothing_is_sent_even_if_sim_passed(self):
        dex = self.buying_trader(AsyncMock(return_value=None))
        dex.simulate_execution = AsyncMock(return_value=(True, "gas: 1"))
        self.assertEqual(await dex.buy_token(self.TOKEN, 0.001, 15), ("", 0.0))
        dex.chain.send_transaction.assert_not_awaited()

    async def test_failed_simulation_means_nothing_is_sent(self):
        dex = self.buying_trader(AsyncMock(return_value=777))
        dex.simulate_execution = AsyncMock(return_value=(False, "reverted"))
        dex.stock_v4 = SimpleNamespace(detect=Mock(return_value={"venue": "NONE"}))
        dex.force_buy_v4_or_stock = AsyncMock(return_value=("", 0.0))
        await dex.buy_token(self.TOKEN, 0.001, 15)
        dex.chain.send_transaction.assert_not_awaited()


class StockRouteTests(unittest.TestCase):
    def test_erc20_v4_leg_has_one_funding_path(self):
        router = object.__new__(StockV4Router)
        address = "0x" + "1" * 40
        router.account = SimpleNamespace(address=address)
        router.w3 = SimpleNamespace(eth=SimpleNamespace(get_transaction_count=lambda *_: 1, estimate_gas=lambda _: 100))
        router.ensure_permit2 = Mock()
        router._gas_fees = lambda: (100, 1)
        router._send = lambda tx: "tx"
        execute = Mock(return_value=SimpleNamespace(build_transaction=lambda tx: tx))
        router.ur = SimpleNamespace(functions=SimpleNamespace(execute=execute))
        router.buy_v4_erc20_in(address, "0x" + "2" * 40, 100, 90, 500, 10, "0x" + "0" * 40)
        self.assertEqual(execute.call_args.args[0], bytes([0x10]))
        self.assertEqual(len(execute.call_args.args[1]), 1)

    def test_every_leg_of_v3_path_is_quoted(self):
        router = object.__new__(StockV4Router)
        router.execution_guard = SimpleNamespace(config=SimpleNamespace(SLIPPAGE_PCT=5))
        router.quote_v3 = Mock(side_effect=[100, 200])
        self.assertEqual(router._v3_floor(["ETH", "USDG", "TOKEN"], [500, 3000], 50), 190)
        self.assertEqual(router.quote_v3.call_args_list[1].args, ("USDG", "TOKEN", 100, 3000))

    def test_missing_quote_never_becomes_minimum_one(self):
        router = object.__new__(StockV4Router)
        router.quote_v3 = Mock(return_value=None)
        with self.assertRaises(RuntimeError):
            router._v3_floor(["ETH", "TOKEN"], [500], 50)

    def test_stock_submission_requires_live_guard(self):
        router = object.__new__(StockV4Router)
        with self.assertRaises(ExecutionUncertain):
            router._send({})
        router.execution_guard = SimpleNamespace(config=SimpleNamespace(DRY_RUN=True))
        with self.assertRaises(ExecutionUncertain):
            router._send({})

    def two_step_router(self, leg2):
        """buy_stock_two_tx with leg 1 filling 1000 stock and leg 2 doing `leg2`."""
        router = object.__new__(StockV4Router)
        me, stock = "0x" + "1" * 40, "0x" + "5" * 40
        router.account = SimpleNamespace(address=me)
        balances = iter([0, 1000])                      # stock before / after leg 1
        bal = SimpleNamespace(functions=SimpleNamespace(
            balanceOf=lambda _: SimpleNamespace(call=lambda: next(balances))))
        router.w3 = SimpleNamespace(to_wei=lambda a, _: int(a * 1e18),
                                    eth=SimpleNamespace(contract=lambda *a, **k: bal))
        router._v3_pool = lambda a, b, f: f == 500
        router.buy_v3_multihop = Mock(return_value="tx1")
        router._stock_leg2 = leg2
        receipts = {"tx1": {"status": 1}, "tx2": {"status": 1}}
        router._wait_receipt = lambda tx, timeout=60, allow_revert=False: receipts[tx]
        router._unwind_stock = Mock()
        return router, stock, receipts

    def test_failed_stock_leg2_unwinds_exactly_what_leg1_bought(self):
        router, stock, _ = self.two_step_router(Mock(side_effect=RuntimeError("execution reverted")))
        with self.assertRaises(RuntimeError):
            router.buy_stock_two_tx("0x" + "7" * 40, stock, 0.001)
        args = router._unwind_stock.call_args.args
        self.assertEqual(args[:3], (stock, 1000, ("v3", [stock_v4_routes.WETH, stock], [500])))

    def test_stock_leg2_reverting_on_chain_is_unwound_too(self):
        router, stock, receipts = self.two_step_router(Mock(return_value="tx2"))
        receipts["tx2"] = {"status": 0}
        self.assertEqual(router.buy_stock_two_tx("0x" + "7" * 40, stock, 0.001), ("tx1", "tx2"))
        router._unwind_stock.assert_called_once()

    def test_uncertain_stock_leg2_is_never_unwound(self):
        # It may still land: selling its input from under it would strand it.
        router, stock, _ = self.two_step_router(Mock(side_effect=ExecutionUncertain("unknown")))
        with self.assertRaises(ExecutionUncertain):
            router.buy_stock_two_tx("0x" + "7" * 40, stock, 0.001)
        router._unwind_stock.assert_not_called()

    def test_successful_stock_buy_is_not_unwound(self):
        router, stock, _ = self.two_step_router(Mock(return_value="tx2"))
        self.assertEqual(router.buy_stock_two_tx("0x" + "7" * 40, stock, 0.001), ("tx1", "tx2"))
        router._unwind_stock.assert_not_called()

    def test_unwind_reverses_leg1_path_and_unwraps_only_what_it_got(self):
        router = object.__new__(StockV4Router)
        me, stock, usdg = "0x" + "1" * 40, "0x" + "5" * 40, stock_v4_routes.USDG
        router.account = SimpleNamespace(address=me)
        weth_bal = iter([7, 7 + 42])                    # WETH before / after the sell
        withdraw = Mock(return_value=SimpleNamespace(build_transaction=lambda tx: dict(tx)))
        weth = SimpleNamespace(functions=SimpleNamespace(
            balanceOf=lambda _: SimpleNamespace(call=lambda: next(weth_bal)), withdraw=withdraw))
        router.w3 = SimpleNamespace(eth=SimpleNamespace(
            contract=lambda *a, **k: weth, get_transaction_count=lambda *_: 3,
            estimate_gas=lambda tx: 40_000))
        router.sell_v3_path = Mock(return_value="sell")
        router._wait_receipt = Mock(return_value={"status": 1})
        router._gas_fees = lambda: (100, 1)
        router._send = Mock(return_value="unwrap")
        router._unwind_stock(stock, 1000, ("v3", [stock_v4_routes.WETH, usdg, stock], [500, 3000]), "x")
        router.sell_v3_path.assert_called_once_with(
            [stock, usdg, stock_v4_routes.WETH], [3000, 500], 1000, simulate=True)
        withdraw.assert_called_once_with(42)            # not the 7 WETH already held
        self.assertEqual(router._send.call_args.args[0]["gas"], 52_000)

    def test_failed_unwind_never_raises(self):
        # The original leg-2 failure is what the caller must see; a failed
        # unwind on top of it is logged, not raised. (Not asserted via logs:
        # the offline runner disables logging.)
        router = object.__new__(StockV4Router)
        router.account = SimpleNamespace(address="0x" + "1" * 40)
        weth = SimpleNamespace(functions=SimpleNamespace(
            balanceOf=lambda _: SimpleNamespace(call=lambda: 0)))
        router.w3 = SimpleNamespace(eth=SimpleNamespace(contract=lambda *a, **k: weth))
        router.sell_v3_path = Mock(side_effect=RuntimeError("no liquidity"))
        router._unwind_stock("0x" + "5" * 40, 1000, ("v3", ["a", "b"], [500]), "x")
        router.sell_v3_path.assert_called_once()

    def test_fee_ceiling_covers_base_and_tip(self):
        w3 = SimpleNamespace(eth=SimpleNamespace(get_block=lambda _: {"baseFeePerGas": 10**9}))
        maximum, tip = eip1559_fees(w3)
        self.assertGreaterEqual(maximum, 2 * 10**9 + tip)

    def test_rpc_rotation_updates_existing_contract_provider(self):
        from web3 import Web3
        chain = object.__new__(ChainClient)
        chain.rpc_pool, chain._rpc_index = ["one", "two"], 0
        chain.w3 = Web3()
        original = chain.w3
        next_provider = Web3.HTTPProvider("http://127.0.0.1:1")
        chain._connect = lambda _: SimpleNamespace(provider=next_provider)
        chain.rotate_rpc()
        self.assertIs(chain.w3, original)
        self.assertIs(chain.w3.provider, next_provider)


if __name__ == "__main__":
    unittest.main(verbosity=2)
