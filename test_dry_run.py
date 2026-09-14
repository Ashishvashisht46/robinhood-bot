import asyncio
from datetime import datetime, timezone
import random
import logging
from config import Config
from models import CallSignal, Position, PositionStatus, StrategyState
from message_parser import MessageParser
from strategy_engine import StrategyEngine
from logger_setup import setup_logging

async def run_test():
    setup_logging()
    logger = logging.getLogger("copytrader")
    
    config = Config()
    config.DRY_RUN = True
    config.INITIAL_CAPITAL_USD = 50.0
    config.SAFETY_FLOOR_USD = 40.0
    config.BASELINE_STAKE_USD = 2.0
    config.COMPOUND_STAKE_USD = 5.0
    config.RR_MULTIPLIER = 1.5
    
    parser = MessageParser()
    engine = StrategyEngine(config, load_state=False)
    eth_price = 2500.0
    
    logger.info("=" * 60)
    logger.info("RUNNING 10-TRADE DRY RUN & PARSER SIMULATION")
    logger.info("=" * 60)
    logger.info(f"Initial Balance: ${config.INITIAL_CAPITAL_USD:.2f} | Floor: ${config.SAFETY_FLOOR_USD:.2f}")

    # Test parser on sample telegram message
    sample_tg_text = """🚨 EARLY CALL — $TITAN · robinhood
💰 called at $79k
📊 Pool Info
🏛 DEX: Pons V2
📈 Mcap: $79k
💧 Liq: $25k | 31.7%
🧾 Tax: B 0% | S 0%
🪙 Token Info
⏱ Age: 17m · 🚀 pons_v2
👥 Holders: 300
📊 Vol 24h: $145k
🔥 461 swaps (5m)
🔎 Proof: 3 elite + 3 good holding — validated on-chain
📹 Live buys (💎 elite · ✅ good)
✅ $703 · 0x4fe6348921890312890321890312890321890abe
⚠️ dyor, nfa"""

    parsed_signal = parser.parse(sample_tg_text, [], 1001, datetime.now(timezone.utc))
    assert parsed_signal is not None, "Parser failed on sample message"
    assert parsed_signal.ticker == "TITAN", f"Parsed ticker was {parsed_signal.ticker}"
    logger.info(f"✓ Parser Test Passed: Extracted ${parsed_signal.ticker}, Mcap=${parsed_signal.mcap_usd:,.0f}, Liq=${parsed_signal.liquidity_usd:,.0f}")

    # Ensure a contract address for simulation
    parsed_signal.contract_address = "0x8876789976decbfcbbbe364623c63652db8c0904"

    random.seed(123)

    for i in range(1, 11):
        decision = engine.get_trade_decision(parsed_signal, eth_price)
        if not decision.should_trade:
            logger.warning(f"Trade #{i:02d}: SKIPPED — Reason: {decision.reason}")
            if engine.state == StrategyState.HALTED:
                logger.critical(f"CIRCUIT BREAKER TRIGGERED! Balance: ${engine.balance_usd:.2f} <= Floor: ${config.SAFETY_FLOOR_USD:.2f}")
                break
            continue
            
        logger.info(f"Trade #{i:02d} [{decision.state.value.upper()}] — Staking ${decision.stake_usd:.2f} ({decision.stake_eth:.5f} ETH)")
        
        # Simulate 3-Tier Multi-TP distribution:
        # 30% Full Moonshot Runner (+47% to +75% gain)
        # 30% Mid Pump with Trailing Stop (+30% to +45% gain)
        # 20% Fast Tier 1 De-Risk (+5% to +10% gain)
        # 20% Early Rug Stop-Loss (-50% loss)
        r = random.random()
        if r < 0.30:
            is_win = True
            pnl_usd = decision.stake_usd * 0.47  # Full TP1 + TP2 + TP3
            trade_desc = "🚀 TP1 + TP2 + TP3 MOONSHOT HIT (+47%)"
        elif r < 0.60:
            is_win = True
            pnl_usd = decision.stake_usd * 0.35  # TP1 + TP2 + Trailing Stop
            trade_desc = "🎯 TP1 + TP2 + DYNAMIC TRAIL HIT (+35%)"
        elif r < 0.80:
            is_win = True
            pnl_usd = decision.stake_usd * 0.08  # TP1 + 0.95x Stop
            trade_desc = "🛡️ TP1 FAST DE-RISK LOCKED (+8%)"
        else:
            is_win = False
            pnl_usd = -decision.stake_usd * 0.50  # -50% loss (0.50x Stop Loss)
            trade_desc = "🛑 INITIAL STOP LOSS (-50%)"
        
        pos = Position(
            ticker=f"TKN{i}",
            contract_address="0x" + "1" * 40,
            stake_usd=decision.stake_usd,
            stake_eth=decision.stake_usd / eth_price,
            entry_price_eth=eth_price,
            entry_price_usd=eth_price,
            status=PositionStatus.CLOSED_TP if is_win else PositionStatus.CLOSED_SL,
            pnl_usd=pnl_usd
        )
        
        engine.record_trade_result(pos, is_win)
        logger.info(
            f"  Result: {trade_desc} (PnL: ${pnl_usd:+.2f}) "
            f"| Balance: ${engine.balance_usd:.2f} | Next State: {engine.state.value.upper()}"
        )
        
    logger.info("=" * 60)
    logger.info("FINAL SIMULATION REPORT:")
    report = engine.get_status_report()
    for k, v in report.items():
        logger.info(f"  {k}: {v}")
    logger.info("=" * 60)

if __name__ == "__main__":
    asyncio.run(run_test())
