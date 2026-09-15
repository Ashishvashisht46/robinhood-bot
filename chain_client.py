import asyncio
import logging
import math
import time
from typing import Dict, Any, Optional, List
import aiohttp
from web3 import Web3
from web3.exceptions import TransactionNotFound
from execution_guard import ExecutionUncertain

logger = logging.getLogger("copytrader")

ERC20_ABI = [
    {"inputs": [{"name": "account", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}], "name": "approve", "outputs": [{"name": "", "type": "bool"}], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}], "name": "allowance", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "totalSupply", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"}
]

DEFAULT_RPCS = [
        "https://robinhood-rpc.publicnode.com",
    "https://4663.rpc.thirdweb.com",
    "https://rpc.mainnet.chain.robinhood.com",
]


def eip1559_fees(w3, headroom: float = 2.0) -> tuple:
    """
    (maxFeePerGas, maxPriorityFeePerGas) for chain 4663, priced off the latest block.

    maxFeePerGas is a CEILING, not a price. Measured on five live transactions, the
    receipt's effectiveGasPrice came back EXACTLY equal to that block's base fee
    every time -- the priority tip was neither charged nor needed, because blocks on
    this chain run 0% full. So raising the ceiling costs nothing and buys tolerance.

    Headroom has to be generous here because this chain's base fee only ever climbs:
    empty blocks still drift it up ~0.3% each instead of decaying it 12.5% the way a
    standard EIP-1559 chain would. A 1.25x ceiling computed before simulation was
    being overtaken before the transaction reached the mempool, and TP1 sells died
    with "max fee per gas less than block base fee" -- profit-taking silently did
    not fire. 2x is the EIP-1559 default for good reason.

    Sync on purpose: callers in stock_v4_routes.py are sync, and the async ones
    wrap it in asyncio.to_thread.
    """
    base = None
    try:
        base = w3.eth.get_block("latest").get("baseFeePerGas")
    except Exception:
        pass
    if not base:
        try:
            base = w3.eth.gas_price
        except Exception:
            base = w3.to_wei(0.5, "gwei")
    base = int(base)
    priority = max(int(base * 0.1), 1_000_000)
    return int(base * headroom) + priority, priority


