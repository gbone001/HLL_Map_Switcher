import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
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
    def test_active_and_next(self):
        current = self.book(0, 1, user_id=1)
        upcoming = self.book(2, 1, user_id=2)

        self.assertEqual(self.service.active_for("server1").id, current.id)
        self.assertEqual(self.service.next_for("server1").id, upcoming.id)

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

    def test_blocking_booking_rules(self):
        booking = self.book(0, 1, user_id=1)

        self.assertEqual(self.service.blocking_booking("server1", user_id=2, is_admin=False).id, booking.id)
        self.assertIsNone(self.service.blocking_booking("server1", user_id=1, is_admin=False))
        self.assertIsNone(self.service.blocking_booking("server1", user_id=2, is_admin=True))
        self.assertIsNone(self.service.blocking_booking("server2", user_id=2, is_admin=False))

        self.clock.ts = T0 + HOUR
        self.assertIsNone(self.service.blocking_booking("server1", user_id=2, is_admin=False))


class CancelTests(BookingServiceTestCase):
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
    def test_disabled_without_role(self):
        config = BookingConfig.from_env({"DISCORD_GUILD_ID": "1"})

        self.assertFalse(config.enabled)
        self.assertIn("BOOKING_ROLE_ID", config.disabled_reason)

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
        self.assertEqual((config.role_id, config.admin_role_id, config.guild_id), (123, 456, 999))
        self.assertEqual(config.timezone.key, "Australia/Sydney")
        self.assertEqual((config.max_duration_seconds, config.max_lead_seconds), (6 * HOUR, 14 * 86400))

    def test_limit_defaults_and_bad_values(self):
        config = BookingConfig.from_env({"BOOKING_ROLE_ID": "1", "DISCORD_GUILD_ID": "1", "BOOKING_MAX_HOURS": "-3"})

        self.assertEqual((config.max_duration_seconds, config.max_lead_seconds), (24 * HOUR, 90 * 86400))


if __name__ == "__main__":
    unittest.main()
