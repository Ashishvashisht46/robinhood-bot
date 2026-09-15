"""Offline regression tests. No ChainClient constructor, login, or network I/O."""
import asyncio
import json
import logging
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from config import Config
from test_support import TestDirectory
from contract_resolution import extract_contract, resolve_pair
from execution_guard import ExecutionGuard, ExecutionUncertain, PreflightFailure
from instance_lock import InstanceLock
from message_parser import MessageParser
from models import Position, PositionStatus, StrategyState
from position_monitor import PositionMonitor
from signal_queue import SignalQueue, SignalResult
from strategy_engine import StrategyEngine

TOKEN = "0x" + "1" * 40
POOL = "0x" + "2" * 40
CALL = "EARLY CALL - $TEST · robinhood\nPool Info\nMcap: $20k\nTax: B 0% | S 0%\n"


def signal(message_id=1, age=0, token=TOKEN):
    parsed = MessageParser().parse(CALL + (f"CA: {token}" if token else ""), [], message_id,
                                   datetime.now(timezone.utc) - timedelta(seconds=age))
    return parsed


def config(directory, live=False):
    import os
    with patch.dict(os.environ, {}, clear=True):
        cfg = Config()
    cfg.BASE_DIR = str(directory)
    cfg.DRY_RUN = not live
    cfg.PRICE_POLL_SECONDS = 0.005
    cfg.BUY_EVERY_SIGNAL = True
    cfg.INITIAL_CAPITAL_USD = 20
    cfg.SAFETY_FLOOR_USD = 6
    cfg.BASELINE_STAKE_USD = 1
    cfg.GAS_RESERVE_USD = 0.25
    return cfg


def position():
    return Position(ticker="TEST", contract_address=TOKEN, tokens_bought=100,
                    remaining_tokens=100, stake_usd=10, stake_eth=0.01,
                    entry_price_eth=0.0001, entry_price_usd=0.1,
                    entry_time=datetime.now(timezone.utc) - timedelta(minutes=1))


class ParserTests(unittest.TestCase):
    def test_explicit_ca_beats_chart_pool_and_wallet(self):
        links = [[SimpleNamespace(url=f"https://dexscreener.com/robinhood/{POOL}")]]
        self.assertEqual(extract_contract(CALL + f"CA: {TOKEN}", [], links, None, "TEST")[0], TOKEN)

    def test_token_link_beats_first_chart_button(self):
        links = [[SimpleNamespace(url=f"https://dexscreener.com/robinhood/{POOL}"),
                  SimpleNamespace(url=f"https://gmgn.ai/robinhood/token/scout_{TOKEN}")]]
        self.assertEqual(extract_contract(CALL, [], links, None, "TEST")[0], TOKEN)

    def test_conflicting_token_links_are_ambiguous(self):
        links = [[SimpleNamespace(url=f"https://gmgn.ai/robinhood/token/{address}") for address in (TOKEN, POOL)]]
        self.assertEqual(extract_contract(CALL, [], links, None, "TEST"), (None, "ambiguous"))

    def test_buyer_wallet_is_not_a_token(self):
        self.assertIsNone(signal(token=None).contract_address)
        text = CALL + f"Live buys\n$200 · {TOKEN}\nhttps://robinhoodchain.blockscout.com/address/{TOKEN}"
        self.assertIsNone(extract_contract(text, [], None, None, "TEST")[0])

    def test_reply_markup_token_address_and_unicode_header(self):
        button = SimpleNamespace(url=f"https://gmgn.ai/robinhood/token/{TOKEN}")
        markup = SimpleNamespace(rows=[SimpleNamespace(buttons=[button])])
        parsed = MessageParser().parse(CALL.replace("TEST", "宋江"), [], 7, datetime.now(), None, markup)
        self.assertEqual((parsed.ticker, parsed.contract_address), ("宋江", TOKEN))

    def test_exact_pair_lookup_checks_chain_pair_and_symbol(self):
        import io
        body = {"pairs": [{"chainId": "robinhood", "pairAddress": POOL,
                           "baseToken": {"symbol": "TEST", "address": TOKEN}}]}
        with patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(body).encode())):
            self.assertEqual(resolve_pair(POOL, "TEST"), TOKEN)
        body["pairs"][0]["chainId"] = "ethereum"
        with patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(body).encode())):
            self.assertIsNone(resolve_pair(POOL, "TEST"))

    def test_celebration_does_not_become_a_buy(self):
        self.assertIsNone(MessageParser().parse("$TEST hit 5X! called at $20k", [], 1, datetime.now()))


