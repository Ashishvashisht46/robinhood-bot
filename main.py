import asyncio
import os
import sys
import time
import logging
from datetime import datetime, timezone

try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from config import Config
from logger_setup import setup_logging
from trade_ledger import log_event, configure_ledger
from models import CallSignal, Position, PositionStatus, StrategyState
from message_parser import MessageParser
from telegram_listener import TelegramListener
from launch_watcher import LaunchWatcher
from chain_client import ChainClient
from dex_trader import DexTrader
from strategy_engine import StrategyEngine
from position_monitor import PositionMonitor
from signal_queue import SignalQueue, SignalResult
from execution_guard import ExecutionGuard, ExecutionUncertain, PreflightFailure


class CopyTraderBot:
    def __init__(self, paper=False, no_telegram=False):
        self.no_telegram = no_telegram
        self.config = Config()
        if paper:
            self.config.DRY_RUN = True
            self.config.BUY_EVERY_SIGNAL = True
            # Paper-only stake override. Set AFTER Config() on purpose: config
            # calls load_dotenv(override=True), so .env beats the shell
            # environment and a plain BASELINE_STAKE_USD=50 is silently ignored.
            # Editing .env instead would risk leaving a $50 stake configured
            # against a $6.79 wallet if the run died.
            paper_stake = os.getenv("PAPER_STAKE_USD")
            if paper_stake:
                self.config.BASELINE_STAKE_USD = float(paper_stake)
                self.config.COMPOUND_STAKE_USD = float(paper_stake)
        log_dir = os.path.join(self.config.BASE_DIR, "logs", "paper") if self.config.DRY_RUN else os.path.join(self.config.BASE_DIR, "logs")
        setup_logging(log_dir)
        configure_ledger(os.path.join(log_dir, "events.jsonl"))
        self.logger = logging.getLogger("copytrader")
        env_path = getattr(self.config, "ENV_PATH", ".env")
        self.logger.info(
            f"Config loaded from {env_path} | exists={os.path.exists(env_path)} | "
            f"DRY_RUN={self.config.DRY_RUN} | API_ID={'set' if self.config.TELEGRAM_API_ID else 'MISSING'} | "
            f"PK={'set' if self.config.PRIVATE_KEY else 'MISSING'}"
        )
        self.config.validate()
        self.parser = MessageParser(allow_ticker_fallback=self.config.ALLOW_TICKER_FALLBACK)
        self.chain_client = ChainClient(self.config)
        self.dex_trader = DexTrader(self.chain_client, self.config)
        self.strategy_engine = StrategyEngine(self.config)
        self.execution = ExecutionGuard(self.config, self.chain_client, self.dex_trader)
        self.execution.inventory_exclusions = lambda: {
            p.contract_address for p in self.strategy_engine.open_positions
        }
        self.signal_queue = SignalQueue(self.config, self.on_signal)
        self.position_monitor = PositionMonitor(
            self.config, self.execution, self.chain_client, self.on_position_closed,
            on_position_changed=lambda _position: self.strategy_engine.save(),
        )
        self.telegram_listener = TelegramListener(self.config, self.parser, self.enqueue_signal)
        # Second signal source, off unless LAUNCH_WATCHER_ENABLED=true. Feeds the
        # same queue, so every gate, stake rule and exit ladder applies unchanged.
        self.launch_watcher = LaunchWatcher(self.config, self.enqueue_signal)
        self._recovery_task = None

    async def start(self):
        banner = (
            f"ROBINHOOD COPY TRADER | {'PAPER' if self.config.DRY_RUN else 'LIVE'}\n"
            f"Stake ${self.config.BASELINE_STAKE_USD:.2f} | Floor ${self.config.SAFETY_FLOOR_USD:.2f} | "
            f"Maximum {self.config.MAX_CONCURRENT_POSITIONS} positions\n"
            f"TP1 {self.config.TP1_RATIO:.0%} @ {self.config.TP1_MULTIPLIER:.2f}x | "
            f"TP2 {self.config.TP2_RATIO:.0%} @ {self.config.TP2_MULTIPLIER:.2f}x | "
            f"Final target {self.config.TP3_MULTIPLIER:.2f}x\n"
            f"Fresh signals only ({self.config.MAX_SIGNAL_AGE_SECONDS}s); fills and returns are not guaranteed."
        )
        print(banner)
        # Print the mtime of the trading code at startup. We repeatedly lost time
        # to a restart that happened moments BEFORE a fix landed, then read the
        # same error and assumed the fix had failed. Now the log says which code
        # is running and nobody has to guess.
        try:
            import os as _os
            from datetime import datetime as _dt
            _code = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "dex_trader.py")
            _mt = _dt.fromtimestamp(_os.path.getmtime(_code))
            self.logger.info(f"CODE VERSION: dex_trader.py last modified {_mt:%Y-%m-%d %H:%M:%S}")
        except Exception as _e:
            self.logger.warning(f"Could not read code version: {_e}")

        self.logger.info("Initializing copy-trader components...")
        await self.dex_trader.initialize()

        try:
            eth_balance = await self.chain_client.get_eth_balance()
            eth_price = await self.chain_client.get_eth_price_usd()
            usd_value = eth_balance * eth_price
            self.logger.info(f"Wallet Balance: {eth_balance:.4f} ETH (${usd_value:.2f} USD) @ ${eth_price:.2f}/ETH")
            if not self.config.DRY_RUN and usd_value < self.config.INITIAL_CAPITAL_USD:
                self.logger.warning(
                    f"LIVE MODE WARNING: Balance (${usd_value:.2f}) is below initial capital target (${self.config.INITIAL_CAPITAL_USD:.2f})!"
                )
        except Exception as e:
            self.logger.warning(f"Startup balance fetch failed ({e}). Continuing — listener will still start.")

        await self.recover_pending_entries()
        await self.recover_open_positions()
        self.signal_queue.start()
        self._recovery_task = asyncio.create_task(self._recovery_loop())

        # Must start BEFORE the listener: telegram_listener.start() ends in
        # run_until_disconnected() and never returns.
        await self.launch_watcher.start()

        if self.no_telegram:
            # The watcher is the only signal source. Nothing else blocks, so the
            # process has to be held open explicitly or start() would return and
            # shut everything down.
            self.logger.info("Telegram DISABLED -- running on the launch watcher alone.")
            await asyncio.Event().wait()
            return

        self.logger.info("Connecting to Telegram channel listener...")
        await self.telegram_listener.start()

    async def recover_open_positions(self):
        """
        Re-adopt positions that outlived the previous run.

        The state file records what we thought we held; the chain records what we
        actually hold, and the chain wins. A position whose tokens are gone was
        already exited elsewhere; one that still has a balance needs its exit ladder
        running again, or it sits there with no stop loss and no take-profit.
        """
        carried = list(self.strategy_engine.open_positions)
        if not carried:
            return

        self.logger.info(f"Reconciling {len(carried)} carried-over position(s)...")
        resumed = dropped = stranded = 0

        for position in carried:
            for operation_id in position.completed_sell_operations:
                if operation_id in self.execution.operations:
                    self.execution.acknowledge(operation_id)
            if position.status != PositionStatus.OPEN and position.remaining_tokens == 0:
                await self.on_position_closed(position, (position.pnl_usd or 0) > 0)
                continue
            if position.remaining_tokens == 0 and position.completed_sell_operations:
                await self.position_monitor._tick(position)
                continue
            if self.config.DRY_RUN:
                # Paper positions have no on-chain balance to reconcile against.
                await self.position_monitor.start_monitoring(position)
                resumed += 1
                continue

            try:
                balance = await self.chain_client.get_token_balance(position.contract_address)
            except Exception as e:
                stranded += 1
                # A failed startup RPC must not disable the exit monitor forever.
                await self.position_monitor.start_monitoring(position)
                self.logger.error(
                    f"Cannot read balance for ${position.ticker} ({position.contract_address}): {e}. "
                    f"Keeping the persisted balance and retrying through its monitor."
                )
                continue

            # A swap can leave a few base units behind, so "closed" cannot mean
            # exactly zero -- a live exit left 204298 units of a 2.88e21 balance.
            # Anything under 0.1% of what we recorded is dust, not a position.
            dust_floor = max((position.remaining_tokens or 0.0) * 0.001, 0.0)
            if balance <= dust_floor and not position.pending_exit:
                self.logger.info(
                    f"${position.ticker}: wallet holds only {balance:g} of "
                    f"{position.contract_address} (dust); already exited. Dropping."
                )
                self.strategy_engine.remove_position(position.id)
                dropped += 1
                continue

            recorded = position.remaining_tokens or 0.0
            if abs(balance - recorded) > max(balance, recorded, 1e-18) * 0.01:
                self.logger.warning(
                    f"${position.ticker}: recorded {recorded:,.4f} tokens but wallet holds "
                    f"{balance:,.4f}. Trusting the chain."
                )
            # A pending exit is reconciled from the journal before adjusting the
            # recorded quantity; otherwise its fill would be subtracted twice.
            if not position.pending_exit:
                position.remaining_tokens = min(balance, recorded) if recorded > 0 else balance

            await self.position_monitor.start_monitoring(position)
            resumed += 1
            self.logger.info(
                f"Resumed ${position.ticker}: {balance:,.4f} tokens | entry {position.entry_price_eth:.12g} ETH | "
                f"TP1={'hit' if position.tp1_hit else 'pending'} TP2={'hit' if position.tp2_hit else 'pending'} | "
                f"stop {position.trailing_stop_multiplier:.2f}x"
            )

        self.strategy_engine.save()
        self.logger.info(f"Recovery complete: {resumed} resumed, {dropped} dropped, {stranded} unreadable.")
        if stranded:
            self.logger.critical(
                f"{stranded} position(s) have unreadable balances; monitors are retrying."
            )

    async def enqueue_signal(self, signal):
        self.signal_queue.submit(signal)

    def _position_from_operation(self, operation, filled):
        context = operation["context"]
        stake_eth = operation["amount"]
        stake_usd = context["stake_usd"]
        gas_usd = self.execution.gas_eth(operation) * operation.get("eth_price_usd", stake_usd / stake_eth)
        if self.config.DRY_RUN:
            gas_usd = self.config.PAPER_FEE_PER_SWAP_USD
        measured = operation.get("cash_delta_usd") is not None
        funding = operation.get("funding")
        if measured:
            stake_usd = max(0.0, -operation["cash_delta_usd"] - gas_usd)
            if funding:
                stake_eth = stake_usd / operation["eth_price_usd"]
        from datetime import datetime, timezone
        return Position(
            ticker=context["ticker"], contract_address=operation["token"],
            entry_price_eth=stake_eth / filled, entry_price_usd=stake_usd / filled,
            tokens_bought=filled, remaining_tokens=filled, stake_usd=stake_usd,
            stake_eth=stake_eth, tx_hash_buy=operation["tx_hash"],
            entry_time=datetime.fromtimestamp(operation["created"], timezone.utc),
            signal_message_id=context["message_id"], buy_operation_id=operation["id"],
            gas_cost_usd=gas_usd, cash_measurement_complete=measured and not funding,
            entry_funding=dict(funding) if funding else None,
            pnl_basis="inventory_quote_estimate" if funding else ("wallet_delta" if measured else "mark_estimate"),
            status=PositionStatus.OPEN)

    async def recover_pending_entries(self):
        for position in self.strategy_engine.open_positions:
            if position.signal_message_id is not None:
                self.signal_queue.reconcile_opened(position.signal_message_id)
        for operation in list(self.execution.pending_buys()):
            if operation["state"] == "prepared" and not operation["transactions"]:
                message_id = operation["context"].get("message_id")
                self.execution.acknowledge(operation["id"])
                if message_id is not None:
                    key = f"{self.config.CHANNEL_USERNAME}:{message_id}"
                    self.signal_queue._finish(key, "retry", "restart before any broadcast; safe to retry if fresh")
                continue
            existing = next((p for p in self.strategy_engine.open_positions
                             if p.buy_operation_id == operation["id"]), None)
            if existing:
                self.execution.acknowledge(operation["id"])
                continue
            filled = await self.execution.reconcile(operation)
            if filled:
                position = self._position_from_operation(operation, filled)
                self.strategy_engine.add_position(position)
                self.signal_queue.reconcile_opened(position.signal_message_id)
                self.execution.acknowledge(operation["id"])
                await self.position_monitor.start_monitoring(position)
                log_event("position_opened", recovered=True, **position.to_dict())

    async def _recovery_loop(self):
        while True:
            await asyncio.sleep(15)
            try:
                # Never reconcile or remove an intent owned by an active worker.
                async with self.execution.lock:
                    await self.recover_pending_entries()
            except Exception:
                self.logger.exception("Pending-entry reconciliation will retry")

    async def on_signal(self, signal: CallSignal):
        start_time = time.time()
        self.logger.info(f"==> Incoming Signal: ${signal.ticker} (DEX: {signal.dex})")
        if not signal.contract_address:
            self.logger.warning(f"Skipping ${signal.ticker}: No contract address identified.")
            return SignalResult("rejected", "No contract address; awaiting a corrected message")
        try:
            eth_price = await self.chain_client.get_eth_price_usd()
        except Exception:
            return SignalResult("retry", "ETH/USD price unavailable")
        decision = self.strategy_engine.get_trade_decision(signal, eth_price)
        if not decision.should_trade:
            self.logger.info(f"Skipping trade for ${signal.ticker}: {decision.reason}")
            retry = decision.reason in ("Max concurrent positions", "Not enough balance above floor", "Invalid ETH/USD price")
            return SignalResult("retry" if retry else "rejected", decision.reason)
        self.logger.info(
            f"Executing BUY for ${signal.ticker}: Stake ${decision.stake_usd:.2f} "
            f"({decision.stake_eth:.5f} ETH) | State: {decision.state.value}")
        try:
            _, filled = await self.execution.buy_token(
                signal.contract_address, decision.stake_eth, self.config.SLIPPAGE_PCT,
                start_time=start_time,
                entry_check=lambda: self.strategy_engine.get_trade_decision(signal, eth_price) == decision,
                context=dict(ticker=signal.ticker, message_id=signal.message_id, stake_usd=decision.stake_usd,
                             check_wallet_funds=True,
                             expires_at=(signal.timestamp.replace(tzinfo=timezone.utc) if signal.timestamp.tzinfo is None
                                         else signal.timestamp).timestamp() + self.config.MAX_SIGNAL_AGE_SECONDS))
            operation = self.execution.last_operation
            position = self._position_from_operation(operation, filled)
            self.strategy_engine.add_position(position)
            self.execution.acknowledge(operation["id"])
            await self.position_monitor.start_monitoring(position)
            self.logger.info(f"Position opened: {position.id} for ${position.ticker} ({filled:,.2f} tokens)")
            # Entry price includes price impact; the monitor marks against spot.
            # On a thin enough pool those diverge enormously and the position
            # shows a ~-90% "loss" the instant it opens. Measure it rather than
            # guess: this is the number that says whether such a trade was ever
            # executable, or whether the quote was wrong.
            try:
                spot = await self.dex_trader.get_token_price_eth(position.contract_address)
                if spot and position.entry_price_eth:
                    ratio = spot / position.entry_price_eth
                    log_event("entry_quality", ticker=position.ticker,
                              contract_address=position.contract_address,
                              spot_over_entry=round(ratio, 4),
                              stake_usd=position.stake_usd)
                    if ratio < 0.70:
                        self.logger.warning(
                            f"THIN POOL {position.ticker}: filled at {1/ratio:.1f}x spot "
                            f"({(1-ratio)*100:.0f}% impact on ${position.stake_usd:.2f}). "
                            f"Not tradeable at this size.")
                    elif ratio > 2.0:
                        # A fill BETTER than spot is not luck, it is a measurement
                        # error -- the fill was quoted against a different pool
                        # than the one being priced. Left unflagged this produced
                        # a position whose every later price read looked like a
                        # 50x outlier, so it held for 110 minutes with no stop.
                        self.logger.critical(
                            f"ENTRY MISMEASURED {position.ticker}: spot is {ratio:.1f}x "
                            f"the fill price. Fill and price feed disagree on the pool; "
                            f"this position's stop-loss will not work.")
            except Exception as exc:
                self.logger.debug("entry quality check failed: %s", exc)
            log_event("position_opened", **position.to_dict())
            return SignalResult("opened", "verified fill and persisted position")
        except PreflightFailure as exc:
            return SignalResult("retry", str(exc))
        except ExecutionUncertain as exc:
            self.logger.error("Entry requires reconciliation: %s", exc)
            return SignalResult("needs_review", str(exc))

    async def on_position_closed(self, position: Position, is_win: bool):
        # Position removal and P&L accounting must be persisted in one snapshot.
        self.strategy_engine.close_position(position, is_win)
        pnl = position.pnl_usd or 0.0
        self.logger.info(
            f"Position closed: ${position.ticker} | Win: {is_win} | PnL: ${pnl:+.2f} | "
            f"New Balance: ${self.strategy_engine.balance_usd:.2f} | Next State: {self.strategy_engine.state.value}"
        )
        log_event(
            "position_closed",
            is_win=is_win,
            balance_usd=self.strategy_engine.balance_usd,
            strategy_state=self.strategy_engine.state.value,
            **position.to_dict(),
        )
        if self.strategy_engine.state == StrategyState.HALTED:
            self.logger.critical("CIRCUIT BREAKER TRIGGERED. Configured capital floor reached.")

    async def shutdown(self):
        self.logger.info("Shutting down bot...")
        if not self.no_telegram:
            await self.telegram_listener.stop()
        await self.launch_watcher.stop()
        await self.signal_queue.stop()
        if self._recovery_task:
            self._recovery_task.cancel()
            await asyncio.gather(self._recovery_task, return_exceptions=True)
        await self.position_monitor.stop_all()
        report = self.strategy_engine.get_status_report()
        self.logger.info(f"Final Session Summary: {report}")


async def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--paper", action="store_true", help="Force paper mode with isolated state and all eligible signals")
    parser.add_argument("--no-telegram", action="store_true",
                        help="Run on the on-chain launch watcher alone, with no channel")
    args = parser.parse_args()
    from pathlib import Path
    from instance_lock import InstanceLock
    with InstanceLock(Path(__file__).resolve().parent / "cache" / "bot.lock"):
        await run_bot(args.paper, args.no_telegram)


async def run_bot(paper, no_telegram=False):
    bot = CopyTraderBot(paper=paper, no_telegram=no_telegram)
    try:
        await bot.start()
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\nStopping bot...")
    finally:
        await bot.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
