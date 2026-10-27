import sys
import tempfile
import threading
import unittest
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from services.booking import (
    BookingConfig,
    BookingError,
    BookingNotFoundError,
    BookingOverlapError,
    BookingPermissionError,
    BookingService,
    parse_duration,
    parse_start,
    schedule_window,
)

T0 = 1_800_000_000  # fixed "now" for tests (2027-01-15 UTC)
HOUR = 3600


class FakeClock:
    def __init__(self, ts: int = T0):
        self.ts = ts

    def __call__(self) -> datetime:
        return datetime.fromtimestamp(self.ts, timezone.utc)


class BookingServiceTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "bookings.db"
        self.clock = FakeClock()
        self.service = BookingService(self.db_path, clock=self.clock)

    def tearDown(self):
        self._tmp.cleanup()

    def book(self, start_offset_h: float, hours: float, user_id: int = 1, server_key: str = "server1"):
        start = T0 + int(start_offset_h * HOUR)
        return self.service.create(server_key, user_id, f"user{user_id}", start, start + int(hours * HOUR))


class CreateAndListTests(BookingServiceTestCase):
    def test_create_then_list(self):
        booking = self.book(1, 2)

        live = self.service.list_live()

        self.assertEqual([b.id for b in live], [booking.id])
        self.assertEqual(live[0].end_utc - live[0].start_utc, 2 * HOUR)

    def test_bookings_survive_a_new_service_instance(self):
        booking = self.book(1, 2)

        reopened = BookingService(self.db_path, clock=self.clock)

        self.assertEqual([b.id for b in reopened.list_live()], [booking.id])

    def test_list_filters_by_server_and_hides_ended_and_cancelled(self):
        kept = self.book(1, 1, server_key="server1")
        self.book(1, 1, server_key="server2")
        cancelled = self.book(3, 1, server_key="server1")
        self.service.cancel(cancelled.id, actor_id=1, is_admin=False)
        ended = self.book(0, 0.5, server_key="server1")
        self.clock.ts = T0 + int(0.75 * HOUR)

        live = self.service.list_live("server1")

        self.assertEqual([b.id for b in live], [kept.id])
        self.assertNotIn(ended.id, [b.id for b in live])

    def test_max_duration_and_horizon(self):
        limited = BookingService(self.db_path, clock=self.clock, max_duration_seconds=4 * HOUR,
                                 max_lead_seconds=7 * 86400)

        with self.assertRaises(BookingError) as too_long:
            limited.create("server1", 1, "u", T0 + HOUR, T0 + 6 * HOUR)
        with self.assertRaises(BookingError) as too_far:
            limited.create("server1", 1, "u", T0 + 8 * 86400, T0 + 8 * 86400 + HOUR)
        limited.create("server1", 1, "u", T0 + HOUR, T0 + 5 * HOUR)  # exactly the limit is allowed

        self.assertIn("4 hours", str(too_long.exception))
        self.assertIn("7 days", str(too_far.exception))

    def test_rejects_past_start_and_non_positive_length(self):
        with self.assertRaises(BookingError):
            self.service.create("server1", 1, "u", T0 - 2 * HOUR, T0 - HOUR)
        with self.assertRaises(BookingError):
            self.service.create("server1", 1, "u", T0 + HOUR, T0 + HOUR)


