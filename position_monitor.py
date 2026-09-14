import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, Any, Callable, Optional
from models import Position, PositionStatus
from trade_ledger import log_event

logger = logging.getLogger("copytrader")


class PositionMonitor:
    def __init__(self, config, dex_trader, chain_client, on_position_closed: Callable[[Position, bool], Any],
                 on_position_changed: Optional[Callable[[Position], Any]] = None):
        self.config = config
        self.dex_trader = dex_trader
        self.chain_client = chain_client
        self.on_position_closed = on_position_closed
        # Called after a partial fill so the change reaches disk. Without it a
        # restart after TP1 would reload the position as untouched and sell that
        # 40% a second time.
        self.on_position_changed = on_position_changed
        self._tasks: Dict[str, asyncio.Task] = {}

    def _persist(self, position: Position) -> None:
        if not self.on_position_changed:
            return
        try:
            self.on_position_changed(position)
        except Exception as e:
            logger.error(f"Could not persist position {position.id}: {e}")

    async def start_monitoring(self, position: Position):
        if position.id in self._tasks:
            return
        self._tasks[position.id] = asyncio.create_task(self._monitor(position))

    async def stop_monitoring(self, position_id: str):
        if position_id in self._tasks:
            self._tasks[position_id].cancel()
            del self._tasks[position_id]

    async def stop_all(self):
        for task in self._tasks.values():
            task.cancel()
        self._tasks.clear()

    async def _sell_or_retry(self, position: Position, tokens: float, slippage: float, tag: str) -> float:
        if tokens <= 0:
            return 0.0
        last_err = None
        for attempt in range(2):
            try:
                tx, sold = await self.dex_trader.sell_token(position.contract_address, tokens, slippage)
                if sold and sold > 0:
                    position.tx_hash_sell = tx
                    return float(sold)
                last_err = f"0 tokens sold tx={tx}"
            except Exception as e:
                last_err = e
                logger.warning(f"{tag} sell attempt {attempt + 1}/2 error for ${position.ticker}: {e}")
            await asyncio.sleep(1)
        logger.error(f"{tag} sell failed for ${position.ticker}: {last_err}")
        return 0.0

    async def _monitor(self, position: Position):
        try:
            if not hasattr(position, "remaining_tokens") or position.remaining_tokens <= 0:
                position.remaining_tokens = position.tokens_bought

            slippage = getattr(self.config, "SLIPPAGE_PCT", 15.0)
            tp1_target = float(getattr(self.config, "TP1_MULTIPLIER", 1.20))
            tp1_ratio = float(getattr(self.config, "TP1_RATIO", 0.40))
            tp2_target = float(getattr(self.config, "TP2_MULTIPLIER", 1.40))
            tp2_ratio = float(getattr(self.config, "TP2_RATIO", 0.40))
            tp3_target = float(getattr(self.config, "TP3_MULTIPLIER", 5.00))
            sl_target = float(getattr(self.config, "SL_MULTIPLIER", 0.50))
            trail_delta = float(getattr(self.config, "TRAILING_STOP_DELTA", 0.25))
            stagnant_timeout_mins = int(getattr(self.config, "STAGNANT_TIMEOUT_MINUTES", 25))
            runner_timeout_mins = int(getattr(self.config, "RUNNER_TIMEOUT_MINUTES", 120))

            # A resumed position keeps its ratcheted stop. Resetting these to the
            # initial values would drop a post-TP1 stop from 0.95x back to 0.50x and
            # hand back profit the ladder had already locked in.
            position.peak_multiplier = max(1.0, getattr(position, "peak_multiplier", 1.0) or 1.0)
            if not (getattr(position, "tp1_hit", False) or getattr(position, "tp2_hit", False)):
                position.trailing_stop_multiplier = sl_target

            while True:
                await asyncio.sleep(getattr(self.config, "PRICE_POLL_SECONDS", 5))
                try:
                    current_price_eth = await self.dex_trader.get_token_price_eth(position.contract_address)
                except Exception as e:
                    logger.debug(f"Price lookup temporary error for ${position.ticker}: {e}")
                    continue

                if current_price_eth <= 0.0:
                    logger.debug(f"Waiting for live DEX price quote for ${position.ticker}...")
                    continue

                if not hasattr(position, "entry_price_eth") or position.entry_price_eth <= 0.0:
                    position.entry_price_eth = current_price_eth
                    position.tokens_bought = position.stake_eth / current_price_eth if current_price_eth > 0 else position.tokens_bought
                    position.remaining_tokens = position.tokens_bought
                    self._persist(position)

                multiplier = current_price_eth / position.entry_price_eth if position.entry_price_eth > 0 else 1.0
                if multiplier > 50.0 * max(1.0, position.peak_multiplier):
                    logger.warning(
                        f"⚠️ Detected potential phantom API price spike on ${position.ticker} "
                        f"({multiplier:.1f}x vs peak {position.peak_multiplier:.1f}x). Clamping outlier quote."
                    )
                    continue

                if multiplier > position.peak_multiplier:
                    position.peak_multiplier = multiplier

                if getattr(position, "tp2_hit", False):
                    dynamic_stop = max(1.20, position.peak_multiplier - trail_delta)
                    position.trailing_stop_multiplier = max(position.trailing_stop_multiplier, dynamic_stop)
                elif getattr(position, "tp1_hit", False):
                    dynamic_stop = max(0.95, position.peak_multiplier - trail_delta)
                    position.trailing_stop_multiplier = max(position.trailing_stop_multiplier, dynamic_stop)
                else:
                    position.trailing_stop_multiplier = sl_target

                now = datetime.now(timezone.utc)
                entry_time = position.entry_time.replace(tzinfo=timezone.utc) if position.entry_time.tzinfo is None else position.entry_time
                elapsed_minutes = (now - entry_time).total_seconds() / 60

                if not getattr(position, "tp1_hit", False):
                    target_info = f"TP1: {tp1_target:.2f}x (40%)"
                    stop_info = f"SL: {position.trailing_stop_multiplier:.2f}x"
                elif not getattr(position, "tp2_hit", False):
                    target_info = f"TP2: {tp2_target:.2f}x (40%)"
                    stop_info = f"Trail SL: {position.trailing_stop_multiplier:.2f}x"
                else:
                    target_info = f"TP3 Moonbag: {tp3_target:.2f}x (20%)"
                    stop_info = f"Trail SL: {position.trailing_stop_multiplier:.2f}x (Guaranteed Profit)"

                logger.info(
                    f"Tracking ${position.ticker}: Multiplier: {multiplier:.2f}x (Peak: {position.peak_multiplier:.2f}x) | "
                    f"{target_info} | {stop_info} | Time: {elapsed_minutes:.1f}m"
                )

                if not getattr(position, "tp1_hit", False) and (multiplier >= tp1_target or round(multiplier, 2) >= tp1_target):
                    tokens_to_sell = min(position.tokens_bought * tp1_ratio, position.remaining_tokens)
                    logger.info(
                        f"🎯 TP1 HIT for ${position.ticker} ({multiplier:.2f}x >= {tp1_target:.2f}x)! "
                        f"Selling {tp1_ratio*100:.0f}% ({tokens_to_sell:,.2f} tokens)..."
                    )
                    sold = await self._sell_or_retry(position, tokens_to_sell, slippage, "TP1")
                    if sold <= 0:
                        logger.error(f"TP1 sell did not fill for ${position.ticker} — will retry next poll")
                        continue
                    position.tp1_hit = True
                    position.tp1_sold_tokens = sold
                    position.remaining_tokens = max(0.0, position.remaining_tokens - sold)
                    partial_pnl_usd = (multiplier - 1.0) * (position.stake_usd * tp1_ratio)
                    position.tp1_pnl_usd = partial_pnl_usd
                    position.accumulated_pnl_usd += partial_pnl_usd
                    position.trailing_stop_multiplier = 0.95
                    self._persist(position)
                    log_event(
                        "tranche_exit", rung="TP1", position_id=position.id,
                        ticker=position.ticker, contract_address=position.contract_address,
                        multiplier=multiplier, tokens_sold=sold,
                        pnl_usd=partial_pnl_usd, remaining_tokens=position.remaining_tokens,
                        trailing_stop_multiplier=position.trailing_stop_multiplier,
                    )
                    logger.info(
                        f"✅ TP1 COMPLETED: Sold {sold:,.2f} ${position.ticker} (Secured +${partial_pnl_usd:.2f} profit). "
                        f"Stop-Loss ratcheted to 0.95x. Remaining {position.remaining_tokens:,.2f} tokens riding to {tp2_target:.2f}x!"
                    )
                    continue

                if getattr(position, "tp1_hit", False) and not getattr(position, "tp2_hit", False) and (
                    multiplier >= tp2_target or round(multiplier, 2) >= tp2_target
                ):
                    tokens_to_sell = min(position.tokens_bought * tp2_ratio, position.remaining_tokens)
                    logger.info(
                        f"🎯 TP2 HIT for ${position.ticker} ({multiplier:.2f}x >= {tp2_target:.2f}x)! "
                        f"Selling {tp2_ratio*100:.0f}% ({tokens_to_sell:,.2f} tokens)..."
                    )
                    sold = await self._sell_or_retry(position, tokens_to_sell, slippage, "TP2")
                    if sold <= 0:
                        logger.error(f"TP2 sell did not fill for ${position.ticker} — will retry next poll")
                        continue
                    position.tp2_hit = True
                    position.tp2_sold_tokens = sold
                    position.remaining_tokens = max(0.0, position.remaining_tokens - sold)
                    partial_pnl_usd = (multiplier - 1.0) * (position.stake_usd * tp2_ratio)
                    position.tp2_pnl_usd = partial_pnl_usd
                    position.accumulated_pnl_usd += partial_pnl_usd
                    position.trailing_stop_multiplier = 1.20
                    self._persist(position)
                    log_event(
                        "tranche_exit", rung="TP2", position_id=position.id,
                        ticker=position.ticker, contract_address=position.contract_address,
                        multiplier=multiplier, tokens_sold=sold,
                        pnl_usd=partial_pnl_usd, remaining_tokens=position.remaining_tokens,
                        trailing_stop_multiplier=position.trailing_stop_multiplier,
                    )
                    logger.info(
                        f"✅ TP2 COMPLETED: Sold {sold:,.2f} ${position.ticker} (Secured +${partial_pnl_usd:.2f} profit). "
                        f"🎉 PRINCIPAL 100% RETURNED TO WALLET! Remaining {position.remaining_tokens:,.2f} (20% Moonbag) riding risk-free to {tp3_target:.2f}x!"
                    )
                    continue

                should_close = False
                close_reason = ""
                tokens_to_close = position.remaining_tokens
                is_derisked = getattr(position, "tp1_hit", False) or getattr(position, "tp2_hit", False)

                if getattr(position, "tp2_hit", False) and multiplier >= tp3_target:
                    should_close = True
                    close_reason = f"🚀 TP3 MOONSHOT HIT ({multiplier:.2f}x >= {tp3_target:.2f}x)"
                    position.tp3_hit = True
                elif is_derisked and multiplier <= position.trailing_stop_multiplier:
                    should_close = True
                    close_reason = (
                        f"Dynamic Trailing Stop Hit ({multiplier:.2f}x <= {position.trailing_stop_multiplier:.2f}x "
                        f"| Peak: {position.peak_multiplier:.2f}x)"
                    )
                elif not getattr(position, "tp1_hit", False) and multiplier <= sl_target and current_price_eth > 0 and elapsed_minutes >= 0.15:
                    should_close = True
                    close_reason = f"Initial Stop Loss ({multiplier:.2f}x <= {sl_target:.2f}x)"
                elif not is_derisked and elapsed_minutes > stagnant_timeout_mins:
                    should_close = True
                    close_reason = f"Stagnation Timeout ({elapsed_minutes:.1f}m > {stagnant_timeout_mins}m)"
                elif is_derisked and elapsed_minutes > runner_timeout_mins:
                    should_close = True
                    close_reason = f"Moonbag Hold Limit ({elapsed_minutes:.1f}m > {runner_timeout_mins}m)"

                if should_close:
                    logger.info(f"Closing position ${position.ticker}: {close_reason}")
                    sold = await self._sell_or_retry(position, tokens_to_close, slippage, "CLOSE")
                    if sold <= 0 and tokens_to_close > 0:
                        logger.error(f"Close sell did not fill for ${position.ticker} — will retry next poll")
                        continue
                    sell_tx = position.tx_hash_sell or ""
                    remaining_ratio = position.remaining_tokens / position.tokens_bought if position.tokens_bought > 0 else 1.0
                    final_leg_pnl_usd = (multiplier - 1.0) * (position.stake_usd * remaining_ratio)
                    total_pnl_usd = (position.tp1_pnl_usd or 0.0) + (position.tp2_pnl_usd or 0.0) + final_leg_pnl_usd
                    is_win = total_pnl_usd > 0.0
                    position.pnl_usd = total_pnl_usd
                    position.pnl_pct = (total_pnl_usd / position.stake_usd) * 100 if position.stake_usd else 0.0
                    position.status = PositionStatus.CLOSED_TP if is_win else PositionStatus.CLOSED_SL
                    position.exit_time = datetime.now(timezone.utc)
                    position.exit_price_eth = current_price_eth
                    position.tx_hash_sell = sell_tx
                    if asyncio.iscoroutinefunction(self.on_position_closed):
                        await self.on_position_closed(position, is_win)
                    else:
                        self.on_position_closed(position, is_win)
                    break

        except asyncio.CancelledError:
            logger.info(f"Monitoring cancelled for position {position.id}")
        except Exception as e:
            logger.error(f"Error in monitor loop for {position.ticker}: {e}", exc_info=True)
            position.status = PositionStatus.CLOSED_ERROR
            if asyncio.iscoroutinefunction(self.on_position_closed):
                await self.on_position_closed(position, False)
            else:
                self.on_position_closed(position, False)
        finally:
            self._tasks.pop(position.id, None)