class RiskTests(unittest.TestCase):
    def setUp(self):
        self.temp = TestDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = config(self.temp.name)
        self.engine = StrategyEngine(self.cfg, load_state=False)

    def test_open_stakes_reserve_capital(self):
        self.engine.balance_usd = 8
        self.engine.add_position(Position(contract_address=POOL, stake_usd=1))
        self.assertFalse(self.engine.get_trade_decision(signal(), 2500).should_trade)

    def test_invalid_price_never_creates_zero_stake_buy(self):
        for price in (0, -1, float("nan"), float("inf")):
            self.assertFalse(self.engine.get_trade_decision(signal(), price).should_trade)

    def test_buy_every_keeps_tax_limit(self):
        candidate = signal()
        candidate.sell_tax = 20
        self.assertEqual(self.engine.get_trade_decision(candidate, 2500).reason, "High tax")

    def test_profit_does_not_automatically_double_stake(self):
        self.engine.record_trade_result(Position(pnl_usd=1), True)
        self.assertEqual(self.engine.state, StrategyState.BASELINE)

    def test_loss_streak_persists_and_blocks_entries(self):
        self.cfg.MAX_CONSECUTIVE_LOSSES = 1
        self.engine.record_trade_result(Position(pnl_usd=-1), False)
        restored = StrategyEngine(self.cfg)
        self.assertEqual(restored.consecutive_losses, 1)
        self.assertEqual(restored.get_trade_decision(signal(), 2500).reason, "Consecutive loss limit reached")

    def test_daily_loss_resets_only_at_utc_day_boundary(self):
        self.cfg.MAX_DAILY_LOSS_USD = 1
        self.engine.daily_pnl_usd = -1
        self.assertFalse(self.engine.get_trade_decision(signal(), 2500).should_trade)
        self.engine.risk_day = "2000-01-01"
        self.assertTrue(self.engine.get_trade_decision(signal(), 2500).should_trade)

    def test_paper_state_does_not_load_live_positions(self):
        live = config(self.temp.name, live=True)
        engine = StrategyEngine(live, load_state=False)
        engine.add_position(position())
        self.assertEqual(StrategyEngine(self.cfg).open_positions, [])

    def test_invalid_settings_fail_validation(self):
        for name, value in (("SLIPPAGE_PCT", 100), ("TP1_RATIO", 0.8),
                            ("BASELINE_STAKE_USD", float("nan")), ("SL_MULTIPLIER", -1)):
            cfg = config(self.temp.name)
            setattr(cfg, name, value)
            with self.assertRaises(ValueError):
                cfg.validate_risk()

    def test_state_write_failure_is_not_swallowed(self):
        with patch("strategy_engine.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.engine.add_position(position())

    def test_close_accounting_is_atomic_and_idempotent(self):
        p = position()
        p.pnl_usd = -1
        self.engine.add_position(p)
        with patch("strategy_engine.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.engine.close_position(p, False)
        self.assertIs(self.engine.open_positions[0], p)
        self.assertEqual(self.engine.total_trades, 0)
        self.engine.close_position(p, False)
        self.engine.close_position(p, False)
        self.assertEqual(self.engine.total_trades, 1)
        self.assertEqual(StrategyEngine(self.cfg).total_trades, 1)

    def test_instance_lock_excludes_second_process_owner(self):
        path = Path(self.temp.name) / "bot.lock"
        with InstanceLock(path):
            with self.assertRaises(RuntimeError):
                with InstanceLock(path):
                    pass
        with InstanceLock(path):
            pass


class QueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TestDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = config(self.temp.name)
        self.callback = AsyncMock(return_value=SignalResult("opened", "filled"))
        self.queue = SignalQueue(self.cfg, self.callback)
        self.addAsyncCleanup(self.queue.stop)

    async def test_duplicate_delivery_and_edit_after_fill_buy_once(self):
        candidate = signal()
        self.queue.submit(candidate)
        self.assertFalse(self.queue.submit(candidate))
        await self.queue.process_one()
        candidate.raw_text += " edited"
        self.assertFalse(self.queue.submit(candidate))
        await self.queue.process_one()
        self.assertEqual(self.callback.await_count, 1)

    async def test_missing_ca_can_be_repaired_by_edit(self):
        self.callback.return_value = SignalResult("rejected", "no CA")
        self.queue.submit(signal(token=None))
        await self.queue.process_one()
        self.callback.return_value = SignalResult("opened", "filled")
        self.queue.submit(signal())
        await self.queue.process_one()
        self.assertEqual(self.callback.await_count, 2)

    async def test_stale_signal_never_reaches_execution(self):
        self.queue.submit(signal(age=1000))
        await self.queue.process_one()
        self.callback.assert_not_awaited()
        self.assertEqual(self.queue.db.execute("SELECT status FROM signals").fetchone()[0], "expired")

    async def test_capacity_retry_and_bounded_attempts(self):
        self.cfg.MAX_SIGNAL_ATTEMPTS = 2
        self.callback.return_value = SignalResult("retry", "Max concurrent positions")
        self.queue.submit(signal())
        for _ in range(3):
            self.queue.db.execute("UPDATE signals SET due=0")
            self.queue.db.commit()
            await self.queue.process_one()
        self.assertEqual(self.callback.await_count, 2)
        self.assertEqual(self.queue.db.execute("SELECT status FROM signals").fetchone()[0], "rejected")

    async def test_crashed_processing_is_never_replayed(self):
        self.queue.submit(signal())
        self.queue.db.execute("UPDATE signals SET status='processing'")
        self.queue.db.commit()
        second = SignalQueue(self.cfg, self.callback)
        try:
            await second.process_one()
            self.callback.assert_not_awaited()
            self.assertEqual(second.db.execute("SELECT status FROM signals").fetchone()[0], "needs_review")
        finally:
            await second.stop()


class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TestDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = config(self.temp.name, live=True)
        self.chain = SimpleNamespace(get_token_balance=AsyncMock(return_value=0),
                                     get_eth_price_usd=AsyncMock(return_value=1000))
        self.dex = SimpleNamespace(buy_token=AsyncMock(return_value=("tx", 100)),
                                   sell_token=AsyncMock(return_value=("sell", 50)))
        self.guard = ExecutionGuard(self.cfg, self.chain, self.dex)
        self.guard._cash_snapshot = AsyncMock(side_effect=RuntimeError("not measured in this test"))

    async def test_hash_written_before_broadcast_and_timeout_blocks_retry(self):
        async def buy(*args, **kwargs):
            self.guard.before_send("0xabc", {"value": 10**15})
            data = json.loads(self.guard.path.read_text())
            self.assertEqual(next(iter(data.values()))["transactions"][0]["hash"], "0xabc")
            raise ExecutionUncertain("receipt timed out")
        self.dex.buy_token = AsyncMock(side_effect=buy)
        with self.assertRaises(ExecutionUncertain):
            await self.guard.buy_token(TOKEN, 0.001, 5)
        with self.assertRaises(ExecutionUncertain):
            await self.guard.buy_token(TOKEN, 0.001, 5)
        self.assertEqual(self.dex.buy_token.await_count, 1)

    async def test_failure_before_broadcast_is_retryable(self):
        self.dex.buy_token.side_effect = TimeoutError("quote RPC unavailable")
        with self.assertRaises(PreflightFailure):
            await self.guard.buy_token(TOKEN, 0.001, 5)
        self.assertEqual(self.guard.operations, {})

    async def test_success_stays_journaled_until_position_persisted(self):
        await self.guard.buy_token(TOKEN, 0.001, 5)
        operation = self.guard.last_operation
        restored = ExecutionGuard(self.cfg, self.chain, self.dex)
        self.assertEqual(await restored.reconcile(restored.operations[operation["id"]]), 100)
        restored.acknowledge(operation["id"])
        self.assertEqual(restored.operations, {})

    async def test_fallback_cannot_spend_native_budget_twice(self):
        async def buy(*args, **kwargs):
            self.guard.before_send("0xfirst", {"value": 10**15})
            self.guard.receipt("0xfirst", {"status": 1})
            self.guard.before_send("0xsecond", {"value": 10**15})
        self.dex.buy_token.side_effect = buy
        with self.assertRaises(ExecutionUncertain):
            await self.guard.buy_token(TOKEN, 0.001, 5)
        operation = next(iter(self.guard.operations.values()))
        self.assertEqual(len(operation["transactions"]), 1)

    async def test_unknown_nonce_blocks_other_tokens(self):
        async def buy(*args, **kwargs):
            self.guard.before_send("0xunknown", {"value": 10**15})
            raise ExecutionUncertain("unknown")
        self.dex.buy_token.side_effect = buy
        with self.assertRaises(ExecutionUncertain):
            await self.guard.buy_token(TOKEN, 0.001, 5)
        with self.assertRaises(ExecutionUncertain):
            await self.guard.sell_token(POOL, 10, 5)
        self.dex.sell_token.assert_not_awaited()

    async def test_reconcile_delayed_receipt_uses_balance_delta(self):
        operation = dict(id="test", side="buy", token=TOKEN, amount=0.001, before=50,
                         context={}, state="needs_review", transactions=[dict(hash="0xabc", status=None, value_wei=10**15)])
        self.guard.operations["test"] = operation
        self.chain.w3 = SimpleNamespace(eth=SimpleNamespace(get_transaction_receipt=lambda _: {"status": 1, "gasUsed": 100, "effectiveGasPrice": 10**9}))
        self.chain.get_token_balance.return_value = 150
        self.assertEqual(await self.guard.reconcile(operation), 100)
        self.assertAlmostEqual(self.guard.gas_eth(operation), 1e-7)

    async def test_wallet_measurement_includes_wrapped_and_usdg_proceeds(self):
        self.guard._cash_snapshot = AsyncMock(side_effect=[{"eth": 1, "usdg": 10}, {"eth": 0.999, "usdg": 13}])
        await self.guard.sell_token(TOKEN, 50, 5)
        self.assertAlmostEqual(self.guard.last_operation["cash_delta_usd"], 2)

    async def test_wallet_operations_are_serialized(self):
        active = peak = 0
        async def sell(*args):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1
            return "sell", 1
        self.dex.sell_token.side_effect = sell
        await asyncio.gather(self.guard.sell_token(TOKEN, 1, 5), self.guard.sell_token(POOL, 1, 5))
        self.assertEqual(peak, 1)

    async def test_expired_signal_cannot_broadcast_after_slow_detection(self):
        async def buy(*args, **kwargs):
            self.guard.before_send("0xlate", {"value": 10**15})
        self.dex.buy_token.side_effect = buy
        with self.assertRaises(PreflightFailure):
            await self.guard.buy_token(TOKEN, 0.001, 5, context={"expires_at": time.time() - 1})
        self.assertEqual(self.guard.operations, {})


class MonitorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TestDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = config(self.temp.name)
        self.cfg.PAPER_FEE_PER_SWAP_USD = 0
        self.cfg.PAPER_SLIPPAGE_PCT = 0
        self.dex = SimpleNamespace(get_token_price_eth=AsyncMock(return_value=0.00012),
                                   sell_token=AsyncMock(return_value=("sell", 40)))
        self.closed = AsyncMock()
        self.persisted = []
        self.monitor = PositionMonitor(self.cfg, self.dex, None, self.closed,
                                       lambda p: self.persisted.append(p.to_dict()))

    async def test_partial_tp_does_not_mark_full_tranche_done(self):
        p = position()
        self.dex.sell_token.return_value = ("sell", 10)
        await self.monitor._tick(p)
        self.assertEqual(p.remaining_tokens, 90)
        self.assertFalse(p.tp1_hit)
        self.assertAlmostEqual(p.tp1_pnl_usd, 0.2)
        self.assertEqual(p.pending_exit["tokens"], 30)

    async def test_partial_close_keeps_remainder_monitored(self):
        p = position()
        self.dex.get_token_price_eth.return_value = 0.00004
        self.dex.sell_token.return_value = ("sell", 25)
        self.assertFalse(await self.monitor._tick(p))
        self.assertEqual(p.remaining_tokens, 75)
        self.assertEqual(p.status, PositionStatus.OPEN)
        self.closed.assert_not_awaited()

    async def test_full_close_deducts_gas(self):
        p = position()
        p.gas_cost_usd = 0.12
        self.dex.get_token_price_eth.return_value = 0.00004
        self.dex.sell_token.return_value = ("sell", 100)
        self.assertTrue(await self.monitor._tick(p))
        self.assertAlmostEqual(p.pnl_usd, -6.12)
        self.closed.assert_awaited_once()

    async def test_monitor_error_never_marks_holdings_closed(self):
        p = position()
        self.dex.get_token_price_eth.side_effect = RuntimeError("temporary RPC")
        await self.monitor.start_monitoring(p)
        await asyncio.sleep(0.10)
        await self.monitor.stop_all()
        self.assertEqual(p.status, PositionStatus.OPEN)
        self.assertGreater(self.dex.get_token_price_eth.await_count, 1)
        self.closed.assert_not_awaited()

    async def test_peak_and_stop_are_persisted_between_sales(self):
        p = position()
        p.tp1_hit, p.tp1_sold_tokens, p.remaining_tokens = True, 40, 60
        p.trailing_stop_multiplier = 0.95
        self.dex.get_token_price_eth.return_value = 0.00013
        await self.monitor._tick(p)
        self.assertAlmostEqual(p.trailing_stop_multiplier, 1.05)
        self.assertTrue(self.persisted)
        self.dex.sell_token.assert_not_awaited()

    async def test_target_not_triggered_by_rounding(self):
        p = position()
        self.dex.get_token_price_eth.return_value = p.entry_price_eth * 1.196
        await self.monitor._tick(p)
        self.dex.sell_token.assert_not_awaited()

    async def test_recent_purchase_has_no_blind_stop_delay(self):
        p = position()
        p.entry_time = datetime.now(timezone.utc)
        self.dex.get_token_price_eth.return_value = p.entry_price_eth * 0.4
        await self.monitor._tick(p)
        self.dex.sell_token.assert_awaited_once()

    async def test_failed_profit_exit_does_not_mask_later_stop(self):
        p = position()
        self.dex.sell_token.return_value = ("", 0)
        await self.monitor._tick(p)
        self.assertEqual(p.pending_exit["rung"], "TP1")
        self.dex.get_token_price_eth.return_value = p.entry_price_eth * 0.4
        self.dex.sell_token.return_value = ("sell", 100)
        self.assertTrue(await self.monitor._tick(p))
        self.assertEqual(self.dex.sell_token.call_args.args[1], 100)

    async def test_pending_sell_recovery_does_not_repeat_completed_fill(self):
        p = position()
        p.remaining_tokens = 0
        p.accumulated_pnl_usd = 1
        p.completed_sell_operations = ["done"]
        guard = SimpleNamespace(operations={"done": {"side": "sell"}},
                                acknowledge=lambda key: guard.operations.pop(key))
        self.monitor.dex_trader = guard
        self.assertTrue(await self.monitor._tick(p))
        self.assertEqual(guard.operations, {})
        self.closed.assert_awaited_once()


class ReplayTests(unittest.TestCase):
    def test_unsold_inventory_is_not_reported_as_realized_profit(self):
        from strategy_report import replay
        result = replay([(0.1, 1.1)], fee=0.03)
        self.assertFalse(result["complete"])
        self.assertEqual(result["remaining_fraction"], 1)
        self.assertAlmostEqual(result["net_realized_pnl"], -0.03)

    def test_stop_uses_observed_gap_price_and_costs(self):
        from strategy_report import replay
        result = replay([(0.1, 0.2)], stop=0.75, fee=0.03, slippage_pct=10)
        self.assertTrue(result["complete"])
        self.assertAlmostEqual(result["net_realized_pnl"], -0.88)


class ListenerTests(unittest.IsolatedAsyncioTestCase):
    async def test_backfill_obeys_freshness_window(self):
        from telegram_listener import TelegramListener
        listener = object.__new__(TelegramListener)
        listener.config = SimpleNamespace(MAX_SIGNAL_AGE_SECONDS=120, SIGNAL_BACKFILL_LIMIT=100)
        now = datetime.now(timezone.utc)
        async def messages(*args, **kwargs):
            for ident, age in ((3, 10), (2, 90), (1, 500)):
                yield SimpleNamespace(id=ident, date=now - timedelta(seconds=age))
        listener.client = SimpleNamespace(iter_messages=messages)
        listener._handle_new_message = AsyncMock()
        await listener._backfill_recent("channel")
        self.assertEqual(listener._handle_new_message.await_count, 2)

    async def test_raw_markup_from_edit_reaches_parser(self):
        from telegram_listener import TelegramListener
        listener = object.__new__(TelegramListener)
        listener.config = SimpleNamespace(CHANNEL_USERNAME="calls")
        listener.parser = MessageParser()
        listener.on_signal = AsyncMock()
        button = SimpleNamespace(url=f"https://gmgn.ai/robinhood/token/{TOKEN}")
        message = SimpleNamespace(id=1, date=datetime.now(timezone.utc), edit_date=datetime.now(timezone.utc),
                                  message=CALL, entities=[], buttons=None,
                                  reply_markup=SimpleNamespace(rows=[SimpleNamespace(buttons=[button])]))
        await listener._handle_new_message(SimpleNamespace(message=message))
        self.assertEqual(listener.on_signal.call_args.args[0].contract_address, TOKEN)


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    import socket
    temporary_root = Path(__file__).resolve().parent / "cache" / "test-tmp"
    temporary_root.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(temporary_root)
    original_connect = socket.socket.connect
    def local_only(sock, address):
        if isinstance(address, tuple) and address[0] not in ("127.0.0.1", "::1"):
            raise AssertionError("External network disabled in regression tests")
        return original_connect(sock, address)
    with patch("socket.socket.connect", new=local_only):
        unittest.main(verbosity=2)
