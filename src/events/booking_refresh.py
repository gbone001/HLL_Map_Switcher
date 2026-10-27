"""Panel refresh at booking start/end (shaping part B8) and once a day.

Display only: these loops re-render the panel and never touch the game server. The
boundary tick refreshes only if a booking started or ended since the last tick, so an
idle bot does not edit the panel every minute. The daily refresh rolls the panel's
two-week booking schedule forward to the new day.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import time

from discord.ext import tasks

from services.booking import BookingService

logger = logging.getLogger(__name__)


class BookingBoundaryRefresher:
    def __init__(
        self,
        service: BookingService,
        refresh: Callable[[], Awaitable[None]],
        interval_seconds: float = 60.0,
        daily_at: time | None = None,
    ) -> None:
        """`daily_at` must be time-zone aware (e.g. 06:00 in the booking time zone)."""
        self.service = service
        self.refresh = refresh
        self._last_tick_ts = service.now_ts()
        self._loop = tasks.loop(seconds=interval_seconds)(self.tick)
        self._daily_loop = (
            tasks.loop(time=daily_at)(self.daily)
            if daily_at is not None
            else None
        )

    async def tick(self) -> bool:
        """Refresh the panel if a booking started or ended since the last successful tick.

        The window only advances once the check (and any refresh) succeeds, so a transient
        database or Discord error is retried on the next tick instead of losing the boundary.
        """
        now = self.service.now_ts()
        try:
            crossed = await asyncio.to_thread(
                self.service.has_boundary_between, self._last_tick_ts, now
            )
        except Exception:
            logger.exception("Booking boundary check failed; will retry")
            return False
        if crossed:
            try:
                await self.refresh()
            except Exception:
                logger.exception(
                    "Panel refresh after booking boundary failed; will retry"
                )
                return False
        self._last_tick_ts = now
        return crossed

    async def daily(self) -> bool:
        try:
            await self.refresh()
        except Exception:
            logger.exception(
                "Daily panel refresh failed; the next booking boundary or day will retry"
            )
            return False
        return True

    def start(self) -> None:
        if not self._loop.is_running():
            # The panel is rendered fresh on startup, so only boundaries after now matter.
            self._last_tick_ts = self.service.now_ts()
            self._loop.start()
        if self._daily_loop is not None and not self._daily_loop.is_running():
            self._daily_loop.start()

    def stop(self) -> None:
        if self._loop.is_running():
            self._loop.cancel()
        if self._daily_loop is not None and self._daily_loop.is_running():
            self._daily_loop.cancel()
