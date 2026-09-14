import os
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv

_BASE_DIR = Path(__file__).resolve().parent
_ENV_PATH = _BASE_DIR / ".env"
# Always load the .env that sits next to this file, not whatever folder you launched Python from.
load_dotenv(_ENV_PATH, override=True)
if not _ENV_PATH.exists():
    load_dotenv(Path.cwd() / ".env", override=True)

class Config:
    WETH_ADDRESS = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"
    PONS_V2_FACTORY = "0xCcC88a9d1B4ED6b0EABA998850414b24f1c315bE"
    UNISWAP_ROUTER = "0x38140F2D60383Fb4d1Dd24d7092130d59a60CD6f"
    EXPLORER_API_BASE = "https://robinhoodchain.blockscout.com/api"

    def __init__(self):
        self.TELEGRAM_API_ID: Optional[int] = int(os.getenv("TELEGRAM_API_ID")) if os.getenv("TELEGRAM_API_ID") else None
        self.TELEGRAM_API_HASH: str = os.getenv("TELEGRAM_API_HASH", "")
        self.TELEGRAM_PHONE: str = os.getenv("TELEGRAM_PHONE", "")
        self.TELEGRAM_CHANNEL: str = os.getenv("TELEGRAM_CHANNEL", "scoutrobinhood")
        self.CHANNEL_USERNAME: str = self.TELEGRAM_CHANNEL

        pk = os.getenv("PRIVATE_KEY", "").strip()
        if pk and not pk.startswith("0x"):
            pk = "0x" + pk
        self.PRIVATE_KEY: str = pk

        # No keyed endpoint as a default: the URL that used to sit here carried a
        # live API key in a public repo. Set RPC_URL in .env; this keyless public
        # node is the fallback.
        self.RPC_URL: str = os.getenv(
            "RPC_URL", "https://robinhood-rpc.publicnode.com")
        self.RPC_WSS_URL: str = os.getenv("RPC_WSS_URL", "")
        self.FALLBACK_RPC_URL: str = os.getenv("FALLBACK_RPC_URL", "https://robinhood-rpc.publicnode.com")
        self.EXTRA_RPC_URLS: str = os.getenv("EXTRA_RPC_URLS", "https://4663.rpc.thirdweb.com")
        self.CHAIN_ID: int = int(os.getenv("CHAIN_ID", "4663"))
        self.DRY_RUN: bool = str(os.getenv("DRY_RUN", "true")).lower() == "true"

        self.INITIAL_CAPITAL_USD: float = float(os.getenv("INITIAL_CAPITAL_USD", "127.65"))
        self.SAFETY_FLOOR_USD: float = float(os.getenv("SAFETY_FLOOR_USD", "40.0"))
        self.BASELINE_STAKE_USD: float = float(os.getenv("BASELINE_STAKE_USD", "2.0"))
        self.COMPOUND_STAKE_USD: float = float(os.getenv("COMPOUND_STAKE_USD", "5.0"))
        self.TP1_MULTIPLIER: float = float(os.getenv("TP1_MULTIPLIER", "1.20"))
        self.TP1_RATIO: float = float(os.getenv("TP1_RATIO", "0.40"))
        self.TP2_MULTIPLIER: float = float(os.getenv("TP2_MULTIPLIER", "1.40"))
        self.TP2_RATIO: float = float(os.getenv("TP2_RATIO", "0.40"))
        self.TP3_MULTIPLIER: float = float(os.getenv("TP3_MULTIPLIER", "5.00"))
        self.TRAILING_STOP_DELTA: float = float(os.getenv("TRAILING_STOP_DELTA", "0.25"))
        self.SL_MULTIPLIER: float = float(os.getenv("SL_MULTIPLIER", "0.50"))
        self.RR_MULTIPLIER: float = float(os.getenv("RR_MULTIPLIER", "1.5"))
        self.BUFFER_GATE_USD: float = float(os.getenv("BUFFER_GATE_USD", "60.0"))

        self.MIN_LIQUIDITY_USD: float = float(os.getenv("MIN_LIQUIDITY_USD", "5000"))
        self.MAX_BUY_TAX: float = float(os.getenv("MAX_BUY_TAX", "0"))
        self.MAX_SELL_TAX: float = float(os.getenv("MAX_SELL_TAX", "0"))
        self.MIN_HOLDERS: int = int(os.getenv("MIN_HOLDERS", "30"))
        self.MAX_TOKEN_AGE_MINUTES: int = int(os.getenv("MAX_TOKEN_AGE_MINUTES", "60"))

        self.SLIPPAGE_PCT: float = float(os.getenv("SLIPPAGE_PCT", "15"))
        self.STAGNANT_TIMEOUT_MINUTES: int = int(os.getenv("STAGNANT_TIMEOUT_MINUTES", "25"))
        self.RUNNER_TIMEOUT_MINUTES: int = int(os.getenv("RUNNER_TIMEOUT_MINUTES", "120"))
        self.POSITION_TIMEOUT_MINUTES: int = self.STAGNANT_TIMEOUT_MINUTES
        self.PRICE_POLL_SECONDS: int = int(os.getenv("PRICE_POLL_SECONDS", "2"))
        self.MAX_CONCURRENT_POSITIONS: int = int(os.getenv("MAX_CONCURRENT_POSITIONS", "3"))
        self.GAS_MULTIPLIER: float = float(os.getenv("GAS_MULTIPLIER", "1.3"))
        self.FAST_EXECUTION_MODE: bool = str(os.getenv("FAST_EXECUTION_MODE", "true")).lower() == "true"
        self.BUY_EVERY_SIGNAL: bool = str(os.getenv("BUY_EVERY_SIGNAL", "true")).lower() == "true"
        # Resolving a contract address from the TICKER is unsafe on this chain:
        # a live search for "ROACH" returns 6 different tokens, and the most
        # liquid one is not the one that was called. Anyone can mint a clone of
        # a ticker, which is exactly how a honeypot gets bought. Off by default;
        # the contract address should come from the call message's buttons.
        # Tokens held deliberately (e.g. USDG kept as swap liquidity). status.py
        # will not flag these as stranded. Comma-separated addresses.
        self.RESERVED_TOKENS: set = {
            a.strip().lower() for a in os.getenv("RESERVED_TOKENS", "").split(",") if a.strip()
        }
        self.ALLOW_TICKER_FALLBACK: bool = str(os.getenv("ALLOW_TICKER_FALLBACK", "false")).lower() == "true"
        self.BASE_DIR = str(_BASE_DIR)
        self.ENV_PATH = str(_ENV_PATH)

    def validate(self):
        if not self.TELEGRAM_API_ID:
            raise ValueError("TELEGRAM_API_ID is required")
        if not self.TELEGRAM_API_HASH:
            raise ValueError("TELEGRAM_API_HASH is required")
        if not self.DRY_RUN:
            if not self.PRIVATE_KEY:
                raise ValueError("PRIVATE_KEY is required when DRY_RUN is false")
            if not self.PRIVATE_KEY.startswith("0x"):
                raise ValueError("PRIVATE_KEY must start with 0x")
            if len(self.PRIVATE_KEY) != 66:
                raise ValueError("PRIVATE_KEY format is invalid, must be 66 characters long starting with 0x")

config = Config()
