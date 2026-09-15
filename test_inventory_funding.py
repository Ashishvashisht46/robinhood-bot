"""Offline tests for bounded wallet-stock entries and funding accounting."""
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from execution_guard import ExecutionGuard, ExecutionUncertain, PreflightFailure
from inventory_funding import InventoryFunding, raw_budget
from main import CopyTraderBot
from models import Position
from stock_v4_routes import STOCK_LIST, USDG, StockV4Router
from test_reliability import config
from test_support import TestDirectory

STOCK = STOCK_LIST[0]
TOKEN = "0x" + "1" * 40
OTHER = "0x" + "2" * 40


def funding_plan(kind="v4", raw=250_000):
    return dict(kind="stock_inventory", token=STOCK, target=TOKEN,
                amount_raw=str(raw), balance_before_raw=str(raw * 40),
                value_eth=0.001, valuation="replacement_quote_estimate",
                route=dict(kind=kind, fee=500, tick=10, hook="0x" + "0" * 40),
                expected_out_raw="100000000", min_out_raw="95000000", quoted_at=time.time())


class InventoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TestDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = config(self.temp.name, live=True)
        self.chain = SimpleNamespace(get_token_balance=AsyncMock(return_value=10),
                                     get_eth_price_usd=AsyncMock(return_value=1000),
                                     get_eth_balance=AsyncMock(return_value=0.0068))
        self.router = SimpleNamespace(_erc20_bal=Mock(return_value=10_000_000),
            sell_v3_path=Mock(return_value="0xswap"),
            buy_v4_erc20_in=Mock(return_value="0xswap"), _wait_receipt=Mock())
        self.dex = SimpleNamespace(stock_v4=self.router, verify_chain_id=AsyncMock(),
            _quote_v3=Mock(return_value=None), _quote_v4=Mock(return_value=None),
            buy_token=AsyncMock(return_value=("0xnative", 100)),
            sell_token=AsyncMock(return_value=("0xsell", 100)))
        self.guard = ExecutionGuard(self.cfg, self.chain, self.dex)
        self.inv = self.guard.inventory
        self.inv.stocks = (STOCK,)
        self.guard._cash_snapshot = AsyncMock(side_effect=[
            {"eth": 0.0068, "usdg": 0}, {"eth": 0.00678, "usdg": 0}])

    def quote_fixture(self, amount=250_000, kind="v4"):
        self.inv._replacement_amount = Mock(return_value=amount)
        self.inv._find_quote = Mock(return_value=(funding_plan(kind)["route"], 100_000_000))

    async def buy(self):
        return await self.guard.buy_token(TOKEN, 0.001, 5,
            context=dict(ticker="TEST", message_id=42, stake_usd=1,
                         check_wallet_funds=True, expires_at=time.time() + 120))

    def guarded_swap(self, *args, **kwargs):
        # Use the same write-ahead boundary as the real stock router.
        self.guard.before_send("0xswap", {"value": 0})
        persisted = json.loads(self.guard.path.read_text())
        op = next(iter(persisted.values()))
        self.assertTrue(op["inventory_input_claimed"])
        self.assertEqual(op["funding"]["amount_raw"], "250000")
        self.guard.receipt("0xswap", {"status": 1, "gasUsed": 20_000,
                                         "effectiveGasPrice": 10**9})
        return "0xswap"

    async def test_surplus_inventory_spends_only_quoted_stake_raw_units(self):
        self.quote_fixture()
        plan = await self.inv.prepare(TOKEN, 0.001, 5)
        self.assertEqual(plan["amount_raw"], "250000")
        self.assertEqual(plan["balance_before_raw"], "10000000")
        self.assertEqual(plan["min_out_raw"], "95000000")
        self.assertEqual(plan["value_eth"], 0.001)

    async def test_six_and_eighteen_decimal_inputs_need_no_float_conversion(self):
        for raw in (250_001, 250_000_000_000_000_001):
            with self.subTest(raw=raw):
                self.router._erc20_bal.return_value = raw
                self.quote_fixture(raw)
                plan = await self.inv.prepare(TOKEN, 0.001, 5)
                self.assertEqual(int(plan["amount_raw"]), raw)

    async def test_insufficient_inventory_does_not_top_up(self):
        self.quote_fixture(10_000_001)
        self.assertIsNone(await self.inv.prepare(TOKEN, 0.001, 5))
        self.inv._find_quote.assert_not_called()
        self.router.buy_v4_erc20_in.assert_not_called()

    async def test_reserved_open_and_self_tokens_are_excluded_by_address(self):
        self.quote_fixture()
        self.cfg.RESERVED_TOKENS = {STOCK.lower()}
        self.assertIsNone(await self.inv.prepare(TOKEN, 0.001, 5))
        self.cfg.RESERVED_TOKENS = set()
        self.assertIsNone(await self.inv.prepare(TOKEN, 0.001, 5, [STOCK.upper()]))
        self.assertIsNone(await self.inv.prepare(STOCK, 0.001, 5))
        self.router._erc20_bal.assert_not_called()

    async def test_unreadable_inventory_is_unavailable(self):
        self.router._erc20_bal.side_effect = TimeoutError()
        self.assertIsNone(await self.inv.prepare(TOKEN, 0.001, 5))

    async def test_paper_and_disabled_modes_do_not_read_or_spend_inventory(self):
        for paper, enabled in ((True, True), (False, False)):
            self.cfg.DRY_RUN, self.cfg.REUSE_STOCK_INVENTORY = paper, enabled
            self.assertIsNone(await self.inv.prepare(TOKEN, 0.001, 5))
        self.router._erc20_bal.assert_not_called()
        self.cfg.DRY_RUN = True
        with self.assertRaises(PreflightFailure):
            await self.inv.execute(funding_plan(), self.guard)
        self.router.buy_v4_erc20_in.assert_not_called()

    async def test_missing_or_dust_output_quote_never_submits(self):
        self.inv._replacement_amount = Mock(return_value=250000)
        self.inv._find_quote = Mock(return_value=None)
        self.assertIsNone(await self.inv.prepare(TOKEN, 0.001, 5))
        self.inv._find_quote.return_value = (funding_plan()["route"], 2)
        self.assertIsNone(await self.inv.prepare(TOKEN, 0.001, 99))

    async def test_v4_entry_bypasses_eth_purchase_and_accounts_for_stock_cost(self):
        self.quote_fixture()
        self.router.buy_v4_erc20_in.side_effect = self.guarded_swap
        self.chain.get_token_balance.side_effect = [10, 110]
        self.router._erc20_bal.side_effect = [10_000_000, 10_000_000, 9_750_000]
        self.assertEqual(await self.buy(), ("0xswap", 100))
        self.dex.buy_token.assert_not_awaited()
        self.router.sell_v3_path.assert_not_called()
        self.assertEqual(self.router.buy_v4_erc20_in.call_args.args[:4],
                         (STOCK, TOKEN, 250000, 95000000))
        operation = self.guard.last_operation
        self.assertEqual(operation["budget_wei"], 0)
        self.assertAlmostEqual(operation["cash_delta_usd"], -1.02)
        bot = object.__new__(CopyTraderBot)
        bot.config, bot.execution = self.cfg, self.guard
        position = bot._position_from_operation(operation, 100)
        self.assertAlmostEqual(position.stake_usd, 1)
        self.assertAlmostEqual(position.gas_cost_usd, 0.02)
        self.assertAlmostEqual(position.entry_price_eth, 0.00001)
        self.assertFalse(position.cash_measurement_complete)
        self.assertEqual(position.pnl_basis, "inventory_quote_estimate")
        restored = Position.from_dict(position.to_dict())
        self.assertEqual(restored.entry_funding["spent_raw"], "250000")

    async def test_v3_inventory_uses_single_erc20_path_with_simulation(self):
        self.quote_fixture(kind="v3")
        self.router.sell_v3_path.side_effect = self.guarded_swap
        self.chain.get_token_balance.side_effect = [0, 100]
        await self.buy()
        self.router.sell_v3_path.assert_called_once_with(
            [STOCK, TOKEN], [500], 250000, 95000000, simulate=True)
        self.dex.buy_token.assert_not_awaited()
        self.router.buy_v4_erc20_in.assert_not_called()

    async def test_no_inventory_keeps_full_native_stake_gate(self):
        self.inv.prepare = AsyncMock(return_value=None)
        with self.assertRaises(PreflightFailure):
            await self.buy()
        self.dex.buy_token.assert_not_awaited()
        self.assertEqual(self.guard.operations, {})
        self.chain.get_eth_balance.return_value = 0.01
        await self.buy()
        self.dex.buy_token.assert_awaited_once()

    async def test_inventory_still_requires_native_floor_and_gas_reserve(self):
        self.quote_fixture()
        self.chain.get_eth_balance.return_value = 0.0061
        with self.assertRaises(PreflightFailure):
            await self.buy()
        self.router.buy_v4_erc20_in.assert_not_called()

    async def test_balance_shrink_before_submission_is_retryable_without_eth_fallback(self):
        self.quote_fixture()
        self.router._erc20_bal.side_effect = [10_000_000, 100]
        with self.assertRaises(PreflightFailure):
            await self.buy()
        self.dex.buy_token.assert_not_awaited()
        self.router.buy_v4_erc20_in.assert_not_called()
        self.assertEqual(self.guard.operations, {})

    async def test_uncertain_inventory_swap_cannot_fall_back_or_repeat_after_restart(self):
        self.quote_fixture()
        def timeout(*args, **kwargs):
            self.guard.before_send("0xunknown", {"value": 0})
            raise TimeoutError("response lost")
        self.router.buy_v4_erc20_in.side_effect = timeout
        with self.assertRaises(ExecutionUncertain):
            await self.buy()
        operation = next(iter(self.guard.operations.values()))
        self.assertEqual(operation["state"], "needs_review")
        restored = ExecutionGuard(self.cfg, self.chain, self.dex)
        with self.assertRaises(ExecutionUncertain):
            await restored.buy_token(TOKEN, 0.001, 5)
        self.router.buy_v4_erc20_in.assert_called_once()
        self.dex.buy_token.assert_not_awaited()
        self.assertEqual(next(iter(restored.operations.values()))["funding"]["amount_raw"], "250000")

    async def test_recovered_inventory_position_keeps_cost_estimate(self):
        self.quote_fixture()
        self.router.buy_v4_erc20_in.side_effect = self.guarded_swap
        self.router._wait_receipt.side_effect = TimeoutError()
        with self.assertRaises(ExecutionUncertain):
            await self.buy()
        op = next(iter(self.guard.operations.values()))
        self.chain.get_token_balance.return_value = 110
        self.assertEqual(await self.guard.reconcile(op), 100)
        bot = object.__new__(CopyTraderBot)
        bot.config, bot.execution = self.cfg, self.guard
        position = bot._position_from_operation(op, 100)
        self.assertEqual(position.stake_usd, 1)
        self.assertIsNotNone(position.entry_funding)
        self.assertFalse(position.cash_measurement_complete)

    async def test_active_and_unresolved_sources_reach_planner_exclusions(self):
        self.guard.inventory_exclusions = lambda: {STOCK}
        self.guard.operations["old"] = dict(token=OTHER, side="sell", transactions=[],
                                             funding=dict(token=STOCK_LIST[1]))
        self.inv.prepare = AsyncMock(return_value=None)
        self.chain.get_eth_balance.return_value = 0.01
        await self.buy()
        excluded = self.inv.prepare.call_args.args[3]
        self.assertTrue({STOCK, OTHER, STOCK_LIST[1]}.issubset(excluded))

    async def test_funding_cannot_claim_twice_or_send_eth(self):
        self.guard.active = dict(funding=funding_plan(), side="buy", context={}, transactions=[])
        self.guard.claim_inventory_input(STOCK, 250000)
        with self.assertRaises(ExecutionUncertain):
            self.guard.claim_inventory_input(STOCK, 250000)
        with self.assertRaises(ExecutionUncertain):
            self.guard.before_send("0xextra", {"value": 1})

    async def test_signal_expiry_after_approval_blocks_swap(self):
        self.guard.active = dict(funding=funding_plan(), side="buy",
            context=dict(expires_at=time.time()-1), transactions=[dict(status=1, value_wei=0)])
        with self.assertRaises(PreflightFailure):
            self.guard.before_send("0xlate", {"value": 0})

    async def test_quote_expiry_before_execution_and_after_approval(self):
        plan = funding_plan()
        plan["quoted_at"] -= 100
        with self.assertRaises(PreflightFailure):
            await self.inv.execute(plan, self.guard)
        self.guard.active = dict(funding=plan, side="buy", context={}, transactions=[])
        with self.assertRaises(PreflightFailure):
            self.guard.before_send("0xlate", {"value": 0})

    async def test_actual_quote_lookup_keeps_zero_fee_v4_key_and_address_pair(self):
        self.dex._quote_v4.return_value = 100
        params = dict(c0=STOCK, c1=TOKEN, fee=0, tick=200, hook=OTHER)
        with patch("inventory_funding._V4_PARAM_CACHE", {"pool": params}):
            route, output = self.inv._find_quote(STOCK, TOKEN, 250000, time.monotonic()+10)
        self.assertEqual(route["fee"], 0)
        self.assertEqual(output, 100)
        self.dex._quote_v4.assert_called_once_with(STOCK, TOKEN, 250000, 0, 200, OTHER)

    async def test_unrelated_stock_does_not_exhaust_other_holdings_scan_budget(self):
        self.inv.stocks = (STOCK, STOCK_LIST[1])
        deadlines = []
        def plan(token, stock, balance, budget, slippage, deadline):
            deadlines.append(deadline)
            return None if stock == STOCK else funding_plan()
        self.inv._plan = Mock(side_effect=plan)
        self.assertIsNotNone(await self.inv.prepare(TOKEN, 0.001, 5))
        self.assertEqual(self.inv._plan.call_count, 2)
        self.assertGreater(deadlines[1] - deadlines[0], 5)

    async def test_direct_v3_quote_uses_exact_raw_input_without_eth_conversion(self):
        self.dex._quote_v3.return_value = 100
        with patch("inventory_funding._V4_PARAM_CACHE", {}):
            route, output = self.inv._find_quote(STOCK, TOKEN, 250001, time.monotonic()+10)
        self.assertEqual(route["kind"], "v3")
        self.assertEqual(self.dex._quote_v3.call_args.args, (STOCK, TOKEN, 250001, route["fee"]))
        self.assertEqual(output, 100)

    async def test_replacement_quote_can_use_usdg_without_trading(self):
        self.inv._find_quote = Mock(side_effect=[None, (dict(kind="v3", fee=500), 250000)])
        self.dex._quote_v3.return_value = 1_000_000
        self.assertEqual(self.inv._replacement_amount(STOCK, 10**15, time.monotonic()+10), 250000)
        self.assertEqual(self.inv._find_quote.call_args.args[:3], (USDG, STOCK, 1_000_000))
        self.dex.buy_token.assert_not_awaited()


