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
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "bookings.db"
START_FORMAT = "%Y-%m-%d %H:%M"
START_FORMAT_HINT = "YYYY-MM-DD HH:MM"
DURATION_HINT = "minutes (e.g. 90) or hours/minutes (e.g. 2h, 1h30m)"

_DURATION_PATTERN = re.compile(r"^\s*(?:(?P<hours>\d+)\s*h)?\s*(?:(?P<minutes>\d+)\s*m?)?\s*$", re.IGNORECASE)

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
    cancelled_by INTEGER
);
CREATE INDEX IF NOT EXISTS bookings_server_time ON bookings (server_key, start_utc, end_utc);
"""

_COLUMNS = "id, server_key, user_id, user_name, start_utc, end_utc, created_at, cancelled_at, cancelled_by"
_LIVE = "cancelled_at IS NULL AND end_utc > ?"


class BookingError(Exception):
    """A booking request was invalid or not permitted; the message is user-facing."""


class BookingOverlapError(BookingError):
    def __init__(self, conflict: "Booking"):
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
    cancelled_at: Optional[int] = None
    cancelled_by: Optional[int] = None

    def is_active(self, now_ts: int) -> bool:
        return self.cancelled_at is None and self.start_utc <= now_ts < self.end_utc


DEFAULT_MAX_HOURS = 24
DEFAULT_MAX_DAYS_AHEAD = 90


@dataclass(frozen=True)
class BookingConfig:
    """Booking is enabled only when BOOKING_ROLE_ID, DISCORD_GUILD_ID and BOOKING_TIMEZONE are valid.

    DISCORD_GUILD_ID is required because booking roles and admin rights only mean something
    inside one guild; without it, admins of any guild the bot is in would count as booking admins.
    """

    enabled: bool
    role_id: Optional[int]
    admin_role_id: Optional[int]
    timezone: ZoneInfo
    db_path: Path
    disabled_reason: Optional[str] = None
    guild_id: Optional[int] = None
    max_duration_seconds: int = DEFAULT_MAX_HOURS * 3600
    max_lead_seconds: int = DEFAULT_MAX_DAYS_AHEAD * 86400

    @classmethod
    def from_env(cls, environ: Optional[dict] = None) -> "BookingConfig":
        env = os.environ if environ is None else environ
        base = {
            "role_id": None,
            "admin_role_id": _optional_int(env.get("BOOKING_ADMIN_ROLE_ID")),
            "timezone": ZoneInfo("UTC"),
            "db_path": Path(env.get("BOOKING_DB_PATH") or DEFAULT_DB_PATH),
            "guild_id": _optional_int(env.get("DISCORD_GUILD_ID")),
            "max_duration_seconds": _positive_int(env.get("BOOKING_MAX_HOURS"), DEFAULT_MAX_HOURS) * 3600,
            "max_lead_seconds": _positive_int(env.get("BOOKING_MAX_DAYS_AHEAD"), DEFAULT_MAX_DAYS_AHEAD) * 86400,
        }

        def disabled(reason: str) -> "BookingConfig":
            return cls(enabled=False, disabled_reason=reason, **base)

        timezone_name = (env.get("BOOKING_TIMEZONE") or "UTC").strip()
        try:
            base["timezone"] = ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError):
            return disabled(f"BOOKING_TIMEZONE {timezone_name!r} is not a valid IANA time zone.")

        raw_role = (env.get("BOOKING_ROLE_ID") or "").strip()
        if not raw_role:
            return disabled("BOOKING_ROLE_ID is not set.")
        base["role_id"] = _optional_int(raw_role)
        if base["role_id"] is None:
            return disabled(f"BOOKING_ROLE_ID {raw_role!r} is not a number.")
        if base["guild_id"] is None:
            return disabled("DISCORD_GUILD_ID must be set so booking roles and admins are scoped to one guild.")

        return cls(enabled=True, **base)


def parse_start(text: str, tz: ZoneInfo) -> int:
    """Parse 'YYYY-MM-DD HH:MM' in the booking time zone into UTC epoch seconds."""
    try:
        naive = datetime.strptime(text.strip(), START_FORMAT)
    except ValueError as exc:
        raise BookingError(f"Start time must be {START_FORMAT_HINT}.") from exc

    local = naive.replace(tzinfo=tz)
    # A local time inside a DST spring-forward gap doesn't round-trip; one inside the
    # fall-back hour has two valid UTC offsets. Reject both rather than guess.
    if datetime.fromtimestamp(local.timestamp(), tz).replace(tzinfo=None) != naive:
        raise BookingError(f"{text.strip()} doesn't exist in {tz.key} (clocks skip it for daylight saving).")
    if local.utcoffset() != naive.replace(tzinfo=tz, fold=1).utcoffset():
        raise BookingError(f"{text.strip()} happens twice in {tz.key} (daylight saving ends); pick another time.")
    return int(local.timestamp())


def parse_duration(text: str) -> int:
    """Parse '90', '45m', '2h', or '1h30m' into seconds."""
    match = _DURATION_PATTERN.match(text or "")
    if not match or not (match.group("hours") or match.group("minutes")):
        raise BookingError(f"Duration must be {DURATION_HINT}.")
    seconds = int(match.group("hours") or 0) * 3600 + int(match.group("minutes") or 0) * 60
    if seconds <= 0:
        raise BookingError("Duration must be greater than zero.")
    return seconds


def format_local(ts: int, tz: ZoneInfo) -> str:
    return datetime.fromtimestamp(ts, tz).strftime(START_FORMAT)


class BookingService:
    def __init__(
        self,
        db_path: Path,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        max_duration_seconds: Optional[int] = None,
        max_lead_seconds: Optional[int] = None,
    ):
        self.db_path = Path(db_path)
        self._clock = clock
        self.max_duration_seconds = max_duration_seconds
        self.max_lead_seconds = max_lead_seconds
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.executescript(_SCHEMA)

    def now_ts(self) -> int:
        return int(self._clock().timestamp())

    def create(self, server_key: str, user_id: int, user_name: str, start_utc: int, end_utc: int) -> Booking:
        """Insert a booking unless it is invalid or overlaps a live booking on the same server."""
        now = self.now_ts()
        if end_utc <= start_utc:
            raise BookingError("The booking must end after it starts.")
        if start_utc < now - 60:
            raise BookingError("The start time is in the past.")
        if self.max_duration_seconds and end_utc - start_utc > self.max_duration_seconds:
            raise BookingError(f"Bookings can be at most {self.max_duration_seconds // 3600} hours long.")
        if self.max_lead_seconds and start_utc - now > self.max_lead_seconds:
            raise BookingError(f"Bookings can start at most {self.max_lead_seconds // 86400} days ahead.")

        conn = self._connect()
        try:
            # BEGIN IMMEDIATE takes the write lock up front so two concurrent /book calls
            # cannot both pass the overlap check.
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM bookings WHERE server_key = ? AND cancelled_at IS NULL "
                "AND start_utc < ? AND end_utc > ? ORDER BY start_utc LIMIT 1",
                (server_key, end_utc, start_utc),
            ).fetchone()
            if row is not None:
                conn.execute("ROLLBACK")
                raise BookingOverlapError(_row_to_booking(row))
            cursor = conn.execute(
                "INSERT INTO bookings (server_key, user_id, user_name, start_utc, end_utc, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (server_key, user_id, user_name, start_utc, end_utc, now),
            )
            booking_id = cursor.lastrowid
            conn.execute("COMMIT")
        finally:
            conn.close()
        return self.get(booking_id)

    def get(self, booking_id: int) -> Optional[Booking]:
        with closing(self._connect()) as conn:
            row = conn.execute(f"SELECT {_COLUMNS} FROM bookings WHERE id = ?", (booking_id,)).fetchone()
        return _row_to_booking(row) if row else None

    def list_live(self, server_key: Optional[str] = None) -> list[Booking]:
        """Bookings that are active or upcoming, earliest first."""
        query = f"SELECT {_COLUMNS} FROM bookings WHERE {_LIVE}"
        params: list = [self.now_ts()]
        if server_key is not None:
            query += " AND server_key = ?"
            params.append(server_key)
        query += " ORDER BY start_utc"
        with closing(self._connect()) as conn:
            return [_row_to_booking(row) for row in conn.execute(query, params).fetchall()]

    def active_for(self, server_key: str) -> Optional[Booking]:
        now = self.now_ts()
        with closing(self._connect()) as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM bookings WHERE server_key = ? AND cancelled_at IS NULL "
                "AND start_utc <= ? AND end_utc > ? ORDER BY start_utc LIMIT 1",
                (server_key, now, now),
            ).fetchone()
        return _row_to_booking(row) if row else None

    def next_for(self, server_key: str) -> Optional[Booking]:
        now = self.now_ts()
        with closing(self._connect()) as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM bookings WHERE server_key = ? AND cancelled_at IS NULL "
                "AND start_utc > ? ORDER BY start_utc LIMIT 1",
                (server_key, now),
            ).fetchone()
        return _row_to_booking(row) if row else None

    def summary_by_server(self) -> dict[str, tuple[Optional[Booking], Optional[Booking]]]:
        """(active, next) booking per server key, from a single query."""
        now = self.now_ts()
        summary: dict[str, tuple[Optional[Booking], Optional[Booking]]] = {}
        for booking in self.list_live():  # earliest first
            active, upcoming = summary.get(booking.server_key, (None, None))
            if booking.start_utc <= now and active is None:
                active = booking
            elif booking.start_utc > now and upcoming is None:
                upcoming = booking
            summary[booking.server_key] = (active, upcoming)
        return summary

    def blocking_booking(self, server_key: str, user_id: int, is_admin: bool) -> Optional[Booking]:
        """The booking that stops this user writing to this server right now, if any."""
        if is_admin:
            return None
        active = self.active_for(server_key)
        if active is None or active.user_id == user_id:
            return None
        return active

    def cancel(self, booking_id: int, actor_id: int, is_admin: bool) -> Booking:
        booking = self.get(booking_id)
        if booking is None or booking.cancelled_at is not None or booking.end_utc <= self.now_ts():
            raise BookingNotFoundError(f"Booking #{booking_id} was not found or has already ended.")
        if booking.user_id != actor_id and not is_admin:
            raise BookingPermissionError(f"Booking #{booking_id} is not yours.")
        with closing(self._connect()) as conn:
            conn.execute(
                "UPDATE bookings SET cancelled_at = ?, cancelled_by = ? WHERE id = ? AND cancelled_at IS NULL",
                (self.now_ts(), actor_id, booking_id),
            )
        return self.get(booking_id)

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


def _positive_int(value: Optional[str], default: int) -> int:
    parsed = _optional_int(value)
    return parsed if parsed is not None and parsed > 0 else default


def _optional_int(value: Optional[str]) -> Optional[int]:
    try:
        return int(str(value).strip()) if value not in (None, "") else None
    except ValueError:
        return None
