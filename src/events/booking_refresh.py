"""Panel refresh at booking start/end (shaping part B8).

Display only: this loop re-renders the panel when a booking starts or ends and never
touches the game server. Each tick refreshes only if a boundary passed since the last
tick, so an idle bot does not edit the panel every minute.
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

from discord.ext import tasks

from services.booking import BookingService

logger = logging.getLogger(__name__)


class BookingBoundaryRefresher:
    def __init__(
        self,
        service: BookingService,
        refresh: Callable[[], Awaitable[None]],
        interval_seconds: float = 60.0,
    ) -> None:
        self.service = service
        self.refresh = refresh
        self._last_tick_ts = service.now_ts()
        self._loop = tasks.loop(seconds=interval_seconds)(self.tick)

    async def tick(self) -> bool:
        """Refresh the panel if a booking started or ended since the last successful tick.

        The window only advances once the check (and any refresh) succeeds, so a transient
        database or Discord error is retried on the next tick instead of losing the boundary.
        """
        now = self.service.now_ts()
        try:
            crossed = self.service.has_boundary_between(self._last_tick_ts, now)
        except Exception:
            logger.exception("Booking boundary check failed; will retry")
            return False
        if crossed:
            try:
                await self.refresh()
            except Exception:
                logger.exception("Panel refresh after booking boundary failed; will retry")
                return False
        self._last_tick_ts = now
        return crossed

    def start(self) -> None:
        if not self._loop.is_running():
            # The panel is rendered fresh on startup, so only boundaries after now matter.
            self._last_tick_ts = self.service.now_ts()
            self._loop.start()

    def stop(self) -> None:
        if self._loop.is_running():
            self._loop.cancel()

