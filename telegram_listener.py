import asyncio
import logging
import os
from typing import Callable, Awaitable, Optional

from telethon import TelegramClient, events, errors

from config import Config
from message_parser import MessageParser
from models import CallSignal
from trade_ledger import log_event
from types import SimpleNamespace
from datetime import datetime, timezone

logger = logging.getLogger("copytrader")

class TelegramListener:
    """Listens to a specific Telegram channel for early calls."""

    def __init__(self, config: Config, parser: MessageParser, on_signal: Callable[[CallSignal], Awaitable[None]]):
        self.config = config
        self.parser = parser
        self.on_signal = on_signal
        self.client: Optional[TelegramClient] = None
        self.is_running = False
        self._handler_registered = False
        self._heartbeat_task: Optional[asyncio.Task] = None

    async def _keep_alive_heartbeat(self):
        """Sends periodic lightweight pings to prevent Windows socket timeout ([WinError 121])."""
        while self.is_running:
            try:
                await asyncio.sleep(45)
                if self.client and self.client.is_connected():
                    await self.client.get_me()
                    await self.client.catch_up()
                    logger.debug("Telegram connection heartbeat ping OK.")
            except (asyncio.CancelledError, GeneratorExit):
                break
            except Exception as e:
                logger.debug(f"Heartbeat ping notice: {e}")

    async def start(self):
        """Starts the Telegram listener and registers the event handler."""
        self.is_running = True
        
        # Instantiate TelegramClient in the active running event loop
        if self.client is None:
            session_dir = getattr(self.config, "BASE_DIR", os.path.dirname(os.path.abspath(__file__)))
            session_path = os.path.join(session_dir, "robinhood_copy_trader_session")
            self.client = TelegramClient(
                session_path,
                self.config.TELEGRAM_API_ID,
                self.config.TELEGRAM_API_HASH,
                connection_retries=None,
                auto_reconnect=True,
                retry_delay=2,
                timeout=15,
                request_retries=5
            )

        # Start keep-alive heartbeat
        if self._heartbeat_task is None or self._heartbeat_task.done():
            self._heartbeat_task = asyncio.create_task(self._keep_alive_heartbeat())
        
        while self.is_running:
            try:
                if not self.client.is_connected():
                    logger.info("Connecting to Telegram client...")
                    await self.client.start(phone=self.config.TELEGRAM_PHONE)
                
                if not self._handler_registered or not self.client.is_connected():
                    logger.info(f"Resolving channel: {self.config.CHANNEL_USERNAME}")
                    channel = await self.client.get_entity(self.config.CHANNEL_USERNAME)
                    
                    try:
                        self.client.remove_event_handler(self._handle_new_message)
                    except Exception:
                        pass

                    self.client.add_event_handler(
                        self._handle_new_message, 
                        events.NewMessage(chats=[channel])
                    )
                    self.client.add_event_handler(
                        self._handle_new_message, events.MessageEdited(chats=[channel]))
                    self._handler_registered = True
                    await self.client.catch_up()
                    await self._backfill_recent(channel)
                    self._lock_strikes = 0
                    logger.info("Telegram listener active. Listening for EARLY CALL alerts...")

                await self.client.run_until_disconnected()
                self._handler_registered = False
                
            except errors.FloodWaitError as e:
                self._handler_registered = False
                logger.warning(f"Flood wait error. Waiting {e.seconds} seconds before reconnecting...")
                await asyncio.sleep(e.seconds)
            except (OSError, ConnectionError, asyncio.TimeoutError) as e:
                self._handler_registered = False
                logger.warning(f"Network socket timeout/drop ([WinError 121]/ConnectionError: {e}). Re-establishing connection in 3s...")
                try:
                    if self.client:
                        await self.client.disconnect()
                except Exception:
                    pass
                await asyncio.sleep(3)
            except Exception as e:
                self._handler_registered = False
                err_msg = str(e)
                if "database is locked" in err_msg:
                    # Telethon keeps its session in a SQLite file that only one
                    # process can hold. A second copy of the bot (or a leftover
                    # inspect_channel.py) makes BOTH sit here reconnecting forever,
                    # silently receiving nothing. Say so instead of looping.
                    self._lock_strikes = getattr(self, "_lock_strikes", 0) + 1
                    if self._lock_strikes >= 5:
                        logger.critical(
                            "Telegram session has been locked for ~25s. Another process is "
                            "using robinhood_copy_trader_session.session - most likely a second "
                            "main.py or an inspect_channel.py still running. Close it and start "
                            "one instance only; this bot is NOT receiving calls until then."
                        )
                        self._lock_strikes = 0
                    else:
                        logger.warning(
                            f"Telegram session locked ({self._lock_strikes}/5) - waiting 5s. "
                            f"If this repeats, another instance is running."
                        )
                    await asyncio.sleep(5)
                else:
                    logger.warning(f"Connection dropped ({e}). Auto-reconnecting in 3 seconds...")
                    try:
                        if self.client:
                            await self.client.disconnect()
                    except Exception:
                        pass
                    await asyncio.sleep(3)

    async def _backfill_recent(self, channel):
        """Recover a bounded recent window; the durable queue deduplicates overlap."""
        async for message in self.client.iter_messages(
                channel, limit=self.config.SIGNAL_BACKFILL_LIMIT):
            stamp = message.date
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - stamp).total_seconds() > self.config.MAX_SIGNAL_AGE_SECONDS:
                break
            await self._handle_new_message(SimpleNamespace(message=message))

    async def stop(self):
        """Gracefully stops the Telegram listener."""
        self.is_running = False
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
        logger.info("Stopping Telegram client...")
        if self.client and self.client.is_connected():
            await self.client.disconnect()
        logger.info("Telegram client stopped.")

    async def _handle_new_message(self, event):
        """Internal handler for new messages."""
        try:
            text = event.message.message
            log_event("message_received", message_id=event.message.id,
                      channel=self.config.CHANNEL_USERNAME,
                      edited=bool(getattr(event.message, "edit_date", None)))
            message_id = event.message.id
            timestamp = event.message.date
            entities = event.message.entities

            buttons = getattr(event.message, 'buttons', None)
            reply_markup = getattr(event.message, 'reply_markup', None)

            # Diagnostic: live events and history reads can populate buttons
            # differently in Telethon. When every call starts reporting "no contract
            # address", this says whether the markup arrived at all.
            try:
                n_rows = len(buttons) if buttons else 0
                n_btns = sum(len(r) if isinstance(r, list) else 1 for r in (buttons or []))
                urls = []
                for row in (buttons or []):
                    for b in (row if isinstance(row, list) else [row]):
                        u = getattr(b, "url", None)
                        if u:
                            urls.append(u)
                logger.info(
                    f"msg {message_id}: buttons={'yes' if buttons else 'NO'} "
                    f"rows={n_rows} btns={n_btns} urls={len(urls)} "
                    f"reply_markup={'yes' if reply_markup else 'NO'} "
                    f"entities={len(entities) if entities else 0} "
                    f"text_has_0x={'yes' if '0x' in (text or '') else 'no'}"
                )
                for u in urls[:5]:
                    logger.info(f"    button url: {u}")
            except Exception as diag_err:
                logger.warning(f"button diagnostic failed: {diag_err}")

            logger.debug(f"Received message {message_id} from channel.")
            
            # parse() can make a blocking DexScreener lookup when the message has no
            # contract address. Run it off the event loop, or that http call freezes
            # every open position's 2-second price monitor while it waits.
            signal = await asyncio.to_thread(
                self.parser.parse,
                text,
                entities,
                message_id,
                timestamp,
                buttons,
                reply_markup,
            )
            
            if not signal:
                log_event("message_ignored", message_id=message_id,
                          reason="not a recognized call or unparseable header")
            if signal:
                logger.info(f"Valid CallSignal extracted for {signal.ticker}.")
                # Call the async callback
                try:
                    await self.on_signal(signal)
                except Exception as e:
                    logger.error(f"Error executing on_signal callback for {signal.ticker}: {str(e)}", exc_info=True)
                    
        except Exception as e:
            logger.error(f"Error handling new message: {str(e)}", exc_info=True)
