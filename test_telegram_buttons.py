"""Offline regressions for the raw Telegram button layout seen on missed calls."""
import logging
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telethon.extensions import BinaryReader
from telethon.tl import types
from telethon.tl.custom.messagebutton import MessageButton

from contract_resolution import button_links, extract_contract
from message_parser import MessageParser
from telegram_listener import TelegramListener

TOKEN = "0x" + "1" * 40
OTHER = "0x" + "2" * 40
URL = "https://gmgn.ai/robinhood/token/" + TOKEN
CALL = "EARLY CALL - $TRS · robinhood\nPool Info\nMcap: $89k\n"


def button(url=URL):
    return types.KeyboardInlineButton("GMGN", types.InlineButtonTypeUrl(url))


def markup(*buttons):
    return types.ReplyInlineMarkup([types.KeyboardInlineButtonRow(list(buttons))])


def parse(rm, text=CALL, buttons=None):
    return MessageParser().parse(text, [], 7972, datetime.now(timezone.utc), buttons, rm)


class RawButtonTests(unittest.TestCase):
    def test_real_raw_payload_has_nested_url_and_resolves_without_wrappers(self):
        raw = button()
        self.assertIsNone(getattr(raw, "url", None))
        self.assertEqual(raw.type.url, URL)
        self.assertEqual(parse(markup(raw)).contract_address, TOKEN)

    def test_deserialized_telegram_markup_resolves_all_reported_header_formats(self):
        with BinaryReader(bytes(markup(button()))) as reader:
            rm = reader.tgread_object()
        for ticker in ("TRS", "旺柴", "VELCOR3"):
            with self.subTest(ticker=ticker):
                result = parse(rm, CALL.replace("TRS", ticker))
                self.assertEqual((result.ticker, result.contract_address), (ticker, TOKEN))

    def test_real_custom_wrapper_still_resolves(self):
        wrapped = MessageButton(None, button(), None, None, 7972)
        self.assertEqual(parse(None, buttons=[[wrapped]]).contract_address, TOKEN)

    def test_wrapper_with_raw_button_only_is_supported(self):
        self.assertEqual(parse(None, buttons=[[SimpleNamespace(button=button())]]).contract_address, TOKEN)

    def test_raw_button_in_grid_is_supported(self):
        self.assertEqual(parse(None, buttons=[(button(),)]).contract_address, TOKEN)

    def test_legacy_raw_url_is_still_supported(self):
        rm = SimpleNamespace(rows=[SimpleNamespace(buttons=[SimpleNamespace(url=URL)])])
        self.assertEqual(parse(rm).contract_address, TOKEN)

    def test_raw_callback_url_bytes_are_candidates_without_clicking(self):
        raw = types.KeyboardInlineButton("Chart", types.InlineButtonTypeCallback(URL.encode()))
        self.assertEqual(parse(markup(raw)).contract_address, TOKEN)

    def test_legacy_raw_callback_url_bytes_are_supported(self):
        rm = SimpleNamespace(rows=[SimpleNamespace(buttons=[SimpleNamespace(data=URL.encode())])])
        self.assertEqual(parse(rm).contract_address, TOKEN)

    def test_duplicate_wrapped_and_raw_urls_are_deduplicated(self):
        raw = button()
        wrapped = MessageButton(None, raw, None, None, 7972)
        self.assertEqual(button_links([[wrapped]], markup(raw)), [URL])

    def test_two_different_token_links_still_refuse_ambiguity(self):
        result = extract_contract(CALL, [], None, markup(button(), button(URL.replace(TOKEN, OTHER))), "TRS")
        self.assertEqual(result, (None, "ambiguous"))

    def test_pool_url_still_requires_exact_pair_resolution(self):
        rm = markup(button("https://dexscreener.com/robinhood/" + OTHER))
        with patch("contract_resolution.resolve_pair", return_value=TOKEN) as resolve:
            self.assertEqual(parse(rm).contract_address, TOKEN)
        resolve.assert_called_once_with(OTHER, "TRS")

    def test_unresolved_pool_never_becomes_a_token_address(self):
        rm = markup(button("https://dexscreener.com/robinhood/" + OTHER))
        with patch("contract_resolution.resolve_pair", return_value=None):
            self.assertIsNone(parse(rm).contract_address)

    def test_direct_token_link_avoids_network_despite_chart_button(self):
        rm = markup(button("https://dexscreener.com/robinhood/" + OTHER), button())
        with patch("contract_resolution.urllib.request.urlopen", side_effect=AssertionError("No network")):
            self.assertEqual(parse(rm).contract_address, TOKEN)

    def test_wallet_and_untrusted_domain_links_are_not_token_identity(self):
        for url in ("https://robinhoodchain.blockscout.com/address/" + TOKEN,
                    "https://gmgn.ai.evil.example/robinhood/token/" + TOKEN,
                    "javascript://gmgn.ai/robinhood/token/" + TOKEN):
            with self.subTest(url=url):
                self.assertIsNone(parse(markup(button(url))).contract_address)

    def test_opaque_callback_and_plain_address_are_not_interpreted(self):
        for data in (b"buy:" + TOKEN.encode(), TOKEN.encode(), b"\xff\x00"):
            raw = types.KeyboardInlineButton("Buy", types.InlineButtonTypeCallback(data))
            self.assertIsNone(parse(markup(raw)).contract_address)

    def test_bad_url_does_not_hide_later_valid_button(self):
        self.assertEqual(parse(markup(button("https://[broken"), button())).contract_address, TOKEN)

    def test_empty_markup_still_produces_missing_address_without_ticker_search(self):
        with patch("contract_resolution.urllib.request.urlopen", side_effect=AssertionError("No network")):
            self.assertIsNone(parse(SimpleNamespace(rows=None)).contract_address)
            self.assertIsNone(parse(SimpleNamespace(rows=[SimpleNamespace(buttons=None)])).contract_address)


class ListenerTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_event_without_cached_buttons_delivers_resolved_signal(self):
        callback = AsyncMock()
        listener = TelegramListener(SimpleNamespace(CHANNEL_USERNAME="test"), MessageParser(), callback)
        message = SimpleNamespace(message=CALL, id=7972, date=datetime.now(timezone.utc),
                                  entities=[], buttons=None, reply_markup=markup(button()))
        with patch("telegram_listener.log_event"), patch("contract_resolution.urllib.request.urlopen",
                                                           side_effect=AssertionError("No network")):
            await listener._handle_new_message(SimpleNamespace(message=message))
        callback.assert_awaited_once()
        self.assertEqual(callback.call_args.args[0].contract_address, TOKEN)
        self.assertIsNone(listener.client)

    async def test_diagnostics_count_raw_links_without_dumping_callback_secrets(self):
        secret = "private-callback-payload"
        rm = markup(types.KeyboardInlineButton("Action", types.InlineButtonTypeCallback(secret.encode())))
        callback = AsyncMock()
        listener = TelegramListener(SimpleNamespace(CHANNEL_USERNAME="test"), MessageParser(), callback)
        message = SimpleNamespace(message=CALL, id=7972, date=datetime.now(timezone.utc),
                                  entities=[], buttons=None, reply_markup=rm)
        previous_disable = logging.root.manager.disable
        try:
            # The offline runner suppresses logs globally; this assertion needs them.
            logging.disable(logging.NOTSET)
            with patch("telegram_listener.log_event"), self.assertLogs("copytrader", level="INFO") as capture:
                await listener._handle_new_message(SimpleNamespace(message=message))
        finally:
            logging.disable(previous_disable)
        logs = "\n".join(capture.output)
        self.assertIn("raw_btns=1", logs)
        self.assertIn("InlineButtonTypeCallback", logs)
        self.assertNotIn(secret, logs)
        self.assertIsNone(callback.call_args.args[0].contract_address)


if __name__ == "__main__":
    unittest.main()
