import asyncio
import importlib
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from services.server_control import ControlResult

# bot.py builds its server list at import time, so give it a minimal single-server config.
with mock.patch.dict(
    os.environ,
    {"CRCON_BASE_URL": "https://crcon.example/api", "CRCON_TOKEN": "token", "DISCORD_GUILD_ID": ""},
):
    bot = importlib.import_module("bot")


class FakeTree:
    def __init__(self, fail_for=()):
        self.copied_to = []
        self.synced = []
        self.fail_for = set(fail_for)

    def copy_global_to(self, *, guild):
        self.copied_to.append(guild.id)

    async def sync(self, *, guild=None):
        if guild is not None and guild.id in self.fail_for:
            raise bot.discord.HTTPException(mock.Mock(status=403, reason="Forbidden"), "Missing Access")
        self.synced.append(guild.id if guild else None)
        return [f"repost_button@{guild.id if guild else 'global'}"]


def registry(home="", guests=""):
    return bot.GuildRegistry.from_env({"DISCORD_GUILD_ID": home, "BOOKING_GUILD_IDS": guests})


class SyncAppCommandsTests(unittest.IsolatedAsyncioTestCase):
    async def test_syncs_to_home_and_every_guest_guild(self):
        tree = FakeTree()

        synced = await bot.sync_app_commands(tree, registry("1234", "5678, 9012"))

        self.assertEqual(synced, ["repost_button@1234"])
        self.assertEqual(tree.copied_to, [1234, 5678, 9012])
        self.assertEqual(tree.synced, [1234, 5678, 9012])

    async def test_one_failing_guild_does_not_block_the_others(self):
        tree = FakeTree(fail_for={5678})

        await bot.sync_app_commands(tree, registry("1234", "5678, 9012"))

        self.assertEqual(tree.synced, [1234, 9012])

    async def test_syncs_globally_without_home_guild(self):
        tree = FakeTree()

        await bot.sync_app_commands(tree, registry())

        self.assertEqual(tree.copied_to, [])
        self.assertEqual(tree.synced, [None])

    async def test_invalid_home_guild_falls_back_to_global(self):
        tree = FakeTree()

        await bot.sync_app_commands(tree, registry("not-a-number"))

        self.assertEqual(tree.synced, [None])

    async def test_setup_hook_syncs_the_repost_command(self):
        self.assertIn("repost_button", [command.name for command in bot.bot.tree.get_commands()])
        with mock.patch.object(bot, "sync_app_commands", new=mock.AsyncMock()) as sync:
            await bot.bot.setup_hook()
        sync.assert_awaited_once_with(bot.bot.tree)


class SlowStatus:
    """Stands in for a CRCON call that blocks; records the thread it ran on."""

    def __init__(self, delay: float = 0.3):
        self.delay = delay
        self.threads = []

    def __call__(self, server_index):
        self.threads.append(threading.current_thread())
        time.sleep(self.delay)
        return ControlResult(success=False, transport=None, message="CRCON down")


class OffEventLoopTests(unittest.IsolatedAsyncioTestCase):
    async def assert_loop_stays_responsive(self, coro, *, min_ticks: int = 5) -> None:
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1

        ticker_task = asyncio.create_task(ticker())
        try:
            await coro
        finally:
            ticker_task.cancel()
        self.assertGreaterEqual(ticks, min_ticks, "event loop was blocked while waiting on network I/O")

    async def test_build_panel_reads_status_off_the_event_loop(self):
        slow_status = SlowStatus()
        with mock.patch.object(bot.server_control, "get_status", side_effect=slow_status):
            await self.assert_loop_stays_responsive(bot.build_panel())

        self.assertTrue(slow_status.threads)
        self.assertNotIn(threading.main_thread(), slow_status.threads)

    async def test_build_panel_disables_crcon_controls_on_outage(self):
        with mock.patch.object(bot.server_control, "get_status", side_effect=SlowStatus(delay=0)):
            embed, view = await bot.build_panel()

        self.assertIn("Issues detected", embed.description)
        crcon_buttons = [
            child for child in view.children if getattr(child, "custom_id", None) in bot.CRCON_PANEL_DEPENDENT_BUTTONS
        ]
        self.assertTrue(crcon_buttons)
        self.assertTrue(all(button.disabled for button in crcon_buttons))

    async def test_team_switch_modal_defers_before_slow_call(self):
        events = []

        class FakeResponse:
            def __init__(self):
                self._done = False

            def is_done(self):
                return self._done

            async def defer(self, **kwargs):
                events.append("defer")
                self._done = True

        interaction = mock.MagicMock()
        interaction.response = FakeResponse()
        interaction.followup.send = mock.AsyncMock(return_value=None)

        def slow_cooldown(server_index, minutes):
            events.append(("set_cooldown", threading.current_thread() is threading.main_thread()))
            time.sleep(0.3)
            return ControlResult(success=True, transport="crcon_http", message="ok")

        modal = bot.TeamSwitchCooldownModal(0)
        modal.cooldown_minutes._value = "5"
        with mock.patch.object(bot.server_control, "set_team_switch_cooldown", side_effect=slow_cooldown):
            await self.assert_loop_stays_responsive(modal.on_submit(interaction))

        self.assertEqual(events, ["defer", ("set_cooldown", False)])
        interaction.followup.send.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()


