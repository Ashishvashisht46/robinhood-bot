import asyncio
import logging
import math
from datetime import datetime, timezone
from typing import Dict, Any, Callable, Optional

from models import Position, PositionStatus
from execution_guard import ExecutionUncertain
from trade_ledger import log_event

logger = logging.getLogger("copytrader")


class PositionMonitor:
    def __init__(self, config, dex_trader, chain_client, on_position_closed: Callable[[Position, bool], Any],
                 on_position_changed: Optional[Callable[[Position], Any]] = None):
        self.config, self.dex_trader, self.chain_client = config, dex_trader, chain_client
        self.on_position_closed, self.on_position_changed = on_position_closed, on_position_changed
        self._tasks: Dict[str, asyncio.Task] = {}
        self._stopping = False

    def _persist(self, position):
        if self.on_position_changed:
            self.on_position_changed(position)

    async def start_monitoring(self, position):
        if position.id not in self._tasks or self._tasks[position.id].done():
            self._tasks[position.id] = asyncio.create_task(self._monitor(position))

    async def stop_monitoring(self, position_id):
        task = self._tasks.get(position_id)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def stop_all(self):
        self._stopping = True
        # Let any in-flight signing/receipt operation finish. Cancelling a
        # to_thread await does not stop its underlying signing thread.
        await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)
        self._tasks.clear()

    async def _close(self, position, reason, multiplier):
        position.pnl_usd = position.accumulated_pnl_usd - position.gas_cost_usd
        position.pnl_pct = position.pnl_usd / position.stake_usd * 100 if position.stake_usd else 0
        position.pnl_basis = "wallet_delta_less_receipt_gas" if position.cash_measurement_complete else "mark_estimate_less_receipt_gas"
        if self.config.DRY_RUN:
            position.pnl_basis = "paper_model"
        position.exit_time = datetime.now(timezone.utc)
        position.exit_price_eth = position.entry_price_eth * multiplier
        position.status = (PositionStatus.CLOSED_TIMEOUT if "timeout" in reason.lower() else
                           PositionStatus.CLOSED_TP if position.pnl_usd > 0 else PositionStatus.CLOSED_SL)
        self._persist(position)
        if asyncio.iscoroutinefunction(self.on_position_closed):
            await self.on_position_closed(position, position.pnl_usd > 0)
        else:
            self.on_position_closed(position, position.pnl_usd > 0)

    async def _execute_exit(self, position):
        pending = position.pending_exit
        if not pending:
            return False
        rung, multiplier = pending["rung"], pending["multiplier"]
        guard = self.dex_trader if hasattr(self.dex_trader, "operations") else None
        operation = None
        try:
            if guard:
                operation = next((o for o in guard.operations.values()
                                  if o["side"] == "sell" and o["token"].lower() == position.contract_address.lower()), None)
                if operation and operation["state"] == "prepared" and not operation["transactions"]:
                    guard.acknowledge(operation["id"])
                    operation = None
            if operation:
                if operation["id"] in position.completed_sell_operations:
                    guard.acknowledge(operation["id"])
                    return False
                sold = await guard.reconcile(operation)
                if sold is None:
                    logger.error("Exit for $%s awaiting transaction reconciliation", position.ticker)
                    return False
                tx = operation["tx_hash"]
            else:
                tx, sold = await self.dex_trader.sell_token(
                    position.contract_address, pending["tokens"], self.config.SLIPPAGE_PCT)
                if guard:
                    operation = guard.last_operation
            if not math.isfinite(sold) or sold <= 0:
                return False
        except ExecutionUncertain as exc:
            logger.error("Exit for $%s requires reconciliation: %s", position.ticker, exc)
            return False
        except Exception as exc:
            logger.warning("Exit for $%s failed before a verified fill: %s", position.ticker, exc)
            return False

        sold = min(float(sold), position.remaining_tokens)
        ratio = sold / position.tokens_bought
        pnl = (multiplier - 1.0) * position.stake_usd * ratio
        if self.config.DRY_RUN:
            pnl = (multiplier * (1 - self.config.PAPER_SLIPPAGE_PCT / 100) - 1) * position.stake_usd * ratio
            position.gas_cost_usd += self.config.PAPER_FEE_PER_SWAP_USD
        if operation and operation.get("cash_delta_usd") is not None:
            gas_usd = guard.gas_eth(operation) * operation["eth_price_usd"]
            proceeds = operation["cash_delta_usd"] + gas_usd
            pnl = proceeds - position.stake_usd * ratio
        else:
            position.cash_measurement_complete = False
        position.accumulated_pnl_usd += pnl
        position.remaining_tokens = max(0.0, position.remaining_tokens - sold)
        position.tx_hash_sell = tx
        if operation:
            usd_per_eth = position.stake_usd / position.stake_eth if position.stake_eth else 0
            position.gas_cost_usd += guard.gas_eth(operation) * operation.get("eth_price_usd", usd_per_eth)
            position.completed_sell_operations.append(operation["id"])
        tolerance = max(position.tokens_bought * 1e-6, 1e-18)
        if rung == "TP1":
            position.tp1_sold_tokens += sold
            position.tp1_pnl_usd += pnl
            position.tp1_hit = position.tp1_sold_tokens + tolerance >= position.tokens_bought * self.config.TP1_RATIO
            if position.tp1_hit:
                position.trailing_stop_multiplier = max(position.trailing_stop_multiplier, 0.95)
        elif rung == "TP2":
            position.tp2_sold_tokens += sold
            position.tp2_pnl_usd += pnl
            position.tp2_hit = position.tp2_sold_tokens + tolerance >= position.tokens_bought * self.config.TP2_RATIO
            if position.tp2_hit:
                position.trailing_stop_multiplier = max(position.trailing_stop_multiplier, 1.20)
        remaining_order = max(0.0, pending["tokens"] - sold)
        position.pending_exit = ({**pending, "tokens": min(remaining_order, position.remaining_tokens)}
                                 if remaining_order > tolerance and position.remaining_tokens > tolerance else None)
        # First persist the actual fill, then acknowledge its journal record.
        self._persist(position)
        if operation:
            guard.acknowledge(operation["id"])
        log_event("tranche_exit", rung=rung, position_id=position.id, ticker=position.ticker,
                  contract_address=position.contract_address, multiplier=multiplier,
                  tokens_sold=sold, pnl_usd=pnl, pnl_basis="wallet_delta" if position.cash_measurement_complete else "mark_estimate",
                  gas_cost_usd=position.gas_cost_usd, remaining_tokens=position.remaining_tokens,
                  trailing_stop_multiplier=position.trailing_stop_multiplier)
        if position.remaining_tokens <= tolerance:
            position.remaining_tokens = 0.0
            position.pending_exit = None
            await self._close(position, pending["reason"], multiplier)
            return True
        return False

    async def _tick(self, position):
        guard = self.dex_trader if hasattr(self.dex_trader, "operations") else None
        if guard:
            # Crash after persisting a fill but before acknowledging its journal.
            for operation_id in position.completed_sell_operations:
                if operation_id in guard.operations:
                    self._persist(position)
                    guard.acknowledge(operation_id)
        if position.remaining_tokens <= max(position.tokens_bought * 1e-6, 1e-18):
            await self._close(position, "recovered completed exit", getattr(position, "peak_multiplier", 1.0))
            return True
        if position.pending_exit:
            unresolved = guard and any(o["side"] == "sell" and o["token"].lower() == position.contract_address.lower()
                                       for o in guard.operations.values())
            if not unresolved:
                # Re-price an unsubmitted remainder. A failed TP attempt must
                # not prevent a later stop from closing the whole position.
                fresh = await self.dex_trader.get_token_price_eth(position.contract_address)
                if math.isfinite(fresh) and fresh > 0 and position.entry_price_eth > 0:
                    multiplier = fresh / position.entry_price_eth
                    position.pending_exit["multiplier"] = multiplier
                    if multiplier <= position.trailing_stop_multiplier:
                        position.pending_exit.update(rung="CLOSE", reason="Stop while exit pending",
                                                     tokens=position.remaining_tokens)
                    self._persist(position)
            return await self._execute_exit(position)
        now = datetime.now(timezone.utc)
        entry = position.entry_time
        if entry.tzinfo is None:
            entry = entry.replace(tzinfo=timezone.utc)
        elapsed = (now - entry).total_seconds() / 60
        derisked = position.tp1_hit or position.tp2_hit
        timeout = self.config.RUNNER_TIMEOUT_MINUTES if derisked else self.config.STAGNANT_TIMEOUT_MINUTES
        price = await self.dex_trader.get_token_price_eth(position.contract_address)
        if not math.isfinite(price) or price <= 0:
            logger.warning("No usable price for $%s; position remains open (age %.1fm)", position.ticker, elapsed)
            return False
        if not math.isfinite(position.entry_price_eth) or position.entry_price_eth <= 0 or position.tokens_bought <= 0:
            raise ValueError("invalid entry measurement; refusing to invent an entry price")
        multiplier = price / position.entry_price_eth
        if multiplier > 50 * max(1.0, position.peak_multiplier):
            logger.warning("Rejected outlier price for $%s", position.ticker)
            return False
        old_peak, old_stop = position.peak_multiplier, position.trailing_stop_multiplier
        position.peak_multiplier = max(1.0, old_peak, multiplier)
        if position.tp2_hit:
            position.trailing_stop_multiplier = max(old_stop, 1.20, position.peak_multiplier - self.config.TRAILING_STOP_DELTA)
        elif position.tp1_hit:
            position.trailing_stop_multiplier = max(old_stop, 0.95, position.peak_multiplier - self.config.TRAILING_STOP_DELTA)
        else:
            position.trailing_stop_multiplier = self.config.SL_MULTIPLIER
        if (old_peak, old_stop) != (position.peak_multiplier, position.trailing_stop_multiplier):
            self._persist(position)
        logger.info("Tracking $%s: Multiplier: %.2fx (Peak: %.2fx) | Stop: %.2fx | Time: %.1fm",
                    position.ticker, multiplier, position.peak_multiplier, position.trailing_stop_multiplier, elapsed)
        rung = reason = None
        tokens = position.remaining_tokens
        # Stops and time limits take priority over another profit tranche.
        if multiplier <= position.trailing_stop_multiplier:
            rung, reason = "CLOSE", "Trailing stop" if derisked else "Initial stop loss"
        elif elapsed >= timeout:
            rung, reason = "CLOSE", "Position timeout"
        elif not position.tp1_hit and multiplier >= self.config.TP1_MULTIPLIER:
            rung, reason = "TP1", "First profit target"
            tokens = min(tokens, position.tokens_bought * self.config.TP1_RATIO - position.tp1_sold_tokens)
        elif position.tp1_hit and not position.tp2_hit and multiplier >= self.config.TP2_MULTIPLIER:
            rung, reason = "TP2", "Second profit target"
            tokens = min(tokens, position.tokens_bought * self.config.TP2_RATIO - position.tp2_sold_tokens)
        elif position.tp2_hit and multiplier >= self.config.TP3_MULTIPLIER:
            rung, reason = "CLOSE", "Final profit target"
            position.tp3_hit = True
        if rung and tokens > 0:
            position.pending_exit = dict(rung=rung, reason=reason, multiplier=multiplier, tokens=tokens)
            self._persist(position)
            return await self._execute_exit(position)
        return False

    async def _monitor(self, position):
        try:
            # Fresh entries set remaining_tokens before persistence. Retain legacy
            # recovery for snapshots written before that field was initialized.
            if position.remaining_tokens <= 0 and not position.completed_sell_operations and not position.tx_hash_sell:
                position.remaining_tokens = position.tokens_bought
                self._persist(position)
            while not self._stopping:
                await asyncio.sleep(getattr(self.config, "PRICE_POLL_SECONDS", 2))
                if self._stopping:
                    break
                try:
                    if await self._tick(position):
                        break
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Monitor tick failed for $%s; keeping holdings tracked and retrying", position.ticker)
        except asyncio.CancelledError:
            logger.info("Monitoring cancelled for position %s", position.id)
        finally:
            self._tasks.pop(position.id, None)
