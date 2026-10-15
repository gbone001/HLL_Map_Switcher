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
    def __init__(self):
        self.copied_to = None
        self.synced_guild = "unset"

    def copy_global_to(self, *, guild):
        self.copied_to = guild

    async def sync(self, *, guild=None):
        self.synced_guild = guild
        return ["repost_button"]


class SyncAppCommandsTests(unittest.IsolatedAsyncioTestCase):
    async def test_syncs_to_configured_guild(self):
        tree = FakeTree()

        synced = await bot.sync_app_commands(tree, "1234")

        self.assertEqual(synced, ["repost_button"])
        self.assertEqual(tree.copied_to.id, 1234)
        self.assertEqual(tree.synced_guild.id, 1234)

    async def test_syncs_globally_without_guild(self):
        tree = FakeTree()

        await bot.sync_app_commands(tree, "")

        self.assertIsNone(tree.copied_to)
        self.assertIsNone(tree.synced_guild)

    async def test_invalid_guild_falls_back_to_global(self):
        tree = FakeTree()

        await bot.sync_app_commands(tree, "not-a-number")

        self.assertIsNone(tree.synced_guild)

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
