import json
import logging
import os
from typing import List
from models import StrategyState, TradeDecision, CallSignal, Position

logger = logging.getLogger("copytrader")

class StrategyEngine:
    def __init__(self, config, load_state: bool = True):
        self.config = config
        self.state = StrategyState.BASELINE
        self.balance_usd = config.INITIAL_CAPITAL_USD
        self.total_trades = 0
        self.wins = 0
        self.losses = 0
        self.consecutive_losses = 0
        self.max_consecutive_losses = 0
        self.total_pnl_usd = 0.0
        self.open_positions: List[Position] = []
        self.trade_history: List[dict] = []
        self._state_file = os.path.join(os.path.dirname(__file__), "cache", "strategy_state.json")
        if load_state:
            self._load_state()
            self._check_circuit_breaker()

    def reset_state(self):
        self.state = StrategyState.BASELINE
        self.balance_usd = self.config.INITIAL_CAPITAL_USD
        self.total_trades = 0
        self.wins = 0
        self.losses = 0
        self.consecutive_losses = 0
        self.max_consecutive_losses = 0
        self.total_pnl_usd = 0.0
        self.open_positions.clear()
        self.trade_history.clear()
        self._save_state()
        logger.info(f"🔄 Strategy state reset to initial balance: ${self.balance_usd:.2f}")

    def _load_state(self):
        try:
            if os.path.exists(self._state_file):
                with open(self._state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.balance_usd = float(data.get("balance_usd", self.balance_usd))
                    self.wins = int(data.get("wins", self.wins))
                    self.losses = int(data.get("losses", self.losses))
                    self.total_trades = int(data.get("total_trades", self.total_trades))
                    self.total_pnl_usd = float(data.get("total_pnl_usd", self.total_pnl_usd))
                    state_name = data.get("state", "BASELINE")
                    self.state = StrategyState[state_name] if state_name in StrategyState.__members__ else StrategyState.BASELINE

                    # Open positions survive a restart. They are NOT resumed here --
                    # main.py reconciles each one against its real on-chain balance
                    # before monitoring restarts, because this file is a snapshot and
                    # the chain is the authority on what is actually still held.
                    self.open_positions = []
                    for raw in data.get("open_positions", []):
                        try:
                            self.open_positions.append(Position.from_dict(raw))
                        except Exception as e:
                            logger.error(f"Could not restore a persisted position ({e}); skipping: {raw}")

                    logger.info(
                        f"💾 Restored persistent state: Balance=${self.balance_usd:.2f} | "
                        f"State={self.state.name} | Trades={self.total_trades} (W:{self.wins} L:{self.losses}) | "
                        f"Open positions carried over: {len(self.open_positions)}"
                    )
        except Exception as e:
            logger.warning(f"Could not load state from {self._state_file}: {e}")

    def _save_state(self):
        try:
            os.makedirs(os.path.dirname(self._state_file), exist_ok=True)
            data = {
                "balance_usd": self.balance_usd,
                "wins": self.wins,
                "losses": self.losses,
                "total_trades": self.total_trades,
                "total_pnl_usd": self.total_pnl_usd,
                "state": self.state.name,
                "open_positions": [p.to_dict() for p in self.open_positions],
            }
            # Write-then-rename: a crash midway through writing this file used to be
            # able to truncate it, which would lose the open positions it now holds.
            tmp = self._state_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._state_file)
        except Exception as e:
            logger.warning(f"Could not save state to {self._state_file}: {e}")

    def save(self):
        """Public save point for callers that mutate a live position (partial fills)."""
        self._save_state()

    def get_trade_decision(self, signal: CallSignal, eth_price_usd: float) -> TradeDecision:
        if self.state == StrategyState.HALTED:
            return TradeDecision(False, 0.0, 0.0, self.state, "Halted circuit breaker")
        self._check_circuit_breaker()
        if self.state == StrategyState.HALTED:
            return TradeDecision(False, 0.0, 0.0, self.state, "Halted circuit breaker")
        if len(self.open_positions) >= self.config.MAX_CONCURRENT_POSITIONS:
            return TradeDecision(False, 0.0, 0.0, self.state, "Max concurrent positions")
        if not signal.contract_address:
            return TradeDecision(False, 0.0, 0.0, self.state, "No contract address")
        if any(p.contract_address.lower() == signal.contract_address.lower() for p in self.open_positions):
            return TradeDecision(False, 0.0, 0.0, self.state, "Position already open for token")

        buy_every = bool(getattr(self.config, "BUY_EVERY_SIGNAL", True))
        if not buy_every:
            if signal.buy_tax > self.config.MAX_BUY_TAX or signal.sell_tax > self.config.MAX_SELL_TAX:
                return TradeDecision(False, 0.0, 0.0, self.state, "High tax")
            if signal.liquidity_usd is not None and signal.liquidity_usd < self.config.MIN_LIQUIDITY_USD:
                return TradeDecision(False, 0.0, 0.0, self.state, "Low liquidity")
            if signal.holders is not None and signal.holders < self.config.MIN_HOLDERS:
                return TradeDecision(False, 0.0, 0.0, self.state, "Low holders")

        if signal.dex and "long" in str(signal.dex).lower():
            return TradeDecision(False, 0.0, 0.0, self.state, "Skipping Longxyz (Proprietary signature-gated launchpad)")

        stake_usd = self.config.BASELINE_STAKE_USD if self.state == StrategyState.BASELINE else self.config.COMPOUND_STAKE_USD
        if hasattr(self.config, "BUFFER_GATE_USD") and self.config.BUFFER_GATE_USD > 0 and self.balance_usd < self.config.BUFFER_GATE_USD:
            stake_usd = self.config.BASELINE_STAKE_USD
            self.state = StrategyState.BASELINE
        if self.balance_usd - stake_usd < self.config.SAFETY_FLOOR_USD:
            return TradeDecision(False, 0.0, 0.0, self.state, "Not enough balance above floor")

        stake_eth = stake_usd / eth_price_usd if eth_price_usd > 0 else 0.0
        reason = "Buy every parsed signal" if buy_every else "Met criteria"
        return TradeDecision(True, stake_usd, stake_eth, self.state, reason)

    def record_trade_result(self, position: Position, is_win: bool):
        pnl = position.pnl_usd or 0.0
        self.balance_usd += pnl
        self.total_pnl_usd += pnl
        self.total_trades += 1
        if is_win:
            self.wins += 1
            self.consecutive_losses = 0
            self.state = StrategyState.COMPOUND if self.state == StrategyState.BASELINE else StrategyState.BASELINE
        else:
            self.losses += 1
            self.consecutive_losses += 1
            self.max_consecutive_losses = max(self.max_consecutive_losses, self.consecutive_losses)
            self.state = StrategyState.BASELINE
        self._check_circuit_breaker()
        self._save_state()
        entry = {"position_id": position.id, "pnl": pnl, "is_win": is_win, "balance": self.balance_usd, "state": self.state.name}
        self.trade_history.append(entry)
        logger.info(f"Recorded trade: {entry}")

    def get_status_report(self) -> dict:
        return {
            "state": self.state.name,
            "balance_usd": self.balance_usd,
            "total_pnl_usd": self.total_pnl_usd,
            "trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "open_positions": len(self.open_positions)
        }

    def add_position(self, position: Position):
        self.open_positions.append(position)
        self._save_state()

    def remove_position(self, position_id: str):
        self.open_positions = [p for p in self.open_positions if p.id != position_id]
        self._save_state()

    def _check_circuit_breaker(self):
        if self.balance_usd <= self.config.SAFETY_FLOOR_USD:
            self.state = StrategyState.HALTED
            logger.critical("SAFETY FLOOR REACHED. Trading halted.")
