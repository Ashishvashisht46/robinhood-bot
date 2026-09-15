"""Durable, expiring signal inbox. Only known pre-broadcast failures are retried."""
import asyncio
import hashlib
import json
import logging
import sqlite3
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from models import CallSignal
from trade_ledger import log_event

logger = logging.getLogger("copytrader")


@dataclass
class SignalResult:
    status: str
    reason: str


class SignalQueue:
    def __init__(self, config, callback, path=None):
        self.config, self.callback = config, callback
        mode = "paper" if config.DRY_RUN else "live"
        path = Path(path or Path(config.BASE_DIR) / "cache" / f"signals_{mode}.sqlite3")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS signals (
            id TEXT PRIMARY KEY, payload TEXT NOT NULL, fingerprint TEXT NOT NULL,
            created REAL NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, due REAL NOT NULL DEFAULT 0)""")
        # A crash during the callback may follow a broadcast. Never replay it.
        self.db.execute("UPDATE signals SET status='needs_review', reason='interrupted execution; reconcile journal' WHERE status='processing'")
        self.db.commit()
        self.task = None
        self.running = False

    def submit(self, signal):
        key = f"{self.config.CHANNEL_USERNAME}:{signal.message_id}"
        data = asdict(signal)
        data["timestamp"] = signal.timestamp.isoformat()
        payload = json.dumps(data, sort_keys=True, ensure_ascii=False)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        existing = self.db.execute("SELECT status,fingerprint,attempts FROM signals WHERE id=?", (key,)).fetchone()
        if existing:
            status, previous, attempts = existing
            if digest == previous or status in ("opened", "processing", "needs_review", "expired"):
                log_event("signal_duplicate", signal_id=key, status=status)
                return False
            # Edits may add the missing CA or change a rejected call. A retry's
            # attempt budget remains intact; editing cannot reset it.
            self.db.execute("UPDATE signals SET payload=?,fingerprint=?,status='queued',reason='message edited',due=0 WHERE id=?", (payload, digest, key))
        else:
            stamp = signal.timestamp
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            self.db.execute("INSERT INTO signals(id,payload,fingerprint,created,status,reason) VALUES(?,?,?,?,?,?)",
                            (key, payload, digest, stamp.timestamp(), "queued", "received"))
        self.db.commit()
        log_event("signal_received", signal_id=key, message_id=signal.message_id,
                  ticker=signal.ticker, contract_address=signal.contract_address)
        return True

    def _finish(self, key, status, reason, due=0):
        self.db.execute("UPDATE signals SET status=?,reason=?,due=? WHERE id=?", (status, reason, due, key))
        self.db.commit()
        log_event("signal_outcome", signal_id=key, status=status, reason=reason)
        logger.info("Signal %s: %s (%s)", key, status, reason)

    async def process_one(self):
        now = time.time()
        row = self.db.execute("SELECT id,payload,created,attempts FROM signals WHERE status IN ('queued','retry') AND due<=? ORDER BY created,id LIMIT 1", (now,)).fetchone()
        if row is None:
            return False
        key, payload, created, attempts = row
        if now - created > self.config.MAX_SIGNAL_AGE_SECONDS or created > now + 30:
            self._finish(key, "expired", "signal outside entry freshness window")
            return True
        if attempts >= self.config.MAX_SIGNAL_ATTEMPTS:
            self._finish(key, "rejected", "retry budget exhausted")
            return True
        self.db.execute("UPDATE signals SET status='processing',attempts=attempts+1 WHERE id=?", (key,))
        self.db.commit()
        data = json.loads(payload)
        data["timestamp"] = datetime.fromisoformat(data["timestamp"])
        try:
            result = await self.callback(CallSignal(**data))
            if not isinstance(result, SignalResult):
                result = SignalResult("needs_review", "callback returned no explicit outcome")
        except asyncio.CancelledError:
            self._finish(key, "needs_review", "execution interrupted; inspect transaction journal")
            raise
        except Exception as exc:
            logger.exception("Signal callback failed")
            result = SignalResult("needs_review", f"unclassified callback failure: {type(exc).__name__}")
        delay = min(30, self.config.SIGNAL_RETRY_SECONDS * (2 ** attempts))
        self._finish(key, result.status, result.reason, time.time() + delay if result.status == "retry" else 0)
        return True

    def reconcile_opened(self, message_id):
        key = f"{self.config.CHANNEL_USERNAME}:{message_id}"
        row = self.db.execute("SELECT status FROM signals WHERE id=?", (key,)).fetchone()
        if row and row[0] == "opened":
            return
        self._finish(key, "opened", "recovered persisted position")

    async def _run(self):
        while self.running:
            if not await self.process_one():
                await asyncio.sleep(0.25)

    def start(self):
        if self.task is None:
            self.running = True
            self.task = asyncio.create_task(self._run())

    async def stop(self):
        self.running = False
        if self.task:
            await self.task  # finish the active operation; do not cancel a signing thread
            self.task = None
        self.db.close()
