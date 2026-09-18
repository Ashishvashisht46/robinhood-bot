import os
import sys
import json
import logging
from execution_guard import ExecutionUncertain, PreflightFailure
import asyncio
from chain_client import eip1559_fees
import time
import aiohttp
from typing import Tuple, Dict, Any, Optional
from web3 import Web3
import eth_abi

try:
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
except (ExecutionUncertain, PreflightFailure):
    raise
except Exception:
    pass

logger = logging.getLogger("copytrader")

from stock_v4_routes import (
    StockV4Router,
    STOCK_TOKENS as EXTENDED_STOCK_TOKENS,
    STOCK_LIST as EXTENDED_STOCK_LIST,
    fetch_dexscreener,
    map_ds_pair,
    pool_id_from_key,
    encode_v3_path,
)

V3_QUOTER_ABI = [
    {"inputs": [{"components": [
        {"name": "tokenIn", "type": "address"}, {"name": "tokenOut", "type": "address"},
        {"name": "amountIn", "type": "uint256"}, {"name": "fee", "type": "uint24"},
        {"name": "sqrtPriceLimitX96", "type": "uint160"}], "name": "params", "type": "tuple"}],
     "name": "quoteExactInputSingle",
     "outputs": [{"name": "amountOut", "type": "uint256"}, {"name": "sqrtPriceX96After", "type": "uint160"},
                 {"name": "initializedTicksCrossed", "type": "uint32"}, {"name": "gasEstimate", "type": "uint256"}],
     "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "path", "type": "bytes"}, {"name": "amountIn", "type": "uint256"}],
     "name": "quoteExactInput",
     "outputs": [{"name": "amountOut", "type": "uint256"}, {"name": "sqrtPriceX96AfterList", "type": "uint160[]"},
                 {"name": "initializedTicksCrossedList", "type": "uint32[]"}, {"name": "gasEstimate", "type": "uint256"}],
     "stateMutability": "nonpayable", "type": "function"},
]

V4_QUOTER_ABI = [
    {"inputs": [{"components": [
        {"components": [{"name": "currency0", "type": "address"}, {"name": "currency1", "type": "address"},
                        {"name": "fee", "type": "uint24"}, {"name": "tickSpacing", "type": "int24"},
                        {"name": "hooks", "type": "address"}], "name": "poolKey", "type": "tuple"},
        {"name": "zeroForOne", "type": "bool"}, {"name": "exactAmount", "type": "uint128"},
        {"name": "hookData", "type": "bytes"}], "name": "params", "type": "tuple"}],
     "name": "quoteExactInputSingle",
     "outputs": [{"name": "amountOut", "type": "uint256"}, {"name": "gasEstimate", "type": "uint256"}],
     "stateMutability": "nonpayable", "type": "function"},
]

# =============== STOCK TOKENS (Robinhood Chain) ===============
STOCK_TOKENS = EXTENDED_STOCK_TOKENS
STOCK_LIST = EXTENDED_STOCK_LIST

WETH = Web3.to_checksum_address("0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73")
USDG = Web3.to_checksum_address("0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168")
UNIVERSAL_ROUTER = Web3.to_checksum_address("0x8876789976dEcBfCbBbe364623C63652db8C0904")
ZERO = "0x0000000000000000000000000000000000000000"

# ─── Hardcoded Addresses (Per Specification) ──────────────────────────────
WETH_ADDRESS            = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"
USDG_ADDRESS            = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"
PERMIT2_ADDRESS         = "0x000000000022D473030F116dDEE9F6B43aC78BA3"
UNIVERSAL_ROUTER_ADDR   = "0x8876789976dEcBfCbBbe364623C63652db8C0904"

UNISWAP_V2_FACTORY      = "0x8bcEaA40B9AcdfAedF85AdF4FF01F5Ad6517937f"
UNISWAP_V2_ROUTER       = "0x89e5DB8B5aA49aA85AC63f691524311AEB649eba"
UNISWAP_V3_FACTORY      = "0x1f7d7550B1b028f7571E69A784071F0205FD2EfA"
UNISWAP_V3_SWAPROUTER02 = "0xCaf681a66D020601342297493863E78C959E5cb2"
UNISWAP_V3_POS_MGR     = "0x73991a25C818Bf1f1128dEAaB1492D45638DE0D3"
UNISWAP_V3_QUOTER_V2   = "0x33e885eD0Ec9bF04EcfB19341582aADCb4c8A9E7"
UNISWAP_V4_POOL_MGR     = "0x8366a39CC670B4001A1121B8F6A443A643e40951"
UNISWAP_V4_STATE_VIEW   = "0xF3334192D15450CdD385c8B70e03F9A6bD9E673b"
UNISWAP_V4_QUOTER       = "0x8Dc178eFB8111BB0973Dd9d722ebeFF267c98F94"
UNISWAP_V4_POS_MGR     = "0x58daec3116aae6D93017bAAea7749052E8a04fA7"

PONS_V2_FACTORY         = "0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e"
PONS_V2_HOOK            = "0xe5e702641ea86f4ae6cc3cdaed2b886f976be044"
PONS_V1_FACTORY         = "0xA5aAb3F0c6EeadF30Ef1D3Eb997108E976351feB"
BAGS_FACTORY            = "0xe8Cc4431adF8b5A847C113EF0c6af9043219Cb37"
BAGS_LENS               = "0xC82Db941dAf90B754aecb5F7D14c683dc608d595"
BAGS_HOOK               = "0x2380aBf72C17aABAb76480244759AC7E2932EEcC"

PONS_LAUNCHED_TOPIC     = "0x8d4aad4953d0ca700d468f3753aa14432d1b35b43ec6409f051fb6aa43a89607"

# ─── ABIs ───────────────────────────────────────────────────────────────
ERC20_ABI = [
    {"inputs": [{"name": "account", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}], "name": "approve", "outputs": [{"name": "", "type": "bool"}], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}], "name": "allowance", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
]

V3_FACTORY_ABI = [
    {"inputs": [{"name": "tokenA", "type": "address"}, {"name": "tokenB", "type": "address"}, {"name": "fee", "type": "uint24"}], "name": "getPool", "outputs": [{"name": "pool", "type": "address"}], "stateMutability": "view", "type": "function"}
]

V2_FACTORY_ABI = [
    {"inputs": [{"name": "tokenA", "type": "address"}, {"name": "tokenB", "type": "address"}], "name": "getPair", "outputs": [{"name": "pair", "type": "address"}], "stateMutability": "view", "type": "function"}
]

BAGS_LENS_ABI = [
    {"inputs": [{"name": "token", "type": "address"}], "name": "getTokenState", "outputs": [{"name": "curve", "type": "address"}, {"name": "migrated", "type": "bool"}], "stateMutability": "view", "type": "function"}
]

STATE_VIEW_ABI = [
    {
        "inputs": [{"name": "poolId", "type": "bytes32"}],
        "name": "getSlot0",
        "outputs": [
            {"name": "sqrtPriceX96", "type": "uint160"},
            {"name": "tick", "type": "int24"},
            {"name": "protocolFee", "type": "uint24"},
            {"name": "lpFee", "type": "uint24"}
        ],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [{"name": "poolId", "type": "bytes32"}],
        "name": "getLiquidity",
        "outputs": [{"name": "", "type": "uint128"}],
        "stateMutability": "view",
        "type": "function"
    }
]

UNIVERSAL_ROUTER_ABI = [
    {
        "name": "execute",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [
            {"name": "commands", "type": "bytes"},
            {"name": "inputs",   "type": "bytes[]"},
            {"name": "deadline", "type": "uint256"}
        ],
        "outputs": []
    }
]

SWAP_ROUTER02_ABI = [
    {
        "inputs": [
            {
                "components": [
                    {"name": "tokenIn", "type": "address"},
                    {"name": "tokenOut", "type": "address"},
                    {"name": "fee", "type": "uint24"},
                    {"name": "recipient", "type": "address"},
                    {"name": "amountIn", "type": "uint256"},
                    {"name": "amountOutMinimum", "type": "uint256"},
                    {"name": "sqrtPriceLimitX96", "type": "uint160"}
                ],
                "name": "params",
                "type": "tuple"
            }
        ],
        "name": "exactInputSingle",
        "outputs": [{"name": "amountOut", "type": "uint256"}],
        "stateMutability": "payable",
        "type": "function"
    },
    {
        "inputs": [
            {
                "components": [
                    {"name": "path", "type": "bytes"},
                    {"name": "recipient", "type": "address"},
                    {"name": "amountIn", "type": "uint256"},
                    {"name": "amountOutMinimum", "type": "uint256"}
                ],
                "name": "params",
                "type": "tuple"
            }
        ],
        "name": "exactInput",
        "outputs": [{"name": "amountOut", "type": "uint256"}],
        "stateMutability": "payable",
        "type": "function"
    }
]

V2_ROUTER_ABI = [
    {"inputs": [{"name": "amountOutMin", "type": "uint256"}, {"name": "path", "type": "address[]"}, {"name": "to", "type": "address"}, {"name": "deadline", "type": "uint256"}], "name": "swapExactETHForTokensSupportingFeeOnTransferTokens", "outputs": [], "stateMutability": "payable", "type": "function"}
]