class ChainClient:
    def __init__(self, config):
        self.config = config
        self.primary_rpc = getattr(self.config, "RPC_URL", DEFAULT_RPCS[0])
        self.wss_rpc = getattr(self.config, "RPC_WSS_URL", "")
        extra = getattr(self.config, "EXTRA_RPC_URLS", "") or ""
        extra_list = [u.strip() for u in extra.split(",") if u.strip()]
        fallback = getattr(self.config, "FALLBACK_RPC_URL", DEFAULT_RPCS[1])
        pool: List[str] = []
        for url in [self.primary_rpc, fallback] + extra_list + DEFAULT_RPCS:
            if url and url not in pool:
                pool.append(url)
        self.rpc_pool = pool
        self.chain_id = int(getattr(self.config, "CHAIN_ID", 4663))
        self._rpc_index = 0
        self.w3 = self._connect(self.rpc_pool[0])
        self._ensure_connected()
        self.fallback_rpc = self.rpc_pool[min(1, len(self.rpc_pool) - 1)]
        self.fallback_w3 = self.w3
        # A malformed key must not take down read-only work. Diagnostics like
        # inspect_channel.py and verify_encoders.py tiers 0-2 never sign anything,
        # so they should still run; config.validate() is what refuses to go live
        # without a usable key.
        self.account = None
        raw_key = getattr(self.config, "PRIVATE_KEY", "") or ""
        if raw_key:
            try:
                self.account = self.w3.eth.account.from_key(raw_key)
            except Exception as e:
                logger.error(
                    f"PRIVATE_KEY in .env is not a valid key ({e}). Expected 0x + 64 hex "
                    f"characters (66 total), got {len(raw_key)}. Continuing WITHOUT a "
                    f"wallet - read-only calls work, nothing can be signed or sent."
                )
        self._eth_price_usd: Optional[float] = None
        self._eth_price_timestamp: float = 0
        self._price_cache_ttl = 60
        self._decimals_cache = {}

    def _connect(self, url: str):
        return Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 10}))

    def _ensure_connected(self):
        last_err = None
        for i, url in enumerate(self.rpc_pool):
            try:
                w3 = self._connect(url)
                if w3.is_connected():
                    self.w3 = w3
                    self._rpc_index = i
                    logger.info(f"RPC connected: {url}")
                    return
            except Exception as e:
                last_err = e
                logger.warning(f"RPC failed ({url}): {e}")
        logger.error(f"No RPC reachable. Last error: {last_err}")

    def rotate_rpc(self):
        if len(self.rpc_pool) <= 1:
            return
        self._rpc_index = (self._rpc_index + 1) % len(self.rpc_pool)
        url = self.rpc_pool[self._rpc_index]
        logger.warning(f"Rotating RPC -> {url}")
        # Preserve the Web3 object used by existing contracts and bound callables.
        # Replacing it left retry callbacks attached to the failed provider.
        self.w3.provider = self._connect(url).provider
        self.fallback_rpc = url
        self.fallback_w3 = self.w3

    async def _run_in_thread(self, func, *args, **kwargs):
        return await asyncio.to_thread(func, *args, **kwargs)

    async def _retry(self, func, *args, **kwargs):
        for attempt in range(4):
            try:
                return await self._run_in_thread(func, *args, **kwargs)
            except Exception as e:
                err_str = str(e)
                if "execution reverted" in err_str:
                    logger.debug(f"Contract view call returned revert: {e}")
                    raise
                logger.warning(f"Network error in chain call (attempt {attempt + 1}/4): {e}")
                if attempt >= 1:
                    self.rotate_rpc()
                if attempt == 3:
                    raise
                await asyncio.sleep(0.4 * (attempt + 1))

    async def get_eth_balance(self) -> float:
        if getattr(self.config, "DRY_RUN", True) and not self.account:
            logger.info("[DRY RUN] No private key provided, using simulated balance of 0.0200 ETH ($50.00)")
            return 0.02
        if not self.account:
            return 0.0
        try:
            balance_wei = await self._retry(self.w3.eth.get_balance, self.account.address)
            return float(self.w3.from_wei(balance_wei, "ether"))
        except Exception as e:
            logger.warning(f"Could not fetch live on-chain balance ({e}), using 0.0 so startup can continue")
            return 0.0

    async def get_token_balance_raw(self, token_address: str) -> int:
        """
        Balance in the token's own base units, no float in the path.

        get_token_balance() divides by 10**decimals and returns a float, which keeps
        ~16 significant digits. An 18-decimal balance has 22, so converting back to
        raw for a swap silently leaves the remainder behind -- a "sell 100%" exit
        that strands dust and keeps the position looking open.
        """
        if not self.account:
            return 0
        token_address = self.w3.to_checksum_address(token_address)
        contract = self.w3.eth.contract(address=token_address, abi=ERC20_ABI)
        return int(await self._retry(contract.functions.balanceOf(self.account.address).call))

    async def get_token_balance(self, token_address: str) -> float:
        if not self.account:
            return 0.0
        token_address = self.w3.to_checksum_address(token_address)
        contract = self.w3.eth.contract(address=token_address, abi=ERC20_ABI)
        balance_raw = await self._retry(contract.functions.balanceOf(self.account.address).call)
        decimals = await self.get_token_decimals(token_address)
        return balance_raw / (10 ** decimals)

    async def get_eth_price_usd(self) -> float:
        now = time.time()
        if self._eth_price_usd and (now - self._eth_price_timestamp) < self._price_cache_ttl:
            return self._eth_price_usd

        timeout = aiohttp.ClientTimeout(total=3)
        headers = {"User-Agent": "Mozilla/5.0"}

        for url, parser in (
            ("https://api.coinbase.com/v2/prices/ETH-USD/spot", lambda d: float(d["data"]["amount"])),
            ("https://api.binance.com/api/v3/ticker/price?symbol=ETHUSDT", lambda d: float(d["price"])),
            ("https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd", lambda d: float(d["ethereum"]["usd"])),
        ):
            try:
                async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                    async with session.get(url) as resp:
                        if resp.status == 200:
                            price = parser(await resp.json())
                            if not math.isfinite(price) or price <= 0:
                                continue
                            self._eth_price_usd = price
                            self._eth_price_timestamp = now
                            return price
            except Exception:
                pass

        if self._eth_price_usd and now - self._eth_price_timestamp <= 300:
            return self._eth_price_usd
        raise RuntimeError("No fresh ETH/USD price available; cannot size an entry")

    async def usd_to_eth(self, usd_amount: float) -> float:
        price = await self.get_eth_price_usd()
        return usd_amount / price

    async def eth_to_usd(self, eth_amount: float) -> float:
        price = await self.get_eth_price_usd()
        return eth_amount * price

    async def get_nonce(self) -> int:
        if not self.account:
            raise ValueError("No private key configured")
        return await self._retry(self.w3.eth.get_transaction_count, self.account.address, "pending")

    async def estimate_gas(self, tx: Dict[str, Any]) -> int:
        base_estimate = await self._retry(self.w3.eth.estimate_gas, tx)
        return int(base_estimate * getattr(self.config, "GAS_MULTIPLIER", 1.2))

    async def send_transaction(self, tx: Dict[str, Any]) -> str:
        if getattr(self.config, "DRY_RUN", True):
            logger.info(f"[DRY RUN] Would send transaction: {tx}")
            return f"DRY_RUN_0x{int(time.time())}"

        if not self.account:
            raise ValueError("No private key configured")

        tx["nonce"] = await self.get_nonce()
        if "gas" not in tx:
            tx["gas"] = await self.estimate_gas(tx)
        if "chainId" not in tx:
            tx["chainId"] = self.chain_id

        # Re-price here, ALWAYS, overwriting whatever the caller set. This block used
        # to be skipped whenever maxFeePerGas was already present -- which every
        # caller in dex_trader.py and stock_v4_routes.py sets before simulating. The
        # good, late, config-aware path was therefore dead code on every real trade,
        # and the price that actually shipped was one computed before an
        # eth_estimateGas round trip that can take seconds under RPC throttling.
        # Signing is the last safe moment to price a transaction, so price it here.
        if "gasPrice" not in tx:
            headroom = max(2.0, float(getattr(self.config, "GAS_MULTIPLIER", 1.3)))
            max_fee, priority = await asyncio.to_thread(
                eip1559_fees, self.w3, headroom)
            tx["maxFeePerGas"] = max_fee
            tx["maxPriorityFeePerGas"] = priority

        signed_tx = self.w3.eth.account.sign_transaction(tx, private_key=self.account.key)
        raw_tx_bytes = getattr(signed_tx, "raw_transaction", getattr(signed_tx, "rawTransaction", None))
        hash_hex = self.w3.to_hex(self.w3.keccak(raw_tx_bytes))
        guard = getattr(self, "execution_guard", None)
        if guard:
            guard.before_send(hash_hex, tx)
        try:
            await self._run_in_thread(self.w3.eth.send_raw_transaction, raw_tx_bytes)
        except Exception as exc:
            if "already known" not in str(exc).lower():
                raise ExecutionUncertain(f"broadcast outcome unknown: {hash_hex}") from exc
        logger.info(f"Transaction sent: {hash_hex}")
        return hash_hex

    async def wait_for_receipt(self, tx_hash: str, timeout: int = 60) -> Dict[str, Any]:
        if tx_hash.startswith("DRY_RUN_"):
            return {"status": 1, "transactionHash": tx_hash}
        start = time.time()
        while time.time() - start < timeout:
            try:
                receipt = await self._run_in_thread(self.w3.eth.get_transaction_receipt, tx_hash)
                if receipt:
                    guard = getattr(self, "execution_guard", None)
                    if guard:
                        guard.receipt(tx_hash, receipt)
                    return receipt
            except TransactionNotFound:
                pass
            except Exception as e:
                logger.warning(f"Error waiting for receipt: {e}")
            await asyncio.sleep(2)
        raise ExecutionUncertain(f"Transaction {tx_hash} receipt not found after {timeout} seconds")

    async def get_token_decimals(self, token_address: str) -> int:
        token_address = self.w3.to_checksum_address(token_address)
        if token_address in self._decimals_cache:
            return self._decimals_cache[token_address]
        contract = self.w3.eth.contract(address=token_address, abi=ERC20_ABI)
        decimals = int(await self._retry(contract.functions.decimals().call))
        if not 0 <= decimals <= 255:
            raise ValueError("Invalid ERC20 decimals")
        self._decimals_cache[token_address] = decimals
        return decimals

    async def approve_token(self, token_address: str, spender: str, amount: int) -> str:
        token_address = self.w3.to_checksum_address(token_address)
        spender = self.w3.to_checksum_address(spender)
        contract = self.w3.eth.contract(address=token_address, abi=ERC20_ABI)
        allowance = await self._retry(contract.functions.allowance(self.account.address, spender).call)
        if allowance >= amount:
            logger.info(f"Token {token_address} already approved for {spender}")
            return "ALREADY_APPROVED"
        if getattr(self.config, "DRY_RUN", True):
            logger.info(f"[DRY RUN] Would approve {amount} of {token_address} for {spender}")
            return f"DRY_RUN_APPROVE_0x{int(time.time())}"
        tx = contract.functions.approve(spender, amount).build_transaction({
            "from": self.account.address,
            "gasPrice": await self._retry(lambda: self.w3.eth.gas_price)
        })
        logger.info(f"Approving token {token_address} for spender {spender}")
        tx_hash = await self.send_transaction(tx)
        if not tx_hash.startswith("DRY_RUN_"):
            await self.wait_for_receipt(tx_hash)
        return tx_hash
