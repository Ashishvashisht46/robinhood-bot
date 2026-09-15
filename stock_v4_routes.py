from execution_guard import ExecutionUncertain, PreflightFailure
# stock_v4_routes.py
# Drop-in routes for Robinhood Chain 4663
# Covers: V3 stock pairs + V4 Native/WETH/USDG/stock pairs + V2 fallback + DexScreener Fallback
# Uses SwapRouter02 for V3
# Uses Universal Router for V4
# Uses V2 Router for V2

import logging
import time
import requests
import eth_abi
import concurrent.futures
from eth_utils import keccak, to_checksum_address
from web3 import Web3
from chain_client import eip1559_fees

# This module used logger.warning() at the stranded-stock guard without ever
# defining it, so the one branch that guard exists for raised NameError instead.
logger = logging.getLogger("copytrader")

CHAIN_ID = 4663
ZERO = "0x0000000000000000000000000000000000000000"
NATIVE = "0x0000000000000000000000000000000000000000"
WETH = to_checksum_address("0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73")
USDG = to_checksum_address("0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168")
UNIVERSAL_ROUTER = to_checksum_address("0x8876789976dEcBfCbBbe364623C63652db8C0904")
SWAP_ROUTER_02 = to_checksum_address("0xCaf681a66D020601342297493863E78C959E5cb2")
V3_FACTORY = to_checksum_address("0x1f7d7550B1b028f7571E69A784071F0205FD2EfA")
V4_STATE_VIEW = to_checksum_address("0xF3334192D15450CdD385c8B70e03F9A6bD9E673b")
V2_FACTORY = to_checksum_address("0x8bcEaA40B9AcdfAedF85AdF4FF01F5Ad6517937f")
V2_ROUTER = to_checksum_address("0x89e5DB8B5aA49aA85AC63f691524311AEB649eba")

PONS_HOOK = to_checksum_address("0xe5e702641ea86f4ae6cc3cdaed2b886f976be044")
BAGS_HOOK = to_checksum_address("0x2380aBf72C17aABAb76480244759AC7E2932EEcC")

DEXSCREENER = "https://api.dexscreener.com/latest/dex/tokens/"

STOCK_TOKENS = {
    "TTWO": "0x5e81213613b6b86eab4c6c50d718d34359459786",
    "AAPL": "0xaF3D76f1834A1d425780943C99Ea8A608f8a93f9",
    "NVDA": "0xd0601CE157Db5bdC3162BbaC2a2C8aF5320D9EEC",
    "TSLA": "0x322F0929c4625eD5bAd873c95208D54E1c003b2d",
    "SPY":  "0x117cc2133c37B721F49dE2A7a74833232B3B4C0C",
    "QQQ":  "0xD5f3879160bc7c32ebb4dC785F8a4F505888de68",
    "MSFT": "0xe93237C50D904957Cf27E7B1133b510C669c2e74",
    "AMZN": "0x12f190a9F9d7D37a250758b26824B97CE941bF54",
    "META": "0xc0D6457C16Cc70d6790Dd43521C899C87ce02f35",
    "GOOGL": "0x2e0847E8910a9732eB3fb1bb4b70a580ADAD4FE3",
    "COIN": "0x6330D8C3178a418788dF01a47479c0ce7CCF450b",
    "AMD":  "0x86923f96303D656E4aa86D9d42D1e57ad2023fdC",
    "NFLX": "0xE0444EF8BF4eD74f74FD73686e2ddF4C1c5591E8",
    "PLTR": "0x894E1EC2D74FFE5AEF8Dc8A9e84686acCB964F2A",
    "MSTR": "0xec262a75e413fAfD0dF80480274532C79D42da09",
    "HOOD": "0x4a0E65A3EcceC6dBe60AE065F2e7bb85Fae35eEa",
    "SPCX": "0x4a0E65A3EcceC6dBe60AE065F2e7bb85Fae35eEa",
    "UPS":  "0xf23250dac154D05Bb671CB0d0eBEf3c635c79CE2",
}
STOCK_LIST = [to_checksum_address(a) for a in STOCK_TOKENS.values()]
QUOTE_TOKENS = [NATIVE, WETH, USDG] + STOCK_LIST

STOCK_SKIP = {a.lower() for a in STOCK_LIST}

V3_FEES = [100, 500, 3000, 10000]
V4_FEE_TICKS = [
    (0, 200), (0, 60), (0, 1),  # Pons / Bags graduated V4
    (100, 1), (500, 10), (2500, 25), (3000, 60), (10000, 200),
    (50, 1), (50000, 5), (900000, 90), (950000, 95),
]
V4_FEE_TICKS_EXTENDED = V4_FEE_TICKS + [
    (0, 200), (0, 60), (0, 1), (802731, 9303), (833690, 200), (43000, 430),
]
V4_HOOKS = [
    to_checksum_address(ZERO),
    PONS_HOOK,
    BAGS_HOOK,
]

V3_FACTORY_ABI = [{
    "inputs": [
        {"name": "tokenA", "type": "address"},
        {"name": "tokenB", "type": "address"},
        {"name": "fee", "type": "uint24"},
    ],
    "name": "getPool",
    "outputs": [{"type": "address"}],
    "stateMutability": "view",
    "type": "function",
}]
V3_POOL_ABI = [{
    "inputs": [],
    "name": "liquidity",
    "outputs": [{"type": "uint128"}],
    "stateMutability": "view",
    "type": "function",
}]
STATE_VIEW_ABI = [{
    "inputs": [{"name": "poolId", "type": "bytes32"}],
    "name": "getLiquidity",
    "outputs": [{"type": "uint128"}],
    "stateMutability": "view",
    "type": "function",
}]
SWAP_ROUTER_02_ABI = [{
    "inputs": [{
        "components": [
            {"name": "path", "type": "bytes"},
            {"name": "recipient", "type": "address"},
            {"name": "amountIn", "type": "uint256"},
            {"name": "amountOutMinimum", "type": "uint256"},
        ],
        "name": "params",
        "type": "tuple",
    }],
    "name": "exactInput",
    "outputs": [{"type": "uint256"}],
    "stateMutability": "payable",
    "type": "function",
}]
UNIVERSAL_ROUTER_ABI = [{
    "inputs": [
        {"name": "commands", "type": "bytes"},
        {"name": "inputs", "type": "bytes[]"},
        {"name": "deadline", "type": "uint256"},
    ],
    "name": "execute",
    "outputs": [],
    "stateMutability": "payable",
    "type": "function",
}]
V2_FACTORY_ABI = [{
    "inputs": [
        {"name": "a", "type": "address"},
        {"name": "b", "type": "address"},
    ],
    "name": "getPair",
    "outputs": [{"type": "address"}],
    "stateMutability": "view",
    "type": "function",
}]
V2_PAIR_ABI = [{
    "inputs": [],
    "name": "getReserves",
    "outputs": [{"type": "uint112"}, {"type": "uint112"}, {"type": "uint32"}],
    "stateMutability": "view",
    "type": "function",
}]
V2_ROUTER_ABI = [{
    "inputs": [
        {"name": "amountOutMin", "type": "uint256"},
        {"name": "path", "type": "address[]"},
        {"name": "to", "type": "address"},
        {"name": "deadline", "type": "uint256"},
    ],
    "name": "swapExactETHForTokensSupportingFeeOnTransferTokens",
    "outputs": [],
    "stateMutability": "payable",
    "type": "function",
}]