class SeriesTests(BookingServiceTestCase):
    def test_weekly_series_creates_each_occurrence(self):
        series = self.service.create_series("server1", 1, "u", T0 + HOUR, T0 + 2 * HOUR, "weekly", 3)

        self.assertEqual([b.start_utc - T0 for b in series], [HOUR, HOUR + 7 * 86400, HOUR + 14 * 86400])
        self.assertEqual([b.id for b in self.service.list_live()], [b.id for b in series])

    def test_series_is_all_or_nothing_on_overlap(self):
        blocker = self.service.create("server1", 2, "other", T0 + 86400 + HOUR, T0 + 86400 + 2 * HOUR)

        with self.assertRaises(BookingOverlapError):
            self.service.create_series("server1", 1, "u", T0 + HOUR, T0 + 2 * HOUR, "daily", 3)

        self.assertEqual([b.id for b in self.service.list_live()], [blocker.id])

    def test_no_recurrence_ignores_count_and_bad_input_is_rejected(self):
        self.assertEqual(len(self.service.create_series("server1", 1, "u", T0 + HOUR, T0 + 2 * HOUR, "none", 5)), 1)
        with self.assertRaises(BookingError):
            self.service.create_series("server1", 1, "u", T0 + HOUR, T0 + 2 * HOUR, "daily", 0)
        with self.assertRaises(BookingError):
            self.service.create_series("server1", 1, "u", T0 + HOUR, T0 + 2 * HOUR, "monthly", 2)


class OverlapTests(BookingServiceTestCase):
    def test_overlap_on_same_server_is_rejected_with_conflict(self):
        first = self.book(1, 2, user_id=1)

        with self.assertRaises(BookingOverlapError) as ctx:
            self.book(2, 1, user_id=2)

        self.assertEqual(ctx.exception.conflict.id, first.id)

    def test_back_to_back_bookings_are_allowed(self):
        self.book(1, 1)
        self.book(2, 1)
        self.book(0, 1)

        self.assertEqual(len(self.service.list_live()), 3)

    def test_same_time_on_other_server_is_allowed(self):
        self.book(1, 2, server_key="server1")
        self.book(1, 2, server_key="server2")

    def test_cancelled_booking_frees_the_slot(self):
        first = self.book(1, 2, user_id=1)
        self.service.cancel(first.id, actor_id=1, is_admin=False)

        self.book(1, 2, user_id=2)

    def test_concurrent_overlapping_creates_only_one_succeeds(self):
        results = []
        barrier = threading.Barrier(5)

        def attempt(user_id):
            service = BookingService(self.db_path, clock=self.clock)
            barrier.wait()
            try:
                service.create("server1", user_id, "u", T0 + HOUR, T0 + 2 * HOUR)
                results.append("ok")
            except BookingOverlapError:
                results.append("overlap")

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(results.count("ok"), 1)
        self.assertEqual(results.count("overlap"), 4)


