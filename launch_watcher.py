#!/usr/bin/env python3
"""
A second signal source: buy on early on-chain activity, not on a Telegram post.

Watches every launch on the chain (Pons V2, Uniswap V4, V2, V3 -- ~900/hour),
waits until a token is WATCH_AGE_MINUTES old, counts how many distinct wallets
received it, and emits a CallSignal when that clears a threshold. It reaches the
same signal queue and the same execution path as the Telegram listener, so
nothing downstream changes.

    WHAT THE MEASUREMENT SAYS (AD-032, n=146) -- read before enabling:

      early holders is a genuine predictor   rho +0.220, p 0.007
      best exit found  TP 2.0x / SL 80%      EV +1.1% per trade
      95% confidence interval                [-5.5%, +8.5%]
      win rate at that setting               22%,  median trade -24.8%

    The edge is NOT established. The confidence interval spans zero, and a
    30-cell exit grid throws up a positive best cell 71% of the time by chance.
    This is built because Ashish asked for it after seeing those numbers.

Off unless LAUNCH_WATCHER_ENABLED=true. Paper mode is the safe way to run it:
main.py --paper leaves the execution layer untouched but broadcasts nothing.

    python launch_watcher.py --self-test     # no network
    python launch_watcher.py --dry-run       # print signals, emit nothing

Environment:
    LAUNCH_WATCHER_ENABLED    default false
    WATCH_MIN_HOLDERS         default 300    the measured threshold
    WATCH_AGE_MINUTES         default 4      earliest age with usable signal
    WATCH_POLL_SECONDS        default 20
    WATCH_MAX_PER_HOUR        default 4      hard cap; a runaway costs real gas
"""
import argparse
import asyncio
import collections
import json
import logging
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

from contract_resolution import SYSTEM
from launch_universe import SOURCES, TRANSFER, BLOCK_S, _rpc
from models import CallSignal

# Must be "copytrader", as every other module here is. setup_logging() attaches
# handlers to that logger only, so getLogger(__name__) produces a "launch_watcher"
# logger whose records reach a handler-less root and are silently dropped -- the
# watcher ran through a whole paper session without printing a single line.
logger = logging.getLogger("copytrader")

ENABLED = os.getenv("LAUNCH_WATCHER_ENABLED", "false").lower() == "true"
# The channel's FLOOR is 20 holders; their MEDIAN is 128. Running at the floor
# buys the worst slice of what they would ever touch, and it showed: 9 watcher
# trades at >=20 went 0 wins, -$18.63 each even on clean fills. The floors below
# are the right shape for a hard reject; they are the wrong place to sit.
MIN_HOLDERS = int(os.getenv("WATCH_MIN_HOLDERS", "128"))
AGE_MINUTES = float(os.getenv("WATCH_AGE_MINUTES", "4"))
POLL_SECONDS = float(os.getenv("WATCH_POLL_SECONDS", "20"))
MAX_PER_HOUR = int(os.getenv("WATCH_MAX_PER_HOUR", "4"))

MAX_POOLS_FOR_A_LAUNCH = 8      # quote tokens sit on hundreds of pairs
PENDING_TTL_BLOCKS = 36_000     # ~1h; a launch we never judged is dropped

# The channel's own floors, read off the minimum of their 223 published calls
# (AD-033). Matching these makes the watcher select like they do, ~5 minutes
# earlier. Holders comes free from transfer logs; the other three need a live
# lookup, so they are only queried for tokens that already clear holders.
MATCH_CHANNEL = os.getenv("MATCH_CHANNEL_CRITERIA", "true").lower() == "true"
FLOOR_MCAP = float(os.getenv("FLOOR_MCAP_USD", "15000"))
FLOOR_LIQUIDITY = float(os.getenv("FLOOR_LIQUIDITY_USD", "5000"))
FLOOR_VOLUME = float(os.getenv("FLOOR_VOLUME_USD", "6000"))
GT = "https://api.geckoterminal.com/api/v2"


def signal_id(address: str) -> int:
    """A stable negative id per token.

    The queue keys on message_id and a Telegram id is always positive, so
    negative ids can never collide with a real call. Deriving it from the
    address (rather than a counter) means a restart cannot re-submit a token
    the queue has already seen.
    """
    return -int(address[-8:], 16)


