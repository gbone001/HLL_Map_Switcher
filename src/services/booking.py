"""Server booking: reservations, overlap checks, and the booking lock decision.

Bookings are stored in SQLite inside the data directory (bind-mounted under Docker,
see docs/booking-shaping.md). All times are stored as UTC epoch seconds and treated
as half-open ranges [start, end), so back-to-back bookings do not overlap.

This module has no Discord or CRCON dependencies; src/commands/booking.py and
src/bot.py wire it into the bot.
"""

from __future__ import annotations

import os
import re
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "bookings.db"
START_FORMAT = "%Y-%m-%d %H:%M"
START_FORMAT_HINT = "YYYY-MM-DD HH:MM"
DURATION_HINT = "minutes (e.g. 90) or hours/minutes (e.g. 2h, 1h30m)"

_DURATION_PATTERN = re.compile(
    r"^\s*(?:(?P<hours>\d+)\s*h)?\s*(?:(?P<minutes>\d+)\s*m?)?\s*$",
    re.IGNORECASE,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    server_key TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    user_name TEXT NOT NULL,
    start_utc INTEGER NOT NULL,
    end_utc INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    cancelled_at INTEGER,
    cancelled_by INTEGER,
    guild_id INTEGER
);
CREATE INDEX IF NOT EXISTS bookings_server_time ON bookings (server_key, start_utc, end_utc);
"""

_COLUMNS = "id, server_key, user_id, user_name, start_utc, end_utc, created_at, cancelled_at, cancelled_by, guild_id"
_LIVE = "cancelled_at IS NULL AND end_utc > ?"


class BookingError(Exception):
    """A booking request was invalid or not permitted; the message is user-facing."""


class BookingOverlapError(BookingError):
    def __init__(self, conflict: Booking):
        super().__init__(f"Overlaps booking #{conflict.id}.")
        self.conflict = conflict


class BookingNotFoundError(BookingError):
    pass


class BookingPermissionError(BookingError):
    pass


@dataclass(frozen=True)
class Booking:
    id: int
    server_key: str
    user_id: int
    user_name: str
    start_utc: int
    end_utc: int
    created_at: int
    cancelled_at: int | None = None
    cancelled_by: int | None = None
    # Discord guild the booking was made from (multi-guild slice 2); None only before migration.
    guild_id: int | None = None

    def is_active(self, now_ts: int) -> bool:
        return (
            self.cancelled_at is None
            and self.start_utc <= now_ts < self.end_utc
        )

    def manageable_by(
        self, user_id: int, is_admin: bool, admin_guild_id: int | None = None
    ) -> bool:
        """Who may cancel: the booker, a home-guild booking admin (`is_admin`), or an
        Administrator of the guild the booking was made from (`admin_guild_id`)."""
        return (
            self.user_id == user_id
            or is_admin
            or (admin_guild_id is not None and self.guild_id == admin_guild_id)
        )


@dataclass(frozen=True)
class ControlDecision:
    """Result of BookingService.control_check; `reason` lets the caller word a denial."""

    allowed: bool
    active: Booking | None
    reason: str


DEFAULT_MAX_HOURS = 24
DEFAULT_MAX_DAYS_AHEAD = 90
DEFAULT_PANEL_REFRESH_TIME = time(6, 0)
SCHEDULE_DAYS = 14
# Days between occurrences for each recurrence choice offered on the panel.
RECURRENCE_DAYS = {"none": 0, "daily": 1, "weekly": 7, "biweekly": 14}
MAX_OCCURRENCES = 20


@dataclass(frozen=True)
class BookingConfig:
    """Booking is enabled when DISCORD_GUILD_ID and BOOKING_TIMEZONE are valid.

    DISCORD_GUILD_ID is required so booking is scoped to one guild.
    Any member of that guild can book; BOOKING_ADMIN_ROLE_ID (or Discord Administrator)
    grants permission to cancel any booking and bypass the booking lock.
    """

    enabled: bool
    timezone: ZoneInfo
    db_path: Path
    admin_role_id: int | None = None
    role_id: int | None = None  # retained for backward compat, not used for access checks
    disabled_reason: str | None = None
    guild_id: int | None = None
    max_duration_seconds: int = DEFAULT_MAX_HOURS * 3600
    max_lead_seconds: int = DEFAULT_MAX_DAYS_AHEAD * 86400
    # Local time (in `timezone`) at which the panel's two-week schedule is re-rendered each day.
    panel_refresh_time: time = DEFAULT_PANEL_REFRESH_TIME

    @classmethod
    def from_env(cls, environ: dict | None = None) -> BookingConfig:
        env = os.environ if environ is None else environ
        base = {
            "role_id": None,
            "admin_role_id": _optional_int(env.get("BOOKING_ADMIN_ROLE_ID")),
            "timezone": ZoneInfo("UTC"),
            "db_path": Path(env.get("BOOKING_DB_PATH") or DEFAULT_DB_PATH),
            "guild_id": _optional_int(env.get("DISCORD_GUILD_ID")),
            "max_duration_seconds": _positive_int(
                env.get("BOOKING_MAX_HOURS"), DEFAULT_MAX_HOURS
            )
            * 3600,
            "max_lead_seconds": _positive_int(
                env.get("BOOKING_MAX_DAYS_AHEAD"), DEFAULT_MAX_DAYS_AHEAD
            )
            * 86400,
            "panel_refresh_time": _clock_time(
                env.get("BOOKING_PANEL_REFRESH_TIME"),
                DEFAULT_PANEL_REFRESH_TIME,
            ),
        }

        def disabled(reason: str) -> BookingConfig:
                    return cls(
                        enabled=False,
                        timezone=base["timezone"],
                        db_path=base["db_path"],
                        disabled_reason=reason,
                        role_id=base["role_id"],
                        admin_role_id=base["admin_role_id"],
                        guild_id=base["guild_id"],
                        max_duration_seconds=base["max_duration_seconds"],
                        max_lead_seconds=base["max_lead_seconds"],
                        panel_refresh_time=base["panel_refresh_time"],
                    )

        timezone_name = (env.get("BOOKING_TIMEZONE") or "UTC").strip()
        try:
            base["timezone"] = ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError):
            return disabled(
                f"BOOKING_TIMEZONE {timezone_name!r} is not a valid IANA time zone."
            )

        if base["guild_id"] is None:
            return disabled(
                "DISCORD_GUILD_ID must be set so bookings are scoped to one guild."
            )

        return cls(
                    enabled=True,
                    timezone=base["timezone"],
                    db_path=base["db_path"],
                    role_id=base["role_id"],
                    admin_role_id=base["admin_role_id"],
                    guild_id=base["guild_id"],
                    max_duration_seconds=base["max_duration_seconds"],
                    max_lead_seconds=base["max_lead_seconds"],
                    panel_refresh_time=base["panel_refresh_time"],
                )


def parse_start(text: str, tz: ZoneInfo) -> int:
    """Parse 'YYYY-MM-DD HH:MM' in the booking time zone into UTC epoch seconds."""
    try:
        naive = datetime.strptime(text.strip(), START_FORMAT)
    except ValueError as exc:
        raise BookingError(f"Start time must be {START_FORMAT_HINT}.") from exc

    local = naive.replace(tzinfo=tz)
    # A local time inside a DST spring-forward gap doesn't round-trip; one inside the
    # fall-back hour has two valid UTC offsets. Reject both rather than guess.
    if (
        datetime.fromtimestamp(local.timestamp(), tz).replace(tzinfo=None)
        != naive
    ):
        raise BookingError(
            f"{text.strip()} doesn't exist in {tz.key} (clocks skip it for daylight saving)."
        )
    if local.utcoffset() != naive.replace(tzinfo=tz, fold=1).utcoffset():
        raise BookingError(
            f"{text.strip()} happens twice in {tz.key} (daylight saving ends); pick another time."
        )
    return int(local.timestamp())


def parse_duration(text: str) -> int:
    """Parse '90', '45m', '2h', or '1h30m' into seconds."""
    match = _DURATION_PATTERN.match(text or "")
    if not match or not (match.group("hours") or match.group("minutes")):
        raise BookingError(f"Duration must be {DURATION_HINT}.")
    seconds = (
        int(match.group("hours") or 0) * 3600
        + int(match.group("minutes") or 0) * 60
    )
    if seconds <= 0:
        raise BookingError("Duration must be greater than zero.")
    return seconds


def schedule_window(
    now_ts: int, tz: ZoneInfo, days: int = SCHEDULE_DAYS
) -> tuple[int, int]:
    """[start of today, start of the day `days` later) in the booking time zone, as UTC epoch seconds."""
    today = datetime.fromtimestamp(now_ts, tz).date()
    start = datetime.combine(today, time(0, 0), tzinfo=tz)
    end = datetime.combine(today + timedelta(days=days), time(0, 0), tzinfo=tz)
    return int(start.timestamp()), int(end.timestamp())


def format_local(ts: int, tz: ZoneInfo) -> str:
    return datetime.fromtimestamp(ts, tz).strftime(START_FORMAT)


class BookingService:
    def __init__(
        self,
        db_path: Path,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        max_duration_seconds: int | None = None,
        max_lead_seconds: int | None = None,
    ):
        self.db_path = Path(db_path)
        self._clock = clock
        self.max_duration_seconds = max_duration_seconds
        self.max_lead_seconds = max_lead_seconds
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.executescript(_SCHEMA)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(bookings)")}
            if "guild_id" not in columns:  # stores created before multi-guild
                conn.execute("ALTER TABLE bookings ADD COLUMN guild_id INTEGER")

    def now_ts(self) -> int:
        return int(self._clock().timestamp())

    def create(
        self,
        server_key: str,
        user_id: int,
        user_name: str,
        start_utc: int,
        end_utc: int,
        guild_id: int | None = None,
    ) -> Booking:
        """Insert a booking unless it is invalid or overlaps a live booking on the same server."""
        return self.create_series(
            server_key, user_id, user_name, start_utc, end_utc, guild_id=guild_id
        )[0]

    def create_series(
        self,
        server_key: str,
        user_id: int,
        user_name: str,
        start_utc: int,
        end_utc: int,
        recurrence: str = "none",
        count: int = 1,
        guild_id: int | None = None,
    ) -> list[Booking]:
        """Insert `count` bookings `recurrence` apart, all or nothing.

        Every occurrence is validated and overlap-checked inside one transaction, so a
        conflict on any of them leaves the store unchanged. Overlaps are checked per game
        server across every guild: one calendar per server.
        """
        if recurrence not in RECURRENCE_DAYS:
            raise BookingError(f"Unknown recurrence {recurrence!r}.")
        if not 1 <= count <= MAX_OCCURRENCES:
            raise BookingError(
                f"Occurrences must be between 1 and {MAX_OCCURRENCES}."
            )
        if RECURRENCE_DAYS[recurrence] == 0:
            count = 1
        step = RECURRENCE_DAYS[recurrence] * 86400
        now = self.now_ts()
        slots = [
            (start_utc + i * step, end_utc + i * step) for i in range(count)
        ]
        for start, end in slots:
            self._validate_slot(start, end, now)

        bookings = []
        conn = self._connect()
        try:
            # BEGIN IMMEDIATE takes the write lock up front so two concurrent /book calls
            # cannot both pass the overlap check.
            conn.execute("BEGIN IMMEDIATE")
            for start, end in slots:
                row = conn.execute(
                    f"SELECT {_COLUMNS} FROM bookings WHERE server_key = ? AND cancelled_at IS NULL "
                    "AND start_utc < ? AND end_utc > ? ORDER BY start_utc LIMIT 1",
                    (server_key, end, start),
                ).fetchone()
                if row is not None:
                    conn.execute("ROLLBACK")
                    raise BookingOverlapError(_row_to_booking(row))
                cursor = conn.execute(
                    "INSERT INTO bookings (server_key, user_id, user_name, start_utc, end_utc, created_at, guild_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (server_key, user_id, user_name, start, end, now, guild_id),
                )
                bookings.append(
                    Booking(
                        cursor.lastrowid,
                        server_key,
                        user_id,
                        user_name,
                        start,
                        end,
                        now,
                        guild_id=guild_id,
                    )
                )
            conn.execute("COMMIT")
        finally:
            conn.close()
        return bookings

    def _validate_slot(self, start_utc: int, end_utc: int, now: int) -> None:
        if end_utc <= start_utc:
            raise BookingError("The booking must end after it starts.")
        if start_utc < now - 60:
            raise BookingError("The start time is in the past.")
        if (
            self.max_duration_seconds
            and end_utc - start_utc > self.max_duration_seconds
        ):
            raise BookingError(
                f"Bookings can be at most {self.max_duration_seconds // 3600} hours long."
            )
        if self.max_lead_seconds and start_utc - now > self.max_lead_seconds:
            raise BookingError(
                f"Bookings can start at most {self.max_lead_seconds // 86400} days ahead."
            )

    def get(self, booking_id: int) -> Booking | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM bookings WHERE id = ?", (booking_id,)
            ).fetchone()
        return _row_to_booking(row) if row else None

    def list_live(self, server_key: str | None = None) -> list[Booking]:
        """Bookings that are active or upcoming, earliest first."""
        query = f"SELECT {_COLUMNS} FROM bookings WHERE {_LIVE}"
        params: list = [self.now_ts()]
        if server_key is not None:
            query += " AND server_key = ?"
            params.append(server_key)
        query += " ORDER BY start_utc"
        with closing(self._connect()) as conn:
            return [
                _row_to_booking(row)
                for row in conn.execute(query, params).fetchall()
            ]

    def active_for(self, server_key: str) -> Booking | None:
        now = self.now_ts()
        with closing(self._connect()) as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM bookings WHERE server_key = ? AND cancelled_at IS NULL "
                "AND start_utc <= ? AND end_utc > ? ORDER BY start_utc LIMIT 1",
                (server_key, now, now),
            ).fetchone()
        return _row_to_booking(row) if row else None

    def summary_by_server(
        self,
    ) -> dict[str, tuple[Booking | None, Booking | None]]:
        """(active, next) booking per server key, from a single query."""
        now = self.now_ts()
        summary: dict[str, tuple[Booking | None, Booking | None]] = {}
        for booking in self.list_live():  # earliest first
            active, upcoming = summary.get(booking.server_key, (None, None))
            if booking.start_utc <= now and active is None:
                active = booking
            elif booking.start_utc > now and upcoming is None:
                upcoming = booking
            summary[booking.server_key] = (active, upcoming)
        return summary

    def control_check(
        self,
        server_key: str,
        user_id: int,
        is_admin: bool,
        *,
        guild_id: int | None = None,
        admin_guild_id: int | None = None,
    ) -> ControlDecision:
        """May this user write to this game server right now? (multi-guild slice 3)

        Rules, in order (admin cam follows the same rules as every other control):
        1. A home booking admin (`is_admin`) may always.
        2. With no live booking on this server, nobody else may.
        3. During a booking, only requests from the booking's guild count, and then only
           from the booker or that guild's Administrators (`admin_guild_id`).
        `guild_id` is where the request comes from. Without guilds (single-guild tests and
        setups) rule 3's guild match is satisfied trivially.
        """
        active = self.active_for(server_key)
        if is_admin:
            return ControlDecision(True, active, "admin")
        if active is None:
            return ControlDecision(False, None, "unbooked")
        if active.guild_id is not None and guild_id is not None and active.guild_id != guild_id:
            reason = "booked_from_other_guild" if active.user_id == user_id else "booked"
            return ControlDecision(False, active, reason)
        if active.user_id == user_id:
            return ControlDecision(True, active, "booker")
        if admin_guild_id is not None and admin_guild_id == active.guild_id:
            return ControlDecision(True, active, "booking_guild_admin")
        return ControlDecision(False, active, "booked")

    def cancel(
        self,
        booking_id: int,
        actor_id: int,
        is_admin: bool,
        admin_guild_id: int | None = None,
    ) -> Booking:
        """Cancel if `Booking.manageable_by` allows it (booker, home admin, or booking guild's admin)."""
        booking = self.get(booking_id)
        if (
            booking is None
            or booking.cancelled_at is not None
            or booking.end_utc <= self.now_ts()
        ):
            raise BookingNotFoundError(
                f"Booking #{booking_id} was not found or has already ended."
            )
        if not booking.manageable_by(actor_id, is_admin, admin_guild_id):
            raise BookingPermissionError(
                f"Booking #{booking_id} is not yours."
            )
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                "UPDATE bookings SET cancelled_at = ?, cancelled_by = ? WHERE id = ? AND cancelled_at IS NULL",
                (self.now_ts(), actor_id, booking_id),
            )
        if cursor.rowcount == 0:
            # Someone else cancelled it between the read above and this update.
            raise BookingNotFoundError(
                f"Booking #{booking_id} was not found or has already ended."
            )
        return self.get(booking_id)

    def list_window(self, since_ts: int, until_ts: int) -> list[Booking]:
        """Non-cancelled bookings overlapping [since, until), earliest first; includes ones already ended."""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM bookings WHERE cancelled_at IS NULL AND start_utc < ? AND end_utc > ? "
                "ORDER BY start_utc",
                (until_ts, since_ts),
            ).fetchall()
        return [_row_to_booking(row) for row in rows]

    def claim_unowned(self, guild_id: int) -> int:
        """Give bookings made before multi-guild (no guild) to `guild_id`; returns how many."""
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                "UPDATE bookings SET guild_id = ? WHERE guild_id IS NULL", (guild_id,)
            )
        return cursor.rowcount

    def reassign_server_key(self, old_key: str, new_key: str) -> int:
        """Move every booking from one server key to another; returns how many moved."""
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                "UPDATE bookings SET server_key = ? WHERE server_key = ?",
                (new_key, old_key),
            )
        return cursor.rowcount

    def has_boundary_between(self, since_ts: int, until_ts: int) -> bool:
        """Did any non-cancelled booking start or end in (since, until]?"""
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT 1 FROM bookings WHERE cancelled_at IS NULL AND "
                "((start_utc > ? AND start_utc <= ?) OR (end_utc > ? AND end_utc <= ?)) LIMIT 1",
                (since_ts, until_ts, since_ts, until_ts),
            ).fetchone()
        return row is not None

    def _connect(self) -> sqlite3.Connection:
        # One short-lived connection per call keeps this safe to use from worker threads.
        return sqlite3.connect(self.db_path, timeout=5.0, isolation_level=None)


def _row_to_booking(row: tuple) -> Booking:
    return Booking(*row)


def _positive_int(value: str | None, default: int) -> int:
    parsed = _optional_int(value)
    return parsed if parsed is not None and parsed > 0 else default


def _clock_time(value: str | None, default: time) -> time:
    try:
        return (
            datetime.strptime(str(value).strip(), "%H:%M").time()
            if value not in (None, "")
            else default
        )
    except ValueError:
        return default


def _optional_int(value: str | None) -> int | None:
    try:
        return int(str(value).strip()) if value not in (None, "") else None
    except ValueError:
        return None