HOME, GUEST = 100, 200


def fake_channel(channel_id, guild_id, message_id=None):
    channel = mock.MagicMock(id=channel_id)
    channel.guild = mock.MagicMock(id=guild_id)
    partial = mock.MagicMock()
    partial.edit = mock.AsyncMock(return_value=f"edited {channel_id}")
    partial.delete = mock.AsyncMock()
    channel.get_partial_message.return_value = partial
    channel.send = mock.AsyncMock(return_value=mock.MagicMock(id=message_id))
    channel.history.side_effect = AssertionError("history should not be read")
    return channel


class PanelMessageTests(unittest.IsolatedAsyncioTestCase):
    """Multi-guild slice 4: one saved panel per guild."""

    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "panel_message.json"
        patches = [
            mock.patch.object(bot, "_PANEL_REF_PATH", self.path),
            mock.patch.object(bot, "panel_refs", {}),
            mock.patch.object(bot, "guild_registry", registry(str(HOME), str(GUEST))),
            mock.patch.object(bot, "build_panel", mock.AsyncMock(return_value=("embed", "view"))),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_panels_survive_a_restart_per_guild(self):
        bot._set_panel_ref(HOME, 11, 22)
        bot._set_panel_ref(GUEST, 33, 44)

        self.assertEqual(bot._load_panel_refs(), {HOME: (11, 22), GUEST: (33, 44)})

    def test_old_single_panel_file_becomes_the_home_guild_panel(self):
        self.path.write_text('{"channel_id": 11, "message_id": 22}', encoding="utf-8")

        self.assertEqual(bot._load_panel_refs(), {HOME: (11, 22)})

    def test_panel_channel_per_guild_with_env_fallback_for_home_only(self):
        with mock.patch.dict(os.environ, {"DISCORD_CHANNEL_ID": "99"}):
            self.assertEqual(bot._panel_channel_id(HOME), 99)
            self.assertIsNone(bot._panel_channel_id(GUEST))
            bot._set_panel_ref(HOME, 11, 22)
            bot._set_panel_ref(GUEST, 33, 44)
            self.assertEqual(bot._panel_channel_id(HOME), 11)
            self.assertEqual(bot._panel_channel_id(GUEST), 33)
            self.assertEqual(bot._panel_channel_ids(), [11, 33])

    async def test_post_panel_replaces_only_this_guilds_panel(self):
        bot._set_panel_ref(HOME, 11, 22)
        bot._set_panel_ref(GUEST, 33, 44)
        old_guest = fake_channel(33, GUEST)
        new_guest = fake_channel(55, GUEST, message_id=66)

        with mock.patch.object(bot, "_resolve_text_channel", mock.AsyncMock(return_value=old_guest)):
            await bot.post_panel(new_guest)

        old_guest.get_partial_message.assert_called_once_with(44)
        old_guest.get_partial_message.return_value.delete.assert_awaited_once()
        self.assertEqual(bot._load_panel_refs(), {HOME: (11, 22), GUEST: (55, 66)})

    async def test_refresh_updates_every_guild_panel_with_one_status_read(self):
        bot._set_panel_ref(HOME, 11, 22)
        bot._set_panel_ref(GUEST, 33, 44)
        channels = {11: fake_channel(11, HOME), 33: fake_channel(33, GUEST)}
        statuses = mock.AsyncMock(return_value={"status": "ok"})

        with mock.patch.object(bot, "_resolve_text_channel", mock.AsyncMock(side_effect=channels.get)), \
                mock.patch.object(bot, "fetch_server_statuses", statuses):
            messages = await bot.refresh_main_embed()

        self.assertEqual(messages, ["edited 11", "edited 33"])
        statuses.assert_awaited_once()
        built_for = [call.args[1] for call in bot.build_panel.await_args_list]
        self.assertEqual(built_for, [HOME, GUEST])
        channels[11].get_partial_message.assert_called_once_with(22)
        channels[33].get_partial_message.assert_called_once_with(44)

    async def test_unapproved_guild_panels_are_not_refreshed(self):
        bot._set_panel_ref(HOME, 11, 22)
        bot._set_panel_ref(999, 77, 88)

        self.assertEqual(bot._panel_channel_ids(), [11])


class GuildApprovalTests(unittest.IsolatedAsyncioTestCase):
    def fake_guild(self, guild_id):
        guild = mock.MagicMock(id=guild_id)
        guild.name = f"guild {guild_id}"
        guild.leave = mock.AsyncMock()
        return guild

    async def test_unlisted_guild_is_left_and_listed_ones_kept(self):
        with mock.patch.object(bot, "guild_registry", registry("100", "200")):
            for guild_id, should_leave in ((100, False), (200, False), (300, True)):
                guild = self.fake_guild(guild_id)
                self.assertEqual(await bot._leave_if_unapproved(guild), should_leave)
                self.assertEqual(guild.leave.await_count, int(should_leave))

    async def test_nothing_is_left_without_a_home_guild(self):
        guild = self.fake_guild(300)
        with mock.patch.object(bot, "guild_registry", registry()):
            self.assertFalse(await bot._leave_if_unapproved(guild))
        guild.leave.assert_not_awaited()
