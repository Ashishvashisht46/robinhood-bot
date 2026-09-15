"""Offline boundary tests for the actual senders and protected fallback routes."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from chain_client import ChainClient, eip1559_fees
from dex_trader import DexTrader
from execution_guard import ExecutionUncertain, PreflightFailure
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