def _cs(addr):
    return to_checksum_address(addr)


def fetch_dexscreener(token: str):
    token = token.lower()
    r = requests.get(DEXSCREENER + token, timeout=3, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    pairs = r.json().get("pairs") or []
    rh = []
    for p in pairs:
        chain = (p.get("chainId") or "").lower()
        if chain not in ("robinhood", "robinhoodchain"):
            continue
        base = (p.get("baseToken") or {}).get("address", "").lower()
        quote = (p.get("quoteToken") or {}).get("address", "").lower()
        if base != token and quote != token:
            continue
        liq = float(((p.get("liquidity") or {}).get("usd") or 0))
        rh.append((liq, p))
    if not rh:
        return None
    rh.sort(key=lambda x: x[0], reverse=True)
    return rh[0][1]


INIT_TOPIC = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"
_V4_PARAM_CACHE = {
    "0x3635abf0d047b848504e790e09bc3153f600e1b5a35cf5fb62216c61ad88129b": {
        "c0": NATIVE, "c1": "0x9A95957b506185a0F678928A6bE63B7D55Bf198E", "fee": 0, "tick": 200, "hook": PONS_HOOK
    },
    "0x4d95e27e10959737bb35e2266b248aad9fa42878c864d137a91f9e3454a57eca": {
        "c0": NATIVE, "c1": "0x5fdA317876385805f1fCA61Bd6e34FEF21Aa06F9", "fee": 802731, "tick": 9303, "hook": ZERO
    },
    "0xdef6136fb6a3758771fa4eff204763c12a0866fb1f587b60b954e2362a46f16b": {
        "c0": "0x5e81213613b6B86EaB4c6c50d718d34359459786", "c1": "0x5fdA317876385805f1fCA61Bd6e34FEF21Aa06F9", "fee": 0, "tick": 200, "hook": PONS_HOOK
    },
    "0x1a70b24c2ade7fc9ad51e3df2e382c453e36bb2ea090c2fc6a8ae018ce55cee1": {
        "c0": USDG, "c1": "0x9A95957b506185a0F678928A6bE63B7D55Bf198E", "fee": 43000, "tick": 430, "hook": ZERO
    },
    "0xd2a6a56247c954adaddced13e8f5e268042220016e19b0b880c0d952aeedbce6": {
        "c0": NATIVE, "c1": "0x9A95957b506185a0F678928A6bE63B7D55Bf198E", "fee": 833690, "tick": 200, "hook": ZERO
    },
    "0xff314097f73a9a373b648dbe9adb6446340d453c585d572087deafcf7112147e": {
        "c0": "0x5D8c7212A3D5d2C22d3849a7A7918c6A61c31961", "c1": USDG, "fee": 0, "tick": 200, "hook": PONS_HOOK
    },
    "0xfe95ba08aece2bbf036a13e074482d12b237103f74e27970d659e67427856019": {
        "c0": NATIVE, "c1": "0x5D8c7212A3D5d2C22d3849a7A7918c6A61c31961", "fee": 802731, "tick": 9303, "hook": ZERO
    },
    "0xa0e29bc6491dbb7f0b642610ea685620a612de2a46c77604021a07178f9411f5": {
        "c0": "0x5e81213613b6B86EaB4c6c50d718d34359459786", "c1": "0x9BAF5f0bA48ECdFCb9E30a937708dA8c3cCA1D0d", "fee": 0, "tick": 200, "hook": PONS_HOOK
    },
}


def resolve_v4_pool_params(w3, pool_id: str, pair_created_at_ms: int = 0):
    if not pool_id or len(pool_id) != 66:
        return None
    pool_id = pool_id.lower()
    if pool_id in _V4_PARAM_CACHE:
        return _V4_PARAM_CACHE[pool_id]
    return None


def map_ds_pair(token: str, p: dict, w3=None):
    token = token.lower()
    base = (p.get("baseToken") or {}).get("address", "")
    quote = (p.get("quoteToken") or {}).get("address", "")
    quote_sym = ((p.get("quoteToken") or {}).get("symbol") or "").upper()
    dex = (p.get("dexId") or "").lower()
    labels = " ".join(p.get("labels") or []).lower()
    liq = float(((p.get("liquidity") or {}).get("usd") or 0))
    pair_addr = p.get("pairAddress") or ""
    created_at = int(p.get("pairCreatedAt") or 0)
    if liq <= 0:
        return {"venue": "NONE", "quote": ZERO, "target": ZERO}

    q = to_checksum_address(quote) if quote.startswith("0x") else quote
    if q.lower() == token:
        q = to_checksum_address(base)

    is_v4 = ("v4" in labels) or ("uniswap" in dex and "v3" not in labels and "v2" not in labels) or (len(pair_addr) == 66)
    is_v3 = "v3" in labels or dex in ("uniswapv3", "uniswap")
    is_v2 = "v2" in labels or "v2" in dex

    # If V4, dynamically resolve exact fee, tick spacing, and hook from on-chain initialize event
    if is_v4:
        v4_params = resolve_v4_pool_params(w3, pair_addr, created_at) if w3 else None
        fee = v4_params["fee"] if v4_params else 0
        tick = v4_params["tick"] if v4_params else 200
        hook = v4_params["hook"] if v4_params else PONS_HOOK
        if v4_params:
            quote_resolved = v4_params["c0"] if v4_params["c0"].lower() != token else v4_params["c1"]
            return {"venue": "UNISWAP_V4", "quote": quote_resolved, "fee": fee, "tick": tick, "hook": hook, "target": UNIVERSAL_ROUTER, "ds": True}
        
        quote_addr = NATIVE if quote_sym in ("ETH", "WETH") or q.lower() in (NATIVE.lower(), WETH.lower()) else q
        return {"venue": "UNISWAP_V4", "quote": quote_addr, "fee": fee, "tick": tick, "hook": hook, "target": UNIVERSAL_ROUTER, "ds": True}

    if quote_sym in ("ETH", "WETH") or q.lower() in (NATIVE.lower(), WETH.lower()):
        if is_v2:
            return {"venue": "UNISWAP_V2_WETH", "quote": WETH, "fee": 0, "target": V2_ROUTER, "ds": True}
        return {"venue": "UNISWAP_V3_WETH", "quote": WETH, "fee": 3000, "target": SWAP_ROUTER_02, "ds": True}

    if q.lower() == USDG.lower() or quote_sym == "USDG":
        return {"venue": "UNISWAP_V3_USDG", "quote": USDG, "fee": 3000, "target": SWAP_ROUTER_02, "ds": True}

    return {"venue": "UNISWAP_V3_STOCK", "quote": q, "fee": 3000, "target": SWAP_ROUTER_02, "ds": True}


PERMIT2 = to_checksum_address("0x000000000022D473030F116dDEE9F6B43aC78BA3")
PERMIT2_TRANSFER_FROM = 0x02
UNWRAP_WETH = 0x0c

PERMIT2_ABI = [{
    "inputs": [
        {"name": "token", "type": "address"},
        {"name": "spender", "type": "address"},
        {"name": "amount", "type": "uint160"},
        {"name": "expiration", "type": "uint48"},
    ],
    "name": "approve",
    "outputs": [],
    "stateMutability": "nonpayable",
    "type": "function",
}, {
    "inputs": [
        {"name": "owner", "type": "address"},
        {"name": "token", "type": "address"},
        {"name": "spender", "type": "address"},
    ],
    "name": "allowance",
    "outputs": [
        {"name": "amount", "type": "uint160"},
        {"name": "expiration", "type": "uint48"},
        {"name": "nonce", "type": "uint48"},
    ],
    "stateMutability": "view",
    "type": "function",
}]

ERC20_ABI = [
    {"inputs": [{"name": "o", "type": "address"}, {"name": "s", "type": "address"}], "name": "allowance", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "s", "type": "address"}, {"name": "v", "type": "uint256"}], "name": "approve", "outputs": [{"type": "bool"}], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "a", "type": "address"}], "name": "balanceOf", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
]