class ActiveAndLockTests(BookingServiceTestCase):
    def test_active_for(self):
        current = self.book(0, 1, user_id=1)
        self.book(2, 1, user_id=2)

        self.assertEqual(self.service.active_for("server1").id, current.id)

        self.clock.ts = T0 + HOUR  # end is exclusive
        self.assertIsNone(self.service.active_for("server1"))

    def test_summary_by_server_returns_active_and_next(self):
        current = self.book(0, 1, user_id=1, server_key="server1")
        upcoming = self.book(2, 1, user_id=2, server_key="server1")
        later = self.book(5, 1, user_id=3, server_key="server1")
        other = self.book(3, 1, user_id=4, server_key="server2")

        summary = self.service.summary_by_server()

        self.assertEqual((summary["server1"][0].id, summary["server1"][1].id), (current.id, upcoming.id))
        self.assertEqual((summary["server2"][0], summary["server2"][1].id), (None, other.id))
        self.assertNotIn(later.id, [b.id for pair in summary.values() for b in pair if b])

    def test_control_check_rules(self):
        """Decision table for multi-guild slice 3 (docs/multi-guild-slices.md)."""
        HOME, GUEST, THIRD = 10, 20, 30
        BOOKER, OTHER = 1, 2
        booking = self.service.create("server1", BOOKER, "u", T0, T0 + HOUR, guild_id=GUEST)

        def check(user, guild, *, admin=False, guild_admin=None, server="server1"):
            decision = self.service.control_check(
                server, user, admin, guild_id=guild, admin_guild_id=guild_admin,
            )
            return decision.allowed, decision.reason

        cases = [
            # (description, kwargs, expected (allowed, reason))
            ("booker from the booking guild", dict(user=BOOKER, guild=GUEST), (True, "booker")),
            ("booking guild's admin", dict(user=OTHER, guild=GUEST, guild_admin=GUEST), (True, "booking_guild_admin")),
            ("booking guild member, not booker", dict(user=OTHER, guild=GUEST), (False, "booked")),
            ("home member during a guest booking", dict(user=OTHER, guild=HOME), (False, "booked")),
            ("home guild admin (not booking admin)", dict(user=OTHER, guild=HOME, guild_admin=HOME), (False, "booked")),
            ("third guild's admin", dict(user=OTHER, guild=THIRD, guild_admin=THIRD), (False, "booked")),
            ("booker acting from another guild", dict(user=BOOKER, guild=HOME), (False, "booked_from_other_guild")),
            ("home booking admin overrides", dict(user=OTHER, guild=HOME, admin=True), (True, "admin")),
            ("unbooked server, booker of another server", dict(user=BOOKER, guild=GUEST, server="server2"), (False, "unbooked")),
            ("unbooked server, home booking admin", dict(user=OTHER, guild=HOME, admin=True, server="server2"), (True, "admin")),
        ]
        for description, kwargs, expected in cases:
            with self.subTest(description):
                self.assertEqual(check(**kwargs), expected)

        self.clock.ts = T0 + HOUR  # booking over
        self.assertEqual(check(BOOKER, GUEST), (False, "unbooked"))

    def test_control_check_without_guilds_keeps_single_guild_rules(self):
        booking = self.book(0, 1, user_id=1)

        self.assertTrue(self.service.control_check("server1", 1, False).allowed)
        self.assertFalse(self.service.control_check("server1", 2, False).allowed)
        self.assertTrue(self.service.control_check("server1", 2, True).allowed)
        self.assertEqual(self.service.control_check("server1", 1, False).active, booking)

    def test_owner_can_cancel(self):
        booking = self.book(1, 1, user_id=1)

        cancelled = self.service.cancel(booking.id, actor_id=1, is_admin=False)

        self.assertIsNotNone(cancelled.cancelled_at)
        self.assertEqual(cancelled.cancelled_by, 1)

    def test_non_owner_cannot_cancel_but_admin_can(self):
        booking = self.book(1, 1, user_id=1)

        with self.assertRaises(BookingPermissionError):
            self.service.cancel(booking.id, actor_id=2, is_admin=False)
        self.assertEqual(self.service.cancel(booking.id, actor_id=99, is_admin=True).cancelled_by, 99)

    def test_cancel_missing_cancelled_or_ended(self):
        booking = self.book(0, 1)
        with self.assertRaises(BookingNotFoundError):
            self.service.cancel(9999, actor_id=1, is_admin=True)
        self.clock.ts = T0 + 2 * HOUR
        with self.assertRaises(BookingNotFoundError):
            self.service.cancel(booking.id, actor_id=1, is_admin=False)

    def test_losing_a_concurrent_cancel_reports_not_found(self):
        booking = self.book(1, 1, user_id=1)
        real_get = self.service.get

        def stale_get(booking_id):
            # The admin's read happens before the owner's cancel lands.
            stale = real_get(booking_id)
            self.service.get = real_get
            BookingService.cancel(self.service, booking_id, actor_id=1, is_admin=False)
            return stale

        self.service.get = stale_get
        with self.assertRaises(BookingNotFoundError):
            self.service.cancel(booking.id, actor_id=99, is_admin=True)
        self.assertEqual(self.service.get(booking.id).cancelled_by, 1)


class BoundaryTests(BookingServiceTestCase):
    def test_detects_start_and_end_inside_window_only(self):
        self.book(1, 1)  # starts T0+1h, ends T0+2h

        self.assertFalse(self.service.has_boundary_between(T0, T0 + HOUR - 1))
        self.assertTrue(self.service.has_boundary_between(T0, T0 + HOUR))
        self.assertFalse(self.service.has_boundary_between(T0 + HOUR, T0 + 2 * HOUR - 1))
        self.assertTrue(self.service.has_boundary_between(T0 + HOUR, T0 + 2 * HOUR))

    def test_cancelled_booking_has_no_boundaries(self):
        booking = self.book(1, 1)
        self.service.cancel(booking.id, actor_id=1, is_admin=False)

        self.assertFalse(self.service.has_boundary_between(T0, T0 + 3 * HOUR))


