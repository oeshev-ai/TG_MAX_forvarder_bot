"""Pair store, link codes, and the private-chat wizard."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import claims
import config
import discovery
import linking
import pairs
from config import ChannelPair


class _StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self._env = list(config.ENV_CHANNEL_PAIRS)
        self._saved_pairs = pairs.list_pairs()
        self._tg = dict(config.TG_TO_MAX)
        self._max = dict(config.MAX_TO_TG)
        self._tg_ids = set(config.TG_CHANNEL_IDS)
        self._max_ids = set(config.MAX_CHANNEL_IDS)
        self.patches = [
            mock.patch.object(pairs, "STORE_PATH", root / "channel_pairs.json"),
            mock.patch.object(pairs, "DATA_DIR", root),
            mock.patch.object(discovery, "STORE_PATH", root / "discovered_channels.json"),
            mock.patch.object(discovery, "DATA_DIR", root),
            mock.patch.object(config, "ENV_CHANNEL_PAIRS", []),
        ]
        for patch in self.patches:
            patch.start()
        pairs._pairs.clear()
        config.TG_TO_MAX.clear()
        config.MAX_TO_TG.clear()
        config.TG_CHANNEL_IDS.clear()
        config.MAX_CHANNEL_IDS.clear()
        claims.reset()

    def tearDown(self) -> None:
        claims.reset()
        pairs._pairs.clear()
        pairs._pairs.extend(self._saved_pairs)
        config.TG_TO_MAX.clear()
        config.TG_TO_MAX.update(self._tg)
        config.MAX_TO_TG.clear()
        config.MAX_TO_TG.update(self._max)
        config.TG_CHANNEL_IDS.clear()
        config.TG_CHANNEL_IDS.update(self._tg_ids)
        config.MAX_CHANNEL_IDS.clear()
        config.MAX_CHANNEL_IDS.update(self._max_ids)
        for patch in self.patches:
            patch.stop()
        config.ENV_CHANNEL_PAIRS[:] = self._env
        self.tmp.cleanup()


class ClaimTests(unittest.TestCase):
    def setUp(self) -> None:
        claims.reset()

    def tearDown(self) -> None:
        claims.reset()

    def test_normalize_and_shape(self) -> None:
        self.assertEqual(claims.normalize("k7mq-4p2r"), "K7MQ4P2R")
        self.assertTrue(claims.looks_like_code("K7MQ-4P2R"))
        self.assertTrue(claims.looks_like_code("k7mq4p2r"))
        self.assertFalse(claims.looks_like_code("привет"))
        self.assertFalse(claims.looks_like_code("/start"))
        self.assertFalse(claims.looks_like_code("K7MQ-4P2R лишнее"))

    def test_code_expires_and_is_single_use(self) -> None:
        code = claims.issue("tg", -100, 10, now=1_000)
        self.assertIsNotNone(claims.lookup(code, now=1_000 + 10))
        self.assertIsNone(claims.lookup(code, now=1_000 + claims.TTL_SEC + 1))

        code = claims.issue("tg", -100, 10, now=2_000)
        self.assertIsNotNone(claims.hold(code, "max", 20, -700, now=2_010))
        self.assertIsNone(claims.lookup(code, now=2_010))
        claims.release("max", 20, now=2_020)
        self.assertIsNotNone(claims.lookup(code, now=2_020))

    def test_failures_block_then_slide_out(self) -> None:
        now = 5_000.0
        for offset in range(claims.MAX_FAILURES):
            claims.note_failure("max", 20, now=now + offset)
        self.assertTrue(claims.is_blocked("max", 20, now=now + 4))
        later = now + claims.FAILURE_WINDOW_SEC + 1
        self.assertFalse(claims.is_blocked("max", 20, now=later))


class PairStoreTests(_StoreCase):
    def test_add_updates_the_same_set_object(self) -> None:
        ids = config.TG_CHANNEL_IDS
        pairs.add_pair(-100, -700, linked_by_tg=1, linked_by_max=2)
        self.assertIs(ids, config.TG_CHANNEL_IDS)
        self.assertIn(-100, ids)
        self.assertEqual(config.MAX_TO_TG[-700], -100)
        with self.assertRaises(pairs.PairError):
            pairs.add_pair(-100, -701)
        removed = pairs.remove_by_tg(-100)
        self.assertEqual(removed["max_id"], -700)
        self.assertNotIn(-100, config.TG_CHANNEL_IDS)

    def test_env_seed_and_conflict(self) -> None:
        config.ENV_CHANNEL_PAIRS = [ChannelPair(-10, -20)]
        pairs.load()
        self.assertEqual(config.TG_TO_MAX[-10], -20)
        self.assertIsNotNone(discovery.get_channel("tg", -10))
        self.assertIsNotNone(discovery.get_channel("max", -20))
        config.ENV_CHANNEL_PAIRS = [ChannelPair(-10, -21), ChannelPair(-11, -20)]
        pairs.load()
        self.assertEqual(config.TG_TO_MAX[-10], -20)
        self.assertNotIn(-11, config.TG_TO_MAX)

    def test_env_pair_can_be_unlinked_by_admin(self) -> None:
        config.ENV_CHANNEL_PAIRS = [ChannelPair(-30, -40)]
        pairs.load()

        async def run() -> None:
            with mock.patch.object(
                linking, "_api_is_admin", new=mock.AsyncMock(return_value=True)
            ):
                self.assertTrue(await linking.user_can_manage("tg", 1, -30))
                menu = await linking.unlink_menu("tg", 1)
                self.assertTrue(any(b.payload == "lnk:u:-30" for b in menu.buttons))

        asyncio.run(run())

    def test_added_by_is_kept(self) -> None:
        discovery.register_telegram_channel(
            -100,
            title="A",
            status="administrator",
            added_by=7,
            added_by_name="Ann",
        )
        again = discovery.register_telegram_channel(-100, title="B", status="creator")
        self.assertEqual(again["added_by"], 7)
        self.assertEqual(again["added_by_name"], "Ann")
        self.assertEqual(again["title"], "B")


class WizardTests(_StoreCase):
    def setUp(self) -> None:
        super().setUp()
        self._names = (linking.tg_bot_username, linking.max_bot_username)

    def tearDown(self) -> None:
        linking.tg_bot_username, linking.max_bot_username = self._names
        super().tearDown()

    def _channels(self) -> None:
        discovery.register_telegram_channel(
            -100,
            title="News",
            username="news",
            status="administrator",
            added_by=10,
            added_by_name="Ann",
        )
        discovery.register_max_channel(
            -700,
            title="News MAX",
            status="channel",
            added_by=20,
            added_by_name="Ann",
        )

    def test_code_confirms_pair_and_unlink(self) -> None:
        self._channels()
        linking.max_bot_username = "maxbot"
        linking.tg_bot_username = "tgbot"

        async def run() -> None:
            with (
                mock.patch.object(linking, "_api_is_admin", new=mock.AsyncMock(return_value=True)),
                mock.patch.object(linking, "_notify", new=mock.AsyncMock()) as notify,
            ):
                issued = await linking.issue("tg", 10, -100)
                self.assertTrue(issued.text.startswith("Код "))
                self.assertTrue(any(button.url and "max.ru/maxbot?start=" in button.url for button in issued.buttons))
                code = next(iter(claims._claims))
                question = await linking.redeem("max", 20, claims.pretty(code))
                self.assertIn("Связать", question.text)
                done = await linking.handle_payload("max", 20, "lnk:y")
                self.assertIn("Готово", done.text)
                self.assertEqual(config.TG_TO_MAX[-100], -700)
                notify.assert_awaited()
                removed = await linking.handle_payload("tg", 10, "lnk:uy:-100")
                self.assertIn("Связь снята", removed.text)
                self.assertNotIn(-100, config.TG_CHANNEL_IDS)
                self.assertIsNone(pairs.pair_by_tg(-100))

        asyncio.run(run())

    def test_several_channels_need_a_choice(self) -> None:
        self._channels()
        discovery.register_max_channel(
            -701,
            title="Other",
            status="channel",
            added_by=20,
        )

        async def run() -> None:
            with (
                mock.patch.object(linking, "_api_is_admin", new=mock.AsyncMock(return_value=True)),
                mock.patch.object(linking, "_notify", new=mock.AsyncMock()),
            ):
                await linking.issue("tg", 10, -100)
                code = next(iter(claims._claims))
                screen = await linking.redeem("max", 20, code)
                payloads = {button.payload for button in screen.buttons}
                self.assertIn("lnk:p:-700", payloads)
                self.assertIn("lnk:p:-701", payloads)
                question = await linking.handle_payload("max", 20, "lnk:p:-701")
                self.assertIn("Other", question.text)
                done = await linking.confirm("max", 20)
                self.assertEqual(config.TG_TO_MAX[-100], -701)

        asyncio.run(run())

    def test_same_side_and_cancel_keep_the_code(self) -> None:
        self._channels()

        async def run() -> None:
            with mock.patch.object(linking, "_api_is_admin", new=mock.AsyncMock(return_value=True)):
                await linking.issue("max", 20, -700)
                code = next(iter(claims._claims))
                wrong = await linking.redeem("max", 20, code)
                self.assertIn("Telegram", wrong.text)
                self.assertIsNotNone(claims.lookup(code))
                await linking.redeem("tg", 10, code)
                self.assertIsNone(claims.lookup(code))
                cancelled = await linking.cancel("tg", 10)
                self.assertIn("Отменено", cancelled.text)
                self.assertIsNotNone(claims.lookup(code))

        asyncio.run(run())

    def test_unknown_api_falls_back_to_added_by(self) -> None:
        self._channels()

        async def run() -> None:
            with mock.patch.object(linking, "_api_is_admin", new=mock.AsyncMock(return_value=None)):
                allowed = await linking.issue("tg", 10, -100)
                self.assertIn("Код", allowed.text)
                denied = await linking.issue("tg", 11, -100)
                self.assertIn("недоступен", denied.text)

        asyncio.run(run())

    def test_chat_is_not_linkable(self) -> None:
        discovery.register_max_channel(-800, title="Group", status="added", added_by=20)

        async def run() -> None:
            with mock.patch.object(linking, "_api_is_admin", new=mock.AsyncMock(return_value=True)):
                screen = await linking.menu("max", 20)
                self.assertFalse(screen.buttons)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
