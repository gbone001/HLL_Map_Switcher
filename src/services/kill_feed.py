import asyncio
import logging
import os
import re
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Deque, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from utils.api_client import HLLAPIClient
from utils.crcon_http import CRCONHTTPError, CRCONHttpClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class KillFeedEvent:
    server_index: int
    server_name: str
    action: str
    killer_name: str
    killer_id: Optional[str]
    victim_name: str
    victim_id: Optional[str]
    weapon: Optional[str]
    message: str
    raw: str
    event_time: str
    timestamp_ms: int
    ingested_at: str

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


@dataclass
class PollFailureState:
    consecutive_failures: int = 0
    current_backoff_seconds: float = 0.0
    next_poll_after_monotonic: float = 0.0


class KillFeedService:
    """Poll CRCON recent logs and retain a deduplicated window of kill events."""

    def __init__(
        self,
        api_client: HLLAPIClient,
        http_client_factory,
        poll_interval_seconds: float = 1.0,
        max_events: int = 25,
        recent_log_window: int = 50,
        failure_backoff_initial_seconds: float = 5.0,
        failure_backoff_max_seconds: float = 60.0,
    ) -> None:
        self.api_client = api_client
        self.http_client_factory = http_client_factory
        self.poll_interval_seconds = poll_interval_seconds
        self.max_events = max_events
        self.recent_log_window = recent_log_window
        self.failure_backoff_initial_seconds = max(failure_backoff_initial_seconds, poll_interval_seconds)
        self.failure_backoff_max_seconds = max(
            failure_backoff_max_seconds,
            self.failure_backoff_initial_seconds,
        )
        self._events: Deque[KillFeedEvent] = deque(maxlen=max_events)
        self._seen_event_keys: Set[Tuple[int, int, str]] = set()
        self._seen_event_order: Deque[Tuple[int, int, str]] = deque(maxlen=max_events * 20)
        self._failure_states: Dict[int, PollFailureState] = {}
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None
        self._started = False

    async def start(self) -> None:
        if self._started:
            return

        self._started = True
        self._task = asyncio.create_task(self._run(), name="kill-feed-poller")
        logger.info(
            "Kill feed service started with poll_interval_seconds=%s max_events=%s recent_log_window=%s",
            self.poll_interval_seconds,
            self.max_events,
            self.recent_log_window,
        )

    async def stop(self) -> None:
        task = self._task
        self._task = None
        self._started = False
        if task is None:
            return

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def get_events(self, server_index: Optional[int] = None, limit: Optional[int] = None) -> List[Dict[str, object]]:
        async with self._lock:
            events = list(self._events)

        if server_index is not None:
            events = [event for event in events if event.server_index == server_index]

        events.sort(key=lambda event: event.timestamp_ms, reverse=True)

        if limit is not None:
            events = events[:limit]

        return [event.to_dict() for event in events]

    async def _run(self) -> None:
        while True:
            try:
                await self._poll_all_servers()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Kill feed polling loop failed unexpectedly")

            await asyncio.sleep(self.poll_interval_seconds)

    async def _poll_all_servers(self) -> None:
        servers = self.api_client.get_servers()
        if not servers:
            return

        poll_coroutines = [
            self._poll_server(server_index=server_index, server_name=server_name)
            for server_index, server_name in servers
        ]
        await asyncio.gather(*poll_coroutines)

    async def _poll_server(self, server_index: int, server_name: str) -> None:
        client: Optional[CRCONHttpClient] = self.http_client_factory(server_index)
        if client is None:
            return

        if self._is_server_in_failure_backoff(server_index):
            return

        try:
            payload = await asyncio.to_thread(
                client.get_recent_logs,
                ["KILL", "TEAM KILL"],
                self.recent_log_window,
            )
        except CRCONHTTPError as exc:
            self._record_poll_failure(server_index=server_index, server_name=server_name, exc=exc)
            return
        except Exception:
            logger.exception("Kill feed poll crashed for server '%s'", server_name)
            return

        self._record_poll_success(server_index=server_index, server_name=server_name)

        if not isinstance(payload, dict):
            logger.warning("Kill feed poll returned unexpected payload for server '%s'", server_name)
            return

        result = payload.get("result")
        if not isinstance(result, dict):
            logger.warning("Kill feed result missing for server '%s'", server_name)
            return

        logs = result.get("logs")
        if not isinstance(logs, list):
            logger.warning("Kill feed logs missing for server '%s'", server_name)
            return

        kill_events = self._build_events(server_index=server_index, server_name=server_name, logs=logs)
        if not kill_events:
            return

        async with self._lock:
            for event in kill_events:
                event_key = (event.server_index, event.timestamp_ms, event.raw)
                if event_key in self._seen_event_keys:
                    continue

                self._events.appendleft(event)
                self._remember_key(event_key)

    def _build_events(
        self,
        server_index: int,
        server_name: str,
        logs: Iterable[object],
    ) -> List[KillFeedEvent]:
        events: List[KillFeedEvent] = []
        for log_entry in logs:
            if not isinstance(log_entry, dict):
                continue

            action = str(log_entry.get("action") or "").strip()
            if action not in {"KILL", "TEAM KILL"}:
                continue

            killer_name = str(log_entry.get("player_name_1") or "").strip()
            victim_name = str(log_entry.get("player_name_2") or "").strip()
            raw_message = str(log_entry.get("raw") or "").strip()
            if not killer_name or not victim_name or not raw_message:
                continue

            timestamp_value = log_entry.get("timestamp_ms")
            try:
                timestamp_ms = int(timestamp_value)
            except (TypeError, ValueError):
                continue

            message = str(log_entry.get("message") or raw_message).strip()
            weapon_value = log_entry.get("weapon")
            weapon = str(weapon_value).strip() if weapon_value else None

            events.append(
                KillFeedEvent(
                    server_index=server_index,
                    server_name=server_name,
                    action=action,
                    killer_name=killer_name,
                    killer_id=self._optional_string(log_entry.get("player_id_1")),
                    victim_name=victim_name,
                    victim_id=self._optional_string(log_entry.get("player_id_2")),
                    weapon=weapon,
                    message=message,
                    raw=raw_message,
                    event_time=str(log_entry.get("event_time") or ""),
                    timestamp_ms=timestamp_ms,
                    ingested_at=datetime.now(timezone.utc).isoformat(),
                )
            )

        events.sort(key=lambda event: event.timestamp_ms)
        return events

    def _remember_key(self, event_key: Tuple[int, int, str]) -> None:
        if len(self._seen_event_order) == self._seen_event_order.maxlen:
            oldest_key = self._seen_event_order.popleft()
            self._seen_event_keys.discard(oldest_key)

        self._seen_event_keys.add(event_key)
        self._seen_event_order.append(event_key)

    def _is_server_in_failure_backoff(self, server_index: int) -> bool:
        failure_state = self._failure_states.get(server_index)
        if failure_state is None:
            return False

        return time.monotonic() < failure_state.next_poll_after_monotonic

    def _record_poll_failure(self, server_index: int, server_name: str, exc: CRCONHTTPError) -> None:
        failure_state = self._failure_states.setdefault(server_index, PollFailureState())
        failure_state.consecutive_failures += 1

        if failure_state.current_backoff_seconds <= 0:
            next_backoff_seconds = self.failure_backoff_initial_seconds
        else:
            next_backoff_seconds = min(
                self.failure_backoff_max_seconds,
                failure_state.current_backoff_seconds * 2,
            )

        failure_state.current_backoff_seconds = next_backoff_seconds
        failure_state.next_poll_after_monotonic = time.monotonic() + next_backoff_seconds

        logger.warning(
            "Kill feed poll failed for server '%s' after %s consecutive failure(s); "
            "pausing this server's kill-feed polling for %.1f seconds: %s",
            server_name,
            failure_state.consecutive_failures,
            next_backoff_seconds,
            self._summarize_exception(exc),
        )

    def _record_poll_success(self, server_index: int, server_name: str) -> None:
        failure_state = self._failure_states.pop(server_index, None)
        if failure_state is None:
            return

        logger.info(
            "Kill feed polling recovered for server '%s' after %s consecutive failure(s)",
            server_name,
            failure_state.consecutive_failures,
        )

    @staticmethod
    def _summarize_exception(exc: Exception) -> str:
        message = re.sub(r"\s+", " ", str(exc)).strip()
        if "<html" not in message.lower():
            return message

        title_match = re.search(r"<title>\s*(.*?)\s*</title>", message, flags=re.IGNORECASE)
        heading_match = re.search(r"<h1>\s*(.*?)\s*</h1>", message, flags=re.IGNORECASE)
        page_summary = title_match or heading_match
        if page_summary:
            html_summary = page_summary.group(1).strip()
            prefix = message.split("<", 1)[0].strip()
            return f"{prefix} HTML error page: {html_summary}" if prefix else f"HTML error page: {html_summary}"

        prefix = message.split("<", 1)[0].strip()
        return f"{prefix} HTML error page returned" if prefix else "HTML error page returned"

    @staticmethod
    def _optional_string(value: object) -> Optional[str]:
        if value is None:
            return None

        text = str(value).strip()
        return text or None


def build_kill_feed_service(api_client: HLLAPIClient, http_client_factory) -> KillFeedService:
    poll_interval_seconds = float(os.getenv("KILL_FEED_POLL_INTERVAL_SECONDS", "1.0"))
    max_events = int(os.getenv("KILL_FEED_MAX_EVENTS", "25"))
    recent_log_window = int(os.getenv("KILL_FEED_RECENT_LOG_WINDOW", "50"))
    failure_backoff_initial_seconds = float(os.getenv("KILL_FEED_FAILURE_BACKOFF_INITIAL_SECONDS", "5.0"))
    failure_backoff_max_seconds = float(os.getenv("KILL_FEED_FAILURE_BACKOFF_MAX_SECONDS", "60.0"))

    return KillFeedService(
        api_client=api_client,
        http_client_factory=http_client_factory,
        poll_interval_seconds=poll_interval_seconds,
        max_events=max_events,
        recent_log_window=recent_log_window,
        failure_backoff_initial_seconds=failure_backoff_initial_seconds,
        failure_backoff_max_seconds=failure_backoff_max_seconds,
    )