class ParsingTests(unittest.TestCase):
    def test_parse_start_uses_booking_time_zone_including_dst(self):
        sydney = ZoneInfo("Australia/Sydney")
        winter = parse_start("2026-07-01 19:00", sydney)  # AEST, UTC+10
        summer = parse_start("2026-12-01 19:00", sydney)  # AEDT, UTC+11

        self.assertEqual(datetime.fromtimestamp(winter, timezone.utc).hour, 9)
        self.assertEqual(datetime.fromtimestamp(summer, timezone.utc).hour, 8)

    def test_parse_start_rejects_dst_gap_and_ambiguous_times(self):
        sydney = ZoneInfo("Australia/Sydney")
        with self.assertRaises(BookingError) as gap:
            parse_start("2026-10-04 02:30", sydney)  # clocks jump 02:00 -> 03:00
        with self.assertRaises(BookingError) as overlap:
            parse_start("2026-04-05 02:30", sydney)  # 02:00-03:00 happens twice

        self.assertIn("doesn't exist", str(gap.exception))
        self.assertIn("happens twice", str(overlap.exception))
        parse_start("2026-10-04 03:30", sydney)  # just after the gap is fine

    def test_parse_start_rejects_bad_format(self):
        for text in ("tomorrow 7pm", "2026-13-01 19:00", "19:00"):
            with self.assertRaises(BookingError):
                parse_start(text, ZoneInfo("UTC"))

    def test_parse_duration(self):
        self.assertEqual(parse_duration("90"), 90 * 60)
        self.assertEqual(parse_duration("45m"), 45 * 60)
        self.assertEqual(parse_duration("2h"), 2 * HOUR)
        self.assertEqual(parse_duration("1h30m"), 90 * 60)
        for text in ("", "0", "abc", "-5", "1d"):
            with self.assertRaises(BookingError):
                parse_duration(text)


class ConfigTests(unittest.TestCase):
    def test_disabled_with_invalid_time_zone(self):
        config = BookingConfig.from_env(
            {"BOOKING_ROLE_ID": "123", "BOOKING_TIMEZONE": "Mars/Base", "DISCORD_GUILD_ID": "1"}
        )

        self.assertFalse(config.enabled)
        self.assertIn("BOOKING_TIMEZONE", config.disabled_reason)

    def test_disabled_without_guild(self):
        config = BookingConfig.from_env({"BOOKING_ROLE_ID": "123"})

        self.assertFalse(config.enabled)
        self.assertIn("DISCORD_GUILD_ID", config.disabled_reason)

    def test_enabled_config(self):
        config = BookingConfig.from_env({
            "BOOKING_ROLE_ID": "123", "BOOKING_ADMIN_ROLE_ID": "456", "BOOKING_TIMEZONE": "Australia/Sydney",
            "DISCORD_GUILD_ID": "999", "BOOKING_MAX_HOURS": "6", "BOOKING_MAX_DAYS_AHEAD": "14",
        })

        self.assertTrue(config.enabled)
        self.assertEqual((config.admin_role_id, config.guild_id), (456, 999))
        self.assertEqual(config.timezone.key, "Australia/Sydney")
        self.assertEqual((config.max_duration_seconds, config.max_lead_seconds), (6 * HOUR, 14 * 86400))

    def test_limit_defaults_and_bad_values(self):
        config = BookingConfig.from_env({"BOOKING_ROLE_ID": "1", "DISCORD_GUILD_ID": "1", "BOOKING_MAX_HOURS": "-3"})

        self.assertEqual((config.max_duration_seconds, config.max_lead_seconds), (24 * HOUR, 90 * 86400))