PONS_CURVE_ABI = [
    {"inputs": [{"name": "quoteIn", "type": "uint256"}, {"name": "minTokensOut", "type": "uint256"}, {"name": "recipient", "type": "address"}], "name": "buy", "outputs": [{"name": "tokensReceived", "type": "uint256"}], "stateMutability": "payable", "type": "function"},
    {"inputs": [{"name": "amount", "type": "uint256"}, {"name": "minQuoteOut", "type": "uint256"}, {"name": "recipient", "type": "address"}], "name": "sell", "outputs": [{"name": "quoteReceived", "type": "uint256"}], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [], "name": "graduated", "outputs": [{"name": "", "type": "bool"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "pairToken", "outputs": [{"name": "", "type": "address"}], "stateMutability": "view", "type": "function"}
]


def make_v4_swap_input(token_in: str, token_out: str, amount_in: int, min_out: int, recipient: str, fee: int = 3000, tick_spacing: int = 60, hook_addr: str = "0x" + "0"*40, w3: Optional[Web3] = None) -> bytes:
    """
    Corrected V4 swap input encoder incorporating:
    1. 7-field ExactInputSingleParams struct (including minHopPriceX36 = 0)
    2. SETTLE_ALL for actual input token (token_in)
    3. TAKE_ALL with 2 parameters (token_out, min_out)
    """
    c0 = Web3.to_checksum_address(token_in)
    c1 = Web3.to_checksum_address(token_out)

    if int(c0, 16) < int(c1, 16):
        currency0, currency1 = c0, c1
        zero_for_one = True
    else:
        currency0, currency1 = c1, c0
        zero_for_one = False

    pool_key = (currency0, currency1, fee, tick_spacing, Web3.to_checksum_address(hook_addr))

    # Action 0x06: ExactInputSingleParams with 7 fields (including minHopPriceX36 = 0)
    param0 = eth_abi.encode(
        [
            '(address,address,uint24,int24,address)',  # poolKey
            'bool',     # zeroForOne
            'uint128',  # amountIn
            'uint128',  # amountOutMinimum
            'uint160',  # sqrtPriceLimitX96
            'uint256',  # minHopPriceX36
            'bytes'     # hookData
        ],
        [
            pool_key,
            zero_for_one,
            amount_in,
            min_out,
            0,          # sqrtPriceLimitX96 = 0
            0,          # minHopPriceX36 = 0
            b''         # hookData = b''
        ]
    )

    # Action 0x0c: SETTLE_ALL params -> settles actual input token (token_in)
    param1 = eth_abi.encode(['address', 'uint256'], [Web3.to_checksum_address(token_in), amount_in])

    # Action 0x0f: TAKE_ALL params -> 2 parameters (token_out, min_out)
    param2 = eth_abi.encode(['address', 'uint256'], [Web3.to_checksum_address(token_out), min_out])

    actions = bytes([0x06, 0x0c, 0x0f])
    return eth_abi.encode(['bytes', 'bytes[]'], [actions, [param0, param1, param2]])


def get_price_eth_dexscreener(token: str) -> float:
    import requests
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{token}",
            timeout=3,
        )
        pairs = (r.json() or {}).get("pairs") or []
        rh = [p for p in pairs if str(p.get("chainId", "")).lower() in ("robinhood", "robinhoodchain")]
        if not rh:
            rh = pairs
        if not rh:
            return 0.0
        p = max(rh, key=lambda x: float(x.get("liquidity", {}).get("usd") or 0))
        native = p.get("priceNative")
        if native:
            return float(native)
        usd = float(p.get("priceUsd") or 0)
        return usd / 2450.0 if usd else 0.0  # rough ETH usd; replace with your live ETH price if you have it
    except (ExecutionUncertain, PreflightFailure):
        raise
    except Exception:
        return 0.0


class DexTrader:
    def __init__(self, chain_client, config):
        self.chain = chain_client
        self.config = config
        self.w3 = self.chain.w3

        self.weth_address       = self.w3.to_checksum_address(WETH_ADDRESS)
        self.usdg_address       = self.w3.to_checksum_address(USDG_ADDRESS)
        self.uni_router_address = self.w3.to_checksum_address(UNIVERSAL_ROUTER_ADDR)
        self.swap_router02_addr = self.w3.to_checksum_address(UNISWAP_V3_SWAPROUTER02)

        self.v3_factory = self.w3.eth.contract(address=self.w3.to_checksum_address(UNISWAP_V3_FACTORY), abi=V3_FACTORY_ABI)
        self.v2_factory = self.w3.eth.contract(address=self.w3.to_checksum_address(UNISWAP_V2_FACTORY), abi=V2_FACTORY_ABI)
        self.bags_lens  = self.w3.eth.contract(address=self.w3.to_checksum_address(BAGS_LENS), abi=BAGS_LENS_ABI)
        self.uni_router = self.w3.eth.contract(address=self.uni_router_address, abi=UNIVERSAL_ROUTER_ABI)
        self.router02   = self.w3.eth.contract(address=self.swap_router02_addr, abi=SWAP_ROUTER02_ABI)
        self.v2_router  = self.w3.eth.contract(address=self.w3.to_checksum_address(UNISWAP_V2_ROUTER), abi=V2_ROUTER_ABI)
        self.state_view = self.w3.eth.contract(address=self.w3.to_checksum_address(UNISWAP_V4_STATE_VIEW), abi=STATE_VIEW_ABI)
        # Both quoters were declared as constants but never instantiated, which is
        # why every swap shipped with min_out = 1. See the slippage section below.
        self.v3_quoter = self.w3.eth.contract(address=self.w3.to_checksum_address(UNISWAP_V3_QUOTER_V2), abi=V3_QUOTER_ABI)
        self.v4_quoter = self.w3.eth.contract(address=self.w3.to_checksum_address(UNISWAP_V4_QUOTER), abi=V4_QUOTER_ABI)
        self.stock_v4   = StockV4Router(self.w3, self.chain.account if hasattr(self.chain, 'account') else None)

        self.stock_v4.quote_v3 = self._quote_v3
        self.stock_v4.quote_v4 = self._quote_v4
        self.stock_v4.scan_workers = getattr(config, "RPC_SCAN_WORKERS", 4)
        self._probe_slots = asyncio.Semaphore(getattr(config, "RPC_SCAN_WORKERS", 4))

        # Venues with pre-flight simulation disabled.
        #
        # This set was only ever DISCARDED from -- the kill-switch re-enables
        # simulation after a bad fill -- but nothing ever added to it, so every
        # entry paid for an eth_estimateGas round trip it never skipped. Measured
        # on real entries: median 760ms, p90 4.3s, worst 5.7s, all spent before
        # the transaction is broadcast. FAST_EXECUTION_MODE was read from config
        # and used nowhere; this is the switch it was meant to be.
        self.no_sim_venues = set()
        # A venue earns its way in: only after this many clean fills in a row
        # does it skip the pre-flight. One bad fill throws it straight back out,
        # so the exposure is a single trade on a route that has already worked
        # repeatedly -- not a blind broadcast on an unknown route.
        self.fast_execution = bool(getattr(config, "FAST_EXECUTION_MODE", False))
        self.venue_clean_runs = {}
        self.SIM_SKIP_AFTER = 3

        # O(1) Route Cache: once a token's venue/route is resolved, never re-run
        # the full detection cascade (incl. DexScreener + on-chain probes) for it
        # again on every price poll. Only successful (non-"NONE") results are
        # cached so a token that hasn't graduated/launched yet keeps retrying.
        self._route_cache: Dict[str, Tuple[str, str, str, int]] = {}

        # V4 Pool Params Cache: stores {token_cs: {"tick": int, "hook": str}}
        # Populated when DexScreener resolves a V4 pair so buy_token / sell_token
        # can use the real tick spacing + hook instead of hardcoded defaults.
        self._v4_pool_params_cache: Dict[str, dict] = {}

        # Which currency the live V4 pool is keyed on: {(token, fee, tick, hook):
        # (currency_in, needs_wrap)}. See _resolve_v4_currency_in.
        self._v4_currency_cache: Dict[Tuple[str, int, int, str], Tuple[str, bool]] = {}

        # Resolved V3 pool addresses: {(token, quote): pool}. See _resolve_v3_pool.
        # Only successful lookups are cached, so a pool that does not exist yet
        # keeps being retried.
        self._v3_pool_cache: Dict[Tuple[str, str], str] = {}

        # O(1) Token Curve Cache
        self._curve_cache_file = os.path.join(os.path.dirname(__file__), "cache", "curve_cache.json")
        self._load_curve_cache()

    def _load_curve_cache(self):
        self._curve_cache: Dict[str, Tuple[Optional[str], str]] = {
            self.w3.to_checksum_address("0x77f06AEE8aB51cF6347fA599D13cB6B97C53d4B6"): (self.w3.to_checksum_address("0x1DB4E3fE3f941e4c19Cf87488dC653D8E9D6Dae4"), self.usdg_address),
            self.w3.to_checksum_address("0x928233827A025193A553DC805ADE16174Be7589f"): (self.w3.to_checksum_address("0x35fe208A3F5400016E3E9C409bb9752cB596D2Dd"), "0x" + "0"*40)
        }
        try:
            if os.path.exists(self._curve_cache_file):
                with open(self._curve_cache_file, "r", encoding="utf-8") as f:
                    disk_cache = json.load(f)
                    for k, v in disk_cache.items():
                        c_addr = self.w3.to_checksum_address(v[0]) if v[0] else None
                        p_tok = self.w3.to_checksum_address(v[1]) if v[1] else "0x" + "0"*40
                        self._curve_cache[self.w3.to_checksum_address(k)] = (c_addr, p_tok)
        except (ExecutionUncertain, PreflightFailure):
            raise
        except Exception as e:
            logger.debug(f"Curve cache load warning: {e}")

    def _save_curve_cache(self):
        try:
            os.makedirs(os.path.dirname(self._curve_cache_file), exist_ok=True)
            data = {k: list(v) for k, v in self._curve_cache.items()}
            with open(self._curve_cache_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except (ExecutionUncertain, PreflightFailure):
            raise
        except Exception as e:
            logger.debug(f"Curve cache save warning: {e}")

    def record_fill(self, venue: str, clean: bool) -> None:
        """
        Track whether a venue is behaving, and promote it to the no-simulation
        fast path once it has proven itself.

        Pre-flight simulation costs a measured 760ms median (4.3s p90) before the
        transaction is even broadcast. Skipping it on a route that has filled
        cleanly several times running is nearly free speed; one bad fill demotes
        it immediately, so the downside is bounded to a single trade.
        """
        if not clean:
            self.venue_clean_runs[venue] = 0
            return
        if not self.fast_execution or venue in self.no_sim_venues:
            return
        self.venue_clean_runs[venue] = self.venue_clean_runs.get(venue, 0) + 1
        if self.venue_clean_runs[venue] >= self.SIM_SKIP_AFTER:
            self.no_sim_venues.add(venue)
            logger.info(
                f"FAST PATH: {venue} filled cleanly "
                f"{self.venue_clean_runs[venue]}x in a row - skipping pre-flight "
                f"simulation on it from now on (~760ms saved per entry).")

    async def initialize(self):
        # Do the chain-id round trip at startup. It used to happen inside the first
        # detect_venue_and_route(), putting ~700ms onto the very first snipe.
        try:
            await self.verify_chain_id()
        except (ExecutionUncertain, PreflightFailure):
            raise
        except Exception as e:
            logger.error(f"Chain ID verification failed at startup: {e}")
        logger.info(f"Initialized DexTrader on Chain ID 4663 with Universal Router: {self.uni_router_address}")

    async def verify_chain_id(self):
        if getattr(self, '_chain_id_verified', False):
            return
        chain_id = await asyncio.to_thread(lambda: self.w3.eth.chain_id)
        if chain_id != 4663:
            raise RuntimeError(f"Chain ID mismatch! Expected 4663, got {chain_id}")
        self._chain_id_verified = True

    # ─── O(1) Token Curve Resolution & Graduated Check ──────────────────

    async def resolve_token_curve(self, token_address: str) -> Tuple[Optional[str], str, bool]:
        """
        Ultra-Fast O(1) Curve Resolution:
        1. Cache Hit: O(1) in-memory lookup -> single graduated() view call.
        2. Cache Miss Step 1: Direct token.curve() view call on token contract (Pons standard, <50ms).
        3. Cache Miss Step 2: Bags Lens getTokenState() view call fallback.
        4. Cache Miss Step 3: Fast 300-block event query on fallback RPC.
        """
        token_cs = self.w3.to_checksum_address(token_address)

        # 1. Cache Hit -> O(1) lookup
        if token_cs in self._curve_cache:
            c_addr, p_tok = self._curve_cache[token_cs]
            if not c_addr:
                return None, "0x" + "0"*40, False
            is_grad = False
            try:
                grad_sel = self.w3.keccak(text="graduated()")[:4]
                res = await asyncio.to_thread(self.w3.eth.call, {'to': c_addr, 'data': grad_sel})
                is_grad = bool(int(res.hex(), 16))
            except (ExecutionUncertain, PreflightFailure):
                raise
            except Exception:
                pass
            if is_grad:
                self._curve_cache[token_cs] = (None, p_tok)
                self._save_curve_cache()
                return None, p_tok, True
            return c_addr, p_tok, False

        # 2. Cache Miss Step 1: Direct token.curve() method (Direct O(1) view call on Pons token)
        try:
            curve_sel = self.w3.keccak(text="curve()")[:4].hex()
            res = await asyncio.wait_for(
                asyncio.to_thread(self.w3.eth.call, {'to': token_cs, 'data': curve_sel}),
                timeout=1.0
            )
            raw_addr = res.hex()
            if len(raw_addr) >= 40 and raw_addr[-40:] != "0"*40:
                c_addr = self.w3.to_checksum_address('0x' + raw_addr[-40:])
                p_tok = "0x" + "0"*40
                is_grad = False
                try:
                    pair_sel = self.w3.keccak(text="pairToken()")[:4].hex()
                    pair_res = await asyncio.to_thread(self.w3.eth.call, {'to': c_addr, 'data': pair_sel})
                    if len(pair_res.hex()) >= 40:
                        p_tok = self.w3.to_checksum_address('0x' + pair_res.hex()[-40:])
                except (ExecutionUncertain, PreflightFailure):
                    raise
                except Exception:
                    pass

                try:
                    grad_sel = self.w3.keccak(text="graduated()")[:4].hex()
                    grad_res = await asyncio.to_thread(self.w3.eth.call, {'to': c_addr, 'data': grad_sel})
                    is_grad = bool(int(grad_res.hex(), 16))
                except (ExecutionUncertain, PreflightFailure):
                    raise
                except Exception:
                    pass

                if not is_grad:
                    self._curve_cache[token_cs] = (c_addr, p_tok)
                    self._save_curve_cache()
                    return c_addr, p_tok, False
                else:
                    self._curve_cache[token_cs] = (None, p_tok)
                    self._save_curve_cache()
                    return None, p_tok, True
        except (ExecutionUncertain, PreflightFailure):
            raise
        except Exception:
            pass

        # 3. Cache Miss Step 2: Try Bags Lens second (0.6s fast timeout)
        try:
            curve_addr, migrated = await asyncio.wait_for(
                asyncio.to_thread(self.bags_lens.functions.getTokenState(token_cs).call),
                timeout=0.6
            )
            if curve_addr and curve_addr != '0x' + '0'*40:
                c_addr = self.w3.to_checksum_address(curve_addr)
                if not migrated:
                    self._curve_cache[token_cs] = (c_addr, "0x" + "0"*40)
                    self._save_curve_cache()
                    return c_addr, "0x" + "0"*40, False
                else:
                    self._curve_cache[token_cs] = (None, "0x" + "0"*40)
                    self._save_curve_cache()
                    return None, "0x" + "0"*40, True
        except (ExecutionUncertain, PreflightFailure):
            raise
        except Exception:
            pass

        # 4. Cache Miss Step 3: Check Pons Factory TokenLaunched event on fallback RPC (50 blocks)
        try:
            fallback = getattr(self.chain, 'fallback_w3', self.w3)
            token_topic = '0x' + token_cs[2:].lower().zfill(64)
            latest_blk = await asyncio.to_thread(lambda: fallback.eth.block_number)
            from_blk = max(0, latest_blk - 50)
            logs = await asyncio.wait_for(
                asyncio.to_thread(
                    fallback.eth.get_logs,
                    {
                        'fromBlock': from_blk,
                        'toBlock': 'latest',
                        'address': self.w3.to_checksum_address(PONS_V2_FACTORY),
                        'topics': [PONS_LAUNCHED_TOPIC, token_topic]
                    }
                ),
                timeout=1.5
            )
            if logs:
                lg = logs[0]
                c_addr = self.w3.to_checksum_address('0x' + lg['topics'][2].hex()[-40:])
                data_hex = lg['data'].hex() if hasattr(lg['data'], 'hex') else str(lg['data'])
                p_tok = "0x" + "0"*40
                if len(data_hex) >= 64:
                    p_tok = self.w3.to_checksum_address('0x' + data_hex[:64][-40:])

                is_grad = False
                try:
                    grad_sel = self.w3.keccak(text='graduated()')[:4].hex()
                    res = await asyncio.to_thread(self.w3.eth.call, {'to': c_addr, 'data': grad_sel})
                    is_grad = bool(int(res.hex(), 16))
                except (ExecutionUncertain, PreflightFailure):
                    raise
                except Exception:
                    pass

                if not is_grad:
                    self._curve_cache[token_cs] = (c_addr, p_tok)
                    self._save_curve_cache()
                    return c_addr, p_tok, False
                else:
                    self._curve_cache[token_cs] = (None, p_tok)
                    self._save_curve_cache()
                    return None, p_tok, True
        except (ExecutionUncertain, PreflightFailure):
            raise
        except Exception as e:
            logger.debug(f"Pons V2 event query failed for {token_cs}: {e}")

        # Mark miss in cache to avoid repeated checks
        self._curve_cache[token_cs] = (None, "0x" + "0"*40)
        return None, "0x" + "0"*40, False

    async def is_contract(self, address: str) -> bool:
        """Quick check if address is a smart contract"""
        try:
            code = await self.chain._retry(
                self.w3.eth.get_code, 
                self.w3.to_checksum_address(address)
            )
            return code not in (b'', b'0x', '0x', None, '')
        except (ExecutionUncertain, PreflightFailure):
            raise
        except Exception:
            return False

    # ─── Fast On-Chain Decision Tree ────────────────────────────────────

    async def detect_venue_and_route(self, token_address: str) -> Tuple[str, str, str, int]:
        """
        Cached front-door for venue detection. Once a token resolves to a real
        venue, that result is reused for the rest of the run instead of
        re-running the whole detection cascade (DexScreener + on-chain probes)
        on every call — this matters a lot because get_token_price_eth() calls
        this on every price-poll tick for every open position.
        """
        token_cs = self.w3.to_checksum_address(token_address)
        cached = self._route_cache.get(token_cs)
        if cached is not None:
            return cached

        result = await self._detect_venue_and_route_uncached(token_cs)

        # A token cannot be quoted against itself. USDG resolved to
        # UNISWAP_V3_USDG with quote == USDG and priced at 0.5582 ETH -- $1403 for
        # a $1 stablecoin, 1403x wrong. Harmless for USDG (nobody calls it), but
        # the same route on a real token would hand position_monitor a garbage
        # entry price and the ladder would fire on noise.
        if result[0] != "NONE" and result[2] and result[2].lower() == token_cs.lower():
            logger.warning(
                f"Discarding self-quoted route for {token_cs}: venue={result[0]} "
                f"quote == token. Treating as NONE."
            )
            result = ("NONE", ZERO, self.weth_address, 0)

        if result[0] != "NONE":
            self._route_cache[token_cs] = result
        return result

    def _v4_candidate_quotes(self, quote_asset: str):
        """
        V4 keys native ETH as address(0), but detection reports WETH for some of
        the same pools. For an ETH-ish quote try both representations.
        """
        q = (quote_asset or ZERO).lower()
        if q in (ZERO.lower(), self.weth_address.lower()):
            return [ZERO, self.weth_address]
        # Prefer a NATIVE pool over the detected quote when one exists.
        #
        # A native pool is a single-hop swap, which is proven. Anything else needs
        # the atomic 2-hop encode, which reverts on this chain (see
        # build_v4_usdg_2hop_tx). SPY is the worked example: detection reported
        # USDG, its USDG pool is genuinely live, and the 2-hop sell still reverted --
        # while its native fee=10000 pool swapped fine at 151k gas and held MORE
        # liquidity. A working route beats the nominally-correct one.
        return [ZERO, self.w3.to_checksum_address(quote_asset)]

    def _v4_key_is_live(self, token_cs: str, quote_asset: str, fee: int, tick: int, hook: str) -> bool:
        return any(
            self._v4_pool_has_liquidity(q, token_cs, fee, tick, hook)
            for q in self._v4_candidate_quotes(quote_asset)
        )

    def v4_resolved_quote(self, token_address: str, fallback: str) -> str:
        """
        The quote the key resolver actually verified, which is not always the one
        detection reported. Every consumer must use this or the PoolKey, the quote
        and the built swap end up describing different pools.
        """
        cached = self._v4_pool_params_cache.get(self.w3.to_checksum_address(token_address), {})
        return cached.get("quote") or fallback

    def _remember_v4_key(self, token_cs: str, quote_asset: str, fee: int, tick: int, hook: str) -> None:
        self._v4_pool_params_cache[token_cs] = {
            "tick": int(tick), "hook": hook, "fee": int(fee),
            "quote": quote_asset, "verified": True,
        }

    def _v4_params_for(self, token_address: str, quote_asset: str, fee: int) -> Tuple[int, int, str]:
        """
        Resolve the V4 PoolKey (fee, tick, hook) for a token — verify, then probe.

        This used to probe only when the cached tick/fee were *missing*, so a key
        guessed from DexScreener's defaults (tick=200, hook=PONS) was trusted
        without ever asking whether that pool exists. That is why buys guessed one
        key while sells probed and found a different, live one — and why V4 buys
        and V4 price polls failed together on the same tokens.
        """
        token_cs = self.w3.to_checksum_address(token_address)
        cached = self._v4_pool_params_cache.get(token_cs, {})
        if cached.get("verified"):
            return int(cached["fee"]), int(cached["tick"]), cached["hook"]

        tick = cached.get("tick", -1)
        hook = cached.get("hook") or ZERO
        fee_out = fee if fee is not None and int(fee) >= 0 else cached.get("fee", -1)

        # 0. For a non-ETH quote, look for a native pool FIRST even if the detected
        #    quote's pool is live. Reaching a USDG- or stock-quoted pool needs the
        #    multi-hop route, which is where every observed failure has happened
        #    (AD-004, and the leg-2 revert that stranded SPY). A native pool is a
        #    single hop and always works. It may be thinner -- the quoted min_out
        #    rejects a bad fill, so the downside is a skipped trade, not a bad one.
        q_low = (quote_asset or ZERO).lower()
        if q_low not in (ZERO.lower(), self.weth_address.lower()):
            # Deepest native pool, not the first one probe_v4_key stumbles on --
            # see find_native_v4_pool for why that difference is worth 25x in gas.
            best = None
            for f_n, t_n, h_n in self.COMMON_V4_KEYS:
                liq = self._v4_pool_liquidity(ZERO, token_cs, f_n, t_n, h_n)
                if liq and (best is None or liq > best[0]):
                    best = (liq, f_n, t_n, h_n)
            if best is None:
                try:
                    probed_native = self.stock_v4.probe_v4_key(token_cs, ZERO)
                except (ExecutionUncertain, PreflightFailure):
                    raise
                except Exception:
                    probed_native = None
                if probed_native:
                    best = (0, int(probed_native["fee"]), int(probed_native["tick"]),
                            probed_native["hook"])
            if best:
                logger.info(
                    f"Routing {token_cs} via its native pool (fee={best[1]} tick={best[2]}, "
                    f"liquidity={best[0]}) instead of multi-hop through {quote_asset}"
                )
                self._remember_v4_key(token_cs, ZERO, best[1], best[2], best[3])
                return int(best[1]), int(best[2]), best[3]

        # 1. Believe the cached/detected key only if that pool actually exists.
        if (not cached.get("unresolved_key") and tick is not None
                and int(tick) >= 0 and int(fee_out) >= 0
                and self._v4_key_is_live(token_cs, quote_asset, fee_out, tick, hook)):
            self._remember_v4_key(token_cs, quote_asset, fee_out, tick, hook)
            return int(fee_out), int(tick), hook

        # 2. Guess was wrong or absent — brute-force the live key.
        # ponytail: up to len(V4_FEE_TICKS_EXTENDED) x len(V4_HOOKS) view calls, but
        # only on a cache miss whose cheap guess already failed. Far cheaper than the
        # reverted snipe it prevents. If it ever shows up in entry latency, seed the
        # search order from the detected fee instead of scanning the full table.
        for candidate_quote in self._v4_candidate_quotes(quote_asset):
            try:
                probed = self.stock_v4.probe_v4_key(token_cs, candidate_quote)
            except (ExecutionUncertain, PreflightFailure):
                raise
            except Exception:
                probed = None
            if probed:
                logger.info(
                    f"V4 key probed for {token_cs}: fee={probed['fee']} tick={probed['tick']} "
                    f"hook={probed['hook']} (guess was fee={fee_out} tick={tick} hook={hook})"
                )
                self._remember_v4_key(token_cs, candidate_quote, probed["fee"], probed["tick"], probed["hook"])
                return int(probed["fee"]), int(probed["tick"]), probed["hook"]

        # 3. Nothing live anywhere. Return the old static defaults so callers still
        #    build a tx, and let simulation reject it rather than failing here.
        logger.warning(f"No live V4 pool found for {token_cs} quote={quote_asset}; using defaults")
        q = (quote_asset or ZERO).lower()
        if q in (self.weth_address.lower(), ZERO.lower()):
            return (3000 if int(fee_out) < 0 else int(fee_out)), (60 if int(tick) < 0 else int(tick)), ZERO
        return (0 if int(fee_out) < 0 else int(fee_out)), (200 if int(tick) < 0 else int(tick)), PONS_V2_HOOK

    # The fee/tick/hook combinations actually seen on this chain, most common first.
    # probe_v4_key scans 54 combinations, which is too slow on a throttled RPC --
    # detection was timing out and reporting NONE for tokens that were perfectly
    # tradeable (SWARM had a native pool with 2.9e22 liquidity when it was skipped).
    COMMON_V4_KEYS = [
        (0, 200, PONS_V2_HOOK), (0, 200, ZERO), (0, 60, ZERO), (0, 1, ZERO),
        (3000, 60, ZERO), (10000, 200, ZERO), (500, 10, ZERO), (2500, 25, ZERO),
        (100, 1, ZERO), (802731, 9303, ZERO),
    ]

    async def find_native_v4_pool(self, token_cs: str):
        """
        Fast, targeted hunt for a native-ETH V4 pool. Returns (fee, tick, hook) or
        None. Ten view calls instead of probe_v4_key's 54, ordered by what this
        chain actually uses, so it survives a slow RPC.
        """
        # Check them all and take the DEEPEST, not the first hit. A token can have
        # several native pools and they are not equivalent: GOOGL's fee=100/tick=1
        # pool estimated at 3,690,531 gas while its fee=10000/tick=200 pool cost
        # 150,260 for the same swap. Tiny tick spacing means the swap crosses far
        # more initialised ticks, and each crossing costs gas. Same number of view
        # calls either way, so there is no reason to take the first one.
        best = None
        for fee, tick, hook in self.COMMON_V4_KEYS:
            try:
                liq = await asyncio.to_thread(
                    self._v4_pool_liquidity, ZERO, token_cs, fee, tick, hook)
            except (ExecutionUncertain, PreflightFailure):
                raise
            except Exception:
                continue
            if liq and (best is None or liq > best[0]):
                best = (liq, fee, tick, hook)
        if best:
            return best[1], best[2], best[3]
        return None

    async def _limited_probe(self, function, *args):
        async with self._probe_slots:
            return await asyncio.to_thread(function, *args)

    async def _detect_venue_and_route_uncached(self, token_address: str) -> Tuple[str, str, str, int]:
        """
        Fast Decision Tree (Stops immediately on first valid match):
        0. Fast Check: Verify address is a smart contract (skip EOAs)
        1. Check Bonding Curve (Pons token.curve() / Bags) -> return immediately if not graduated
        2. Check Uniswap V3 WETH & USDG in parallel (requires live liquidity > 0)
        3. Check Stock Token Pools (V3/V4 pools with TTWO, TSLA, PLTR, etc.)
        4. Check Uniswap V2 in parallel
        5. Return NONE
        """
        await self.verify_chain_id()
        token_cs = self.w3.to_checksum_address(token_address)

        # Start the three independent probes together. They used to run one after
        # another, so a DexScreener miss -- which is the normal case for a token
        # minutes old, the exact kind being sniped -- added its latency in front of
        # the curve lookup that actually resolves it.
        contract_task = asyncio.create_task(self.is_contract(token_cs))
        ds_task = asyncio.create_task(
            asyncio.wait_for(asyncio.to_thread(fetch_dexscreener, token_cs), timeout=2.5)
        )
        curve_task = asyncio.create_task(self.resolve_token_curve(token_cs))

        def _drop(*tasks):
            for t in tasks:
                if not t.done():
                    t.cancel()

        # ========== FAST FILTER: EOA vs Smart Contract ==========
        if not await contract_task:
            _drop(ds_task, curve_task)
            logger.info(f"⏩ Skipping {token_cs} -- not a contract (EOA)")
            print(f"⏩ Skipping {token_cs} — not a contract (EOA)")
            return "NONE", ZERO, self.weth_address, 0
        # ========================================================

        # An ungraduated bonding curve is authoritative: the token has no DEX pool
        # yet, so it can only be bought on the curve. Check that FIRST, because for
        # a token minutes old -- the snipe case -- DexScreener has not indexed it
        # and waiting on that miss just delays an answer already in hand.
        try:
            curve_addr, pair_token, is_graduated = await curve_task
        except (ExecutionUncertain, PreflightFailure):
            raise
        except Exception as e:
            logger.debug(f"Curve resolution failed for {token_cs}: {e}")
            curve_addr, pair_token, is_graduated = None, ZERO, False
        if curve_addr and not is_graduated:
            _drop(ds_task)
            if pair_token.lower() == self.usdg_address.lower():
                return "LAUNCHPAD_CURVE_USDG", curve_addr, self.usdg_address, 0
            return "LAUNCHPAD_CURVE_ETH", curve_addr, ZERO, 0

        # ========== 0. ULTRA-FAST DEXSCREENER CHECK (~150ms) ==========
        # IMPORTANT: fetch_dexscreener() uses blocking `requests`, not aiohttp.
        # Never call it directly in an async function - it freezes the whole
        # event loop (including the Telegram listener) for the length of the
        # HTTP call. Always push it to a thread, with a hard timeout so a slow
        # DexScreener response can't stall detection for long.
        try:
            ds_pair = await ds_task
            if ds_pair:
                mapped = map_ds_pair(token_cs, ds_pair, self.w3)
                if mapped.get("venue") != "NONE":
                    # Cache V4 pool params (tick, hook) so buy_token/sell_token
                    # can use the real values instead of hardcoded defaults
                    if "tick" in mapped or "hook" in mapped:
                        self._v4_pool_params_cache[token_cs] = {
                            "tick": mapped.get("tick", 200),
                            "hook": mapped.get("hook", PONS_V2_HOOK),
                        }
                    logger.info(f"⚡ DexScreener fast-route: {mapped['venue']} | {mapped['quote']} | {mapped['target']}")
                    _drop(curve_task)
                    return mapped["venue"], mapped["target"], mapped["quote"], mapped.get("fee", 0)
        except asyncio.TimeoutError:
            logger.debug(f"DexScreener fast-check timed out for {token_cs}, falling through to on-chain checks")
        except (ExecutionUncertain, PreflightFailure):
            raise
        except Exception as e:
            logger.debug(f"DexScreener fast-check failed: {e}")
        # ==============================================================

        # (bonding curve already resolved above)

        # 2. Parallel V3 WETH & V3 USDG Liquidity Probes
        async def check_v3_pool(quote_asset: str, fee: int, tag: str):
            try:
                pool_addr = await self._limited_probe(self.v3_factory.functions.getPool(quote_asset, token_cs, fee).call)
                if pool_addr and pool_addr != ZERO:
                    pool_contract = self.w3.eth.contract(address=pool_addr, abi=[
                        {"inputs":[],"name":"liquidity","outputs":[{"type":"uint128"}],"stateMutability":"view","type":"function"},
                        {"inputs":[],"name":"slot0","outputs":[{"name":"sqrtPriceX96","type":"uint160"},{"name":"tick","type":"int24"}],"stateMutability":"view","type":"function"}
                    ])
                    liq = await self._limited_probe(pool_contract.functions.liquidity().call)
                    slot0 = await self._limited_probe(pool_contract.functions.slot0().call)
                    # sqrtPriceX96 == 0 means pool exists but was never initialized (swap will revert)
                    if liq > 0 and slot0[0] > 0:
                        return f"UNISWAP_V3_{tag}", pool_addr, quote_asset, fee
            except (ExecutionUncertain, PreflightFailure):
                raise
            except Exception:
                pass
            return None

        v3_tasks = []
        for fee in [3000, 10000, 500, 100]:
            v3_tasks.append(check_v3_pool(self.weth_address, fee, "WETH"))
            v3_tasks.append(check_v3_pool(self.usdg_address, fee, "USDG"))

        v3_results = await asyncio.gather(*v3_tasks)
        for res in v3_results:
            if res is not None:
                return res

        # 3. Check against stock tokens (V3 / V4 style pools)
        async def check_stock_pool(stock_addr: str, fee: int):
            try:
                pool_addr = await self._limited_probe(self.v3_factory.functions.getPool(stock_addr, token_cs, fee).call)
                if pool_addr and pool_addr != ZERO:
                    liq_contract = self.w3.eth.contract(address=pool_addr, abi=[{"inputs":[],"name":"liquidity","outputs":[{"type":"uint128"}],"stateMutability":"view","type":"function"}])
                    liq = await self._limited_probe(liq_contract.functions.liquidity().call)
                    if liq > 0:
                        return "STOCK_PAIR", pool_addr, stock_addr, fee
            except (ExecutionUncertain, PreflightFailure):
                raise
            except Exception:
                pass
            return None

        # Check the FULL stock list (was previously hardcoded to just 5 tickers,
        # silently missing PLTR/AMD/GOOGL/COIN/MSTR/META/AMZN/NFLX/MSFT/QQQ
        # pairs on this fast path and forcing them onto the much slower
        # StockV4Router.detect() fallback). Still fully parallelized via gather.
        stock_tasks = []
        for stock_addr in EXTENDED_STOCK_LIST:
            if stock_addr.lower() != token_cs.lower():
                for fee in [3000, 10000, 500]:
                    stock_tasks.append(check_stock_pool(stock_addr, fee))

        if stock_tasks:
            stock_results = await asyncio.gather(*stock_tasks)
            for res in stock_results:
                if res is not None:
                    return res

        # 4. Uniswap V2 Pair fallback (Parallel)
        async def check_v2_pair(quote_asset: str, tag: str):
            try:
                pair_addr = await self._limited_probe(self.v2_factory.functions.getPair(quote_asset, token_cs).call)
                if pair_addr and pair_addr != ZERO:
                    res_contract = self.w3.eth.contract(address=pair_addr, abi=[{"inputs":[],"name":"getReserves","outputs":[{"type":"uint112"},{"type":"uint112"},{"type":"uint32"}],"stateMutability":"view","type":"function"}])
                    res = await self._limited_probe(res_contract.functions.getReserves().call)
                    if res[0] > 0 and res[1] > 0:
                        return f"UNISWAP_V2_{tag}", UNISWAP_V2_ROUTER, quote_asset, 0
            except (ExecutionUncertain, PreflightFailure):
                raise
            except Exception:
                pass
            return None

        v2_results = await asyncio.gather(
            check_v2_pair(self.weth_address, "WETH"),
            check_v2_pair(self.usdg_address, "USDG")
        )
        for res in v2_results:
            if res is not None:
                return res

        # Before declaring NONE, check for a native V4 pool directly. Detection can
        # miss one when the RPC is slow, and the stock_v4 fallback that used to
        # cover this took 8s and timed out -- 16 of 18 live buy failures traced
        # here, on tokens that had a live pool the whole time.
        native = await self.find_native_v4_pool(token_cs)
        if native:
            fee_n, tick_n, hook_n = native
            self._remember_v4_key(token_cs, ZERO, fee_n, tick_n, hook_n)
            logger.info(
                f"Late native V4 pool found for {token_cs}: fee={fee_n} tick={tick_n} "
                f"-- would have been reported NONE"
            )
            return "UNISWAP_V4", self.uni_router_address, ZERO, fee_n

        return "NONE", ZERO, self.weth_address, 0

    async def detect_venue_fast(self, token: str) -> dict:
        """Helper alias returning dict format"""
        venue, target, quote, fee = await self.detect_venue_and_route(token)
        return {
            "venue": venue,
            "target": target,
            "quote": quote,
            "fee": fee
        }

    async def force_buy_v4_or_stock(self, token: str, amount_eth: float, slippage_pct: float = 15.0,
                                    fee: int = 3000, tick: int = 60, hook: str = "0x" + "0"*40) -> Tuple[str, float]:
        """
        Fallback V4 buy with a quote, simulation, and journaled broadcast.
        """
        token = self.w3.to_checksum_address(token)
        amount_wei = self.w3.to_wei(amount_eth, "ether")
        recipient = self.chain.account.address if self.chain.account else ZERO

        print(f"⚡ FALLBACK BUY (Simulated) for {token}")
        logger.info(f"⚡ FALLBACK BUY (Simulated) for {token} with {amount_eth} ETH")

        if getattr(self.config, "DRY_RUN", True):
            fake_hash = f"DRY_RUN_0x{int(time.time())}"
            logger.info(f"[DRY RUN] Forced V4/Stock buy simulated: {fake_hash}")
            return fake_hash, await self._dry_run_fill(token, amount_eth)

        # The fallback enforces both a quoted output floor and simulation.
        min_out = await self.min_out_for(
            "UNISWAP_V4", token, ZERO, fee, amount_wei, self.uni_router_address, slippage_pct
        )
        if min_out is None:
            logger.error(f"❌ Cannot quote forced V4 buy for {token} -- refusing to send unprotected")
            return "", 0.0

        # Same encoder as the simulated path, so the native-vs-WETH PoolKey is
        # resolved here too. This used to hardcode WETH and revert on every
        # native-keyed pool.
        built_tx, _cmds = await asyncio.to_thread(
            self.build_v4_swap_tx, token, amount_wei, min_out, recipient, ZERO,
            fee, tick, hook
        )
        calldata = built_tx['data']

        max_fee, max_priority = eip1559_fees(self.w3)

        tx = {
            "from": recipient,
            "to": self.uni_router_address,
            "value": amount_wei,
            "data": calldata,
            "gas": 450000,
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": max_priority,
            "nonce": await self.chain.get_nonce(),
            "chainId": 4663
        }

        # Read the balance before broadcasting so the fill can be measured rather
        # than assumed. This used to return a hardcoded 1.0, which became the
        # denominator of the position's entry price in main.py:105 -- wrong by
        # orders of magnitude, so the monitor stop-lossed seconds after entry.
        bal_before = await self._safe_token_balance(token)

        success, _ = await self.simulate_execution(tx, "UNISWAP_V4", self.uni_router_address, _cmds)
        if not success:
            return "", 0.0
        tx_hash = await self.chain.send_transaction(tx)
        logger.info(f"⚡ Forced V4/Stock Buy Tx broadcasted: {tx_hash}")
        print(f"⚡ Forced V4/Stock Buy Tx: {tx_hash}")

        receipt = await self.chain.wait_for_receipt(tx_hash, timeout=45)
        status = receipt.get("status", 0)
        if status != 1:
            logger.error(f"⚡ Forced Buy reverted: {tx_hash}")
            return tx_hash, 0.0

        received = await self._measure_fill(token, bal_before)
        logger.info(
            f"⚡ Forced Buy confirmed in block {receipt.get('blockNumber')}: "
            f"{received} tokens received"
        )
        return tx_hash, received

    # ─── Simulation Step (eth_call / eth_estimateGas) ───────────────────

    async def simulate_execution(self, tx: Dict[str, Any], venue: str, target: str, commands_bytes: str) -> Tuple[bool, str]:
        await self.verify_chain_id()
        try:
            gas_est = await asyncio.to_thread(self.w3.eth.estimate_gas, tx)
            msg = f"SIMULATION OK | Venue: {venue} | To: {target} | gas: {gas_est}"
            logger.info(msg)
            print(f"🎉🎉🎉 {msg}")
            return True, f"gas: {gas_est}"
        except (ExecutionUncertain, PreflightFailure):
            raise
        except Exception as e:
            revert_reason = str(e)
            msg = f"SIMULATION REVERT | Venue: {venue} | To: {target} | Revert: {revert_reason}"
            logger.warning(msg)
            print(f"❌ {msg}")
            return False, revert_reason

    # ─── Builders ────────────────────────────────────────────────────────

    def build_curve_buy_tx(self, curve_address: str, amount_in_wei: int, amount_out_min: int, recipient: str, pair_token: str) -> Tuple[Dict[str, Any], str]:
        """
        Pons curve function: buy(uint256 quoteIn, uint256 minTokensOut, address recipient)
        Signature selector: 0x2a3e87ea = buy(uint256,uint256,address)
        Native quote (pairToken == 0x0): msg.value == quoteIn
        USDG quote: msg.value == 0
        """
        buy_sel = self.w3.keccak(text='buy(uint256,uint256,address)')[:4].hex()
        calldata = buy_sel + eth_abi.encode(['uint256', 'uint256', 'address'], [amount_in_wei, amount_out_min, recipient]).hex()

        is_native = (pair_token == "0x" + "0"*40)
        tx = {
            'from': recipient,
            'to': self.w3.to_checksum_address(curve_address),
            'value': amount_in_wei if is_native else 0,
            'data': calldata,
            'chainId': 4663
        }
        return tx, "buy(uint256,uint256,address)"

    def build_v3_swap_tx(self, token_address: str, amount_in_wei: int, amount_out_min: int, recipient: str, quote_asset: str, fee: int) -> Tuple[Dict[str, Any], str]:
        """
        Builds SwapRouter02 transaction for V3 pools:
        - Single-hop exactInputSingle if quote_asset == WETH
        - Multi-hop exactInput if quote_asset == USDG (WETH -> USDG -> Token)
        """
        if quote_asset.lower() == self.usdg_address.lower():
            path_bytes = (
                bytes.fromhex(self.weth_address[2:]) +
                (500).to_bytes(3, 'big') +
                bytes.fromhex(self.usdg_address[2:]) +
                fee.to_bytes(3, 'big') +
                bytes.fromhex(self.w3.to_checksum_address(token_address)[2:])
            )
            calldata = self.router02.functions.exactInput((path_bytes, recipient, amount_in_wei, amount_out_min))._encode_transaction_data()
            tx = {
                'from': recipient,
                'to': self.swap_router02_addr,
                'value': amount_in_wei,
                'data': calldata,
                'chainId': 4663
            }
            return tx, "exactInput"
        else:
            params = (self.weth_address, self.w3.to_checksum_address(token_address), fee, recipient, amount_in_wei, amount_out_min, 0)
            calldata = self.router02.functions.exactInputSingle(params)._encode_transaction_data()
            tx = {
                'from': recipient,
                'to': self.swap_router02_addr,
                'value': amount_in_wei,
                'data': calldata,
                'chainId': 4663
            }
            return tx, "exactInputSingle"

    def _v4_pool_liquidity(self, currency_a: str, currency_b: str, fee: int, tick: int, hook: str) -> int:
        a, b = self.w3.to_checksum_address(currency_a), self.w3.to_checksum_address(currency_b)
        c0, c1 = (a, b) if int(a, 16) < int(b, 16) else (b, a)
        try:
            pid = pool_id_from_key(c0, c1, int(fee), int(tick), self.w3.to_checksum_address(hook))
            return int(self.state_view.functions.getLiquidity(pid).call())
        except (ExecutionUncertain, PreflightFailure):
            raise
        except Exception:
            return 0

    def _v4_pool_has_liquidity(self, currency_a: str, currency_b: str, fee: int, tick: int, hook: str) -> bool:
        a, b = self.w3.to_checksum_address(currency_a), self.w3.to_checksum_address(currency_b)
        c0, c1 = (a, b) if int(a, 16) < int(b, 16) else (b, a)
        try:
            pid = pool_id_from_key(c0, c1, int(fee), int(tick), self.w3.to_checksum_address(hook))
            return int(self.state_view.functions.getLiquidity(pid).call()) > 0
        except (ExecutionUncertain, PreflightFailure):
            raise
        except Exception:
            return False

    def _resolve_v4_currency_in(self, token_address: str, fee: int, tick: int, hook: str) -> Tuple[str, bool]:
        """
        Decide whether the live V4 pool for this token is keyed on native ETH or on WETH.

        Uniswap V4 addresses native ETH as address(0), NOT as WETH. A PoolKey built
        with WETH and one built with address(0) hash to different poolIds, so keying
        on the wrong one encodes a swap against a pool that does not exist and the
        router reverts with a bare 0x — which is exactly what this bot was doing on
        every V4 buy.

        Ask the chain rather than assuming. Returns (currency_in, needs_wrap);
        needs_wrap is True only for a genuine WETH-keyed pool, where the Universal
        Router must WRAP_ETH before the swap.
        """
        key = (self.w3.to_checksum_address(token_address), int(fee), int(tick), str(hook).lower())
        cached = self._v4_currency_cache.get(key)
        if cached is not None:
            return cached

        # ponytail: one blocking view call per (token, poolkey), then cached for the
        # process lifetime. Fine for a sniper; if it ever shows up in entry latency,
        # resolve it inside detect_venue_and_route (already async + cached) instead.
        if self._v4_pool_has_liquidity(ZERO, token_address, fee, tick, hook):
            result = (ZERO, False)
        elif self._v4_pool_has_liquidity(self.weth_address, token_address, fee, tick, hook):
            result = (self.weth_address, True)
        else:
            # Neither is live at this PoolKey. Native is the far more common shape on
            # this chain, so prefer it and let the caller's simulation catch the miss.
            logger.warning(
                f"No live V4 pool for {token_address} at fee={fee} tick={tick} hook={hook} "
                f"on either native or WETH key -- defaulting to native"
            )
            result = (ZERO, False)

        self._v4_currency_cache[key] = result
        return result

    def build_v4_swap_tx(self, token_address: str, amount_in_wei: int, amount_out_min: int, recipient: str, quote_asset: str, fee: int = 3000, tick: int = 60, hook: str = "0x" + "0"*40) -> Tuple[Dict[str, Any], str]:
        """
        Single-hop V4 swap ETH -> Token.

        Keys the pool on whichever currency is actually live (native address(0) or
        WETH) and only emits WRAP_ETH when the pool genuinely wants WETH.
        """
        currency_in, needs_wrap = self._resolve_v4_currency_in(token_address, fee, tick, hook)

        v4_swap_input = make_v4_swap_input(currency_in, token_address, amount_in_wei, amount_out_min, recipient, fee=fee, tick_spacing=tick, hook_addr=hook)
        WRAP_ETH = 0x0b
        V4_SWAP  = 0x10
        deadline = int(time.time()) + 300

        if needs_wrap:
            commands = bytes([WRAP_ETH, V4_SWAP])
            wrap_input = eth_abi.encode(['address', 'uint256'], [self.uni_router_address, amount_in_wei])
            inputs = [wrap_input, v4_swap_input]
        else:
            # Native pool settles ETH straight from msg.value — nothing to wrap.
            commands = bytes([V4_SWAP])
            inputs = [v4_swap_input]

        calldata = self.uni_router.functions.execute(commands, inputs, deadline)._encode_transaction_data()

        tx = {
            'from': recipient,
            'to': self.uni_router_address,
            'value': amount_in_wei,
            'data': calldata,
            'chainId': 4663
        }
        return tx, commands.hex()

    def build_v4_usdg_2hop_tx(self, token_address: str, eth_amount_wei: int, amount_out_min: int, recipient: str,
                              fee2: int = 3000, tick2: int = 60, hook2: str = PONS_V2_HOOK) -> Tuple[Dict[str, Any], str]:
        """
        KNOWN BROKEN on chain 4663 — kept for reference, not wired into any live path.

        Intent: one atomic WRAP_ETH + V4_SWAP doing WETH->USDG then USDG->Token via
        OPEN_DELTA. It has never produced a transaction this chain accepts.

        Measured against live state (eth_estimateGas, 0.0004 ETH, AAPL/USDG):
          - both hops are fine in isolation: native->USDG estimates at 137k gas, and
            the V4 quoter prices both legs (0.0004 ETH -> 1.0289 USDG -> AAPL)
          - chained SWAP_EXACT_IN_SINGLE with OPEN_DELTA  -> DeltaNotPositive(address(0))
          - same, with an explicit hop-2 amountIn          -> DeltaNotPositive(address(0))
          - canonical SWAP_EXACT_IN (0x07) path form       -> bare revert
          - mixed V3_SWAP_EXACT_IN + V4_SWAP in one execute-> bare revert
          - TAKE-before-SETTLE and SETTLE-between orderings -> revert
        Action/param alignment is not the issue: a 4-action list with a duplicate
        TAKE_ALL estimates fine (140k gas).

        The 0x3b99b53d selector the README mentions is SliceOutOfBounds().

        USDG-quoted buys go through StockV4Router.buy_usdg_two_tx instead: two
        transactions, both using encodings proven to work.
        """
        commands = bytes([0x0b, 0x10])  # WRAP_ETH + single V4_SWAP command

        wrap_input = eth_abi.encode(['address', 'uint256'], [self.uni_router_address, eth_amount_wei])

        weth_cs = self.weth_address
        usdg_cs = self.usdg_address
        tok_cs = self.w3.to_checksum_address(token_address)

        # Hop 1: WETH -> USDG (fee 500, tick 10, hook ZERO)
        if int(weth_cs, 16) < int(usdg_cs, 16):
            c0_1, c1_1 = weth_cs, usdg_cs
            z4o_1 = True
        else:
            c0_1, c1_1 = usdg_cs, weth_cs
            z4o_1 = False

        pool_key1 = (c0_1, c1_1, 500, 10, self.w3.to_checksum_address(ZERO))
        param_hop1 = eth_abi.encode(
            ['(address,address,uint24,int24,address)', 'bool', 'uint128', 'uint128', 'uint160', 'uint256', 'bytes'],
            [pool_key1, z4o_1, eth_amount_wei, 0, 0, 0, b'']
        )

        # Hop 2: USDG -> Token (fee2, tick2, hook2)
        # amt_in = 0 (OPEN_DELTA: consumes intermediate USDG delta from hop 1)
        if int(usdg_cs, 16) < int(tok_cs, 16):
            c0_2, c1_2 = usdg_cs, tok_cs
            z4o_2 = True
        else:
            c0_2, c1_2 = tok_cs, usdg_cs
            z4o_2 = False

        pool_key2 = (c0_2, c1_2, int(fee2), int(tick2), self.w3.to_checksum_address(hook2))
        param_hop2 = eth_abi.encode(
            ['(address,address,uint24,int24,address)', 'bool', 'uint128', 'uint128', 'uint160', 'uint256', 'bytes'],
            [pool_key2, z4o_2, 0, amount_out_min, 0, 0, b'']
        )

        # Action 0x0c: SETTLE_ALL (WETH)
        param_settle = eth_abi.encode(['address', 'uint256'], [weth_cs, eth_amount_wei])
        # Action 0x0f: TAKE_ALL (Token)
        param_take = eth_abi.encode(['address', 'uint256'], [tok_cs, amount_out_min])

        actions = bytes([0x06, 0x06, 0x0c, 0x0f])
        v4_swap_input = eth_abi.encode(
            ['bytes', 'bytes[]'],
            [actions, [param_hop1, param_hop2, param_settle, param_take]]
        )

        inputs = [wrap_input, v4_swap_input]
        deadline = int(time.time()) + 300

        calldata = self.uni_router.functions.execute(commands, inputs, deadline)._encode_transaction_data()

        tx = {
            'from': recipient,
            'to': self.uni_router_address,
            'value': eth_amount_wei,
            'data': calldata,
            'chainId': 4663
        }
        return tx, commands.hex()

    def build_v4_sell_tx(self, token_address: str, amount_in: int, min_out: int, recipient: str,
                         quote_asset: str, fee: int = 3000, tick: int = 60, hook: str = "0x" + "0"*40) -> Tuple[Dict[str, Any], str]:
        """
        Builds a V4 sell transaction via Universal Router.
        Swaps via V4_SWAP (0x10). The token is pulled by V4's own SETTLE_ALL action
        through Permit2 -- no separate PERMIT2_TRANSFER_FROM command (see below).
        Supports:
        1. Single-hop: Token -> WETH -> ETH
        2. Two-hop USDG: Token -> USDG -> WETH -> ETH
        """
        tok_cs = self.w3.to_checksum_address(token_address)
        weth_cs = self.weth_address
        usdg_cs = self.usdg_address

        unwrap_input = eth_abi.encode(
            ["address", "uint256"],
            [recipient, int(min_out)]
        )

        if quote_asset.lower() == usdg_cs.lower():
            # 2-hop V4: Token -> USDG -> WETH + UNWRAP_WETH
            if int(tok_cs, 16) < int(usdg_cs, 16):
                c0_1, c1_1 = tok_cs, usdg_cs
                z4o_1 = True
            else:
                c0_1, c1_1 = usdg_cs, tok_cs
                z4o_1 = False
            pk1 = (c0_1, c1_1, int(fee), int(tick), self.w3.to_checksum_address(hook))
            p_hop1 = eth_abi.encode(
                ['(address,address,uint24,int24,address)', 'bool', 'uint128', 'uint128', 'uint160', 'uint256', 'bytes'],
                [pk1, z4o_1, int(amount_in), 0, 0, 0, b'']
            )

            # Hop 2: USDG -> WETH (fee 500, tick 10, hook ZERO)
            if int(usdg_cs, 16) < int(weth_cs, 16):
                c0_2, c1_2 = usdg_cs, weth_cs
                z4o_2 = True
            else:
                c0_2, c1_2 = weth_cs, usdg_cs
                z4o_2 = False
            pk2 = (c0_2, c1_2, 500, 10, self.w3.to_checksum_address(ZERO))
            p_hop2 = eth_abi.encode(
                ['(address,address,uint24,int24,address)', 'bool', 'uint128', 'uint128', 'uint160', 'uint256', 'bytes'],
                [pk2, z4o_2, 0, int(min_out), 0, 0, b'']
            )

            p_settle = eth_abi.encode(['address', 'uint256'], [tok_cs, int(amount_in)])
            p_take = eth_abi.encode(['address', 'uint256'], [weth_cs, int(min_out)])

            actions = bytes([0x06, 0x06, 0x0c, 0x0f])
            v4_input = eth_abi.encode(['bytes', 'bytes[]'], [actions, [p_hop1, p_hop2, p_settle, p_take]])

            # No PERMIT2_TRANSFER_FROM here. V4's SETTLE_ALL pulls the token from
            # the user through Permit2 itself, so pulling it to the router first
            # made the router pull the same amount TWICE -- which is why selling
            # more than half the balance reverted with TRANSFER_FROM_FAILED.
            commands = bytes([0x10, 0x0c])  # V4_SWAP + UNWRAP_WETH
            inputs = [v4_input, unwrap_input]
        else:
            # Single-hop V4: Token -> ETH. Same native-vs-WETH PoolKey problem as
            # the buy side — key on whichever currency the live pool actually uses,
            # or this reverts against a pool that does not exist.
            currency_out, pool_wants_weth = self._resolve_v4_currency_in(token_address, fee, tick, hook)

            if int(tok_cs, 16) < int(currency_out, 16):
                c0, c1 = tok_cs, currency_out
                z4o = True
            else:
                c0, c1 = currency_out, tok_cs
                z4o = False
            pk = (c0, c1, int(fee), int(tick), self.w3.to_checksum_address(hook))
            p_hop = eth_abi.encode(
                ['(address,address,uint24,int24,address)', 'bool', 'uint128', 'uint128', 'uint160', 'uint256', 'bytes'],
                [pk, z4o, int(amount_in), int(min_out), 0, 0, b'']
            )
            p_settle = eth_abi.encode(['address', 'uint256'], [tok_cs, int(amount_in)])
            p_take = eth_abi.encode(['address', 'uint256'], [currency_out, int(min_out)])

            actions = bytes([0x06, 0x0c, 0x0f])
            v4_input = eth_abi.encode(['bytes', 'bytes[]'], [actions, [p_hop, p_settle, p_take]])

            if pool_wants_weth:
                # Proceeds land as WETH in the router — unwrap to the recipient.
                commands = bytes([0x10, 0x0c])  # V4_SWAP + UNWRAP_WETH
                inputs = [v4_input, unwrap_input]
            else:
                # TAKE_ALL on a native pool already credits the ETH to the caller,
                # so there is nothing left in the router to SWEEP. Verified on-chain:
                # this shape estimates at ~151k gas selling a FULL balance, where the
                # pull+sweep version reverted with TRANSFER_FROM_FAILED.
                commands = bytes([0x10])  # V4_SWAP only
                inputs = [v4_input]

        deadline = int(time.time()) + 300
        calldata = self.uni_router.functions.execute(commands, inputs, deadline)._encode_transaction_data()
        tx = {
            'from': recipient,
            'to': self.uni_router_address,
            'value': 0,
            'data': calldata,
            'chainId': 4663
        }
        return tx, commands.hex()

    # ─── Public Buy / Sell ────────────────────────────────────────────────

    async def _dry_run_fill(self, token_address: str, eth_amount: float) -> float:
        """Paper entry at a route quote, including the configured slippage assumption."""
        try:
            venue, target, quote, fee = await self.detect_venue_and_route(token_address)
            expected = await asyncio.to_thread(
                self._quote_route, venue, token_address, quote, fee,
                self.w3.to_wei(eth_amount, "ether"), target, False)
            if expected and expected > 0:
                decimals = await self.chain.get_token_decimals(token_address)
                return expected / 10**decimals * (1 - self.config.PAPER_SLIPPAGE_PCT / 100)
        except (ExecutionUncertain, PreflightFailure):
            raise
        except Exception as exc:
            logger.warning("Paper quote unavailable for %s: %s", token_address, exc)
        logger.warning("[DRY RUN] No route quote for %s; refusing to invent a fill", token_address)
        return 0.0

    async def buy_token(self, token_address: str, eth_amount: float, slippage_pct: float, start_time: Optional[float] = None) -> Tuple[str, float]:
        if start_time is None:
            start_time = time.time()

        await self.verify_chain_id()
        token_address = self.w3.to_checksum_address(token_address)
        eth_wei = self.w3.to_wei(eth_amount, "ether")
        recipient = self.chain.account.address if self.chain.account else "0x" + "0"*40

        # Step 1: Detect venue and route per decision tree
        venue, target_addr, quote_asset, fee = await self.detect_venue_and_route(token_address)
        t_detect = time.time()
        logger.info(f"🔎 Token: {token_address} | Venue: {venue} | Target: {target_addr} | QuoteAsset: {quote_asset} | Fee: {fee} | Detection time: {t_detect - start_time:.2f}s")
        print(f"🔎 Token: {token_address} | Venue: {venue} | Target: {target_addr} | QuoteAsset: {quote_asset} | Fee: {fee}")

        if venue == "NONE":
            info = {}
            try:
                info = await asyncio.wait_for(
                    asyncio.to_thread(self.stock_v4.detect, token_address),
                    timeout=8.0,
                )
            except (ExecutionUncertain, PreflightFailure):
                raise
            except Exception as e:
                logger.warning(f"stock_v4.detect skipped/timed out: {e}")
                info = {}
            # info is {} when detect() times out. {}.get("venue") is None, which is
            # not "NONE", so this branch used to be entered and then KeyError on
            # info["venue"] -- killing the trade on every detect timeout.
            if info.get("venue", "NONE") != "NONE":
                venue = info["venue"]
                quote_asset = info.get("quote", ZERO)
                target_addr = info.get("target", ZERO)
                fee = info.get("fee", 0)
                if "tick" in info or "hook" in info:
                    self._v4_pool_params_cache[token_address] = {
                        "tick": info.get("tick", 200),
                        "hook": info.get("hook", PONS_V2_HOOK),
                    }
                logger.info(f"⚡ StockV4Router detected: {venue} | Target: {target_addr} | Quote: {quote_asset}")
                print(f"⚡ StockV4Router detected: {venue} | Target: {target_addr} | Quote: {quote_asset}")

                if getattr(self.config, "DRY_RUN", True):
                    ms = int((time.time() - start_time) * 1000)
                    log_line = f"{ms}ms | {token_address} | {venue} | {target_addr} | sent=False (DRY_RUN) | tx=DRY_RUN_0x{int(time.time())} | status=1 | token_delta=+{eth_amount*1000:,.4f} | quote_delta=-{eth_amount:.6f}"
                    logger.info(log_line)
                    print(log_line)
                    return f"DRY_RUN_0x{int(time.time())}", await self._dry_run_fill(token_address, eth_amount)

                bal_before = await self._safe_token_balance(token_address)
                tx_hash = None
                try:
                    tx_res = await asyncio.to_thread(self.stock_v4.buy, token_address, eth_amount)
                    tx_hash = tx_res[1] if isinstance(tx_res, tuple) else tx_res
                except (ExecutionUncertain, PreflightFailure):
                    raise
                except Exception as e:
                    logger.warning(f"stock_v4 skip {token_address} {e}")
                    tx_hash = None

                if not tx_hash and venue == "UNISWAP_V4" and quote_asset.lower() not in (self.weth_address.lower(), ZERO.lower(), "0x0000000000000000000000000000000000000000"):
                    v4_tick = info.get("tick", 200)
                    v4_hook = info.get("hook", PONS_V2_HOOK)
                    v4_fee = fee if fee is not None and int(fee) >= 0 else 0
                    try:
                        if quote_asset.lower() == self.usdg_address.lower():
                            logger.info(f"USDG V4 two-tx buy for {token_address} fee={v4_fee} tick={v4_tick}")
                            res = await asyncio.to_thread(
                                self.stock_v4.buy_usdg_two_tx,
                                token_address, eth_amount, v4_fee, v4_tick, v4_hook,
                                self.usdg_leg_min_out(token_address, v4_fee, v4_tick, v4_hook, slippage_pct)
                            )
                        else:
                            res = await asyncio.to_thread(
                                self.stock_v4.buy_stock_two_tx,
                                token_address, quote_asset, eth_amount,
                                v4_fee, v4_tick, v4_hook
                            )
                        tx_hash = res[1] if isinstance(res, tuple) else res
                    except (ExecutionUncertain, PreflightFailure):
                        raise
                    except Exception as e:
                        logger.error(f"Fallback two-tx buy failed for {token_address} quote={quote_asset}: {e}")

                if tx_hash:
                    receipt = await self.chain.wait_for_receipt(tx_hash)
                    tokens_received = await self._measure_fill(token_address, bal_before)
                    status = receipt.get("status", 0)
                    ms = int((time.time() - start_time) * 1000)
                    log_line = f"{ms}ms | {token_address} | {venue} | {target_addr} | sent=True | tx={tx_hash} | status={status} | token_delta={tokens_received:+,.4f} | quote_delta=-{eth_amount:.6f}"
                    logger.info(log_line)
                    print(log_line)
                    if status == 1 and tokens_received > 0:
                        return tx_hash, tokens_received
                    return tx_hash, 0.0
                else:
                    logger.error(f"❌ StockV4Router.buy returned None for {token_address}")
                    return "", 0.0
            else:
                ms = int((time.time() - start_time) * 1000)
                log_line = f"{ms}ms | {token_address} | NONE | 0x0000000000000000000000000000000000000000 | sent=False | tx=None | status=None | token_delta=+0.0000 | quote_delta=+0.000000"
                logger.info(log_line)
                print(log_line)
                return "", 0.0

        if getattr(self.config, "DRY_RUN", True):
            ms = int((time.time() - start_time) * 1000)
            log_line = f"{ms}ms | {token_address} | {venue} | {target_addr} | sent=False (DRY_RUN) | tx=DRY_RUN_0x{int(time.time())} | status=1 | token_delta=+{eth_amount*1000:,.4f} | quote_delta=-{eth_amount:.6f}"
            logger.info(log_line)
            print(log_line)
            return f"DRY_RUN_0x{int(time.time())}", await self._dry_run_fill(token_address, eth_amount)

        min_out = await self.min_out_for(
            venue, token_address, quote_asset, fee, eth_wei, target_addr, slippage_pct
        )
        if min_out is None:
            # No quote means no slippage protection. Sending anyway is how you get
            # sandwiched for the full stake, so skip the trade instead.
            logger.error(
                f"❌ Cannot quote {venue} route for {token_address} -- refusing to buy "
                f"without slippage protection"
            )
            return "", 0.0

        if venue in ["V4", "STOCK_PAIR", "V4_STOCK"]:
            v4_fee, v4_tick, v4_hook = await asyncio.to_thread(
                self._v4_params_for, token_address, quote_asset, fee)
            return await self.force_buy_v4_or_stock(
                token_address, eth_amount, slippage_pct,
                fee=v4_fee, tick=v4_tick, hook=v4_hook
            )

        candidate_txs = []

        # Handle UNISWAP_V4 from stock_v4 fallback (includes USDG & stock quotes)
        if venue == "UNISWAP_V4":
            # Resolve against live pool state instead of guessing from defaults —
            # the guess is what made V4 buys target pools that do not exist.
            v4_fee_resolved, v4_tick, v4_hook = await asyncio.to_thread(
                self._v4_params_for, token_address, quote_asset, fee)
            fee = v4_fee_resolved

            if quote_asset.lower() == self.usdg_address.lower():
                # Prefer two-tx ETH->USDG->token to avoid 0x3b99b53d hybrid encode
                try:
                    if not getattr(self.config, "DRY_RUN", True):
                        bal_before = await self._safe_token_balance(token_address)
                        res = await asyncio.to_thread(
                            self.stock_v4.buy_usdg_two_tx,
                            token_address, eth_amount, fee if fee is not None else 3000, v4_tick, v4_hook,
                            self.usdg_leg_min_out(token_address, fee if fee is not None else 3000, v4_tick, v4_hook, slippage_pct)
                        )
                        tx2 = res[1] if isinstance(res, tuple) else res
                        if tx2:
                            receipt = await self.chain.wait_for_receipt(tx2)
                            got = await self._measure_fill(token_address, bal_before)
                            if receipt.get("status", 0) == 1 and got > 0:
                                return tx2, got
                except (ExecutionUncertain, PreflightFailure):
                    raise
                except Exception as e:
                    logger.warning(f"buy_usdg_two_tx failed for {token_address}: {e}")
                # No atomic fallback here on purpose. build_v4_usdg_2hop_tx has never
                # produced a transaction this chain accepts -- see the note on that
                # method. Queuing it only wasted a simulation round-trip and then
                # dropped through to the generic fallbacks anyway, so go there directly.
                logger.warning(
                    f"No single-tx V4 route exists for USDG-quoted {token_address}; "
                    f"deferring to fallback detection"
                )
            elif self.w3.to_checksum_address(quote_asset) in EXTENDED_STOCK_LIST or quote_asset.lower() not in (self.weth_address.lower(), ZERO.lower(), "0x0000000000000000000000000000000000000000"):
                if getattr(self.config, "DRY_RUN", True):
                    return f"DRY_RUN_0x{int(time.time())}", await self._dry_run_fill(token_address, eth_amount)
                tick = v4_tick
                hook = v4_hook
                bal_before = await self._safe_token_balance(token_address)
                tx2 = None
                last_err = None
                # Two-leg buy (ETH->STOCK, then STOCK->TOKEN) has more failure
                # surface than a single-hop swap - a transient revert on either
                # leg (slippage on the stock leg, a stale nonce, etc.) used to
                # kill the whole trade with no retry. Give it one retry before
                # giving up.
                for attempt in range(2):
                    try:
                        tx1, tx2 = await asyncio.to_thread(
                            self.stock_v4.buy_stock_two_tx,
                            token_address, quote_asset, eth_amount,
                            fee if fee else 0,
                            tick,
                            hook
                        )
                        break
                    except (ExecutionUncertain, PreflightFailure):
                        raise
                    except Exception as e:
                        last_err = e
                        logger.warning(f"Stock two-tx buy attempt {attempt + 1}/2 failed for {token_address}: {e}")
                        if attempt == 0:
                            await asyncio.sleep(1)
                if tx2 is None:
                    logger.error(f"Stock two-tx buy failed for {token_address} after retry: {last_err}")
                    return "", 0.0
                # tx2 is the actual STOCK->TOKEN leg - that's the trade that
                # matters for position tracking. Confirm it and measure the
                # real tokens received instead of assuming a fixed amount.
                try:
                    await self.chain.wait_for_receipt(tx2)
                except (ExecutionUncertain, PreflightFailure):
                    raise
                except Exception as e:
                    logger.warning(f"Could not confirm stock-leg buy receipt for {token_address}: {e}")
                tokens_received = await self._measure_fill(token_address, bal_before)
                return tx2, tokens_received if tokens_received > 0 else 0.0
            else:
                tx_v4, cmd_v4 = self.build_v4_swap_tx(
                    token_address, eth_wei, min_out, recipient, quote_asset, fee=fee, tick=v4_tick, hook=v4_hook
                )
                candidate_txs.append((venue, self.uni_router_address, tx_v4, cmd_v4))

        elif venue in ["LAUNCHPAD_CURVE_ETH", "LAUNCHPAD_CURVE_USDG"]:
            # If pair is USDG, ensure USDG is approved to curve contract first and msg.value = 0
            if quote_asset.lower() == self.usdg_address.lower():
                eth_price_usd = await self.chain.get_eth_price_usd()
                quote_in_usdg = int(eth_amount * eth_price_usd * 10**6)
                try:
                    usdg_contract = self.w3.eth.contract(address=self.usdg_address, abi=ERC20_ABI)
                    usdg_bal = await self.chain._retry(usdg_contract.functions.balanceOf(recipient).call)
                    if usdg_bal < quote_in_usdg:
                        logger.warning(f"Insufficient USDG balance ({usdg_bal/1e6:.2f} USDG < {quote_in_usdg/1e6:.2f} USDG) to buy USDG curve. Skipping.")
                        return "", 0.0

                    allowance = await self.chain._retry(usdg_contract.functions.allowance(recipient, target_addr).call)
                    if allowance < quote_in_usdg:
                        logger.info(f"Approving USDG Curve {target_addr} for USDG spending...")
                        await self.chain.approve_token(self.usdg_address, target_addr, 2**256 - 1)
                except (ExecutionUncertain, PreflightFailure):
                    raise
                except Exception as e:
                    logger.warning(f"USDG approval/balance check to curve {target_addr} failed: {e}")

                tx_c, cmd_c = self.build_curve_buy_tx(target_addr, quote_in_usdg, min_out, recipient, quote_asset)
                candidate_txs.append((venue, target_addr, tx_c, cmd_c))
            else:
                tx_c, cmd_c = self.build_curve_buy_tx(target_addr, eth_wei, min_out, recipient, quote_asset)
                candidate_txs.append((venue, target_addr, tx_c, cmd_c))

        elif "UNISWAP_V3" in venue:
            tx_v3, cmd_v3 = self.build_v3_swap_tx(token_address, eth_wei, min_out, recipient, quote_asset, fee=fee)
            candidate_txs.append((venue, self.swap_router02_addr, tx_v3, cmd_v3))

        elif "UNISWAP_V2" in venue:
            calldata = self.v2_router.functions.swapExactETHForTokensSupportingFeeOnTransferTokens(
                min_out, [self.weth_address, token_address], recipient, int(time.time()) + 300
            )._encode_transaction_data()
            tx_v2 = {'from': recipient, 'to': UNISWAP_V2_ROUTER, 'value': eth_wei, 'data': calldata, 'chainId': 4663}
            candidate_txs.append((venue, UNISWAP_V2_ROUTER, tx_v2, "swapExactETHForTokens"))

        # Step 2: Check per-venue simulation setting (Kill-Switch Enabled)
        gas_limits = {
            "LAUNCHPAD_CURVE_ETH": 130000,
            "LAUNCHPAD_CURVE_USDG": 160000,
            "UNISWAP_V3_WETH": 260000,
            "UNISWAP_V3_USDG": 300000,
            "UNISWAP_V2_WETH": 200000,
            "UNISWAP_V2_USDG": 250000
        }

        for v_name, target, tx, cmd_str in candidate_txs:
            tx['maxFeePerGas'], tx['maxPriorityFeePerGas'] = eip1559_fees(self.w3)
            tx['gas'] = gas_limits.get(v_name, 350000)

            skip_sim = (v_name in self.no_sim_venues)

            if not skip_sim:
                t_sim_start = time.time()
                success, sim_result = await self.simulate_execution(tx, v_name, target, cmd_str)
                t_sim_end = time.time()
                if not success:
                    ms = int((time.time() - start_time) * 1000)
                    log_line = f"{ms}ms | {token_address} | {v_name} | {target} | sent=False | tx=None | status=SIM_FAIL | token_delta=+0.0000 | quote_delta=+0.000000"
                    logger.warning(log_line)
                    print(log_line)
                    continue
                logger.info(f"🚀 Simulation PASSED in {t_sim_end - t_sim_start:.2f}s for {v_name}")
            else:
                logger.info(f"⚡ NO-SIM FAST BROADCAST for {v_name} (proven venue)")

            # Query balance BEFORE transaction to ensure clean delta check
            bal_before = await self._safe_token_balance(token_address)

            tx_hash = await self.chain.send_transaction(tx)
            receipt = await self.chain.wait_for_receipt(tx_hash)

            # Query balance AFTER transaction receipt
            tokens_received = await self._measure_fill(token_address, bal_before)
            status = receipt.get("status", 0)

            ms = int((time.time() - start_time) * 1000)
            quote_spent = -(eth_amount if quote_asset.lower() != self.usdg_address.lower() else (eth_wei / 1e6))
            log_line = f"{ms}ms | {token_address} | {v_name} | {target} | sent=True | tx={tx_hash} | status={status} | token_delta={tokens_received:+,.4f} | quote_delta={quote_spent:+.6f}"
            logger.info(log_line)
            print(log_line)

            if status == 1 and tokens_received > 0:
                self.record_fill(v_name, True)
                return tx_hash, tokens_received
            else:
                self.record_fill(v_name, False)
                # KILL SWITCH: If transaction failed or 0 tokens received on a no-sim venue, re-enable sim
                if v_name in self.no_sim_venues:
                    self.no_sim_venues.discard(v_name)
                    logger.critical(f"🚨 KILL-SWITCH TRIGGERED on {v_name}! Status={status}, Delta={tokens_received}. Re-enabling pre-flight simulation for {v_name}.")
                    print(f"🚨 KILL-SWITCH TRIGGERED on {v_name}! Status={status}, Delta={tokens_received}. Re-enabling pre-flight simulation for {v_name}.")
                return tx_hash, 0.0

        # ==========================================
        # FALLBACK: Primary route sim failed — try StockV4Router + V4/V2
        # ==========================================
        logger.warning(f"Primary route failed for {token_address}, trying fallback detection...")
        try:
            info = await asyncio.to_thread(self.stock_v4.detect, token_address)
            if info.get("venue") != "NONE":
                fb_venue = info["venue"]
                fb_quote = info.get("quote", ZERO)
                fb_target = info.get("target", ZERO)
                fb_fee = info.get("fee", 0)
                fb_fee_resolved, fb_tick, fb_hook = await asyncio.to_thread(
                    self._v4_params_for, token_address, fb_quote, fb_fee)
                fb_fee = fb_fee_resolved
                logger.info(f"Fallback detected: {fb_venue} | Target: {fb_target} | Quote: {fb_quote}")

                if fb_venue == "UNISWAP_V4":
                    if fb_quote.lower() in (self.weth_address.lower(), ZERO.lower(), "0x0000000000000000000000000000000000000000"):
                        # force_buy_v4_or_stock measures its own fill now, so pass
                        # its result straight through instead of inventing one.
                        tx_hash, received = await self.force_buy_v4_or_stock(
                            token_address, eth_amount, slippage_pct,
                            fee=fb_fee, tick=fb_tick, hook=fb_hook
                        )
                        if tx_hash and received > 0:
                            return tx_hash, received
                    elif fb_quote.lower() == self.usdg_address.lower():
                        # Two transactions, not one atomic encode — the atomic V4
                        # USDG route does not work on this chain (see
                        # build_v4_usdg_2hop_tx). Measure the real balance delta
                        # rather than reporting an assumed token count.
                        try:
                            fb_bal_before = await self._safe_token_balance(token_address)
                            res = await asyncio.to_thread(
                                self.stock_v4.buy_usdg_two_tx,
                                token_address, eth_amount, fb_fee, fb_tick, fb_hook,
                                self.usdg_leg_min_out(token_address, fb_fee, fb_tick, fb_hook, slippage_pct)
                            )
                            tx_hash = res[1] if isinstance(res, tuple) else res
                            if tx_hash:
                                await self.chain.wait_for_receipt(tx_hash)
                                got = await self.chain.get_token_balance(token_address) - fb_bal_before
                                if got > 0:
                                    return tx_hash, got
                        except (ExecutionUncertain, PreflightFailure):
                            raise
                        except Exception as e:
                            logger.warning(f"Fallback USDG two-tx buy failed: {e}")
                    else:
                        # Stock quote fallback
                        if getattr(self.config, "DRY_RUN", True):
                            return f"DRY_RUN_0x{int(time.time())}", await self._dry_run_fill(token_address, eth_amount)
                        fb_bal_before = await self._safe_token_balance(token_address)
                        try:
                            tx1, tx2 = await asyncio.to_thread(
                                self.stock_v4.buy_stock_two_tx,
                                token_address, fb_quote, eth_amount,
                                fb_fee, fb_tick, fb_hook
                            )
                            # tx2 is the STOCK->TOKEN leg that actually delivers
                            # the token - return that hash, and measure real
                            # tokens received instead of assuming a fixed amount.
                            try:
                                await self.chain.wait_for_receipt(tx2)
                            except (ExecutionUncertain, PreflightFailure):
                                raise
                            except Exception as e:
                                logger.warning(f"Could not confirm fallback stock-leg receipt for {token_address}: {e}")
                            fb_tokens_received = await self._measure_fill(token_address, fb_bal_before)
                            return tx2, fb_tokens_received if fb_tokens_received > 0 else 0.0
                        except (ExecutionUncertain, PreflightFailure):
                            raise
                        except Exception as e:
                            logger.warning(f"Stock two-tx fallback failed: {e}")

                elif fb_venue.startswith("UNISWAP_V2"):
                    # Re-quote for the route actually being taken. min_out was
                    # computed for the originally detected venue, and a floor from a
                    # different pool is either too strict (reverts) or too loose.
                    v2_min_out = await self.min_out_for(
                        fb_venue, token_address, self.weth_address, 0, eth_wei,
                        UNISWAP_V2_ROUTER, slippage_pct
                    )
                    if v2_min_out is None:
                        logger.error(f"Cannot quote V2 fallback for {token_address}; skipping")
                    else:
                        calldata = self.v2_router.functions.swapExactETHForTokensSupportingFeeOnTransferTokens(
                            v2_min_out, [self.weth_address, token_address], recipient, int(time.time()) + 300
                        )._encode_transaction_data()
                        tx_v2 = {'from': recipient, 'to': UNISWAP_V2_ROUTER, 'value': eth_wei, 'data': calldata, 'chainId': 4663}
                        try:
                            gas_est = await asyncio.to_thread(self.w3.eth.estimate_gas, tx_v2)
                            tx_v2['gas'] = int(gas_est * 1.2)
                            v2_bal_before = await self._safe_token_balance(token_address)
                            tx_hash = await self.chain.send_transaction(tx_v2)
                            receipt = await self.chain.wait_for_receipt(tx_hash)
                            if receipt.get("status") == 1:
                                received = await self._measure_fill(token_address, v2_bal_before)
                                if received > 0:
                                    return tx_hash, received
                        except (ExecutionUncertain, PreflightFailure):
                            raise
                        except Exception as e:
                            logger.warning(f"Fallback V2 sim failed: {e}")
        except (ExecutionUncertain, PreflightFailure):
            raise
        except Exception as e:
            logger.warning(f"Fallback detection failed: {e}")

        # ==========================================
        # LAST RESORT: Aggressive no-sim V4 broadcast
        # ==========================================
        logger.warning(f"All simulated routes failed for {token_address}. Attempting no-sim V4 last resort...")
        try:
            tx_hash, received = await self.force_buy_v4_or_stock(token_address, eth_amount, slippage_pct)
            if tx_hash and received > 0:
                return tx_hash, received
        except (ExecutionUncertain, PreflightFailure):
            raise
        except Exception as e:
            logger.error(f"No-sim V4 last resort failed: {e}")

        logger.error(f"❌ All buy routes failed for {token_address}")
        return "", 0.0

    async def sell_token(self, token_address: str, token_amount: float, slippage_pct: float = 15.0) -> Tuple[str, float]:
        self.w3 = self.chain.w3
        await self.verify_chain_id()
        token_address = self.w3.to_checksum_address(token_address)
        recipient = self.chain.account.address if self.chain.account else "0x" + "0"*40

        venue, target_addr, quote_asset, fee = await self.detect_venue_and_route(token_address)
        logger.info(f"Selling {token_amount} of {token_address} | Detected Venue: {venue}")
        print(f"Selling {token_amount} of {token_address}")
        print(f"Detected Venue: {venue}")

        if getattr(self.config, "DRY_RUN", True):
            logger.info(f"[DRY RUN] Would sell {token_amount} of {token_address} on venue {venue}")
            print(f"⚡ [DRY RUN] Would sell {token_amount} of {token_address} on venue {venue}")
            return f"DRY_RUN_SELL_0x{int(time.time())}", token_amount

        # Get decimals
        token_contract = self.w3.eth.contract(address=token_address, abi=ERC20_ABI)
        try:
            decimals = await self.chain._retry(token_contract.functions.decimals().call)
        except (ExecutionUncertain, PreflightFailure):
            raise
        except Exception:
            decimals = 18

        amount_in = int(token_amount * (10**decimals))

        # Clamp to the real on-chain balance BEFORE choosing a venue, so EVERY
        # route gets it. position.remaining_tokens is a float; float64 holds ~16
        # significant digits against a 22-digit raw balance, so amount_in can come
        # out ABOVE what the wallet holds. Seen twice live: 637,541 wei over on
        # KELLYOW (V4), and 964,804 wei over on a curve-quoted token which kept
        # failing ERC20InsufficientBalance because this clamp was nested inside the
        # V4 branch and every other venue skipped it.
        bal_before = await self.chain.get_token_balance(token_address)
        live_raw = await self.chain.get_token_balance_raw(token_address)
        if live_raw <= 0:
            logger.error(f"Cannot sell {token_address}: wallet holds none of it")
            return "", 0.0
        if amount_in > live_raw:
            # Never ask for more than the wallet holds. Either the caller meant
            # "all of it" and float precision overshot, or the balance moved.
            logger.info(
                f"Clamping sell of {token_address}: asked {amount_in}, "
                f"wallet holds {live_raw} (over by {amount_in - live_raw})"
            )
            amount_in = live_raw
        elif abs(live_raw - amount_in) <= max(1, live_raw // 1_000_000):
            # Within a rounding hair of the whole balance: sell exactly all of it
            # so no dust is stranded and the position closes cleanly.
            amount_in = live_raw
        # min_out is quoted from amount_in below, so the clamped size gets the
        # correct floor automatically -- nothing to scale here.

        min_out = await self.min_out_for(
            venue, token_address, quote_asset, fee, amount_in, target_addr,
            slippage_pct, selling=True
        )
        if min_out is None:
            # Refuse rather than dump the bag at any price. The monitor retries on
            # the next poll (position_monitor.py:37), so a transient quoter failure
            # delays the exit instead of giving the position away.
            logger.error(
                f"❌ Cannot quote sell route {venue} for {token_address} -- refusing to "
                f"sell without slippage protection; will retry"
            )
            return "", 0.0

        if venue in ("UNISWAP_V4", "NONE"):
            v4_fee, v4_tick, v4_hook = self._v4_params_for(token_address, quote_asset, fee)
            # The resolver may have settled on a different (working) pool than the
            # one detection named -- build and quote against that one.
            quote_asset = self.v4_resolved_quote(token_address, quote_asset)

            # sell_to_eth still keys its PoolKey on WETH and appends UNWRAP_WETH, so
            # on this chain's native-keyed pools it always reverts and costs a
            # round trip before the real path runs. build_v4_sell_tx handles the
            # ETH-quoted case correctly (verified on-chain), so only fall back to
            # sell_to_eth for the stock/USDG multi-hop exits it uniquely handles.
            quote_is_ethish = quote_asset.lower() in (
                self.weth_address.lower(), ZERO.lower(),
                "0x0000000000000000000000000000000000000000")

            try:
                if quote_is_ethish:
                    raise RuntimeError("ETH-quoted: using build_v4_sell_tx directly")
                txh = await asyncio.to_thread(
                    self.stock_v4.sell_to_eth,
                    token_address, amount_in, v4_fee, v4_tick, v4_hook, quote_asset,
                    min_out
                )
                if txh:
                    receipt = await self.chain.wait_for_receipt(txh)
                    bal_after = await self.chain.get_token_balance(token_address)
                    tokens_sold = bal_before - bal_after
                    if receipt.get("status", 0) == 1 and tokens_sold > 0:
                        logger.info(f"✅ V4/stock sell filled: {tokens_sold:.4f} tokens tx={txh}")
                        return txh, tokens_sold
            except (ExecutionUncertain, PreflightFailure):
                raise
            except Exception as e:
                logger.warning(f"sell_to_eth failed ({venue} quote={quote_asset}): {e}")

            try:
                await asyncio.to_thread(self.stock_v4.ensure_permit2, token_address, amount_in)
                tx_v4, cmd_v4 = self.build_v4_sell_tx(
                    token_address, amount_in, min_out, recipient, quote_asset,
                    fee=v4_fee if v4_fee >= 0 else 0, tick=v4_tick if v4_tick > 0 else 200, hook=v4_hook
                )
                tx = tx_v4
                spender = self.uni_router_address
                venue = "UNISWAP_V4"
            except (ExecutionUncertain, PreflightFailure):
                raise
            except Exception as e:
                logger.error(f"Failed to build V4 sell tx for {token_address}: {e}")
                return "", 0.0

        elif venue in ["LAUNCHPAD_CURVE_ETH", "LAUNCHPAD_CURVE_USDG"]:
            spender = target_addr
            allowance = await self.chain._retry(token_contract.functions.allowance(recipient, spender).call)
            if allowance < amount_in:
                logger.info(f"Approving {spender} to spend {token_address}...")
                await self.chain.approve_token(token_address, spender, 2**256 - 1)
            curve = self.w3.eth.contract(address=spender, abi=PONS_CURVE_ABI)
            calldata = curve.functions.sell(amount_in, min_out, recipient)._encode_transaction_data()
            tx = {'from': recipient, 'to': spender, 'value': 0, 'data': calldata, 'chainId': 4663}

        elif venue == "UNISWAP_V3_WETH":
            spender = self.swap_router02_addr
            allowance = await self.chain._retry(token_contract.functions.allowance(recipient, spender).call)
            if allowance < amount_in:
                logger.info(f"Approving {spender} to spend {token_address}...")
                await self.chain.approve_token(token_address, spender, 2**256 - 1)
            router = self.w3.eth.contract(address=self.swap_router02_addr, abi=SWAP_ROUTER02_ABI)
            params = (token_address, self.weth_address, fee if fee else 10000, recipient, amount_in, min_out, 0)
            calldata = router.functions.exactInputSingle(params)._encode_transaction_data()
            tx = {'from': recipient, 'to': self.swap_router02_addr, 'value': 0, 'data': calldata, 'chainId': 4663}

        elif venue == "UNISWAP_V3_USDG":
            spender = self.swap_router02_addr
            allowance = await self.chain._retry(token_contract.functions.allowance(recipient, spender).call)
            if allowance < amount_in:
                logger.info(f"Approving {spender} to spend {token_address}...")
                await self.chain.approve_token(token_address, spender, 2**256 - 1)
            router = self.w3.eth.contract(address=self.swap_router02_addr, abi=SWAP_ROUTER02_ABI)
            path_bytes = (
                bytes.fromhex(token_address[2:]) +
                (fee if fee else 3000).to_bytes(3, 'big') +
                bytes.fromhex(self.usdg_address[2:]) +
                (500).to_bytes(3, 'big') +
                bytes.fromhex(self.weth_address[2:])
            )
            calldata = router.functions.exactInput((path_bytes, recipient, amount_in, min_out))._encode_transaction_data()
            tx = {'from': recipient, 'to': self.swap_router02_addr, 'value': 0, 'data': calldata, 'chainId': 4663}

        elif venue in ("STOCK_PAIR", "UNISWAP_V3_STOCK"):
            spender = self.swap_router02_addr
            allowance = await self.chain._retry(token_contract.functions.allowance(recipient, spender).call)
            if allowance < amount_in:
                logger.info(f"Approving {spender} to spend {token_address}...")
                await self.chain.approve_token(token_address, spender, 2**256 - 1)
            router = self.w3.eth.contract(address=self.swap_router02_addr, abi=SWAP_ROUTER02_ABI)
            path_bytes = (
                bytes.fromhex(token_address[2:]) +
                (fee if fee else 3000).to_bytes(3, 'big') +
                bytes.fromhex(quote_asset[2:]) +
                (500).to_bytes(3, 'big') +
                bytes.fromhex(self.usdg_address[2:]) +
                (500).to_bytes(3, 'big') +
                bytes.fromhex(self.weth_address[2:])
            )
            calldata = router.functions.exactInput((path_bytes, recipient, amount_in, min_out))._encode_transaction_data()
            tx = {'from': recipient, 'to': self.swap_router02_addr, 'value': 0, 'data': calldata, 'chainId': 4663}

        elif venue == "UNISWAP_V2_WETH":
            spender = self.w3.to_checksum_address(UNISWAP_V2_ROUTER)
            allowance = await self.chain._retry(token_contract.functions.allowance(recipient, spender).call)
            if allowance < amount_in:
                logger.info(f"Approving {spender} to spend {token_address}...")
                await self.chain.approve_token(token_address, spender, 2**256 - 1)
            calldata = self.v2_router.functions.swapExactTokensForETHSupportingFeeOnTransferTokens(
                amount_in, min_out, [token_address, self.weth_address], recipient, int(time.time()) + 300
            )._encode_transaction_data()
            tx = {'from': recipient, 'to': UNISWAP_V2_ROUTER, 'value': 0, 'data': calldata, 'chainId': 4663}

        elif venue == "UNISWAP_V2_USDG":
            spender = self.w3.to_checksum_address(UNISWAP_V2_ROUTER)
            allowance = await self.chain._retry(token_contract.functions.allowance(recipient, spender).call)
            if allowance < amount_in:
                logger.info(f"Approving {spender} to spend {token_address}...")
                await self.chain.approve_token(token_address, spender, 2**256 - 1)
            calldata = self.v2_router.functions.swapExactTokensForTokensSupportingFeeOnTransferTokens(
                amount_in, min_out, [token_address, self.usdg_address], recipient, int(time.time()) + 300
            )._encode_transaction_data()
            tx = {'from': recipient, 'to': UNISWAP_V2_ROUTER, 'value': 0, 'data': calldata, 'chainId': 4663}

        else:
            logger.error(f"No supported sell venue found for {token_address}")
            return "", 0.0

        # Broadcast
        gas_limits = {
            "LAUNCHPAD_CURVE_ETH": 120000,
            "LAUNCHPAD_CURVE_USDG": 140000,
            "UNISWAP_V3_WETH": 260000,
            "UNISWAP_V3_USDG": 300000,
            "UNISWAP_V3_STOCK": 360000,
            "STOCK_PAIR": 360000,
            "UNISWAP_V2_WETH": 220000,
            "UNISWAP_V2_USDG": 260000,
            "UNISWAP_V4": 450000,
        }

        tx['maxFeePerGas'], tx['maxPriorityFeePerGas'] = eip1559_fees(self.w3)
        tx['gas'] = gas_limits.get(venue, 350000)

        skip_sim = (venue in self.no_sim_venues)

        if not skip_sim:
            success, sim_result = await self.simulate_execution(tx, f"SELL_{venue}", tx['to'], "sell")
            if not success:
                logger.error(f"Sell simulation failed for {token_address}: {sim_result}")
                return "", 0.0
        else:
            logger.info(f"⚡ NO-SIM FAST BROADCAST for sell on {venue} (proven venue)")

        t_sell_start = time.time()
        bal_before = await self.chain.get_token_balance(token_address)
        tx_hash = await self.chain.send_transaction(tx)
        logger.info(f"✅ SELL submitted! Tx: {tx_hash}. Waiting for receipt...")
        receipt = await self.chain.wait_for_receipt(tx_hash)
        bal_after = await self.chain.get_token_balance(token_address)
        tokens_sold = bal_before - bal_after
        status = receipt.get("status", 0)

        ms = int((time.time() - t_sell_start) * 1000)
        log_line = f"{ms}ms | {token_address} | {venue} | {spender} | sent=True | tx={tx_hash} | status={status} | token_delta={-tokens_sold:+,.4f} | quote_delta=+0.000000"
        logger.info(log_line)
        print(log_line)

        if status == 1 and tokens_sold > 0:
            return tx_hash, tokens_sold
        else:
            if venue in self.no_sim_venues:
                self.no_sim_venues.discard(venue)
                logger.critical(f"🚨 KILL-SWITCH TRIGGERED on {venue} sell! Status={status}, Sold={tokens_sold}. Re-enabling simulation.")
                print(f"🚨 KILL-SWITCH TRIGGERED on {venue} sell! Status={status}, Sold={tokens_sold}. Re-enabling simulation.")
            return tx_hash, 0.0

    # ─── Slippage protection ─────────────────────────────────────────────
    #
    # Every swap in this file used to go out with min_out = 1 wei, which is not
    # 15% slippage — it is none at all. Any output above zero was accepted, so a
    # sandwich could take essentially the whole trade and a thin pool would fill
    # at any price instead of reverting. SLIPPAGE_PCT was threaded through every
    # signature and never used to compute anything.
    #
    # These quote the route on live pool state (which prices in impact, unlike the
    # slot0 spot price) and floor the output by the configured tolerance.

    @staticmethod
    def _apply_slippage(expected_out: int, slippage_pct: float) -> int:
        pct = max(0.0, min(float(slippage_pct), 100.0))
        return max(1, int(int(expected_out) * (100.0 - pct) / 100.0))

    def _quote_v3(self, token_in: str, token_out: str, amount_in: int, fee: int,
                  via: Optional[str] = None) -> Optional[int]:
        cs = self.w3.to_checksum_address
        try:
            if via:
                path = encode_v3_path([cs(token_in), cs(via), cs(token_out)], [500, int(fee)])
                return int(self.v3_quoter.functions.quoteExactInput(path, int(amount_in)).call()[0])
            return int(self.v3_quoter.functions.quoteExactInputSingle(
                (cs(token_in), cs(token_out), int(amount_in), int(fee), 0)).call()[0])
        except (ExecutionUncertain, PreflightFailure):
            raise
        except Exception as e:
            logger.debug(f"V3 quote failed {token_in}->{token_out} fee={fee} via={via}: {e}")
            return None

    def _quote_v4(self, token_in: str, token_out: str, amount_in: int,
                  fee: int, tick: int, hook: str) -> Optional[int]:
        cs = self.w3.to_checksum_address
        a, b = cs(token_in), cs(token_out)
        c0, c1 = (a, b) if int(a, 16) < int(b, 16) else (b, a)
        try:
            return int(self.v4_quoter.functions.quoteExactInputSingle(
                ((c0, c1, int(fee), int(tick), cs(hook)), c0.lower() == a.lower(),
                 int(amount_in), b'')).call()[0])
        except (ExecutionUncertain, PreflightFailure):
            raise
        except Exception as e:
            logger.debug(f"V4 quote failed {a}->{b} fee={fee} tick={tick}: {e}")
            return None

    def _quote_v2(self, token_in: str, token_out: str, amount_in: int) -> Optional[int]:
        cs = self.w3.to_checksum_address
        try:
            amounts = self.v2_router.functions.getAmountsOut(
                int(amount_in), [cs(token_in), cs(token_out)]).call()
            return int(amounts[-1])
        except (ExecutionUncertain, PreflightFailure):
            raise
        except Exception as e:
            logger.debug(f"V2 quote failed {token_in}->{token_out}: {e}")
            return None

    def _quote_curve(self, curve_address: str, amount_in: int, token_is_out: bool) -> Optional[int]:
        """
        Bonding curves have no quoter, so price the swap off the pair reserves that
        get_token_price_eth already reads (selector 0x0902f1ac).

        ponytail: assumes constant product with no fee, so it can over-estimate the
        output on a curve that is not x*y=k. That direction is safe — it makes
        min_out stricter, never looser — but a curve-specific quote would be tighter.
        """
        try:
            res = self.w3.eth.call({'to': self.w3.to_checksum_address(curve_address),
                                    'data': '0x0902f1ac'})
            raw = res.hex()
            if len(raw) < 128:
                return None
            r_quote, r_token = int(raw[:64], 16), int(raw[64:128], 16)
            r_in, r_out = (r_quote, r_token) if token_is_out else (r_token, r_quote)
            if r_in <= 0 or r_out <= 0:
                return None
            return int((int(amount_in) * r_out) // (r_in + int(amount_in)))
        except (ExecutionUncertain, PreflightFailure):
            raise
        except Exception as e:
            logger.debug(f"Curve quote failed {curve_address}: {e}")
            return None

    def _quote_route(self, venue: str, token_address: str, quote_asset: str, fee: int,
                     amount_in: int, target_addr: str, selling: bool) -> Optional[int]:
        """
        Expected output for one swap along the route `venue` describes.
        Returns None when the route cannot be quoted — callers must refuse to
        trade rather than fall back to an unprotected min_out.
        """
        cs = self.w3.to_checksum_address
        token, weth, usdg = cs(token_address), self.weth_address, self.usdg_address

        if "CURVE" in venue:
            return self._quote_curve(target_addr, amount_in, token_is_out=not selling)

        if "UNISWAP_V2" in venue:
            other = usdg if quote_asset.lower() == usdg.lower() else weth
            return self._quote_v2(token, other, amount_in) if selling else self._quote_v2(other, token, amount_in)

        if "UNISWAP_V3" in venue or venue in ("STOCK_PAIR", "UNISWAP_V3_STOCK"):
            if quote_asset.lower() == usdg.lower():
                return (self._quote_v3(token, weth, amount_in, fee, via=usdg) if selling
                        else self._quote_v3(weth, token, amount_in, fee, via=usdg))
            return (self._quote_v3(token, weth, amount_in, fee) if selling
                    else self._quote_v3(weth, token, amount_in, fee))

        if "V4" in venue:
            v4_fee, v4_tick, v4_hook = self._v4_params_for(token, quote_asset, fee)
            resolved_q = self.v4_resolved_quote(token, quote_asset)
            if resolved_q.lower() in (weth.lower(), ZERO.lower()):
                other, _wrap = self._resolve_v4_currency_in(token, v4_fee, v4_tick, v4_hook)
            else:
                other = cs(resolved_q)
            return (self._quote_v4(token, other, amount_in, v4_fee, v4_tick, v4_hook) if selling
                    else self._quote_v4(other, token, amount_in, v4_fee, v4_tick, v4_hook))

        return None

    async def min_out_for(self, venue: str, token_address: str, quote_asset: str, fee: int,
                          amount_in: int, target_addr: str, slippage_pct: float,
                          selling: bool = False) -> Optional[int]:
        """Quote the route and floor it by slippage. None means 'do not trade'."""
        expected = await asyncio.to_thread(
            self._quote_route, venue, token_address, quote_asset, fee,
            amount_in, target_addr, selling
        )
        if not expected or expected <= 0:
            return None
        floor = self._apply_slippage(expected, slippage_pct)
        logger.info(
            f"{'SELL' if selling else 'BUY'} quote {venue} {token_address}: expected "
            f"{expected}, min_out {floor} at {slippage_pct}% slippage"
        )
        return floor

    # ─── Fill measurement ────────────────────────────────────────────────
    #
    # Several buy paths used to report an invented token count -- a hardcoded 1.0,
    # or eth_amount * 1000. main.py:105 divides the stake by that number to get the
    # entry price, so a fabricated count produced an entry price wrong by orders of
    # magnitude, and position_monitor then stop-lossed the position within seconds.
    # Always measure the balance delta instead.

    async def _safe_token_balance(self, token_address: str) -> Optional[float]:
        """
        Balance read that never raises. Used around a broadcast, where an exception
        would abandon a position the wallet has already paid for.
        """
        for attempt in range(3):
            try:
                return await self.chain.get_token_balance(token_address)
            except (ExecutionUncertain, PreflightFailure):
                raise
            except Exception as e:
                logger.warning(f"Balance read failed for {token_address} ({attempt + 1}/3): {e}")
                await asyncio.sleep(0.5 * (attempt + 1))
        return None

    async def _measure_fill(self, token_address: str, bal_before: Optional[float]) -> float:
        """
        Tokens actually delivered by a buy.

        The ETH is already spent by the time this runs, so it retries rather than
        giving up: main.py:99 discards any position reporting <= 0 tokens, which
        would leave a funded bag on-chain with no exit ladder and no stop loss.
        """
        after = await self._safe_token_balance(token_address)
        if after is None:
            if getattr(self.chain, "execution_guard", None):
                raise ExecutionUncertain("post-buy token balance unreadable")
            logger.critical(
                f"Bought {token_address} but the balance is unreadable -- the position "
                f"cannot be sized or monitored. Check this wallet manually."
            )
            return 0.0
        if bal_before is None:
            guard = getattr(self.chain, "execution_guard", None)
            if guard and guard.active:
                return max(0.0, float(after) - guard.active["before"])
            # Starting balance unknown. Size from the full balance: the strategy
            # engine already refuses a second position in the same token
            # (strategy_engine.py:86), so a pre-existing balance is unlikely, and an
            # unmonitored funded position is the worse failure of the two.
            logger.critical(
                f"Pre-trade balance for {token_address} was unreadable; sizing from the "
                f"full balance {after}. Verify manually."
            )
            return float(after)
        delta = max(0.0, float(after) - float(bal_before))
        guard = getattr(self.chain, "execution_guard", None)
        if delta <= 0 and guard and guard.active and guard.active["transactions"]:
            raise ExecutionUncertain("submitted buy has no measured token delta")
        return delta

    def usdg_leg_min_out(self, token_address: str, fee: int, tick: int, hook: str,
                         slippage_pct: float):
        """
        Callback for buy_usdg_two_tx: leg 2's input is only known after leg 1
        settles, so quote it then. Returns 0 when unquotable, which makes the
        caller abort rather than swap unprotected.
        """
        def _fn(usdg_amount: int) -> int:
            expected = self._quote_v4(self.usdg_address, token_address, usdg_amount, fee, tick, hook)
            return self._apply_slippage(expected, slippage_pct) if expected else 0
        return _fn

    async def _resolve_v3_pool(self, token_address: str, quote_asset: str, fee: int) -> Optional[str]:
        """
        Find the real V3 pool for token/quote by asking the factory.

        Do NOT trust the `target` that detect_venue_and_route returns for pricing.
        The on-chain branch puts the pool address there (check_v3_pool), but the
        DexScreener branch puts SwapRouter02 there for every V3 venue
        (stock_v4_routes.py:256-261). Pricing that trusts `target` ends up calling
        slot0() on the router, which reverts on every poll -- get_token_price_eth
        then falls through to 0.0, and position_monitor.py:78-80 skips the tick
        entirely, so TP/SL never fire for that position.

        Mirrors what the UNISWAP_V2 pricing branch already does correctly.
        """
        token_cs = self.w3.to_checksum_address(token_address)
        quote_cs = self.w3.to_checksum_address(quote_asset)
        cached = self._v3_pool_cache.get((token_cs, quote_cs))
        if cached:
            return cached

        tried = []
        for candidate in ([int(fee)] if fee else []) + [3000, 10000, 500, 100]:
            if candidate in tried:
                continue
            tried.append(candidate)
            try:
                pool = await asyncio.to_thread(
                    self.v3_factory.functions.getPool(quote_cs, token_cs, candidate).call
                )
            except (ExecutionUncertain, PreflightFailure):
                raise
            except Exception:
                continue
            if pool and int(pool, 16) != 0:
                self._v3_pool_cache[(token_cs, quote_cs)] = pool
                return pool
        return None

    async def get_token_price_eth(self, token_address: str) -> float:
        """
        Calculates the current live DEX price of 1 token in ETH across all supported venues.
        Supports:
        - LAUNCHPAD_CURVE_ETH & LAUNCHPAD_CURVE_USDG
        - UNISWAP_V3_WETH & UNISWAP_V3_USDG
        - UNISWAP_V4 (Native, WETH, USDG, and Stock quote pools)
        - STOCK_PAIR & UNISWAP_V3_STOCK
        - UNISWAP_V2_WETH & UNISWAP_V2_USDG
        - Fallback: Non-blocking DexScreener lookup
        """
        await self.verify_chain_id()
        token_cs = self.w3.to_checksum_address(token_address)
        try:
            venue, target_addr, quote_asset, fee = await self.detect_venue_and_route(token_cs)

            if venue == "LAUNCHPAD_CURVE_ETH":
                res = await asyncio.to_thread(self.w3.eth.call, {'to': target_addr, 'data': '0x0902f1ac'})
                if res and len(res) >= 64:
                    r0 = int(res.hex()[:64], 16)
                    r1 = int(res.hex()[64:], 16)
                    if r1 > 0:
                        return (r0 / 1e18) / (r1 / 1e18)

            elif venue == "LAUNCHPAD_CURVE_USDG":
                res = await asyncio.to_thread(self.w3.eth.call, {'to': target_addr, 'data': '0x0902f1ac'})
                if res and len(res) >= 64:
                    r0 = int(res.hex()[:64], 16)
                    r1 = int(res.hex()[64:], 16)
                    if r1 > 0:
                        price_usdg = (r0 / 1e6) / (r1 / 1e18)
                        eth_price_usd = await self.chain.get_eth_price_usd()
                        return price_usdg / eth_price_usd if eth_price_usd > 0 else 0.0

            elif venue == "UNISWAP_V3_WETH":
                pool_addr = await self._resolve_v3_pool(token_cs, quote_asset, fee)
                if not pool_addr:
                    raise ValueError(f"no V3 pool for {token_cs}/{quote_asset}")
                pool_contract = self.w3.eth.contract(address=pool_addr, abi=[
                    {'inputs': [], 'name': 'slot0', 'outputs': [{'name': 'sqrtPriceX96', 'type': 'uint160'}, {'name': 'tick', 'type': 'int24'}], 'stateMutability': 'view', 'type': 'function'},
                    {'inputs': [], 'name': 'token0', 'outputs': [{'name': '', 'type': 'address'}], 'stateMutability': 'view', 'type': 'function'},
                    {'inputs': [], 'name': 'token1', 'outputs': [{'name': '', 'type': 'address'}], 'stateMutability': 'view', 'type': 'function'}
                ])
                s0 = await asyncio.to_thread(pool_contract.functions.slot0().call)
                t0 = await asyncio.to_thread(pool_contract.functions.token0().call)
                token_contract = self.w3.eth.contract(address=token_cs, abi=ERC20_ABI)
                try:
                    dec = await self.chain._retry(token_contract.functions.decimals().call)
                except (ExecutionUncertain, PreflightFailure):
                    raise
                except Exception:
                    dec = 18

                sqrtPrice = s0[0]
                if sqrtPrice > 0:
                    raw_price = (sqrtPrice / (2**96)) ** 2
                    if t0.lower() == self.weth_address.lower():
                        price_in_eth = (1.0 / raw_price) * (10**dec / 1e18) if raw_price > 0 else 0.0
                    else:
                        price_in_eth = raw_price * (10**dec / 1e18)
                    return price_in_eth

            elif venue == "UNISWAP_V3_USDG":
                pool_addr = await self._resolve_v3_pool(token_cs, quote_asset, fee)
                if not pool_addr:
                    raise ValueError(f"no V3 pool for {token_cs}/{quote_asset}")
                pool_contract = self.w3.eth.contract(address=pool_addr, abi=[
                    {'inputs': [], 'name': 'slot0', 'outputs': [{'name': 'sqrtPriceX96', 'type': 'uint160'}, {'name': 'tick', 'type': 'int24'}], 'stateMutability': 'view', 'type': 'function'},
                    {'inputs': [], 'name': 'token0', 'outputs': [{'name': '', 'type': 'address'}], 'stateMutability': 'view', 'type': 'function'},
                    {'inputs': [], 'name': 'token1', 'outputs': [{'name': '', 'type': 'address'}], 'stateMutability': 'view', 'type': 'function'}
                ])
                s0 = await asyncio.to_thread(pool_contract.functions.slot0().call)
                t0 = await asyncio.to_thread(pool_contract.functions.token0().call)
                token_contract = self.w3.eth.contract(address=token_cs, abi=ERC20_ABI)
                try:
                    dec = await self.chain._retry(token_contract.functions.decimals().call)
                except (ExecutionUncertain, PreflightFailure):
                    raise
                except Exception:
                    dec = 18

                sqrtPrice = s0[0]
                if sqrtPrice > 0:
                    raw_price = (sqrtPrice / (2**96)) ** 2
                    if t0.lower() == self.usdg_address.lower():
                        price_usdg = (1.0 / raw_price) * (10**dec / 1e6) if raw_price > 0 else 0.0
                    else:
                        price_usdg = raw_price * (10**dec / 1e6)
                    eth_price_usd = await self.chain.get_eth_price_usd()
                    return price_usdg / eth_price_usd if eth_price_usd > 0 else 0.0

            elif venue == "UNISWAP_V4":
                # Same resolved key the buy and sell paths use. Guessing here meant
                # a wrong key produced sqrtPrice=0, price 0.0, and a skipped tick.
                fee_val, tick_val, hook_val = await asyncio.to_thread(
                    self._v4_params_for, token_cs, quote_asset, fee)
                # Use the quote that actually verified, not the one detection
                # reported — they differ whenever a native pool was reported as WETH.
                q_cs = self.w3.to_checksum_address(
                    self._v4_pool_params_cache.get(token_cs, {}).get("quote") or quote_asset
                )

                if int(token_cs, 16) < int(q_cs, 16):
                    c0, c1 = token_cs, q_cs
                else:
                    c0, c1 = q_cs, token_cs

                pid = pool_id_from_key(c0, c1, fee_val, tick_val, hook_val)
                s0 = await asyncio.to_thread(self.state_view.functions.getSlot0(pid).call)
                sqrtPrice = s0[0]
                if sqrtPrice > 0:
                    raw_price = (sqrtPrice / (2**96)) ** 2
                    token_contract = self.w3.eth.contract(address=token_cs, abi=ERC20_ABI)
                    try:
                        dec = await self.chain._retry(token_contract.functions.decimals().call)
                    except (ExecutionUncertain, PreflightFailure):
                        raise
                    except Exception:
                        dec = 18

                    q_dec = 6 if q_cs.lower() == self.usdg_address.lower() else 18

                    if token_cs.lower() == c0.lower():
                        price_in_quote = raw_price * (10**dec) / (10**q_dec)
                    else:
                        price_in_quote = (1.0 / raw_price) * (10**dec) / (10**q_dec) if raw_price > 0 else 0.0

                    if q_cs.lower() in (self.weth_address.lower(), ZERO.lower(), "0x0000000000000000000000000000000000000000"):
                        return price_in_quote
                    elif q_cs.lower() == self.usdg_address.lower():
                        eth_price_usd = await self.chain.get_eth_price_usd()
                        return price_in_quote / eth_price_usd if eth_price_usd > 0 else 0.0
                    elif True:  # any non-ETH/USDG V4 quote (HOOD/UPS/TTWO/...)
                        # Walk fee tiers instead of assuming 500 — a miss here used
                        # to silently fall out of the whole price function and return 0.
                        stock_pool = await self._resolve_v3_pool(q_cs, self.usdg_address, 500)
                        if stock_pool and stock_pool != ZERO:
                            sp_contract = self.w3.eth.contract(address=stock_pool, abi=[
                                {'inputs': [], 'name': 'slot0', 'outputs': [{'name': 'sqrtPriceX96', 'type': 'uint160'}, {'name': 'tick', 'type': 'int24'}], 'stateMutability': 'view', 'type': 'function'},
                                {'inputs': [], 'name': 'token0', 'outputs': [{'name': '', 'type': 'address'}], 'stateMutability': 'view', 'type': 'function'}
                            ])
                            s0_sp = await asyncio.to_thread(sp_contract.functions.slot0().call)
                            t0_sp = await asyncio.to_thread(sp_contract.functions.token0().call)
                            if s0_sp[0] > 0:
                                sp_raw = (s0_sp[0] / (2**96)) ** 2
                                stock_usd = (sp_raw * 1e12) if t0_sp.lower() == q_cs.lower() else ((1.0 / sp_raw) * 1e12 if sp_raw > 0 else 0.0)
                                price_usd = price_in_quote * stock_usd
                                eth_price_usd = await self.chain.get_eth_price_usd()
                                return price_usd / eth_price_usd if eth_price_usd > 0 else 0.0

            elif venue in ("STOCK_PAIR", "UNISWAP_V3_STOCK"):
                pool_addr = await self._resolve_v3_pool(token_cs, quote_asset, fee)
                if not pool_addr:
                    raise ValueError(f"no V3 stock pool for {token_cs}/{quote_asset}")
                pool_contract = self.w3.eth.contract(address=pool_addr, abi=[
                    {'inputs': [], 'name': 'slot0', 'outputs': [{'name': 'sqrtPriceX96', 'type': 'uint160'}, {'name': 'tick', 'type': 'int24'}], 'stateMutability': 'view', 'type': 'function'},
                    {'inputs': [], 'name': 'token0', 'outputs': [{'name': '', 'type': 'address'}], 'stateMutability': 'view', 'type': 'function'},
                    {'inputs': [], 'name': 'token1', 'outputs': [{'name': '', 'type': 'address'}], 'stateMutability': 'view', 'type': 'function'}
                ])
                s0 = await asyncio.to_thread(pool_contract.functions.slot0().call)
                t0 = await asyncio.to_thread(pool_contract.functions.token0().call)
                token_contract = self.w3.eth.contract(address=token_cs, abi=ERC20_ABI)
                try:
                    dec = await self.chain._retry(token_contract.functions.decimals().call)
                except (ExecutionUncertain, PreflightFailure):
                    raise
                except Exception:
                    dec = 18

                sqrtPrice = s0[0]
                if sqrtPrice > 0:
                    raw_price = (sqrtPrice / (2**96)) ** 2
                    if t0.lower() == quote_asset.lower():
                        price_in_stock = (1.0 / raw_price) * (10**dec / 1e18) if raw_price > 0 else 0.0
                    else:
                        price_in_stock = raw_price * (10**dec / 1e18)

                    stock_pool = await self._resolve_v3_pool(quote_asset, self.usdg_address, 500)
                    if stock_pool and stock_pool != ZERO:
                        sp_contract = self.w3.eth.contract(address=stock_pool, abi=[
                            {'inputs': [], 'name': 'slot0', 'outputs': [{'name': 'sqrtPriceX96', 'type': 'uint160'}, {'name': 'tick', 'type': 'int24'}], 'stateMutability': 'view', 'type': 'function'},
                            {'inputs': [], 'name': 'token0', 'outputs': [{'name': '', 'type': 'address'}], 'stateMutability': 'view', 'type': 'function'}
                        ])
                        s0_sp = await asyncio.to_thread(sp_contract.functions.slot0().call)
                        t0_sp = await asyncio.to_thread(sp_contract.functions.token0().call)
                        if s0_sp[0] > 0:
                            sp_raw = (s0_sp[0] / (2**96)) ** 2
                            stock_usd = (sp_raw * 1e12) if t0_sp.lower() == quote_asset.lower() else ((1.0 / sp_raw) * 1e12 if sp_raw > 0 else 0.0)
                            price_usd = price_in_stock * stock_usd
                            eth_price_usd = await self.chain.get_eth_price_usd()
                            return price_usd / eth_price_usd if eth_price_usd > 0 else 0.0

            elif "UNISWAP_V2" in venue:
                pair_addr = await asyncio.to_thread(self.v2_factory.functions.getPair(token_cs, quote_asset).call)
                if pair_addr and pair_addr != ZERO:
                    pair_contract = self.w3.eth.contract(address=pair_addr, abi=[
                        {"inputs":[],"name":"getReserves","outputs":[{"type":"uint112"},{"type":"uint112"},{"type":"uint32"}],"stateMutability":"view","type":"function"},
                        {"inputs":[],"name":"token0","outputs":[{"type":"address"}],"stateMutability":"view","type":"function"}
                    ])
                    res = await asyncio.to_thread(pair_contract.functions.getReserves().call)
                    t0 = await asyncio.to_thread(pair_contract.functions.token0().call)
                    token_contract = self.w3.eth.contract(address=token_cs, abi=ERC20_ABI)
                    try:
                        dec = await self.chain._retry(token_contract.functions.decimals().call)
                    except (ExecutionUncertain, PreflightFailure):
                        raise
                    except Exception:
                        dec = 18
                    if t0.lower() == token_cs.lower():
                        r_token, r_quote = res[0], res[1]
                    else:
                        r_quote, r_token = res[0], res[1]
                    if r_token > 0:
                        if quote_asset.lower() == self.weth_address.lower():
                            return (r_quote / 1e18) / (r_token / (10**dec))
                        elif quote_asset.lower() == self.usdg_address.lower():
                            px_usdg = (r_quote / 1e6) / (r_token / (10**dec))
                            eth_price_usd = await self.chain.get_eth_price_usd()
                            return px_usdg / eth_price_usd if eth_price_usd > 0 else 0.0

        except (ExecutionUncertain, PreflightFailure):
            raise
        except Exception as e:
            logger.debug(f"Error fetching token price in ETH for {token_address}: {e}")

        # The DexScreener fallback is a guess, and a wrong guess here drives the
        # exit ladder. USDG came back at 0.5582 ETH -- $1403 for a $1 stablecoin.
        # Only trust it for a token we actually found a venue for; if there is no
        # tradeable route, a price is meaningless and 0.0 is the safe answer
        # (position_monitor skips the tick rather than acting on noise).
        try:
            venue_now, _t, _q, _f = await self.detect_venue_and_route(token_cs)
        except (ExecutionUncertain, PreflightFailure):
            raise
        except Exception:
            venue_now = "NONE"
        if venue_now == "NONE":
            logger.debug(f"No venue for {token_address}; not trusting a DexScreener price")
            return 0.0

        px = await asyncio.to_thread(get_price_eth_dexscreener, token_address)
        if px > 0:
            return px

        return 0.0

    async def estimate_tokens_for_eth(self, token_address: str, eth_amount: float) -> float:
        """
        Estimates the number of tokens received for a given ETH amount.
        """
        price_eth = await self.get_token_price_eth(token_address)
        if price_eth > 0:
            return eth_amount / price_eth
        return 0.0


# ─── Standalone Forced Simulation Helpers ─────────────────────────────────

def force_simulate_usdg_curve(token_address: str, quote_amount_usdg: float = 1.0, w3=None, recipient=None):
    """Force test Ungraduated USDG Curve buy"""
    print("=" * 70)
    print(f"FORCED SIMULATION: Ungraduated USDG Curve for {token_address}")
    print("=" * 70)

    if w3 is None:
        w3 = Web3(Web3.HTTPProvider('https://rpc.mainnet.chain.robinhood.com'))
    if recipient is None:
        recipient = "0x801F3F2822Af33A5A127aeEc4487882464AB4C31"

    token = w3.to_checksum_address(token_address)
    usdg = w3.to_checksum_address(USDG_ADDRESS)
    recipient = w3.to_checksum_address(recipient)

    quote_in = int(quote_amount_usdg * 10**6)
    min_out = 1

    usdg_contract = w3.eth.contract(address=usdg, abi=ERC20_ABI)
    balance = usdg_contract.functions.balanceOf(recipient).call()
    print(f"USDG Balance: {balance / 10**6}")

    curve_contract_abi = [
        {"inputs": [{"name": "quoteIn", "type": "uint256"}, {"name": "minTokensOut", "type": "uint256"}, {"name": "recipient", "type": "address"}], "name": "buy", "outputs": [{"name": "tokensReceived", "type": "uint256"}], "stateMutability": "payable", "type": "function"}
    ]

    try:
        # Example or placeholder curve address
        curve_addr = "0x1DB4E3fE3f941e4c19Cf87488dC653D8E9D6Dae4"
        curve_contract = w3.eth.contract(address=w3.to_checksum_address(curve_addr), abi=curve_contract_abi)
        allowance = usdg_contract.functions.allowance(recipient, curve_addr).call()
        print(f"Current Allowance: {allowance / 10**6}")

        tx = curve_contract.functions.buy(
            quote_in,
            min_out,
            recipient
        ).build_transaction({
            "from": recipient,
            "value": 0,
            "gas": 300000,
            "nonce": w3.eth.get_transaction_count(recipient),
            "chainId": 4663
        })

        gas_estimate = w3.eth.estimate_gas(tx)
        print(f"SIMULATION OK | Venue: LAUNCHPAD_CURVE_USDG | To: {curve_addr} | gas: {gas_estimate}")
        return True

    except (ExecutionUncertain, PreflightFailure):
        raise
    except Exception as e:
        print(f"SIMULATION REVERT | Venue: LAUNCHPAD_CURVE_USDG")
        print(f"Revert reason: {e}")
        return False


def force_simulate_v4_weth(token_address: str, eth_amount: float = 0.0002, w3=None, recipient=None, fee: int = 3000, hook: str = "0x0000000000000000000000000000000000000000"):
    print("=" * 70)
    print(f"FORCED SIMULATION: Graduated V4 WETH for {token_address}")
    print("=" * 70)

    if w3 is None:
        w3 = Web3(Web3.HTTPProvider('https://rpc.mainnet.chain.robinhood.com'))
    if recipient is None:
        recipient = "0x801F3F2822Af33A5A127aeEc4487882464AB4C31"

    token = w3.to_checksum_address(token_address)
    amount_in = w3.to_wei(eth_amount, 'ether')
    recipient = w3.to_checksum_address(recipient)
    min_out = 1
    weth = w3.to_checksum_address(WETH_ADDRESS)
    router_addr = w3.to_checksum_address(UNIVERSAL_ROUTER_ADDR)

    try:
        commands = bytes([0x0b, 0x10])  # WRAP_ETH + V4_SWAP

        wrap_input = eth_abi.encode(
            ['address', 'uint256'],
            [router_addr, amount_in]
        )

        v4_input = make_v4_swap_input(
            token_in=weth,
            token_out=token,
            amount_in=amount_in,
            min_out=min_out,
            recipient=recipient,
            fee=fee,
            tick_spacing=60,
            hook_addr=hook
        )

        inputs = [wrap_input, v4_input]
        router = w3.eth.contract(address=router_addr, abi=UNIVERSAL_ROUTER_ABI)

        tx = router.functions.execute(
            commands,
            inputs,
            int(time.time()) + 600
        ).build_transaction({
            "from": recipient,
            "value": amount_in,
            "gas": 500000,
            "nonce": w3.eth.get_transaction_count(recipient),
            "chainId": 4663
        })

        gas_estimate = w3.eth.estimate_gas(tx)
        print(f"SIMULATION OK | Venue: UNISWAP_V4_WETH | gas: {gas_estimate}")
        return True

    except (ExecutionUncertain, PreflightFailure):
        raise
    except Exception as e:
        print(f"SIMULATION REVERT | Venue: UNISWAP_V4_WETH")
        print(f"Revert: {e}")
        return False


def force_simulate_v3_usdg(token_address: str, eth_amount: float = 0.0002, w3=None, recipient=None, fee1: int = 500, fee2: int = 3000):
    print("=" * 70)
    print(f"FORCED SIMULATION: Graduated V3 USDG for {token_address}")
    print("=" * 70)

    if w3 is None:
        w3 = Web3(Web3.HTTPProvider('https://rpc.mainnet.chain.robinhood.com'))
    if recipient is None:
        recipient = "0x801F3F2822Af33A5A127aeEc4487882464AB4C31"

    token = w3.to_checksum_address(token_address)
    amount_in = w3.to_wei(eth_amount, 'ether')
    recipient = w3.to_checksum_address(recipient)
    min_out = 1
    weth = w3.to_checksum_address(WETH_ADDRESS)
    usdg = w3.to_checksum_address(USDG_ADDRESS)
    router_addr = w3.to_checksum_address(UNIVERSAL_ROUTER_ADDR)

    try:
        # SwapRouter02 exactInput multi-hop (WETH -> USDG -> Token)
        router02_addr = w3.to_checksum_address(UNISWAP_V3_SWAPROUTER02)
        router02 = w3.eth.contract(address=router02_addr, abi=SWAP_ROUTER02_ABI)

        path_bytes = (
            bytes.fromhex(weth[2:]) +
            fee1.to_bytes(3, 'big') +
            bytes.fromhex(usdg[2:]) +
            fee2.to_bytes(3, 'big') +
            bytes.fromhex(token[2:])
        )

        calldata = router02.functions.exactInput((path_bytes, recipient, amount_in, min_out))._encode_transaction_data()
        tx = {
            'from': recipient,
            'to': router02_addr,
            'value': amount_in,
            'data': calldata,
            'chainId': 4663
        }

        gas_estimate = w3.eth.estimate_gas(tx)
        print(f"SIMULATION OK | Venue: UNISWAP_V3_USDG | gas: {gas_estimate}")
        return True

    except (ExecutionUncertain, PreflightFailure):
        raise
    except Exception as e:
        print(f"SIMULATION REVERT | Venue: UNISWAP_V3_USDG")
        print(f"Revert: {e}")
        return False


def force_simulate_v4_usdg_2hop(token_address: str, eth_amount: float = 0.0002, w3=None, recipient=None, fee2: int = 3000, hook2: str = PONS_V2_HOOK):
    print("=" * 70)
    print(f"FORCED SIMULATION: Graduated V4 USDG 2-HOP for {token_address}")
    print("=" * 70)

    if w3 is None:
        w3 = Web3(Web3.HTTPProvider('https://rpc.mainnet.chain.robinhood.com'))
    if recipient is None:
        recipient = "0x801F3F2822Af33A5A127aeEc4487882464AB4C31"

    token = w3.to_checksum_address(token_address)
    amount_in = w3.to_wei(eth_amount, 'ether')
    recipient = w3.to_checksum_address(recipient)
    min_out = 1
    weth = w3.to_checksum_address(WETH_ADDRESS)
    usdg = w3.to_checksum_address(USDG_ADDRESS)
    router_addr = w3.to_checksum_address(UNIVERSAL_ROUTER_ADDR)

    try:
        commands = bytes([0x0b, 0x10, 0x10])  # WRAP_ETH + V4 + V4

        wrap_input = eth_abi.encode(
            ['address', 'uint256'],
            [router_addr, amount_in]
        )

        # 2. WETH -> USDG
        hop1 = make_v4_swap_input(
            token_in=weth,
            token_out=usdg,
            amount_in=amount_in,
            min_out=1,
            recipient=router_addr,
            fee=500,
            tick_spacing=10,
            hook_addr="0x0000000000000000000000000000000000000000"
        )

        # 3. USDG -> Token
        hop2 = make_v4_swap_input(
            token_in=usdg,
            token_out=token,
            amount_in=1,
            min_out=min_out,
            recipient=recipient,
            fee=fee2,
            tick_spacing=60,
            hook_addr=hook2
        )

        inputs = [wrap_input, hop1, hop2]
        router = w3.eth.contract(address=router_addr, abi=UNIVERSAL_ROUTER_ABI)

        tx = router.functions.execute(
            commands,
            inputs,
            int(time.time()) + 600
        ).build_transaction({
            "from": recipient,
            "value": amount_in,
            "gas": 700000,
            "nonce": w3.eth.get_transaction_count(recipient),
            "chainId": 4663
        })

        gas_estimate = w3.eth.estimate_gas(tx)
        print(f"SIMULATION OK | Venue: UNISWAP_V4_USDG_2HOP | gas: {gas_estimate}")
        return True

    except (ExecutionUncertain, PreflightFailure):
        raise
    except Exception as e:
        print(f"SIMULATION REVERT | Venue: UNISWAP_V4_USDG_2HOP")
        print(f"Revert: {e}")
        return False


# ─── Standalone Sell Functions & Simulator ────────────────────────────────

def get_default_w3_and_account():
    from config import Config
    from chain_client import ChainClient, eip1559_fees
    cfg = Config()
    chain = ChainClient(cfg)
    return chain.w3, chain.account


def sell_eth_curve(token: str, amount_tokens: float, min_out: int = 1, w3=None, account=None):
    if w3 is None or account is None:
        w3, account = get_default_w3_and_account()
    token = w3.to_checksum_address(token)

    curve_addr = None
    if token.lower() == "0x928233827A025193A553DC805ADE16174Be7589f".lower():
        curve_addr = "0x35fe208A3F5400016E3E9C409bb9752cB596D2Dd"
    else:
        from config import Config
        from chain_client import ChainClient
        cfg = Config()
        trader = DexTrader(ChainClient(cfg), cfg)
        c, _, _ = asyncio.run(trader.resolve_token_curve(token))
        curve_addr = c

    if not curve_addr:
        print("Not a valid ETH curve")
        return None

    curve_addr = w3.to_checksum_address(curve_addr)
    token_contract = w3.eth.contract(address=token, abi=ERC20_ABI)
    try:
        decimals = token_contract.functions.decimals().call()
    except (ExecutionUncertain, PreflightFailure):
        raise
    except Exception:
        decimals = 18
    amount_in = int(amount_tokens * (10**decimals))

    allowance = token_contract.functions.allowance(account.address, curve_addr).call()
    if allowance < amount_in:
        approve_tx = token_contract.functions.approve(curve_addr, 2**256 - 1).build_transaction({
            "from": account.address,
            "nonce": w3.eth.get_transaction_count(account.address),
            "gas": 80000,
            "chainId": 4663
        })
        raise RuntimeError("Approval required; standalone simulation helpers cannot broadcast. Use the guarded trader.")

    curve = w3.eth.contract(address=curve_addr, abi=PONS_CURVE_ABI)
    tx = curve.functions.sell(
        amount_in,
        min_out,
        account.address
    ).build_transaction({
        "from": account.address,
        "value": 0,
        "gas": 300000,
        "nonce": w3.eth.get_transaction_count(account.address),
        "chainId": 4663
    })
    return tx


def sell_usdg_curve(token: str, amount_tokens: float, min_out: int = 1, w3=None, account=None):
    if w3 is None or account is None:
        w3, account = get_default_w3_and_account()
    token = w3.to_checksum_address(token)

    curve_addr = None
    if token.lower() == "0x77f06AEE8aB51cF6347fA599D13cB6B97C53d4B6".lower():
        curve_addr = "0x1DB4E3fE3f941e4c19Cf87488dC653D8E9D6Dae4"
    else:
        from config import Config
        from chain_client import ChainClient
        cfg = Config()
        trader = DexTrader(ChainClient(cfg), cfg)
        c, _, _ = asyncio.run(trader.resolve_token_curve(token))
        curve_addr = c

    if not curve_addr:
        print("Not a valid USDG curve")
        return None

    curve_addr = w3.to_checksum_address(curve_addr)
    token_contract = w3.eth.contract(address=token, abi=ERC20_ABI)
    try:
        decimals = token_contract.functions.decimals().call()
    except (ExecutionUncertain, PreflightFailure):
        raise
    except Exception:
        decimals = 18
    amount_in = int(amount_tokens * (10**decimals))

    allowance = token_contract.functions.allowance(account.address, curve_addr).call()
    if allowance < amount_in:
        approve_tx = token_contract.functions.approve(curve_addr, 2**256 - 1).build_transaction({
            "from": account.address,
            "nonce": w3.eth.get_transaction_count(account.address),
            "gas": 80000,
            "chainId": 4663
        })
        raise RuntimeError("Approval required; standalone simulation helpers cannot broadcast. Use the guarded trader.")

    curve = w3.eth.contract(address=curve_addr, abi=PONS_CURVE_ABI)
    tx = curve.functions.sell(
        amount_in,
        min_out,
        account.address
    ).build_transaction({
        "from": account.address,
        "value": 0,
        "gas": 300000,
        "nonce": w3.eth.get_transaction_count(account.address),
        "chainId": 4663
    })
    return tx


def sell_v3_weth(token: str, amount_tokens: float, min_out: int = 1, w3=None, account=None, fee: int = 10000):
    if w3 is None or account is None:
        w3, account = get_default_w3_and_account()
    token = w3.to_checksum_address(token)
    token_contract = w3.eth.contract(address=token, abi=ERC20_ABI)
    try:
        decimals = token_contract.functions.decimals().call()
    except (ExecutionUncertain, PreflightFailure):
        raise
    except Exception:
        decimals = 18
    amount_in = int(amount_tokens * (10**decimals))

    router = w3.to_checksum_address(UNISWAP_V3_SWAPROUTER02)
    allowance = token_contract.functions.allowance(account.address, router).call()
    if allowance < amount_in:
        approve_tx = token_contract.functions.approve(router, 2**256 - 1).build_transaction({
            "from": account.address,
            "nonce": w3.eth.get_transaction_count(account.address),
            "gas": 80000,
            "chainId": 4663
        })
        raise RuntimeError("Approval required; standalone simulation helpers cannot broadcast. Use the guarded trader.")

    router_contract = w3.eth.contract(address=router, abi=SWAP_ROUTER02_ABI)
    params = (token, w3.to_checksum_address(WETH_ADDRESS), fee, account.address, amount_in, min_out, 0)
    calldata = router_contract.functions.exactInputSingle(params)._encode_transaction_data()
    tx = {
        "from": account.address,
        "to": router,
        "value": 0,
        "data": calldata,
        "gas": 350000,
        "nonce": w3.eth.get_transaction_count(account.address),
        "chainId": 4663
    }
    return tx


def sell_v3_usdg(token: str, amount_tokens: float, min_out: int = 1, w3=None, account=None, fee1: int = 3000, fee2: int = 500):
    if w3 is None or account is None:
        w3, account = get_default_w3_and_account()
    token = w3.to_checksum_address(token)
    token_contract = w3.eth.contract(address=token, abi=ERC20_ABI)
    try:
        decimals = token_contract.functions.decimals().call()
    except (ExecutionUncertain, PreflightFailure):
        raise
    except Exception:
        decimals = 18
    amount_in = int(amount_tokens * (10**decimals))

    router = w3.to_checksum_address(UNISWAP_V3_SWAPROUTER02)
    allowance = token_contract.functions.allowance(account.address, router).call()
    if allowance < amount_in:
        approve_tx = token_contract.functions.approve(router, 2**256 - 1).build_transaction({
            "from": account.address,
            "nonce": w3.eth.get_transaction_count(account.address),
            "gas": 80000,
            "chainId": 4663
        })
        raise RuntimeError("Approval required; standalone simulation helpers cannot broadcast. Use the guarded trader.")

    # Path: Token -> (fee1) -> USDG -> (fee2) -> WETH
    path_bytes = (
        bytes.fromhex(token[2:]) +
        fee1.to_bytes(3, 'big') +
        bytes.fromhex(USDG_ADDRESS[2:]) +
        fee2.to_bytes(3, 'big') +
        bytes.fromhex(WETH_ADDRESS[2:])
    )

    router_contract = w3.eth.contract(address=router, abi=SWAP_ROUTER02_ABI)
    calldata = router_contract.functions.exactInput((path_bytes, account.address, amount_in, min_out))._encode_transaction_data()
    tx = {
        "from": account.address,
        "to": router,
        "value": 0,
        "data": calldata,
        "gas": 450000,
        "nonce": w3.eth.get_transaction_count(account.address),
        "chainId": 4663
    }
    return tx


def sell_token(token_address: str, amount_tokens: float, min_out: int = 1, w3=None, account=None):
    """Main sell function - automatically chooses the correct route"""
    if w3 is None or account is None:
        w3, account = get_default_w3_and_account()
    token = w3.to_checksum_address(token_address)

    from config import Config
    from chain_client import ChainClient
    cfg = Config()
    chain = ChainClient(cfg)
    if account:
        chain.account = account
    trader = DexTrader(chain, cfg)

    try:
        tx_hash, tokens_sold = asyncio.run(trader.sell_token(token, amount_tokens))
        if tx_hash:
            return tx_hash
        return None
    except (ExecutionUncertain, PreflightFailure):
        raise
    except Exception as e:
        print(f"Sell execution failed: {e}")
        return None


def force_simulate_sell(token_address: str, amount_tokens: float = 1000, w3=None, account=None):
    print("=" * 70)
    print(f"FORCED SELL SIMULATION for {token_address}")
    print("=" * 70)
    if w3 is None or account is None:
        w3, account = get_default_w3_and_account()
    try:
        tx = sell_token(token_address, amount_tokens, min_out=1, w3=w3, account=account)
        if tx is None:
            return False
        gas_estimate = w3.eth.estimate_gas(tx)
        print(f"SIMULATION OK | Sell gas: {gas_estimate}")
        return True
    except (ExecutionUncertain, PreflightFailure):
        raise
    except Exception as e:
        print(f"SIMULATION REVERT | {e}")
        return False

