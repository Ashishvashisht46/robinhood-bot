"""One wallet operation at a time, with write-ahead transaction evidence.

An RPC timeout does not mean a transaction failed. Hashes are persisted before
broadcast, and unresolved operations require reconciliation, never a blind retry.
"""
import asyncio
import json
import math
import os
import time
import uuid
from pathlib import Path

from trade_ledger import log_event
from persistence import write_json


class ExecutionUncertain(RuntimeError):
    pass


class PreflightFailure(RuntimeError):
    pass


class ExecutionGuard:
    def __init__(self, config, chain, dex, path=None):
        self.config, self.chain, self.dex = config, chain, dex
        mode = "paper" if config.DRY_RUN else "live"
        self.path = Path(path or Path(config.BASE_DIR) / "cache" / f"execution_{mode}.json")
        self.operations = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        self.lock = asyncio.Lock()
        self.active = None
        self.last_operation = None
        self.inventory = None
        self.inventory_exclusions = lambda: ()
        chain.execution_guard = self
        if hasattr(dex, "stock_v4"):
            dex.stock_v4.execution_guard = self
            from inventory_funding import InventoryFunding
            self.inventory = InventoryFunding(config, chain, dex)

    def _save(self):
        write_json(self.path, self.operations)

    def before_send(self, tx_hash, tx):
        if self.active is None:
            raise ExecutionUncertain("broadcast outside a journaled wallet operation")
        operation = self.active
        deadline = operation["context"].get("expires_at")
        funding = operation.get("funding")
        if operation["side"] == "buy" and deadline and time.time() > deadline and (funding or not operation["transactions"]):
            raise PreflightFailure("signal expired during route discovery")
        if funding and time.time() - funding["quoted_at"] > self.inventory.QUOTE_SECONDS:
            raise PreflightFailure("inventory quote expired during approval or simulation")
        if any(t.get("status") is None for t in operation["transactions"]):
            raise ExecutionUncertain("previous transaction still unconfirmed")
        value = int(tx.get("value", 0))
        if funding and value != 0:
            raise ExecutionUncertain("inventory entry cannot also spend native ETH")
        spent = sum(t["value_wei"] for t in operation["transactions"])
        if operation["side"] == "buy" and spent + value > operation["budget_wei"] + 1024:
            raise ExecutionUncertain("fallback would spend the entry budget twice")
        operation["transactions"].append({"hash": tx_hash, "value_wei": value,
                                           "status": None, "gas_eth": None})
        operation["state"] = "submitted"
        self._save()  # a failure to persist must prevent broadcast
        log_event("transaction_intent", operation_id=operation["id"], tx_hash=tx_hash,
                  side=operation["side"], contract_address=operation["token"])

    def claim_inventory_input(self, token, amount):
        operation = self.active
        funding = operation.get("funding") if operation else None
        if (not funding or token.lower() != funding["token"].lower()
                or amount != int(funding["amount_raw"]) or amount <= 0
                or operation.get("inventory_input_claimed")):
            raise ExecutionUncertain("inventory input differs from plan or was already claimed")
        operation["inventory_input_claimed"] = True
        self._save()

    def receipt(self, tx_hash, receipt):
        for operation in self.operations.values():
            for transaction in operation["transactions"]:
                if transaction["hash"].lower() == tx_hash.lower():
                    transaction["status"] = int(receipt.get("status", 0))
                    # Rollups may charge an additional L1 fee when supplied.
                    transaction["gas_eth"] = (int(receipt.get("gasUsed", 0)) *
                        int(receipt.get("effectiveGasPrice", 0)) + int(receipt.get("l1Fee", 0) or 0)) / 1e18
                    self._save()

    def acknowledge(self, operation_id):
        """Call only after the position state has reached disk."""
        self.operations.pop(operation_id, None)
        self._save()

    @staticmethod
    def gas_eth(operation):
        return sum(t.get("gas_eth") or 0 for t in operation["transactions"])

    def pending_buys(self):
        return [o for o in self.operations.values() if o["side"] == "buy"]

    async def _cash_snapshot(self):
        """Value settlement assets, including WETH/USDG proceeds left by routers."""
        native = await self.chain._retry(
            lambda: self.chain.w3.eth.get_balance(self.chain.account.address))
        weth = await self.chain.get_token_balance(self.dex.weth_address)
        usdg = await self.chain.get_token_balance(self.dex.usdg_address)
        return {"eth": int(native) / 1e18 + weth, "usdg": usdg}

    async def _measure_cash(self, operation):
        if not operation.get("cash_before"):
            return
        try:
            after = await self._cash_snapshot()
            price = operation["eth_price_usd"]
            before = operation["cash_before"]
            operation["cash_delta_usd"] = ((after["eth"] - before["eth"]) * price
                                            + after["usdg"] - before["usdg"])
            funding = operation.get("funding")
            if funding:
                remaining = await self.inventory.balance_raw(funding["token"])
                spent = int(funding["balance_before_raw"]) - remaining
                if spent <= 0 or spent > int(funding["amount_raw"]):
                    raise ValueError("inventory balance changed outside the planned input")
                funding["spent_raw"] = str(spent)
                operation["cash_delta_usd"] -= (
                    spent / int(funding["amount_raw"]) * funding["value_eth"] * price)
        except Exception:
            operation["cash_delta_usd"] = None

    async def _execute(self, side, token, amount, slippage, context=None, start_time=None, entry_check=None):
        async with self.lock:
            if side == "buy" and entry_check is not None and not entry_check():
                raise PreflightFailure("entry risk limits changed while waiting for the wallet")
            if any(o["side"] == "buy" for o in self.operations.values()) and side == "buy":
                raise ExecutionUncertain("prior entry awaiting position reconciliation")
            if any(o["token"].lower() == token.lower() for o in self.operations.values()):
                raise ExecutionUncertain("token has an unresolved wallet operation")
            if any(t.get("status") is None for o in self.operations.values() for t in o["transactions"]):
                raise ExecutionUncertain("wallet has an unconfirmed transaction")
            before = 0.0
            if not self.config.DRY_RUN:
                try:
                    before = await self.chain.get_token_balance(token)
                except Exception as exc:
                    raise PreflightFailure("cannot establish token balance before trading") from exc
            operation = {"id": uuid.uuid4().hex, "side": side, "token": token,
                         "amount": amount, "before": before, "context": context or {},
                         "created": time.time(), "state": "prepared", "transactions": [],
                         "budget_wei": int(round(amount * 1e18)) if side == "buy" else 0}
            self.operations[operation["id"]] = operation
            self.active = operation
            self._save()
            try:
                if not self.config.DRY_RUN:
                    try:
                        operation["eth_price_usd"] = await self.chain.get_eth_price_usd()
                        operation["cash_before"] = await self._cash_snapshot()
                        self._save()
                    except Exception:
                        # An ancillary quote-asset RPC failure must not disable
                        # selling. Such fills remain explicitly estimated.
                        operation["cash_before"] = None
                if side == "buy" and not self.config.DRY_RUN:
                    if self.inventory:
                        excluded = set(self.inventory_exclusions())
                        for other in self.operations.values():
                            excluded.add(other["token"])
                            if other.get("funding"):
                                excluded.add(other["funding"]["token"])
                        operation["funding"] = await self.inventory.prepare(token, amount, slippage, excluded)
                        if operation["funding"]:
                            operation["budget_wei"] = 0
                        self._save()
                    if operation["context"].get("check_wallet_funds"):
                        price = operation.get("eth_price_usd")
                        if not price or not math.isfinite(price) or price <= 0:
                            raise PreflightFailure("ETH/USD price unavailable for wallet funding")
                        native = await self.chain.get_eth_balance() * price
                        required = self.config.SAFETY_FLOOR_USD + self.config.GAS_RESERVE_USD
                        if not operation.get("funding"):
                            required += amount * price
                        if native < required:
                            raise PreflightFailure("Insufficient native funds above capital floor and gas reserve")
                    if entry_check is not None and not entry_check():
                        raise PreflightFailure("entry risk limits changed during route discovery")
                if side == "buy":
                    if operation.get("funding"):
                        result = await self.inventory.execute(operation["funding"], self)
                    else:
                        result = await self.dex.buy_token(token, amount, slippage, start_time=start_time)
                else:
                    result = await self.dex.sell_token(token, amount, slippage)
                tx_hash, filled = result
                if not math.isfinite(filled) or filled <= 0:
                    if operation["transactions"] or (tx_hash and not tx_hash.startswith("DRY_RUN")):
                        raise ExecutionUncertain("submitted transaction has no verified fill")
                    raise PreflightFailure("no quoted executable route; no transaction submitted")
                operation.update(state="filled", tx_hash=tx_hash, filled=filled)
                await self._measure_cash(operation)
                self._save()
                self.last_operation = operation
                return result
            except BaseException as exc:
                if operation["transactions"]:
                    operation["state"] = "needs_review"
                    # Later wallet activity could contaminate a balance-delta
                    # valuation during recovery. Recovered P&L stays estimated.
                    operation["cash_before"] = None
                    operation["cash_delta_usd"] = None
                    self._save()
                    log_event("execution_needs_review", operation_id=operation["id"],
                              contract_address=token, side=side, error=type(exc).__name__)
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    raise ExecutionUncertain(f"{side} requires reconciliation: {operation['id']}") from exc
                self.acknowledge(operation["id"])
                if isinstance(exc, (ExecutionUncertain, asyncio.CancelledError)) or not isinstance(exc, Exception):
                    raise
                if isinstance(exc, PreflightFailure):
                    raise
                raise PreflightFailure(f"{side} failed before broadcast: {type(exc).__name__}") from exc
            finally:
                self.active = None

    async def buy_token(self, token, amount, slippage, start_time=None, context=None, entry_check=None):
        return await self._execute("buy", token, amount, slippage, context, start_time, entry_check)

    async def sell_token(self, token, amount, slippage=15.0):
        return await self._execute("sell", token, amount, slippage)

    async def get_token_price_eth(self, token):
        return await self.dex.get_token_price_eth(token)

    async def reconcile(self, operation):
        """Read only. Return a verified token delta or None if still uncertain."""
        if operation.get("state") == "filled":
            return operation["filled"]
        if self.config.DRY_RUN or not operation["transactions"]:
            return None
        for transaction in operation["transactions"]:
            if transaction.get("status") is None:
                try:
                    receipt = await asyncio.to_thread(self.chain.w3.eth.get_transaction_receipt, transaction["hash"])
                    self.receipt(transaction["hash"], receipt)
                except Exception:
                    return None
        try:
            after = await self.chain.get_token_balance(operation["token"])
        except Exception:
            return None
        delta = after - operation["before"] if operation["side"] == "buy" else operation["before"] - after
        if delta > 0:
            operation.update(state="filled", filled=delta,
                             tx_hash=operation["transactions"][-1]["hash"])
            await self._measure_cash(operation)
            self._save()
            return delta
        # Confirmed failures may include a completed first hop. Keep them visible
        # for review; do not consume the stake again or sweep intermediate assets.
        return None