class ScheduleTests(BookingServiceTestCase):
    SYDNEY = ZoneInfo("Australia/Sydney")

    def test_window_runs_from_local_midnight_for_fourteen_days(self):
        # 2026-10-01 15:00 Sydney (AEST); clocks go forward on 2026-10-04 inside the window.
        now = int(datetime(2026, 10, 1, 15, 0, tzinfo=self.SYDNEY).timestamp())

        start, end = schedule_window(now, self.SYDNEY)

        self.assertEqual(datetime.fromtimestamp(start, self.SYDNEY), datetime(2026, 10, 1, tzinfo=self.SYDNEY))
        self.assertEqual(datetime.fromtimestamp(end, self.SYDNEY), datetime(2026, 10, 15, tzinfo=self.SYDNEY))
        self.assertEqual(end - start, 14 * 86400 - HOUR)

    def test_list_window_includes_ended_and_excludes_cancelled_and_outside(self):
        ended = self.book(0, 1)
        self.clock.ts = T0 + 2 * HOUR
        upcoming = self.book(3, 1)
        cancelled = self.book(5, 1)
        self.service.cancel(cancelled.id, actor_id=1, is_admin=False)
        self.book(20 * 24, 1)  # far outside

        listed = self.service.list_window(T0 - HOUR, T0 + 14 * 24 * HOUR)

        self.assertEqual([b.id for b in listed], [ended.id, upcoming.id])

    def test_panel_refresh_time_config(self):
        base = {"BOOKING_ROLE_ID": "1", "DISCORD_GUILD_ID": "2"}
        self.assertEqual(BookingConfig.from_env(base).panel_refresh_time, time(6, 0))
        self.assertEqual(
            BookingConfig.from_env({**base, "BOOKING_PANEL_REFRESH_TIME": "07:30"}).panel_refresh_time, time(7, 30)
        )
        self.assertEqual(
            BookingConfig.from_env({**base, "BOOKING_PANEL_REFRESH_TIME": "25:99"}).panel_refresh_time, time(6, 0)
        )


if __name__ == "__main__":
    unittest.main()