V3_SWAP_EXACT_IN = 0x00
WRAP_ETH = 0x0b
V4_SWAP = 0x10


def encode_v3_path(tokens, fees):
    raw = bytes.fromhex(_cs(tokens[0])[2:])
    for fee, tok in zip(fees, tokens[1:]):
        raw += int(fee).to_bytes(3, "big") + bytes.fromhex(_cs(tok)[2:])
    return raw


def encode_v3_exact_in(token_in, token_out, fee, recipient, amount_in, min_out, payer_is_user=False):
    path = encode_v3_path([token_in, token_out], [fee])
    return eth_abi.encode(
        ["address", "uint256", "uint256", "bytes", "bool"],
        [_cs(recipient), int(amount_in), int(min_out), path, bool(payer_is_user)],
    )


def pool_id_from_key(currency0, currency1, fee, tick_spacing, hook):
    key = (
        _cs(currency0),
        _cs(currency1),
        int(fee),
        int(tick_spacing),
        _cs(hook),
    )
    return keccak(eth_abi.encode(
        ["(address,address,uint24,int24,address)"],
        [key],
    ))


def make_v4_swap_input(token_in, token_out, amount_in, min_out, fee, tick_spacing, hook_addr):
    token_in = _cs(token_in)
    token_out = _cs(token_out)
    if int(token_in, 16) < int(token_out, 16):
        currency0, currency1 = token_in, token_out
        zero_for_one = True
    else:
        currency0, currency1 = token_out, token_in
        zero_for_one = False
    pool_key = (currency0, currency1, int(fee), int(tick_spacing), _cs(hook_addr))
    param0 = eth_abi.encode(
        [
            "(address,address,uint24,int24,address)",
            "bool",
            "uint128",
            "uint128",
            "uint160",
            "uint256",
            "bytes",
        ],
        [pool_key, zero_for_one, int(amount_in), int(min_out), 0, 0, b""],
    )
    amt_settle = int(amount_in) if int(amount_in) > 0 else (1 << 256) - 1
    param1 = eth_abi.encode(["address", "uint256"], [token_in, amt_settle])
    param2 = eth_abi.encode(["address", "uint256"], [token_out, int(min_out)])
    actions = bytes([0x06, 0x0c, 0x0f])
    return eth_abi.encode(["bytes", "bytes[]"], [actions, [param0, param1, param2]])