def build_signal(address: str, holders: int, transfers: int, venue: str,
                 age_min: float) -> CallSignal:
    """Timestamp is DETECTION time, not launch time.

    The queue expires a signal MAX_SIGNAL_AGE_SECONDS after signal.timestamp.
    Stamping it with the launch time would make every signal arrive already
    expired, because the token is deliberately AGE_MINUTES old by then.
    """
    return CallSignal(
        ticker=f"ONCHAIN_{address[2:8].upper()}",
        contract_address=address,
        mcap_usd=None, liquidity_usd=None, liquidity_pct=None,
        buy_tax=0.0, sell_tax=0.0,
        token_age_minutes=int(age_min),
        holders=holders,
        volume_24h=None,
        swaps_5m=transfers,
        elite_wallets=0, good_wallets=0,
        dex=venue,
        raw_text=(f"on-chain launch watcher: {holders} holders / {transfers} "
                  f"transfers at {age_min:.1f} min on {venue}"),
        timestamp=datetime.now(timezone.utc),
        message_id=signal_id(address),
    )


class LaunchWatcher:
    def __init__(self, config, on_signal):
        self.config = config
        self.on_signal = on_signal
        self.pending = {}            # token -> (launch_block, venue)
        self.seen = set()
        self.pool_count = collections.Counter()   # quote tokens pair with everything
        self.emitted = []            # timestamps, for the rate cap
        self.last_block = None
        self._task = None
        self._stop = asyncio.Event()

    # ---- chain reads, all blocking; callers push them to a thread ----------

    def _head(self):
        return int(_rpc("eth_blockNumber", [])["result"], 16)

    def _new_launches(self, lo, hi):
        found = {}
        for name, addr, topic, slots in SOURCES:
            r = _rpc("eth_getLogs", [{"fromBlock": hex(lo), "toBlock": hex(hi),
                                      "address": addr, "topics": [topic]}])
            if "error" in r:
                logger.warning("launch scan %s: %s", name, r["error"].get("message"))
                continue
            for log in r.get("result") or []:
                t = log.get("topics", [])
                bn = int(log["blockNumber"], 16)
                for i in slots:
                    if i < len(t):
                        found.setdefault("0x" + t[i][-40:], (bn, name))
        return found

    def is_launch(self, token: str) -> bool:
        """A pair event names BOTH sides, so the quote asset looks like a launch.

        A live dry run emitted a buy signal for USDG -- 729 'holders', 7,413
        'transfers' -- because a stablecoin is on one side of every new pool.
        Two guards: the codebase's existing SYSTEM denylist, and the fact that a
        real launch appears in one or two pools while a quote appears in
        hundreds.
        """
        if token.lower() in SYSTEM:
            return False
        return self.pool_count[token] <= MAX_POOLS_FOR_A_LAUNCH

    def _channel_metrics(self, token):
        """Liquidity, market cap and volume -- the fields the channel publishes
        that transfer logs cannot give. Returns None when unknown, which is
        treated as a REJECT: an unquotable token is not one to buy blind."""
        try:
            req = urllib.request.Request(
                f"{GT}/networks/robinhood/tokens/{token}/pools",
                headers={"User-Agent": "launch-watcher/1", "Accept": "application/json"})
            d = json.load(urllib.request.urlopen(req, timeout=15))
        except Exception as exc:
            logger.debug("metrics lookup failed for %s: %s", token, exc)
            return None
        pools = d.get("data") or []
        if not pools:
            return None
        best = max(pools, key=lambda p: float(p["attributes"].get("reserve_in_usd") or 0))
        a = best["attributes"]
        vol = a.get("volume_usd") or {}
        return {
            "liquidity": float(a.get("reserve_in_usd") or 0),
            "mcap": float(a.get("fdv_usd") or a.get("market_cap_usd") or 0),
            "volume": float(vol.get("h24") or vol.get("h1") or 0),
        }

    def passes_channel(self, holders, m):
        """The channel's floors. Their minimum published call, not a guess."""
        if holders < 20:
            return False, f"holders {holders} < 20"
        if not MATCH_CHANNEL:
            return True, "holders only (MATCH_CHANNEL_CRITERIA=false)"
        if m is None:
            return False, "no pool/metrics"
        if m["liquidity"] < FLOOR_LIQUIDITY:
            return False, f"liquidity ${m['liquidity']:,.0f} < ${FLOOR_LIQUIDITY:,.0f}"
        if m["mcap"] < FLOOR_MCAP:
            return False, f"mcap ${m['mcap']:,.0f} < ${FLOOR_MCAP:,.0f}"
        if m["volume"] < FLOOR_VOLUME:
            return False, f"volume ${m['volume']:,.0f} < ${FLOOR_VOLUME:,.0f}"
        return True, (f"liq ${m['liquidity']:,.0f} mcap ${m['mcap']:,.0f} "
                      f"vol ${m['volume']:,.0f}")

    def _holders(self, token, launch_block):
        """Distinct wallets that received the token in its first AGE_MINUTES."""
        span = int(AGE_MINUTES * 60 / BLOCK_S)
        r = _rpc("eth_getLogs", [{"fromBlock": hex(launch_block),
                                  "toBlock": hex(launch_block + span),
                                  "address": token, "topics": [TRANSFER]}])
        if "error" in r:
            return None, None
        logs = r.get("result") or []
        recips = {l["topics"][2][-40:] for l in logs if len(l.get("topics", [])) > 2}
        return len(recips), len(logs)

    # ---- the loop ---------------------------------------------------------

    def _rate_limited(self):
        cutoff = time.time() - 3600
        self.emitted = [t for t in self.emitted if t > cutoff]
        return len(self.emitted) >= MAX_PER_HOUR

    async def _tick(self):
        head = await asyncio.to_thread(self._head)
        if self.last_block is None:
            self.last_block = head - int(AGE_MINUTES * 60 / BLOCK_S)

        if head > self.last_block:
            found = await asyncio.to_thread(self._new_launches,
                                            self.last_block + 1, head)
            for tok, (bn, venue) in found.items():
                self.pool_count[tok] += 1
                if tok not in self.seen:
                    self.seen.add(tok)
                    self.pending[tok] = (bn, venue)
            self.last_block = head

        ripe_at = head - int(AGE_MINUTES * 60 / BLOCK_S)
        ripe = [(t, b, v) for t, (b, v) in self.pending.items() if b <= ripe_at]
        for tok, bn, venue in ripe:
            del self.pending[tok]
            if bn < head - PENDING_TTL_BLOCKS:
                continue                      # too old to judge fairly
            if not self.is_launch(tok):
                continue                      # quote asset, not a new token
            if self._rate_limited():
                logger.info("launch watcher: rate cap %d/h reached, skipping %s",
                            MAX_PER_HOUR, tok)
                continue
            holders, transfers = await asyncio.to_thread(self._holders, tok, bn)
            if holders is None or holders < MIN_HOLDERS:
                continue
            # Only tokens already past the holder screen get a metrics lookup,
            # so this costs a few API calls an hour rather than hundreds.
            m = await asyncio.to_thread(self._channel_metrics, tok) if MATCH_CHANNEL else None
            ok, why = self.passes_channel(holders, m)
            if not ok:
                logger.info("skip %s (%d holders): %s", tok, holders, why)
                continue
            age = (head - bn) * BLOCK_S / 60
            sig = build_signal(tok, holders, transfers, venue, age)
            logger.info("LAUNCH SIGNAL %s  %d holders / %d transfers at %.1f min "
                        "(%s) | %s", tok, holders, transfers, age, venue, why)
            self.emitted.append(time.time())
            await self.on_signal(sig)

    async def _run(self):
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception:
                logger.exception("launch watcher tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=POLL_SECONDS)
            except asyncio.TimeoutError:
                pass

    async def start(self):
        if not ENABLED:
            logger.info("launch watcher disabled (LAUNCH_WATCHER_ENABLED != true)")
            return
        logger.info("launch watcher ON: >=%d holders at %.0f min, max %d/h",
                    MIN_HOLDERS, AGE_MINUTES, MAX_PER_HOUR)
        self._task = asyncio.create_task(self._run())

    async def stop(self):
        self._stop.set()
        if self._task:
            await asyncio.gather(self._task, return_exceptions=True)


# ---- checks ---------------------------------------------------------------

def self_test() -> int:
    fails = []

    def check(name, got, want):
        ok = got == want
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
        if not ok:
            print(f"          got {got!r} want {want!r}")
            fails.append(name)

    print("signal ids cannot collide with Telegram messages:")
    a = "0x8ac912be572f532ca536c907b5e26fa2da0af1b5"
    b = "0x05a7a8ab2996ca400738bb4c751ebe176a651fe2"
    check("id is negative", signal_id(a) < 0, True)
    check("distinct tokens -> distinct ids", signal_id(a) != signal_id(b), True)
    check("same token -> same id (restart cannot resubmit)",
          signal_id(a), signal_id(a))

    print("\nthe signal the queue will see:")
    s = build_signal(a, 412, 1900, "uni_v4", 4.0)
    check("carries the contract address", s.contract_address, a)
    check("holders populated", s.holders, 412)
    check("stamped NOW, not at launch",
          (datetime.now(timezone.utc) - s.timestamp).total_seconds() < 5, True)
    check("no fake tax that would trip the tax gate", (s.buy_tax, s.sell_tax), (0.0, 0.0))
    check("venue recorded as dex", s.dex, "uni_v4")

    print("\nthe rate cap is a real stop, not a suggestion:")
    w = LaunchWatcher.__new__(LaunchWatcher)
    w.emitted = [time.time()] * MAX_PER_HOUR
    check(f"at {MAX_PER_HOUR}/h -> limited", w._rate_limited(), True)
    w.emitted = [time.time() - 4000] * MAX_PER_HOUR
    check("an hour later -> clear", w._rate_limited(), False)

    print("\nquote assets are NOT launches (a live dry run signalled USDG):")
    w2 = LaunchWatcher.__new__(LaunchWatcher)
    w2.pool_count = collections.Counter()
    check("USDG rejected", w2.is_launch("0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"), False)
    check("WETH rejected", w2.is_launch("0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"), False)
    check("a real token accepted", w2.is_launch(a), True)
    w2.pool_count[a] = MAX_POOLS_FOR_A_LAUNCH + 1
    check("anything pairing with everything rejected", w2.is_launch(a), False)

    print("\nlogging actually reaches the bot's handlers:")
    check("logger is the one setup_logging configures",
          logger.name, "copytrader")

    print("\nthe channel's floors are enforced, not just holders:")
    w3 = LaunchWatcher.__new__(LaunchWatcher)
    good = {"liquidity": 20_000, "mcap": 50_000, "volume": 30_000}
    check("passes when everything clears", w3.passes_channel(120, good)[0], True)
    check("holders below their floor -> reject", w3.passes_channel(19, good)[0], False)
    check("thin liquidity -> reject",
          w3.passes_channel(120, {**good, "liquidity": 4_999})[0], False)
    check("tiny mcap -> reject",
          w3.passes_channel(120, {**good, "mcap": 14_999})[0], False)
    check("no volume -> reject",
          w3.passes_channel(120, {**good, "volume": 5_999})[0], False)
    check("UNQUOTABLE token is rejected, not waved through",
          w3.passes_channel(120, None)[0], False)

    print("\ndefaults are the safe ones:")
    check("watcher OFF unless explicitly enabled", ENABLED, False)
    # Their median, not their floor. Running at the floor (20) went 0 wins from
    # 9 trades at -$18.63 each, on clean fills.
    check("threshold is the channel's MEDIAN holders", MIN_HOLDERS, 128)

    print()
    if fails:
        print(f"FAILED: {', '.join(fails)}")
        return 1
    print("All launch-watcher checks passed")
    return 0


async def _dry_run():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    seen = []

    async def collect(sig):
        seen.append(sig)
        print(f"\n  WOULD BUY {sig.contract_address}  {sig.holders} holders "
              f"({sig.dex})  -- dry run, nothing sent\n")

    w = LaunchWatcher(config=None, on_signal=collect)
    print(f"watching: >={MIN_HOLDERS} holders at {AGE_MINUTES:.0f} min, "
          f"cap {MAX_PER_HOUR}/h. Ctrl-C to stop. NOTHING IS BROADCAST.\n")
    try:
        while True:
            await w._tick()
            print(f"  ...{len(w.pending)} launches pending judgement, "
                  f"{len(seen)} signals so far", end="\r", flush=True)
            await asyncio.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        print(f"\nstopped. {len(seen)} signals in this run.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    if args.dry_run:
        asyncio.run(_dry_run())
        return 0
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