class GuildColumnTests(unittest.TestCase):
    def test_old_store_gains_guild_column_and_old_rows_can_be_claimed(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "bookings.db"
            conn = sqlite3.connect(db)
            conn.executescript(
                "CREATE TABLE bookings (id INTEGER PRIMARY KEY AUTOINCREMENT, server_key TEXT NOT NULL, "
                "user_id INTEGER NOT NULL, user_name TEXT NOT NULL, start_utc INTEGER NOT NULL, "
                "end_utc INTEGER NOT NULL, created_at INTEGER NOT NULL, cancelled_at INTEGER, cancelled_by INTEGER);"
                f"INSERT INTO bookings (server_key, user_id, user_name, start_utc, end_utc, created_at) "
                f"VALUES ('server1', 1, 'u', {T0 + HOUR}, {T0 + 2 * HOUR}, {T0});"
            )
            conn.commit()
            conn.close()

            service = BookingService(db, clock=FakeClock())
            self.assertIsNone(service.list_live()[0].guild_id)
            self.assertEqual(service.claim_unowned(42), 1)
            self.assertEqual(service.claim_unowned(42), 0)
            self.assertEqual(service.list_live()[0].guild_id, 42)

    def test_manageable_by_booker_home_admin_or_booking_guild_admin(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = BookingService(Path(tmp) / "b.db", clock=FakeClock())
            booking = service.create("server1", 1, "u", T0 + HOUR, T0 + 2 * HOUR, guild_id=7)

            self.assertTrue(booking.manageable_by(1, False))
            self.assertTrue(booking.manageable_by(2, True))
            self.assertTrue(booking.manageable_by(2, False, admin_guild_id=7))
            self.assertFalse(booking.manageable_by(2, False, admin_guild_id=8))
            self.assertFalse(booking.manageable_by(2, False))


class EventNameTests(unittest.TestCase):
    def test_event_name_is_stored_cleaned_and_shared_by_a_series(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = BookingService(Path(tmp) / "b.db", clock=FakeClock())
            series = service.create_series(
                "server1", 1, "u", T0 + HOUR, T0 + 2 * HOUR, "weekly", 2,
                event_name="  Scrim   vs\n1st Para  ",
            )
            unnamed = service.create("server2", 1, "u", T0 + HOUR, T0 + 2 * HOUR, event_name="   ")

            self.assertEqual([b.event_name for b in series], ["Scrim vs 1st Para"] * 2)
            self.assertEqual(service.get(series[1].id).event_name, "Scrim vs 1st Para")
            self.assertIsNone(unnamed.event_name)
            long = service.create("server3", 1, "u", T0 + HOUR, T0 + 2 * HOUR, event_name="x" * 100)
            self.assertEqual(len(long.event_name), 60)

    def test_store_without_event_column_is_upgraded(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "bookings.db"
            conn = sqlite3.connect(db)
            conn.executescript(
                "CREATE TABLE bookings (id INTEGER PRIMARY KEY AUTOINCREMENT, server_key TEXT NOT NULL, "
                "user_id INTEGER NOT NULL, user_name TEXT NOT NULL, start_utc INTEGER NOT NULL, "
                "end_utc INTEGER NOT NULL, created_at INTEGER NOT NULL, cancelled_at INTEGER, "
                "cancelled_by INTEGER, guild_id INTEGER);"
            )
            conn.close()

            service = BookingService(db, clock=FakeClock())
            self.assertEqual(
                service.create("server1", 1, "u", T0 + HOUR, T0 + 2 * HOUR, event_name="Night ops").event_name,
                "Night ops",
            )


class SeriesParentTests(BookingServiceTestCase):
    def test_series_dates_point_at_the_first_date_and_singles_have_no_series(self):
        series = self.service.create_series("server1", 1, "u", T0 + HOUR, T0 + 2 * HOUR, "weekly", 3)
        single = self.service.create("server2", 1, "u", T0 + HOUR, T0 + 2 * HOUR)

        parent = series[0].id
        self.assertEqual([b.series_id for b in series], [parent] * 3)
        self.assertEqual([self.service.get(b.id).series_id for b in series], [parent] * 3)
        self.assertEqual({b.recurrence for b in series}, {"weekly"})
        self.assertFalse(single.is_recurring)
        self.assertIsNone(single.recurrence)

    def test_group_by_series_collapses_each_series(self):
        from services.booking import group_by_series

        self.service.create_series("server1", 1, "u", T0 + HOUR, T0 + 2 * HOUR, "daily", 3)
        self.service.create("server2", 1, "u", T0 + 3 * HOUR, T0 + 4 * HOUR)

        groups = group_by_series(self.service.list_live())

        self.assertEqual([(g.is_series, len(g.bookings)) for g in groups], [(True, 3), (False, 1)])
        self.assertEqual(groups[0].recurrence_label, "Daily")

    def test_cancel_series_cancels_remaining_dates_for_the_owner_only(self):
        series = self.service.create_series("server1", 1, "u", T0 + HOUR, T0 + 2 * HOUR, "weekly", 3)

        with self.assertRaises(BookingPermissionError):
            self.service.cancel_series(series[1].id, actor_id=2, is_admin=False)
        cancelled = self.service.cancel_series(series[1].id, actor_id=1, is_admin=False)

        self.assertEqual([b.id for b in cancelled], [b.id for b in series])
        self.assertEqual(self.service.list_live(), [])
        with self.assertRaises(BookingNotFoundError):
            self.service.cancel_series(series[0].id, actor_id=1, is_admin=False)

    def test_cancel_series_on_a_single_booking_cancels_just_it(self):
        single = self.service.create("server1", 1, "u", T0 + HOUR, T0 + 2 * HOUR)

        self.assertEqual([b.id for b in self.service.cancel_series(single.id, 1, False)], [single.id])

    def test_cancel_all_live_keeps_ended_bookings(self):
        self.service.create("server1", 1, "u", T0, T0 + 60)
        self.service.create_series("server2", 1, "u", T0 + HOUR, T0 + 2 * HOUR, "daily", 2)
        self.clock.ts = T0 + 120  # the first booking has ended

        self.assertEqual(self.service.cancel_all_live(actor_id=9), 2)
        self.assertEqual(self.service.list_live(), [])


class RebookTests(BookingServiceTestCase):
    def test_edit_single_booking_time_server_and_name(self):
        booking = self.service.create("server1", 1, "u", T0 + HOUR, T0 + 2 * HOUR, event_name="Old", guild_id=5)

        [updated] = self.service.rebook(
            booking.id, 1, False, server_key="server2",
            start_utc=T0 + 3 * HOUR, end_utc=T0 + 5 * HOUR, event_name="New",
        )

        self.assertIsNotNone(self.service.get(booking.id).cancelled_at)
        self.assertEqual((updated.server_key, updated.start_utc, updated.end_utc, updated.event_name, updated.guild_id),
                         ("server2", T0 + 3 * HOUR, T0 + 5 * HOUR, "New", 5))
        self.assertEqual([b.id for b in self.service.list_live()], [updated.id])

    def test_edit_can_change_the_repeat_pattern(self):
        series = self.service.create_series("server1", 1, "u", T0 + HOUR, T0 + 2 * HOUR, "weekly", 4)

        updated = self.service.rebook(
            series[2].id, 1, False, server_key="server1",
            start_utc=T0 + HOUR, end_utc=T0 + 2 * HOUR, recurrence="daily", count=3,
        )

        self.assertEqual([b.start_utc - T0 for b in updated], [HOUR, HOUR + 86400, HOUR + 2 * 86400])
        self.assertEqual({b.recurrence for b in updated}, {"daily"})
        self.assertEqual([b.id for b in self.service.list_live()], [b.id for b in updated])

        [single] = self.service.rebook(
            updated[0].id, 1, False, server_key="server1", start_utc=T0 + HOUR, end_utc=T0 + 2 * HOUR,
        )
        self.assertFalse(single.is_recurring)
        self.assertEqual(len(self.service.list_live()), 1)

    def test_edit_can_reuse_its_own_slot_but_a_clash_changes_nothing(self):
        booking = self.service.create("server1", 1, "u", T0 + HOUR, T0 + 3 * HOUR)
        other = self.service.create("server1", 2, "o", T0 + 4 * HOUR, T0 + 5 * HOUR)

        [moved] = self.service.rebook(booking.id, 1, False, server_key="server1",
                                      start_utc=T0 + 2 * HOUR, end_utc=T0 + 4 * HOUR)
        with self.assertRaises(BookingOverlapError) as caught:
            self.service.rebook(moved.id, 1, False, server_key="server1",
                                start_utc=T0 + 2 * HOUR, end_utc=T0 + 4 * HOUR + 1)
        self.assertEqual(caught.exception.conflict.id, other.id)
        self.assertEqual({b.id for b in self.service.list_live()}, {moved.id, other.id})

    def test_edit_rules_owner_only_and_running_booking_keeps_its_start(self):
        booking = self.service.create("server1", 1, "u", T0, T0 + 2 * HOUR)
        self.clock.ts = T0 + HOUR  # now running

        with self.assertRaises(BookingPermissionError):
            self.service.rebook(booking.id, 2, False, server_key="server1", start_utc=T0, end_utc=T0 + 3 * HOUR)
        [extended] = self.service.rebook(booking.id, 1, False, server_key="server1",
                                         start_utc=T0, end_utc=T0 + 3 * HOUR)
        self.assertEqual(extended.end_utc, T0 + 3 * HOUR)
        with self.assertRaises(BookingError):
            self.service.rebook(extended.id, 1, False, server_key="server1",
                                start_utc=T0 - HOUR, end_utc=T0 + 3 * HOUR)

    def test_at_most_ten_dates(self):
        with self.assertRaises(BookingError):
            self.service.create_series("server1", 1, "u", T0 + HOUR, T0 + 2 * HOUR, "daily", 11)
        self.assertEqual(len(self.service.create_series("server1", 1, "u", T0 + HOUR, T0 + 2 * HOUR, "daily", 10)), 10)
