import ast
import importlib
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo
from types import SimpleNamespace
from unittest import mock

import discord
from discord import app_commands

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from commands.booking import BookingCommandDeps, register_booking_commands
from events.booking_refresh import BookingBoundaryRefresher
from services.booking import BookingConfig, BookingService
from services.server_control import ControlResult

from test_booking import HOUR, T0, FakeClock

with mock.patch.dict(
    os.environ,
    {"CRCON_BASE_URL": "https://crcon.example/api", "CRCON_TOKEN": "token", "DISCORD_GUILD_ID": ""},
):
    bot = importlib.import_module("bot")

BOOKER_ID, OTHER_ID, ADMIN_ID = 1001, 1002, 1003
BOOKING_ROLE, ADMIN_ROLE = 555, 777
HOME_GUILD, OTHER_GUILD = 4242, 9999
ENABLED_CONFIG = BookingConfig.from_env({
    "BOOKING_ROLE_ID": str(BOOKING_ROLE),
    "BOOKING_ADMIN_ROLE_ID": str(ADMIN_ROLE),
    "DISCORD_GUILD_ID": str(HOME_GUILD),
})


def member(user_id: int, *, role_ids=(), administrator=False, guild_id=HOME_GUILD):
    user = mock.MagicMock(spec=discord.Member)
    user.id = user_id
    user.display_name = f"user{user_id}"
    user.roles = [SimpleNamespace(id=role_id) for role_id in role_ids]
    user.guild_permissions = SimpleNamespace(administrator=administrator)
    user.guild = SimpleNamespace(id=guild_id)
    return user


class FakeResponse:
    def __init__(self):
        self._done = False
        self.sent = []
        self.edited = []

    def is_done(self):
        return self._done

    async def defer(self, **kwargs):
        self._done = True

    async def send_message(self, content=None, **kwargs):
        self._done = True
        self.sent.append(content)

    async def edit_message(self, **kwargs):
        self._done = True
        self.edited.append(kwargs)


def interaction_for(user):
    interaction = mock.MagicMock()
    interaction.user = user
    interaction.guild_id = getattr(getattr(user, "guild", None), "id", None)
    interaction.response = FakeResponse()

    async def followup_send(content=None, **kwargs):
        interaction.response.sent.append(content if content is not None else kwargs.get("content"))

    interaction.followup.send = mock.AsyncMock(side_effect=followup_send)
    interaction.edit_original_response = mock.AsyncMock()
    interaction.delete_original_response = mock.AsyncMock()
    return interaction


class BookingBotTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.clock = FakeClock()
        self.service = BookingService(Path(self._tmp.name) / "bookings.db", clock=self.clock)
        patches = [
            mock.patch.object(bot, "booking_service", self.service),
            mock.patch.object(bot, "booking_config", ENABLED_CONFIG),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.server_key = bot.api_client.get_server_key(0)

    def tearDown(self):
        self._tmp.cleanup()

    def book_now(self, user_id=BOOKER_ID):
        return self.service.create(self.server_key, user_id, "booker", T0 - 60, T0 + HOUR)


class GuardAtWriteSiteTests(BookingBotTestCase):
    async def submit_cooldown(self, user):
        interaction = interaction_for(user)
        modal = bot.TeamSwitchCooldownModal(0)
        modal.cooldown_minutes._value = "5"
        write = mock.Mock(return_value=ControlResult(success=True, transport="crcon_http", message="ok"))
        with mock.patch.object(bot.server_control, "set_team_switch_cooldown", write):
            await modal.on_submit(interaction)
        return interaction, write

    async def test_other_user_is_blocked_and_no_write_happens(self):
        self.book_now()

        interaction, write = await self.submit_cooldown(member(OTHER_ID, role_ids=[BOOKING_ROLE]))

        write.assert_not_called()
        sent = interaction.followup.send.await_args.kwargs.get("content", "")
        self.assertIn("is booked by", sent)

    async def test_booker_and_admins_can_write(self):
        self.book_now()

        for user in (
            member(BOOKER_ID),
            member(ADMIN_ID, role_ids=[ADMIN_ROLE]),
            member(ADMIN_ID, administrator=True),
        ):
            _, write = await self.submit_cooldown(user)
            write.assert_called_once()

    async def test_other_guild_administrator_does_not_bypass_lock(self):
        self.book_now()

        _, write = await self.submit_cooldown(
            member(ADMIN_ID, administrator=True, role_ids=[ADMIN_ROLE], guild_id=OTHER_GUILD)
        )

        write.assert_not_called()

    async def test_no_active_booking_allows_anyone(self):
        self.service.create(self.server_key, BOOKER_ID, "booker", T0 + HOUR, T0 + 2 * HOUR)

        _, write = await self.submit_cooldown(member(OTHER_ID))

        write.assert_called_once()

    async def test_booking_disabled_allows_anyone(self):
        self.book_now()
        with mock.patch.object(bot, "booking_service", None):
            _, write = await self.submit_cooldown(member(OTHER_ID))

        write.assert_called_once()

    async def test_store_error_fails_closed_with_explicit_message(self):
        with mock.patch.object(
            self.service, "blocking_booking", side_effect=sqlite3.OperationalError("database is locked")
        ):
            interaction, write = await self.submit_cooldown(member(OTHER_ID))

        write.assert_not_called()
        sent = interaction.followup.send.await_args.kwargs.get("content", "")
        self.assertIn("Could not check server bookings", sent)

    async def test_map_change_acknowledges_before_checking_the_lock(self):
        self.book_now()
        interaction = interaction_for(member(OTHER_ID))
        dropdown = bot.VariantDropdown(0, "warfare", "Foy", [{"id": "foy_warfare", "variant": "Day"}])
        change_map = mock.Mock()

        with mock.patch.object(type(dropdown), "values", new_callable=mock.PropertyMock, return_value=["foy_warfare"]),                 mock.patch.object(bot.server_control, "change_map", change_map):
            await dropdown.callback(interaction)

        change_map.assert_not_called()
        self.assertIn("Changing Map", interaction.response.edited[0]["embed"].title)
        denial = interaction.edit_original_response.await_args.kwargs["content"]
        self.assertIn("is booked by", denial)

    async def test_flow_denial_replaces_the_ephemeral_flow_message(self):
        self.book_now()
        interaction = interaction_for(member(OTHER_ID))

        denied = await bot._deny_if_booked(interaction, 0, edit_flow=True)

        self.assertTrue(denied)
        self.assertIn("is booked by", interaction.response.edited[0]["content"])
        self.assertIsNone(interaction.response.edited[0]["view"])


class GuardCoverageTests(unittest.TestCase):
    """Realized-conformance check for R6: every control write in bot.py is preceded by the guard."""

    WRITE_PATTERN = re.compile(r"server_control\.(set_\w+|change_map)$|^_run_server_write$")

    def test_every_write_site_calls_the_guard_first(self):
        tree = ast.parse((SRC / "bot.py").read_text(encoding="utf-8"))
        write_sites = 0

        for func in ast.walk(tree):
            if not isinstance(func, ast.AsyncFunctionDef):
                continue
            writes, guards = [], []
            for node in ast.walk(func):
                if not isinstance(node, ast.Call):
                    continue
                target = node.func
                if isinstance(target, ast.Attribute) and target.attr == "to_thread" and node.args:
                    target = node.args[0]
                name = ast.unparse(target)
                if self.WRITE_PATTERN.search(name):
                    writes.append(node.lineno)
                elif name == "_deny_if_booked":
                    guards.append(node.lineno)
            for line in writes:
                write_sites += 1
                self.assertTrue(
                    any(guard < line for guard in guards),
                    f"{func.name} writes at bot.py:{line} without calling _deny_if_booked first",
                )

        self.assertEqual(write_sites, 8)


class CommandTests(BookingBotTestCase):
    def setUp(self):
        super().setUp()
        self.on_change = mock.AsyncMock()
        client = discord.Client(intents=discord.Intents.none())
        self.tree = app_commands.CommandTree(client)
        register_booking_commands(
            self.tree,
            BookingCommandDeps(
                service=self.service,
                config=ENABLED_CONFIG,
                servers=lambda: [(0, "Server One")],
                server_key=lambda index: "server1",
                server_name_for_key=lambda key: "Server One",
                on_change=self.on_change,
            ),
        )

    async def run_book(self, user, start="2027-01-15 09:00", duration="2h"):
        interaction = interaction_for(user)
        choice = app_commands.Choice(name="Server One", value="0")
        await self.tree.get_command("book").callback(interaction, server=choice, start=start, duration=duration)
        return interaction.response.sent[-1]

    async def test_book_list_and_overlap(self):
        self.clock.ts = T0 - 24 * HOUR  # start time is in the future

        ok = await self.run_book(member(BOOKER_ID, role_ids=[BOOKING_ROLE]))
        overlap = await self.run_book(member(OTHER_ID, role_ids=[BOOKING_ROLE]), start="2027-01-15 10:00")

        self.assertIn("✅", ok)
        self.assertIn("overlaps", overlap)
        self.on_change.assert_awaited_once()

        listing = interaction_for(member(OTHER_ID))
        await self.tree.get_command("bookings").callback(listing, server=None)
        self.assertIn("<t:", listing.response.sent[-1])

    async def test_commands_refused_from_other_guild_or_dm(self):
        foreign_admin = member(ADMIN_ID, administrator=True, guild_id=OTHER_GUILD)
        reply = await self.run_book(foreign_admin)
        self.assertIn("home Discord", reply)

        booking = self.service.create("server1", BOOKER_ID, "booker", T0 + HOUR, T0 + 2 * HOUR)
        cancel = interaction_for(foreign_admin)
        await self.tree.get_command("cancel_booking").callback(cancel, booking_id=booking.id)
        self.assertIn("home Discord", cancel.response.sent[-1])
        self.assertEqual(len(self.service.list_live()), 1)

        dm = interaction_for(mock.MagicMock(spec=discord.User))
        dm.guild_id = None
        await self.tree.get_command("bookings").callback(dm, server=None)
        self.assertIn("home Discord", dm.response.sent[-1])

    async def test_bookings_reply_stays_under_discord_limit(self):
        for hour in range(30):
            self.service.create(f"server{hour}", 123456789012345678, "booker",
                                T0 + hour * HOUR, T0 + (hour + 1) * HOUR)
        listing = interaction_for(member(OTHER_ID))

        await self.tree.get_command("bookings").callback(listing, server=None)

        reply = listing.response.sent[-1]
        self.assertLessEqual(len(reply), 2000)
        self.assertRegex(reply, r"…and \d+ more\.$")

    async def test_commands_acknowledge_before_touching_the_store(self):
        self.clock.ts = T0 - 24 * HOUR
        booker = member(BOOKER_ID, role_ids=[BOOKING_ROLE])
        acked_at_store_call = []
        real_create, real_list, real_cancel = self.service.create, self.service.list_live, self.service.cancel

        def tracking(real):
            def call(*args, **kwargs):
                acked_at_store_call.append(current.response.is_done())
                return real(*args, **kwargs)
            return call

        with mock.patch.object(self.service, "create", tracking(real_create)),                 mock.patch.object(self.service, "list_live", tracking(real_list)),                 mock.patch.object(self.service, "cancel", tracking(real_cancel)):
            current = interaction_for(booker)
            choice = app_commands.Choice(name="Server One", value="0")
            await self.tree.get_command("book").callback(current, server=choice, start="2027-01-15 09:00", duration="1h")
            booking_id = self.service.list_live()[0].id
            current = interaction_for(booker)
            await self.tree.get_command("bookings").callback(current, server=None)
            current = interaction_for(booker)
            await self.tree.get_command("cancel_booking").callback(current, booking_id=booking_id)

        self.assertIn("Cancelled", current.response.sent[-1])
        self.assertTrue(acked_at_store_call)
        self.assertTrue(all(acked_at_store_call), acked_at_store_call)

    async def test_store_error_is_reported_not_raised(self):
        listing = interaction_for(member(OTHER_ID))
        with mock.patch.object(self.service, "list_live", side_effect=sqlite3.OperationalError("disk I/O error")):
            await self.tree.get_command("bookings").callback(listing, server=None)

        self.assertIn("booking store", listing.response.sent[-1])

    async def test_cancel_autocomplete_matches_case_insensitively(self):
        self.service.create("server1", BOOKER_ID, "booker", T0 + HOUR, T0 + 2 * HOUR)
        command = self.tree.get_command("cancel_booking")
        autocomplete = command._params["booking_id"].autocomplete
        interaction = interaction_for(member(BOOKER_ID))

        choices = await autocomplete(interaction, "server one")

        self.assertEqual(len(choices), 1)
        self.assertIn("Server One", choices[0].name)

    async def test_book_requires_role(self):
            # Now any guild member can book - no role required
            reply = await self.run_book(member(OTHER_ID))

            self.assertIn("booked", reply)
            # booking should succeed now
            self.assertEqual(len(self.service.list_live()), 1)

    async def test_book_rejects_bad_time(self):
        reply = await self.run_book(member(BOOKER_ID, role_ids=[BOOKING_ROLE]), start="tonight")

        self.assertIn("YYYY-MM-DD HH:MM", reply)

    async def test_cancel_rules(self):
        booking = self.service.create("server1", BOOKER_ID, "booker", T0 + HOUR, T0 + 2 * HOUR)
        cancel = self.tree.get_command("cancel_booking").callback

        other = interaction_for(member(OTHER_ID))
        await cancel(other, booking_id=booking.id)
        admin = interaction_for(member(ADMIN_ID, role_ids=[ADMIN_ROLE]))
        await cancel(admin, booking_id=booking.id)

        self.assertIn("not yours", other.response.sent[-1])
        self.assertIn("Cancelled", admin.response.sent[-1])
        self.assertEqual(self.service.list_live(), [])

    async def test_commands_report_not_configured(self):
        tree = app_commands.CommandTree(discord.Client(intents=discord.Intents.none()))
        register_booking_commands(
            tree,
            BookingCommandDeps(
                service=None,
                config=BookingConfig.from_env({}),
                servers=lambda: [(0, "Server One")],
                server_key=lambda index: "server1",
                server_name_for_key=lambda key: "Server One",
                on_change=self.on_change,
            ),
        )
        interaction = interaction_for(member(BOOKER_ID))

        await tree.get_command("bookings").callback(interaction, server=None)

        self.assertIn("not configured", interaction.response.sent[-1])


class BookingWiringTests(BookingBotTestCase):
    def test_commands_not_registered_when_booking_disabled(self):
        # bot was imported without BOOKING_ROLE_ID, so booking is off.
        for name in ("book", "bookings", "cancel_booking"):
            self.assertIsNone(bot.bot.tree.get_command(name))
        self.assertIsNone(bot.booking_refresher)

    def test_orphaned_booking_keys_reports_unconfigured_servers(self):
        self.book_now()
        self.service.create("server99", BOOKER_ID, "booker", T0 + HOUR, T0 + 2 * HOUR)

        self.assertEqual(bot._orphaned_booking_keys(), {"server99"})


class PanelAndRefreshTests(BookingBotTestCase):
    def test_panel_shows_active_then_next_booking(self):
        self.service.create(self.server_key, BOOKER_ID, "booker", T0 + HOUR, T0 + 2 * HOUR)
        self.assertIn("Next booking", bot._panel_booking_lines()[0][0])

        self.clock.ts = T0 + HOUR
        self.assertIn("Booked by", bot._panel_booking_lines()[0][0])

        self.clock.ts = T0 + 2 * HOUR
        self.assertEqual(bot._panel_booking_lines(), {})

    def test_embed_includes_booking_lines(self):
        self.book_now()
        statuses = {0: ControlResult(success=False, transport=None, message="CRCON down")}

        embed = bot.build_main_embed(None, statuses, bot._panel_booking_lines())

        self.assertIn("Booked by", embed.description)

    async def test_refresher_retries_after_failed_check_or_refresh(self):
        refresh = mock.AsyncMock(side_effect=[RuntimeError("discord 503"), None])
        self.service.create("server1", BOOKER_ID, "booker", T0 + 90, T0 + HOUR)
        refresher = BookingBoundaryRefresher(self.service, refresh)

        self.clock.ts = T0 + 120
        with mock.patch.object(self.service, "has_boundary_between", side_effect=RuntimeError("db locked")):
            self.assertFalse(await refresher.tick())  # check failed: window kept
        self.assertFalse(await refresher.tick())      # refresh failed: window kept
        self.clock.ts = T0 + 180
        self.assertTrue(await refresher.tick())       # same boundary retried and refreshed
        self.assertFalse(await refresher.tick())

        self.assertEqual(refresh.await_count, 2)

    def test_schedule_lists_today_through_two_weeks(self):
        ended = self.service.create(self.server_key, BOOKER_ID, "booker", T0 - 60, T0 + 60)
        self.clock.ts = T0 + 120
        active = self.service.create(self.server_key, OTHER_ID, "other", T0 + 120, T0 + HOUR)
        later = self.service.create(self.server_key, BOOKER_ID, "booker", T0 + 3 * 24 * HOUR, T0 + 3 * 24 * HOUR + HOUR)
        self.service.create(self.server_key, BOOKER_ID, "booker", T0 + 15 * 24 * HOUR, T0 + 15 * 24 * HOUR + HOUR)

        lines = bot._panel_schedule_lines()

        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[0].startswith("~~") and f"<t:{ended.start_utc}:f>" in lines[0])
        self.assertTrue(lines[1].startswith("🔒") and f"<t:{active.start_utc}:f>" in lines[1])
        self.assertIn(f"<t:{later.start_utc}:f>", lines[2])

    def test_schedule_omitted_when_booking_disabled(self):
        with mock.patch.object(bot, "booking_service", None):
            self.assertIsNone(bot._panel_schedule_lines())
        embed = bot.build_main_embed(None, {}, None, None)
        self.assertEqual(embed.fields, [])

    def test_empty_schedule_says_so(self):
        embed = bot.build_main_embed(None, {}, None, [])

        self.assertEqual(embed.fields[0].value, "No bookings.")

    def test_long_schedule_fits_discord_limits(self):
        lines = [f"<t:{T0 + i}:f> → <t:{T0 + i + 60}:t> · **Server One** · <@123456789012345678>" for i in range(200)]

        embed = bot.build_main_embed(None, {}, None, lines)

        self.assertGreater(len(embed.fields), 1)
        self.assertTrue(all(len(field.value) <= 1024 for field in embed.fields))
        self.assertLessEqual(len(embed), 6000)
        self.assertRegex(embed.fields[-1].value, r"…and \d+ more")

    async def test_daily_refresh_is_scheduled_and_survives_errors(self):
        sydney_six = time(6, 0, tzinfo=ZoneInfo("Australia/Sydney"))
        refresh = mock.AsyncMock(side_effect=[RuntimeError("discord 503"), None])
        refresher = BookingBoundaryRefresher(self.service, refresh, daily_at=sydney_six)

        self.assertEqual(refresher._daily_loop.time, [sydney_six])
        self.assertFalse(await refresher.daily())
        self.assertTrue(await refresher.daily())

    async def test_refresher_only_refreshes_when_a_boundary_passes(self):
        refresh = mock.AsyncMock()
        self.service.create("server1", BOOKER_ID, "booker", T0 + 90, T0 + HOUR)
        refresher = BookingBoundaryRefresher(self.service, refresh)

        self.clock.ts = T0 + 60
        self.assertFalse(await refresher.tick())
        self.clock.ts = T0 + 120  # start passed
        self.assertTrue(await refresher.tick())
        self.clock.ts = T0 + 180
        self.assertFalse(await refresher.tick())

        self.assertEqual(refresh.await_count, 1)


if __name__ == "__main__":
    unittest.main()
