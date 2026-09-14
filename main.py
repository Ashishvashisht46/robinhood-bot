import asyncio
import os
import sys
import time
import logging

try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from config import Config
from logger_setup import setup_logging
from trade_ledger import log_event
from models import CallSignal, Position, PositionStatus, StrategyState
from message_parser import MessageParser
from telegram_listener import TelegramListener
from chain_client import ChainClient
from dex_trader import DexTrader
from strategy_engine import StrategyEngine
from position_monitor import PositionMonitor


class CopyTraderBot:
    def __init__(self):
        setup_logging()
        self.logger = logging.getLogger("copytrader")
        self.config = Config()
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
        self.position_monitor = PositionMonitor(
            self.config, self.dex_trader, self.chain_client, self.on_position_closed,
            on_position_changed=lambda _position: self.strategy_engine.save(),
        )
        self.telegram_listener = TelegramListener(self.config, self.parser, self.on_signal)

    async def start(self):
        banner = f"""
╔══════════════════════════════════════════════════╗
║     ROBINHOOD CHAIN COPY-TRADER BOT v1.1        ║
╠══════════════════════════════════════════════════╣
║  Mode:     {'DRY RUN (Paper Trading)' if self.config.DRY_RUN else 'LIVE TRADING (Real Money)'}              ║
║  Capital:  ${self.config.INITIAL_CAPITAL_USD:.2f} USD (Floor: ${self.config.SAFETY_FLOOR_USD:.2f})            ║
║  Strategy: 2-Step Compound (${self.config.BASELINE_STAKE_USD:.0f}/${self.config.COMPOUND_STAKE_USD:.0f})               ║
║  Ladder:   40% @ {self.config.TP1_MULTIPLIER:.2f}x | 40% @ {self.config.TP2_MULTIPLIER:.2f}x | 20% Moonbag 🚀║
║  Shield:   Dynamic Trailing Stop (-0.25x)        ║
║  Timeouts: 25m Stagnant / 120m Moonbag Hold      ║
║  Channel:  {self.config.CHANNEL_USERNAME}                       ║
╚══════════════════════════════════════════════════╝
        """
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

        await self.recover_open_positions()

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
            if self.config.DRY_RUN:
                # Paper positions have no on-chain balance to reconcile against.
                await self.position_monitor.start_monitoring(position)
                resumed += 1
                continue

            try:
                balance = await self.chain_client.get_token_balance(position.contract_address)
            except Exception as e:
                stranded += 1
                self.logger.error(
                    f"Cannot read balance for ${position.ticker} ({position.contract_address}): {e}. "
                    f"Leaving it recorded but UNMONITORED - check this one by hand."
                )
                continue

            # A swap can leave a few base units behind, so "closed" cannot mean
            # exactly zero -- a live exit left 204298 units of a 2.88e21 balance.
            # Anything under 0.1% of what we recorded is dust, not a position.
            dust_floor = max((position.remaining_tokens or 0.0) * 0.001, 0.0)
            if balance <= dust_floor:
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
            position.remaining_tokens = balance

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
                f"{stranded} position(s) could not be reconciled and are NOT being monitored."
            )

    async def on_signal(self, signal: CallSignal):
        start_time = time.time()
        self.logger.info(f"==> Incoming Signal: ${signal.ticker} (DEX: {signal.dex})")
        if not signal.contract_address:
            self.logger.warning(f"Skipping ${signal.ticker}: No contract address identified.")
            return

        eth_price = await self.chain_client.get_eth_price_usd()
        decision = self.strategy_engine.get_trade_decision(signal, eth_price)
        if not decision.should_trade:
            self.logger.info(f"Skipping trade for ${signal.ticker}: {decision.reason}")
            return

        self.logger.info(
            f"Executing BUY for ${signal.ticker}: Stake ${decision.stake_usd:.2f} "
            f"({decision.stake_eth:.5f} ETH) | State: {decision.state.value}"
        )
        try:
            tx_hash, tokens_bought = await self.dex_trader.buy_token(
                signal.contract_address, decision.stake_eth, self.config.SLIPPAGE_PCT, start_time=start_time
            )
            if tokens_bought <= 0:
                self.logger.error(f"Buy failed for ${signal.ticker}: 0 tokens received.")
                return
            position = Position(
                ticker=signal.ticker,
                contract_address=signal.contract_address,
                entry_price_eth=decision.stake_eth / tokens_bought if tokens_bought > 0 else 0.0,
                entry_price_usd=decision.stake_usd / tokens_bought if tokens_bought > 0 else 0.0,
                tokens_bought=tokens_bought,
                stake_usd=decision.stake_usd,
                stake_eth=decision.stake_eth,
                tx_hash_buy=tx_hash,
                status=PositionStatus.OPEN
            )
            self.strategy_engine.add_position(position)
            await self.position_monitor.start_monitoring(position)
            self.logger.info(f"Position opened: {position.id} for ${position.ticker} ({tokens_bought:,.2f} tokens)")
            log_event("position_opened", **position.to_dict())
        except Exception as e:
            self.logger.error(f"Trade execution exception for ${signal.ticker}: {e}", exc_info=True)

    async def on_position_closed(self, position: Position, is_win: bool):
        self.strategy_engine.remove_position(position.id)
        self.strategy_engine.record_trade_result(position, is_win)
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
            self.logger.critical("CIRCUIT BREAKER TRIGGERED ($40 Floor Reached). All further trading is HALTED.")

    async def shutdown(self):
        self.logger.info("Shutting down bot...")
        await self.position_monitor.stop_all()
        await self.telegram_listener.stop()
        report = self.strategy_engine.get_status_report()
        self.logger.info(f"Final Session Summary: {report}")


async def main():
    bot = CopyTraderBot()
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