class StockV4Router:
    def __init__(self, w3: Web3, account=None):
        self.w3 = w3
        self.account = account
        self.v3_factory = w3.eth.contract(address=V3_FACTORY, abi=V3_FACTORY_ABI)
        self.state_view = w3.eth.contract(address=V4_STATE_VIEW, abi=STATE_VIEW_ABI)
        self.sr02 = w3.eth.contract(address=SWAP_ROUTER_02, abi=SWAP_ROUTER_02_ABI)
        self.ur = w3.eth.contract(address=UNIVERSAL_ROUTER, abi=UNIVERSAL_ROUTER_ABI)
        self.v2_factory = w3.eth.contract(address=V2_FACTORY, abi=V2_FACTORY_ABI)
        self.v2_router = w3.eth.contract(address=V2_ROUTER, abi=V2_ROUTER_ABI)

    def _v3_pool(self, a, b, fee):
        try:
            pool = self.v3_factory.functions.getPool(_cs(a), _cs(b), int(fee)).call()
            if not pool or int(pool, 16) == 0:
                return None
            liq = self.w3.eth.contract(_cs(pool), abi=V3_POOL_ABI).functions.liquidity().call()
            if liq <= 0:
                return None
            return _cs(pool)
        except (ExecutionUncertain, PreflightFailure):
            raise
        except Exception:
            return None

    def _v4_live(self, a, b, fee, tick, hook):
        a, b = _cs(a), _cs(b)
        if int(a, 16) < int(b, 16):
            c0, c1 = a, b
        else:
            c0, c1 = b, a
        pid = pool_id_from_key(c0, c1, fee, tick, hook)
        try:
            liq = self.state_view.functions.getLiquidity(pid).call()
        except (ExecutionUncertain, PreflightFailure):
            raise
        except Exception:
            return False
        return int(liq) > 0

    def _v2_pair(self, a, b):
        try:
            pair = self.v2_factory.functions.getPair(_cs(a), _cs(b)).call()
            if not pair or int(pair, 16) == 0:
                return None
            res = self.w3.eth.contract(_cs(pair), abi=V2_PAIR_ABI).functions.getReserves().call()
            if res[0] > 0 and res[1] > 0:
                return _cs(pair)
            return None
        except (ExecutionUncertain, PreflightFailure):
            raise
        except Exception:
            return None

    def _scan_parallel(self, fns, max_workers=16):
        """
        Run a batch of independent on-chain probe functions concurrently in a
        thread pool and return the first truthy result (a dict), or None if
        none hit. These are network-bound RPC calls, so threading gives a
        real wall-clock win even under the GIL - previously this scan ran
        every combination sequentially, one RPC round-trip at a time, which
        for the full V4 fee/tick/hook/quote-asset matrix could mean hundreds
        of blocking calls (multiple seconds to over a minute) before a token
        was ever recognized as tradeable.

        Note: this does increase the number of concurrent RPC requests hitting
        your endpoint. If you're on a rate-limited free tier, tune max_workers
        down (or the RPC provider may start throttling you instead).
        """
        max_workers = min(max_workers, getattr(self, "scan_workers", 4))
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = [ex.submit(fn) for fn in fns]
            try:
                for fut in concurrent.futures.as_completed(futures):
                    try:
                        res = fut.result()
                    except (ExecutionUncertain, PreflightFailure):
                        raise
                    except Exception:
                        res = None
                    if res:
                        return res
            finally:
                for f in futures:
                    f.cancel()
        return None

    def detect(self, token: str) -> dict:
        token = _cs(token)

        # 1. Fast DexScreener Check (Real-time index for V4, V3, and V2 pairs with live liquidity)
        try:
            p = fetch_dexscreener(token)
            if p:
                mapped = map_ds_pair(token, p, self.w3)
                if mapped.get("venue") != "NONE":
                    return mapped
        except (ExecutionUncertain, PreflightFailure):
            raise
        except Exception:
            pass

        # 2. Fast Uniswap V3 WETH & USDG check on-chain (parallel)
        def check_v3(quote, fee, tag):
            pool = self._v3_pool(token, quote, fee)
            if pool:
                return {"venue": f"UNISWAP_V3_{tag}", "quote": quote, "fee": fee, "target": pool}
            return None

        v3_fns = [
            (lambda q=quote, f=fee, t=tag: check_v3(q, f, t))
            for quote, tag in [(WETH, "WETH"), (USDG, "USDG")]
            for fee in V3_FEES
        ]
        result = self._scan_parallel(v3_fns)
        if result:
            return result

        # 3. Uniswap V3 Stock pairs on-chain fallback (parallel across all stocks x fees)
        def check_stock_v3(stock, fee):
            pool = self._v3_pool(token, stock, fee)
            if pool:
                return {"venue": "UNISWAP_V3_STOCK", "quote": stock, "fee": fee, "target": pool}
            return None

        stock_v3_fns = [
            (lambda s=stock, f=fee: check_stock_v3(s, f))
            for stock in STOCK_LIST
            for fee in V3_FEES
        ]
        result = self._scan_parallel(stock_v3_fns)
        if result:
            return result

        # 4. Uniswap V4 on-chain exhaustive scan (parallel). Native ETH first
        # (cheapest/most common case), then WETH/USDG/stocks all together.
        def check_v4(quote, fee, tick, hook):
            if self._v4_live(token, quote, fee, tick, hook):
                return {
                    "venue": "UNISWAP_V4",
                    "quote": quote,
                    "fee": fee,
                    "tick": tick,
                    "hook": hook,
                    "target": UNIVERSAL_ROUTER,
                }
            return None

        native_fns = [
            (lambda f=fee, t=tick, h=hook: check_v4(NATIVE, f, t, h))
            for fee, tick in V4_FEE_TICKS
            for hook in V4_HOOKS
        ]
        result = self._scan_parallel(native_fns)
        if result:
            return result

        other_fns = [
            (lambda q=quote, f=fee, t=tick, h=hook: check_v4(q, f, t, h))
            for quote in [WETH, USDG] + STOCK_LIST
            for fee, tick in V4_FEE_TICKS
            for hook in V4_HOOKS
        ]
        result = self._scan_parallel(other_fns, max_workers=24)
        if result:
            return result

        # 5. Uniswap V2 fallback (WETH, USDG)
        v2_weth_pair = self._v2_pair(token, WETH)
        if v2_weth_pair:
            return {"venue": "UNISWAP_V2_WETH", "quote": WETH, "fee": 0, "target": V2_ROUTER}

        v2_usdg_pair = self._v2_pair(token, USDG)
        if v2_usdg_pair:
            return {"venue": "UNISWAP_V2_USDG", "quote": USDG, "fee": 0, "target": V2_ROUTER}

        return {"venue": "NONE", "quote": ZERO, "target": ZERO}

    def _gas_fees(self):
        """One shared fee policy -- see eip1559_fees in chain_client.py."""
        return eip1559_fees(self.w3)

    def _send(self, tx):
        guard = getattr(self, "execution_guard", None)
        if not guard or guard.config.DRY_RUN:
            raise ExecutionUncertain("live stock-route submission requires an active execution guard")
        tx["nonce"] = self.w3.eth.get_transaction_count(self.account.address, "pending")
        if "gasPrice" not in tx:
            tx["maxFeePerGas"], tx["maxPriorityFeePerGas"] = self._gas_fees()
        signed = self.account.sign_transaction(tx)
        raw = getattr(signed, "rawTransaction", None) or getattr(signed, "raw_transaction")
        tx_hash = self.w3.to_hex(self.w3.keccak(raw))
        guard.before_send(tx_hash, tx)
        try:
            self.w3.eth.send_raw_transaction(raw)
        except (ExecutionUncertain, PreflightFailure):
            raise
        except Exception as exc:
            if "already known" not in str(exc).lower():
                raise ExecutionUncertain(f"stock-route broadcast outcome unknown: {tx_hash}") from exc
        return tx_hash

    def _wait_receipt(self, tx_hash, timeout=60):
        try:
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout)
        except (ExecutionUncertain, PreflightFailure):
            raise
        except Exception as exc:
            raise ExecutionUncertain(f"stock-route receipt unknown: {tx_hash}") from exc
        guard = getattr(self, "execution_guard", None)
        if guard:
            guard.receipt(tx_hash, receipt)
        if receipt.get("status") != 1:
            raise ExecutionUncertain(f"stock-route transaction reverted: {tx_hash}")
        return receipt

    def _floor(self, expected):
        if expected is None or int(expected) <= 1:
            raise RuntimeError("Route has no usable quote; refusing an unprotected swap")
        pct = self.execution_guard.config.SLIPPAGE_PCT if getattr(self, "execution_guard", None) else 0
        return max(1, int(int(expected) * (1 - pct / 100)))

    def _v3_floor(self, path, fees, amount):
        for token_in, token_out, fee in zip(path, path[1:], fees):
            amount = self.quote_v3(token_in, token_out, amount, fee)
            if not amount:
                raise RuntimeError("Cannot quote complete V3 path")
        return self._floor(amount)

    def buy_v3_multihop(self, path_tokens, path_fees, amount_wei, recipient, min_out=1, gas=350000):
        if min_out <= 1:
            min_out = self._v3_floor(path_tokens, path_fees, amount_wei)
        path = encode_v3_path(path_tokens, path_fees)
        max_fee, prio = self._gas_fees()
        tx = self.sr02.functions.exactInput((
            path,
            _cs(recipient),
            int(amount_wei),
            int(min_out),
        )).build_transaction({
            "from": self.account.address,
            "value": int(amount_wei),
            "gas": gas,
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": prio,
            "nonce": self.w3.eth.get_transaction_count(self.account.address),
            "chainId": CHAIN_ID,
        })
        return self._send(tx)

    def buy_v4(self, hops, amount_wei, recipient, gas=650000):
        hops = [dict(h) for h in hops]
        for hop in hops:
            if hop.get("min_out", 1) <= 1:
                hop["min_out"] = self._floor(self.quote_v4(
                    hop["token_in"], hop["token_out"], hop["amount_in"],
                    hop["fee"], hop["tick"], hop["hook"]))
        first_in = hops[0]["token_in"]
        needs_wrap = (_cs(first_in).lower() == WETH.lower())

        commands_list = []
        inputs = []
        if needs_wrap:
            commands_list.append(0x0b)
            wrap = eth_abi.encode(["address", "uint256"], [UNIVERSAL_ROUTER, int(amount_wei)])
            inputs.append(wrap)

        if len(hops) == 1:
            commands_list.append(0x10)
            h = hops[0]
            inputs.append(make_v4_swap_input(
                h["token_in"], h["token_out"], h["amount_in"], h.get("min_out", 1),
                h["fee"], h["tick"], h["hook"],
            ))
        else:
            commands_list.append(0x10)
            actions = []
            params = []
            for i, h in enumerate(hops):
                actions.append(0x06)
                c0 = _cs(h["token_in"])
                c1 = _cs(h["token_out"])
                if int(c0, 16) < int(c1, 16):
                    currency0, currency1 = c0, c1
                    zero_for_one = True
                else:
                    currency0, currency1 = c1, c0
                    zero_for_one = False
                pool_key = (currency0, currency1, int(h["fee"]), int(h["tick"]), _cs(h["hook"]))
                amt_in = int(amount_wei) if i == 0 else 0
                min_o = int(hops[-1].get("min_out", 1)) if i == len(hops) - 1 else 0
                p = eth_abi.encode(
                    ["(address,address,uint24,int24,address)", "bool", "uint128", "uint128", "uint160", "uint256", "bytes"],
                    [pool_key, zero_for_one, amt_in, min_o, 0, 0, b""]
                )
                params.append(p)

            # SETTLE_ALL (first token in)
            actions.append(0x0c)
            params.append(eth_abi.encode(["address", "uint256"], [_cs(hops[0]["token_in"]), int(amount_wei)]))

            # TAKE_ALL (final token out)
            actions.append(0x0f)
            params.append(eth_abi.encode(["address", "uint256"], [_cs(hops[-1]["token_out"]), int(hops[-1].get("min_out", 1))]))

            inputs.append(eth_abi.encode(["bytes", "bytes[]"], [bytes(actions), params]))
        commands = bytes(commands_list)
        max_fee, prio = self._gas_fees()
        tx = self.ur.functions.execute(
            commands,
            inputs,
            int(time.time()) + 300,
        ).build_transaction({
            "from": self.account.address,
            "value": int(amount_wei),
            "gas": gas,
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": prio,
            "nonce": self.w3.eth.get_transaction_count(self.account.address),
            "chainId": CHAIN_ID,
        })

        # Pre-flight estimate_gas check: If reverts, do NOT broadcast
        try:
            est_gas = self.w3.eth.estimate_gas(tx)
            if est_gas and est_gas > 0:
                tx["gas"] = max(gas, int(est_gas * 1.2))
        except (ExecutionUncertain, PreflightFailure):
            raise
        except Exception as e:
            raise RuntimeError(f"Universal Router V4 pre-flight estimate_gas reverted: {e}")

        return self._send(tx)

    def buy_v2(self, path_or_token, amount_wei, recipient, min_out=1, gas=250000):
        if isinstance(path_or_token, (list, tuple)):
            path = path_or_token
        else:
            path = [WETH, path_or_token]
        if min_out <= 1:
            min_out = self._floor(self.v2_router.functions.getAmountsOut(
                int(amount_wei), [_cs(p) for p in path]).call()[-1])
        max_fee, prio = self._gas_fees()
        tx = self.v2_router.functions.swapExactETHForTokensSupportingFeeOnTransferTokens(
            int(min_out),
            [_cs(p) for p in path],
            _cs(recipient),
            int(time.time()) + 300,
        ).build_transaction({
            "from": self.account.address,
            "value": int(amount_wei),
            "gas": gas,
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": prio,
            "nonce": self.w3.eth.get_transaction_count(self.account.address),
            "chainId": CHAIN_ID,
        })
        return self._send(tx)

    def buy_quote_then_v4(self, token, quote, amount_wei, recipient, min_out=1):
        """The mixed V3/V4 atomic encoder is unverified on chain 4663."""
        raise PreflightFailure("Atomic mixed V3/V4 route is unverified; use protected two-hop routing")

    def buy_stock_two_tx(self, token: str, stock: str, amount_eth: float, token_fee=0, token_tick=200, token_hook=None):
        token = _cs(token)
        stock = _cs(stock)
        hook = token_hook or PONS_HOOK
        amount_wei = self.w3.to_wei(amount_eth, "ether")
        me = self.account.address

        _bal_abi = [{"inputs": [{"name": "a", "type": "address"}], "name": "balanceOf",
                     "outputs": [{"type": "uint256"}], "stateMutability": "view",
                     "type": "function"}]
        _stock = self.w3.eth.contract(stock, abi=_bal_abi)
        stock_before = _stock.functions.balanceOf(me).call()

        # Leg 2 can revert after leg 1 has already bought the stock, leaving it
        # stranded in the wallet. Retrying used to re-run leg 1 and buy the stock
        # AGAIN, doubling the ETH spent. If we are already holding some, use that
        # instead of buying more.
        # Existing stock belongs to the wallet, not this new signal. A failed
        # operation is reconciled explicitly instead of sweeping earlier holdings.
        acquired = None
        tx1 = None

        if acquired is None:
            # --- tx1: ETH -> STOCK (1-hop, same as BETA) ---
            # prefer V3 WETH/STOCK or V3 WETH/USDG/STOCK if it exists
            stock_v3 = None
            for f in V3_FEES:
                if self._v3_pool(WETH, stock, f):
                    stock_v3 = f
                    break
            if stock_v3 is not None:
                tx1 = self.buy_v3_multihop([WETH, stock], [stock_v3], amount_wei, me, 1, 280000)
            else:
                usdg_v3 = self._v3_pool(WETH, USDG, 500)
                stock_usdg = None
                for f in V3_FEES:
                    if self._v3_pool(USDG, stock, f):
                        stock_usdg = f
                        break
                if usdg_v3 and stock_usdg is not None:
                    tx1 = self.buy_v3_multihop([WETH, USDG, stock], [500, stock_usdg], amount_wei, me, 1, 380000)
                else:
                    tx1 = self.buy_v4([{
                        "token_in": WETH, "token_out": stock,
                        "amount_in": amount_wei, "min_out": 1,
                        "fee": 500, "tick": 10, "hook": ZERO,
                    }], amount_wei, me, 500000)

        if acquired is None:
            self._wait_receipt(tx1, timeout=60)
            # Only what THIS leg bought -- reading the whole balance would sweep
            # stock stranded by an earlier failed attempt into this trade too.
            acquired = _stock.functions.balanceOf(me).call() - stock_before
            if acquired <= 0:
                raise RuntimeError(f"tx1 filled no stock: {tx1}")
        stock_bal = acquired

        # approve Universal Router if needed
        allow_abi = [{
            "inputs": [{"name": "o", "type": "address"}, {"name": "s", "type": "address"}],
            "name": "allowance",
            "outputs": [{"type": "uint256"}],
            "stateMutability": "view",
            "type": "function",
        }, {
            "inputs": [{"name": "s", "type": "address"}, {"name": "v", "type": "uint256"}],
            "name": "approve",
            "outputs": [{"type": "bool"}],
            "stateMutability": "nonpayable",
            "type": "function",
        }]
        tkn = self.w3.eth.contract(stock, abi=allow_abi)
        if tkn.functions.allowance(me, UNIVERSAL_ROUTER).call() < stock_bal:
            max_fee, prio = self._gas_fees()
            atx = tkn.functions.approve(UNIVERSAL_ROUTER, 2**256 - 1).build_transaction({
                "from": me, "gas": 80000,
                "maxFeePerGas": max_fee, "maxPriorityFeePerGas": prio,
                "nonce": self.w3.eth.get_transaction_count(me), "chainId": 4663,
            })
            self._wait_receipt(self._send(atx), timeout=60)

        # --- tx2: STOCK -> TOKEN (1-hop V4, value=0, real amount) ---
        tx2 = self.buy_v4_erc20_in(
            stock, token, stock_bal, 1,
            int(token_fee), int(token_tick), hook
        )
        return tx1, tx2

    def buy_usdg_two_tx(self, token: str, amount_eth: float, token_fee=3000, token_tick=60, token_hook=None,
                        min_out_fn=None):
        """
        min_out_fn(usdg_amount) -> int lets the caller quote leg 2 once leg 1's
        output is known, since that amount is not knowable up front. DexTrader
        passes one; without it leg 2 goes out unprotected, so callers that handle
        real money should always supply it.
        """
        token = _cs(token)
        hook = token_hook or ZERO
        amount_wei = self.w3.to_wei(amount_eth, "ether")
        me = self.account.address
        usdg = self.w3.eth.contract(USDG, abi=ERC20_ABI)

        # Spend only the USDG this trade actually bought. Reading the whole
        # balance here would sweep any USDG the wallet already held -- including
        # another position's proceeds -- into this one token.
        usdg_before = usdg.functions.balanceOf(me).call()
        tx1 = self.buy_v3_multihop([WETH, USDG], [500], amount_wei, me, 1, 280000)
        self._wait_receipt(tx1, timeout=60)
        acquired = usdg.functions.balanceOf(me).call() - usdg_before
        if acquired <= 0:
            raise RuntimeError(f"tx1 filled no USDG: {tx1}")

        fee = int(token_fee) if token_fee is not None and int(token_fee) >= 0 else 2500
        tick = int(token_tick) if token_tick and int(token_tick) > 0 else 25

        leg2_min = 1
        if min_out_fn is not None:
            leg2_min = max(1, int(min_out_fn(acquired) or 0))
            if leg2_min <= 1:
                raise RuntimeError(
                    f"Could not quote USDG->{token} leg; refusing to swap {acquired} "
                    f"USDG without slippage protection"
                )
        tx2 = self.buy_v4_erc20_in(USDG, token, acquired, leg2_min, fee, tick, hook)
        return tx1, tx2

    def probe_v4_key(self, token: str, quote: str):
        token, quote = _cs(token), (NATIVE if str(quote).lower() in (NATIVE.lower(), ZERO.lower()) else _cs(quote))
        ticks = V4_FEE_TICKS_EXTENDED if "V4_FEE_TICKS_EXTENDED" in globals() else V4_FEE_TICKS
        for fee, tick in ticks:
            for hook in V4_HOOKS:
                if self._v4_live(token, quote, fee, tick, hook):
                    return {"fee": fee, "tick": tick, "hook": hook, "quote": quote}
        return None

    def _erc20_bal(self, token):
        return self.w3.eth.contract(_cs(token), abi=ERC20_ABI).functions.balanceOf(self.account.address).call()

    def sell_v3_path(self, path, fees, amount_in, min_out=1, simulate=False):
        me = self.account.address
        token_in = _cs(path[0])
        if min_out <= 1:
            min_out = self._v3_floor(path, fees, amount_in)
        erc = self.w3.eth.contract(token_in, abi=ERC20_ABI)
        max_fee, prio = self._gas_fees()
        if erc.functions.allowance(me, SWAP_ROUTER_02).call() < amount_in:
            atx = erc.functions.approve(SWAP_ROUTER_02, 2**256 - 1).build_transaction({
                "from": me, "gas": 80000,
                "maxFeePerGas": max_fee, "maxPriorityFeePerGas": prio,
                "nonce": self.w3.eth.get_transaction_count(me), "chainId": CHAIN_ID,
            })
            self._wait_receipt(self._send(atx), timeout=60)
        raw = bytes.fromhex(_cs(path[0])[2:])
        for fee, tok in zip(fees, path[1:]):
            raw += int(fee).to_bytes(3, "big") + bytes.fromhex(_cs(tok)[2:])
        tx = self.sr02.functions.exactInput((raw, me, int(amount_in), int(min_out))).build_transaction({
            "from": me, "value": 0, "gas": 380000,
            "maxFeePerGas": max_fee, "maxPriorityFeePerGas": prio,
            "nonce": self.w3.eth.get_transaction_count(me), "chainId": CHAIN_ID,
        })
        if simulate:
            tx["gas"] = max(tx["gas"], int(self.w3.eth.estimate_gas(tx) * 1.2))
        return self._send(tx)

    def sell_to_eth(self, token: str, amount_tokens: int, fee=0, tick=200, hook=None, quote=None,
                    min_out: int = 1):
        """
        min_out is the caller's ETH floor for the whole sale. It defaults to 1 only
        so older callers keep working — DexTrader.sell_token passes a quoted value.
        """
        token = _cs(token)
        hook = hook or PONS_HOOK
        quote = quote or WETH
        qlow = str(quote).lower()
        me = self.account.address
        min_out = max(1, int(min_out))
        if qlow in (NATIVE.lower(), WETH.lower(), ZERO.lower()):
            self.ensure_permit2(token, amount_tokens)
            pull = eth_abi.encode(["address", "address", "uint160"], [token, UNIVERSAL_ROUTER, int(amount_tokens)])
            v4 = make_v4_swap_input(token, WETH, amount_tokens, min_out, int(fee), int(tick) if tick > 0 else 60, hook)
            unwrap = eth_abi.encode(["address", "uint256"], [me, min_out])
            # SETTLE_ALL inside the V4 swap pulls via Permit2 already; adding a
            # PERMIT2_TRANSFER_FROM made it pull twice and revert.
            commands = bytes([0x10, UNWRAP_WETH])
            inputs = [v4, unwrap]
            max_fee, prio = self._gas_fees()
            tx = self.ur.functions.execute(commands, inputs, int(time.time()) + 300).build_transaction({
                "from": me, "value": 0, "gas": 500000,
                "maxFeePerGas": max_fee, "maxPriorityFeePerGas": prio,
                "nonce": self.w3.eth.get_transaction_count(me), "chainId": CHAIN_ID,
            })
            try:
                tx["gas"] = max(500000, int(self.w3.eth.estimate_gas(tx) * 1.2))
            except (ExecutionUncertain, PreflightFailure):
                raise
            except Exception as e:
                raise RuntimeError(f"V4 ETH sell estimate revert: {e}")
            return self._send(tx)

        # Spend only what this leg produced. Reading the whole balance would sweep
        # any quote token the wallet already held into this sale.
        q_before = self._erc20_bal(quote)
        tx1 = self.buy_v4_erc20_in(token, quote, amount_tokens, 1, int(fee), int(tick) if tick > 0 else 200, hook)
        self._wait_receipt(tx1, timeout=60)
        q_bal = self._erc20_bal(quote) - q_before
        if q_bal <= 0:
            raise RuntimeError(f"V4 sell hop filled no quote ({quote}): {tx1}")

        # min_out is an ETH floor, so it belongs on the final leg. Leg 1 stays
        # unprotected: a bad fill there makes leg 2 revert on this floor rather
        # than draining the position, but it can strand the quote token — the
        # caller retries, and the stranded balance is excluded by the delta above.
        if _cs(quote).lower() == USDG.lower():
            return self.sell_v3_path([USDG, WETH], [500], q_bal, min_out=min_out)
        stock = _cs(quote)
        for f in V3_FEES:
            if self._v3_pool(stock, WETH, f):
                return self.sell_v3_path([stock, WETH], [f], q_bal, min_out=min_out)
        stock_usdg = None
        for f in V3_FEES:
            if self._v3_pool(stock, USDG, f):
                stock_usdg = f
                break
        if stock_usdg is not None:
            return self.sell_v3_path([stock, USDG, WETH], [stock_usdg, 500], q_bal, min_out=min_out)
        raise RuntimeError(f"No V3 path stock->ETH for {stock}")

    def ensure_permit2(self, token, amount):
        token = _cs(token)
        me = self.account.address
        erc = self.w3.eth.contract(token, abi=ERC20_ABI)
        p2 = self.w3.eth.contract(PERMIT2, abi=PERMIT2_ABI)
        max_fee, prio = self._gas_fees()

        if erc.functions.allowance(me, PERMIT2).call() < amount:
            tx = erc.functions.approve(PERMIT2, 2**256 - 1).build_transaction({
                "from": me, "gas": 80000,
                "maxFeePerGas": max_fee, "maxPriorityFeePerGas": prio,
                "nonce": self.w3.eth.get_transaction_count(me), "chainId": CHAIN_ID,
            })
            self._wait_receipt(self._send(tx), timeout=60)

        amt, exp, _ = p2.functions.allowance(me, token, UNIVERSAL_ROUTER).call()
        now = int(time.time())
        if amt < amount or exp < now + 60:
            tx = p2.functions.approve(token, UNIVERSAL_ROUTER, 2**160 - 1, now + 365 * 24 * 3600).build_transaction({
                "from": me, "gas": 80000,
                "maxFeePerGas": max_fee, "maxPriorityFeePerGas": prio,
                "nonce": self.w3.eth.get_transaction_count(me), "chainId": CHAIN_ID,
            })
            self._wait_receipt(self._send(tx), timeout=60)

    def buy_v4_erc20_in(self, token_in, token_out, amount_in, min_out, fee, tick, hook):
        token_in, token_out = _cs(token_in), _cs(token_out)
        if min_out <= 1:
            min_out = self._floor(self.quote_v4(token_in, token_out, amount_in, fee, tick, hook))
        self.ensure_permit2(token_in, amount_in)

        pull = eth_abi.encode(
            ["address", "address", "uint160"],
            [token_in, UNIVERSAL_ROUTER, int(amount_in)],
        )
        v4 = make_v4_swap_input(token_in, token_out, amount_in, min_out, fee, tick, hook)
        # SETTLE_ALL in make_v4_swap_input pulls the input through Permit2.
        # An additional transfer command pulled it twice and reverted leg two.
        commands = bytes([0x10])
        inputs = [v4]

        max_fee, prio = self._gas_fees()
        tx = self.ur.functions.execute(commands, inputs, int(time.time()) + 300).build_transaction({
            "from": self.account.address,
            "value": 0,
            "gas": 500000,
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": prio,
            "nonce": self.w3.eth.get_transaction_count(self.account.address),
            "chainId": CHAIN_ID,
        })
        self.w3.eth.estimate_gas(tx)
        return self._send(tx)

    def sell(self, token: str, amount_tokens: int, min_out_wei: int = 1):
        token = _cs(token)
        info = self.detect(token)
        if info.get("venue") != "UNISWAP_V4":
            return None
        quote = (info.get("quote") or ZERO).lower()
        if quote not in (NATIVE.lower(), WETH.lower(), ZERO.lower()):
            return None

        fee = int(info.get("fee") or 2500)
        tick = int(info.get("tick") or 25)
        hook = info.get("hook") or ZERO
        if fee <= 0 or fee > 1_000_000:
            fee, tick = 2500, 25
        if tick <= 0 or tick > 200:
            tick = 25

        if not self.account:
            return None

        self.ensure_permit2(token, amount_tokens)

        pull = eth_abi.encode(
            ["address", "address", "uint160"],
            [token, UNIVERSAL_ROUTER, int(amount_tokens)],
        )
        v4 = make_v4_swap_input(token, WETH, amount_tokens, min_out_wei, fee, tick, hook)
        unwrap = eth_abi.encode(
            ["address", "uint256"],
            [self.account.address, int(min_out_wei)],
        )
        commands = bytes([0x10, UNWRAP_WETH])
        inputs = [v4, unwrap]

        max_fee, prio = self._gas_fees()
        tx = self.ur.functions.execute(commands, inputs, int(time.time()) + 300).build_transaction({
            "from": self.account.address,
            "value": 0,
            "gas": 500000,
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": prio,
            "nonce": self.w3.eth.get_transaction_count(self.account.address),
            "chainId": CHAIN_ID,
        })
        self.w3.eth.estimate_gas(tx)
        return self._send(tx)

    def buy(self, token: str, amount_eth: float, min_out: int = 1):
        token = _cs(token)
        amount_wei = self.w3.to_wei(amount_eth, "ether")
        info = self.detect(token)
        venue = info.get("venue")
        quote = info.get("quote") or ZERO
        if not self.account:
            return None
        recipient = self.account.address

        if venue == "NONE":
            return None

        if venue.startswith("UNISWAP_V3"):
            if venue == "UNISWAP_V3_WETH":
                return self.buy_v3_multihop([WETH, token], [info.get("fee") or 3000], amount_wei, recipient, min_out, 280000)
            if venue == "UNISWAP_V3_USDG":
                return self.buy_v3_multihop([WETH, USDG, token], [500, info.get("fee") or 3000], amount_wei, recipient, min_out, 350000)
            stock = quote
            return self.buy_v3_multihop([WETH, USDG, stock, token], [500, 500, info.get("fee") or 3000], amount_wei, recipient, min_out, 420000)

        if venue.startswith("UNISWAP_V2"):
            return self.buy_v2(token, amount_wei, recipient)

        # -------- V4 --------
        fee = int(info.get("fee") if info.get("fee") is not None else 2500)
        tick = int(info.get("tick") if info.get("tick") is not None else 25)
        hook = info.get("hook") or ZERO
        if fee < 0 or fee > 1_000_000:
            fee, tick = 2500, 25
        if tick <= 0:
            tick = 25

        q = str(quote).lower()

        # V4 USDG quote: 1-tx ETH -> USDG (V3) -> TOKEN (V4)
        if q == USDG.lower():
            # The atomic mixed-version encoder is known to revert on this chain.
            return self.buy_usdg_two_tx(token, amount_eth, fee, tick, hook,
                lambda amount: self._floor(self.quote_v4(USDG, token, amount, fee, tick, hook)))

        # V4 Stock / ERC20 quote: 2-tx ETH -> STOCK then STOCK -> TOKEN
        if _cs(quote) in STOCK_LIST or q not in (NATIVE.lower(), WETH.lower(), ZERO.lower(), USDG.lower()):
            res = self.buy_stock_two_tx(token, quote, amount_eth, fee, tick, hook)
            if isinstance(res, tuple):
                return res[1]
            return res

        # Native / WETH V4 single hop
        if q in (NATIVE.lower(), WETH.lower(), ZERO.lower()):
            token_in = NATIVE if q in (NATIVE.lower(), ZERO.lower()) else WETH
            hops = [{
                "token_in": token_in,
                "token_out": token,
                "amount_in": amount_wei,
                "min_out": min_out,
                "fee": fee, "tick": tick, "hook": hook,
            }]
            return self.buy_v4(hops, amount_wei, recipient, 500000)

        return None
