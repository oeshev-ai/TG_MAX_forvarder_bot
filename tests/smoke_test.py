"""
Smoke tests for config parsing and text formatting helpers.
Run: python -m unittest tests.smoke_test -v
"""

from __future__ import annotations

import asyncio
import os
import unittest
from unittest import mock


class ChannelPairsTests(unittest.TestCase):
    def test_parse_multiple_pairs(self) -> None:
        env = {
            "CHANNEL_PAIRS": "-1001:-7001, -1002:-7002",
            "TG_CHANNEL_ID": "",
            "MAX_CHANNEL_ID": "",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            from config import _parse_pairs

            pairs = _parse_pairs()
            self.assertEqual([(p.tg_id, p.max_id) for p in pairs], [(-1001, -7001), (-1002, -7002)])

    def test_empty_pairs_are_allowed(self) -> None:
        env = {
            "CHANNEL_PAIRS": "",
            "TG_CHANNEL_ID": "",
            "MAX_CHANNEL_ID": "",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            from config import _parse_pairs

            self.assertEqual(_parse_pairs(), [])

    def test_reject_duplicate_tg(self) -> None:
        env = {
            "CHANNEL_PAIRS": "-1001:-7001,-1001:-7002",
            "TG_CHANNEL_ID": "",
            "MAX_CHANNEL_ID": "",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            from config import _parse_pairs

            with self.assertRaises(SystemExit):
                _parse_pairs()


class FormattingTests(unittest.TestCase):
    def test_adapt_tg_to_max_underline_and_spoiler(self) -> None:
        from formatting import adapt_html_tg_to_max

        html = (
            "<u>x</u><tg-spoiler>y</tg-spoiler>"
            "<blockquote expandable>z</blockquote>"
        )
        out = adapt_html_tg_to_max(html)
        self.assertIn("<ins>x</ins>", out)
        self.assertIn("y", out)
        self.assertNotIn("tg-spoiler", out)
        self.assertIn("<blockquote>z</blockquote>", out)

    def test_adapt_max_to_tg(self) -> None:
        from formatting import adapt_html_max_to_tg

        html = (
            '<ins>u</ins><h1>h</h1><mark>m</mark>'
            '<a href="max://user/42">n</a>'
        )
        out = adapt_html_max_to_tg(html)
        self.assertIn("<u>u</u>", out)
        self.assertIn("<b>h</b>", out)
        self.assertIn("m", out)
        self.assertNotIn("<mark>", out)
        self.assertIn('href="tg://user?id=42"', out)

    def test_split_long_html_falls_back_to_plain(self) -> None:
        from aiogram.enums import ParseMode
        from formatting import split_telegram_text

        text = "<b>" + ("слово " * 900) + "</b>"
        chunks = split_telegram_text(text, ParseMode.HTML, limit=200)
        self.assertGreater(len(chunks), 1)
        for chunk, mode in chunks:
            self.assertIsNone(mode)
            self.assertNotIn("<b>", chunk)
            self.assertLessEqual(len(chunk), 200)

    def test_wrap_blockquote(self) -> None:
        from formatting import wrap_blockquote

        self.assertEqual(
            wrap_blockquote("hi", is_html=False),
            "<blockquote>hi</blockquote>",
        )
        self.assertEqual(
            wrap_blockquote("<blockquote>hi</blockquote>", is_html=True),
            "<blockquote>hi</blockquote>",
        )


class RetryTests(unittest.TestCase):
    def test_retry_eventually_succeeds(self) -> None:
        from retry_util import retry_async

        calls = {"n": 0}

        async def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("nope")
            return "ok"

        result = asyncio.run(
            retry_async(flaky, attempts=3, base_delay=0.01, label="test")
        )
        self.assertEqual(result, "ok")
        self.assertEqual(calls["n"], 3)


class MessageMapTests(unittest.TestCase):
    def test_link_and_unlink(self) -> None:
        import tempfile
        from pathlib import Path
        from unittest import mock

        import message_map

        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "message_map.json"
            with mock.patch.object(message_map, "STORE_PATH", store), mock.patch.object(
                message_map, "DATA_DIR", Path(tmp)
            ):
                message_map.link_pair(
                    tg_chat_id=-1001,
                    tg_message_ids=[10, 11],
                    max_chat_id=-7001,
                    max_mid="mid-1",
                )
                self.assertEqual(
                    message_map.get_max_for_tg(-1001, 10)["max_mid"],
                    "mid-1",
                )
                rev = message_map.unlink_by_max(-7001, "mid-1")
                self.assertEqual(rev["tg_message_ids"], [10, 11])
                self.assertIsNone(message_map.get_max_for_tg(-1001, 10))


class FingerprintTests(unittest.TestCase):
    def test_fingerprint_uses_attachment_identity(self) -> None:
        from types import SimpleNamespace

        from bridge_guard import content_fingerprint

        a = SimpleNamespace(
            type="image",
            payload=SimpleNamespace(photo_id="p1", token="", url=""),
            token="",
            filename="",
        )
        b = SimpleNamespace(
            type="image",
            payload=SimpleNamespace(photo_id="p2", token="", url=""),
            token="",
            filename="",
        )
        self.assertNotEqual(
            content_fingerprint("", [a]),
            content_fingerprint("", [b]),
        )


if __name__ == "__main__":
    unittest.main()