class SizingAndSenderTests(unittest.TestCase):
    def test_raw_eth_budget_rounds_down(self):
        self.assertEqual(raw_budget("0.0000000000000000019"), 1)
        for bad in (0, -1, "nan", "inf"):
            with self.assertRaises(PreflightFailure):
                raw_budget(bad)

    def test_v3_inventory_simulation_failure_prevents_swap_broadcast(self):
        router = object.__new__(StockV4Router)
        router.account = SimpleNamespace(address=OTHER)
        erc = SimpleNamespace(functions=SimpleNamespace(
            allowance=lambda *_: SimpleNamespace(call=lambda: 10**30)))
        router.w3 = SimpleNamespace(eth=SimpleNamespace(contract=lambda *_args, **_kwargs: erc,
            get_transaction_count=lambda *_: 1, estimate_gas=Mock(side_effect=RuntimeError("revert"))))
        router.sr02 = SimpleNamespace(functions=SimpleNamespace(exactInput=Mock(
            return_value=SimpleNamespace(build_transaction=lambda tx: tx))))
        router._gas_fees = lambda: (100, 1)
        router._send = Mock()
        with self.assertRaises(RuntimeError):
            router.sell_v3_path([STOCK, TOKEN], [500], 250000, 900, simulate=True)
        router._send.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
