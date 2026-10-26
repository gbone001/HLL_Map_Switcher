import asyncio
import functools
import json
import os
import sys
import threading
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands
from dotenv import load_dotenv

from commands.booking import (
    BookingCommandDeps,
    booker_text,
    guild_admin_id,
    is_booking_admin,
    register_booking_commands,
)
from commands.booking_panel import open_book_panel, open_manage_panel
from events.booking_refresh import BookingBoundaryRefresher
from services.guild_registry import GuildRegistry
from services.booking import (
    SCHEDULE_DAYS,
    Booking,
    BookingConfig,
    BookingService,
    ControlDecision,
    schedule_window,
)
from services.server_control import (
    ControlResult,
    GameStatus,
    ServerControlService,
)
from utils.api_client import HLLAPIClient
from utils.crcon_http import CRCONHttpClient, CRCONHTTPError
from utils.map_data import (
    get_maps_for_mode,
    get_variants_for_map,
    refresh_map_cache,
)

# Load environment variables
load_dotenv()


async def sync_app_commands(
    tree: discord.app_commands.CommandTree,
    registry: GuildRegistry | None = None,
) -> list:
    """Register slash commands with Discord.

    Commands are copied to every approved guild (DISCORD_GUILD_ID plus BOOKING_GUILD_IDS) so
    they appear immediately. Without a home guild they are synced globally (can take up to an
    hour). Returns the commands synced to the home guild (or globally).
    """
    registry = registry or guild_registry
    for entry in registry.invalid_entries:
        print(f"Ignoring invalid BOOKING_GUILD_IDS entry: {entry!r}")

    if not registry.configured:
        try:
            synced = await tree.sync()
        except discord.HTTPException as exc:
            print(f"Failed to sync slash commands globally: {exc}")
            return []
        print(f"Synced {len(synced)} slash command(s) globally.")
        return synced

    home_synced: list = []
    for guild_id in registry.approved_ids():
        guild = discord.Object(id=guild_id)
        try:
            tree.copy_global_to(guild=guild)
            synced = await tree.sync(guild=guild)
        except discord.HTTPException as exc:
            # One unreachable guild (e.g. the bot isn't in it yet) must not block the others.
            print(f"Failed to sync slash commands to guild {guild_id}: {exc}")
            continue
        print(f"Synced {len(synced)} slash command(s) to guild {guild_id}.")
        if registry.is_home(guild_id):
            home_synced = synced
    return home_synced


class MapSwitcherBot(commands.Bot):
    async def setup_hook(self) -> None:
        await sync_app_commands(self.tree)

    async def close(self) -> None:
        _stop_cache_cleanup_task()
        if booking_refresher is not None:
            booking_refresher.stop()
        await super().close()


# Bot configuration
intents = discord.Intents.none()
intents.guilds = True
intents.guild_messages = True
intents.message_content = True
bot = MapSwitcherBot(command_prefix="!", intents=intents)

# Approved Discord guilds: DISCORD_GUILD_ID (home) plus BOOKING_GUILD_IDS (guests).
guild_registry = GuildRegistry.from_env()

# API client
api_client = HLLAPIClient()
_http_clients: dict[int | None, CRCONHttpClient] = {}
_http_client_errors: dict[int | None, str] = {}
# Clients are created lazily from worker threads (see asyncio.to_thread call sites).
# Use RLock to protect all access (reads + writes) to the dicts across threads.
_http_clients_lock = threading.RLock()


def _server_number(server_index: int | None) -> int | None:
    if server_index is None:
        return None
    resolved_server_number = api_client.get_server_number(server_index)
    if resolved_server_number is not None:
        return resolved_server_number
    return server_index + 1


def _get_http_client(
    server_index: int | None = None,
) -> CRCONHttpClient | None:
    with _http_clients_lock:
        if server_index in _http_clients:
            return _http_clients[server_index]
        if server_index in _http_client_errors:
            return None

        server_number = _server_number(server_index)
        try:
            client = CRCONHttpClient.from_env(server_number=server_number)
        except CRCONHTTPError as exc:
            context = (
                f"server slot #{server_number}"
                if server_number is not None
                else "shared CRCON credentials"
            )
            print(f"CRCON HTTP client disabled for {context}: {exc}")
            _http_client_errors[server_index] = str(exc)
            return None

        _http_clients[server_index] = client
        return client


def _http_error_message(server_index: int | None) -> str:
    with _http_clients_lock:
        return (
            _http_client_errors.get(server_index)
            or "HTTP API credentials are not configured for this server."
        )


server_control = ServerControlService(
    server_number_resolver=api_client.get_server_number,
    http_client_factory=_get_http_client,
)


# Server booking (docs/booking-shaping.md). Disabled unless BOOKING_ROLE_ID is set.
booking_config = BookingConfig.from_env()
booking_service: BookingService | None = None
if booking_config.enabled:
    try:
        booking_service = BookingService(
            booking_config.db_path,
            max_duration_seconds=booking_config.max_duration_seconds,
            max_lead_seconds=booking_config.max_lead_seconds,
        )
        print(
            f"Server booking enabled (store: {booking_config.db_path}, time zone: {booking_config.timezone.key})."
        )
    except Exception as exc:
        print(
            f"Server booking disabled: could not open {booking_config.db_path}: {exc}"
        )
else:
    print(f"Server booking disabled: {booking_config.disabled_reason}")


def _guild_name(guild_id: int | None) -> str:
    guild = bot.get_guild(guild_id) if guild_id is not None else None
    return guild.name if guild is not None else "another Discord"


def _booked_by(booking: Booking, viewer_guild_id: int | None = None) -> str:
    """Booker as shown to a viewing guild (the home guild for the shared panel)."""
    return booker_text(
        booking, viewer_guild_id or guild_registry.home_guild_id, _guild_name
    )


# Shared by the booking slash commands and the panel's booking buttons.
booking_deps = BookingCommandDeps(
    service=booking_service,
    config=booking_config,
    servers=api_client.get_servers,
    server_key=api_client.get_server_key,
    server_name_for_key=api_client.get_server_name_for_key,
    on_change=lambda: refresh_main_embed(),
    registry=guild_registry,
    guild_name=_guild_name,
)

if booking_service is not None and guild_registry.home_guild_id is not None:
    try:
        _claimed = booking_service.claim_unowned(guild_registry.home_guild_id)
    except Exception as exc:
        print(f"Could not assign older bookings to the home guild: {exc}")
    else:
        if _claimed:
            print(f"Assigned {_claimed} older booking(s) to the home guild.")

def _orphaned_booking_keys() -> set[str]:
    """Server keys of live bookings that match no configured server (e.g. after SERVER{N}_* changes)."""
    if booking_service is None:
        return set()
    known = {
        api_client.get_server_key(index)
        for index, _ in api_client.get_servers()
    }
    return {
        booking.server_key for booking in booking_service.list_live()
    } - known


def _adopt_single_server_bookings() -> int:
    """Moving from single-server (CRCON_*) to SERVER{N}_* config renames the first server's
    booking key from 'default' to 'server1'. Carry its bookings across so they keep locking it."""
    if booking_service is None:
        return 0
    keys = {api_client.get_server_key(index) for index, _ in api_client.get_servers()}
    if "default" in keys or "server1" not in keys:
        return 0
    return booking_service.reassign_server_key("default", "server1")


if booking_service is not None:
    try:
        _moved = _adopt_single_server_bookings()
    except Exception as exc:
        print(f"Could not move single-server bookings to server1: {exc}")
    else:
        if _moved:
            print(f"Moved {_moved} booking(s) from the single-server key to server1.")
    try:
        _orphaned = _orphaned_booking_keys()
    except Exception as exc:
        print(f"Could not check bookings against configured servers: {exc}")
    else:
        if _orphaned:
            print(
                "WARNING: live bookings exist for servers that are no longer configured and will not "
                f"lock anything: {', '.join(sorted(_orphaned))}. Restore the matching SERVER{{N}}_* "
                "settings or cancel those bookings."
            )


async def _booking_control_check(
    interaction: discord.Interaction, server_index: int
) -> ControlDecision:
    if booking_service is None:
        return ControlDecision(True, None, "booking_disabled")
    return await asyncio.to_thread(
        functools.partial(
            booking_service.control_check,
            api_client.get_server_key(server_index),
            interaction.user.id,
            is_booking_admin(interaction.user, booking_config),
            guild_id=interaction.guild_id,
            admin_guild_id=guild_admin_id(interaction.user),
        )
    )


def _denial_message(decision: ControlDecision, server_name: str, viewer_guild_id: int | None) -> str:
    active = decision.active
    if active is None:
        return f"🔒 **{server_name}** has no live booking. Book it with 📅 Book to control it."
    if decision.reason == "booked_from_other_guild":
        return (
            f"🔒 Your booking on **{server_name}** was made in **{_guild_name(active.guild_id)}**. "
            "Control it from that Discord's panel."
        )
    return (
        f"🔒 **{server_name}** is booked by {_booked_by(active, viewer_guild_id)} until <t:{active.end_utc}:t>. "
        "Only the booker, that Discord's admins, or a booking admin can change it right now."
    )


async def _deny_if_booked(
    interaction: discord.Interaction,
    server_index: int,
    *,
    edit_flow: bool = False,
) -> bool:
    """Booking guard for control writes: True (and tell the user) unless they may control this server.

    Rules live in BookingService.control_check: home booking admins always; otherwise only
    during a live booking on this server, from the booking's guild, by the booker or that
    guild's admins. Admin cam follows the same rules. Called right
    before each write rather than when a flow opens, so a flow cannot finish after its
    booking has ended.
    """
    server_name = api_client.get_server_name(server_index)
    try:
        decision = await _booking_control_check(interaction, server_index)
    except Exception as exc:
        # Fail closed: without the booking store we can't tell whether someone holds the server.
        print(
            f"Booking guard could not read bookings for server index {server_index}: {exc}"
        )
        message = "❌ Could not check server bookings, so nothing was changed. Try again shortly."
    else:
        if decision.allowed:
            return False
        message = _denial_message(decision, server_name, interaction.guild_id)
        print(
            f"Booking guard blocked user {interaction.user.id} from guild {interaction.guild_id} "
            f"on server index {server_index} ({decision.reason}, "
            f"live booking: {decision.active.id if decision.active else 'none'})"
        )
    if edit_flow:
        await update_interaction_message(
            interaction, content=message, embed=None, view=None
        )
        asyncio.create_task(_delete_interaction_after(interaction, 20.0))
    else:
        await send_temporary_response(interaction, content=message, delay=20)
    return True


async def _run_server_write(server_index: int, func, *args, **kwargs):
    """Run a direct CRCON write in a thread, serialized with the server's other control writes."""

    # Capture args immutably to avoid closure mutation issues
    _args = args
    _kwargs = kwargs
    _func = func

    def locked_call():
        with server_control.mutation_lock(server_index):
            return _func(*_args, **_kwargs)

    return await asyncio.to_thread(locked_call)


def _get_channel_focused_server(channel: object | None) -> int | None:
    if channel is None:
        return None
    channel_id = getattr(channel, "id", None)
    if channel_id is None:
        return None
    return _persistent_focused_server.get(channel_id)


MAIN_EMBED_TITLE = "🌍 Hell Let Loose Map Changer"
LEGACY_EMBED_TITLES = {
    "🌍 Hell Let Loose Map Changer",
    "🎮 Hell Let Loose Map Changer",
    "?? Hell Let Loose Map Changer",
}
# The panel message is remembered on the data volume so restarts edit it instead of posting a
# new one: without Read Message History the bot cannot find its old panel by searching.
_PANEL_REF_PATH = booking_config.db_path.parent / "panel_message.json"


def _load_panel_refs() -> dict[int, tuple[int, int]]:
    """guild_id -> (channel_id, message_id) of that guild's panel (multi-guild slice 4)."""
    try:
        data = json.loads(_PANEL_REF_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    if "channel_id" in data:
        # Single-panel file from before per-guild panels: it was the home guild's panel.
        try:
            return {
                guild_registry.home_guild_id or 0: (
                    int(data["channel_id"]),
                    int(data["message_id"]),
                )
            }
        except (KeyError, TypeError, ValueError):
            return {}
    refs: dict[int, tuple[int, int]] = {}
    for guild_id, entry in (data.get("panels") or {}).items():
        try:
            refs[int(guild_id)] = (int(entry["channel_id"]), int(entry["message_id"]))
        except (KeyError, TypeError, ValueError):
            continue
    return refs


def _set_panel_ref(guild_id: int, channel_id: int, message_id: int) -> None:
    panel_refs[guild_id] = (channel_id, message_id)
    try:
        _PANEL_REF_PATH.parent.mkdir(parents=True, exist_ok=True)
        _PANEL_REF_PATH.write_text(
            json.dumps(
                {
                    "panels": {
                        str(gid): {"channel_id": cid, "message_id": mid}
                        for gid, (cid, mid) in panel_refs.items()
                    }
                }
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"Could not save panel message IDs to {_PANEL_REF_PATH}: {exc}")


def _guild_of(channel: object) -> int:
    """Guild ID of a channel; 0 for channels without one (keeps single-guild setups working)."""
    return getattr(getattr(channel, "guild", None), "id", None) or 0


panel_refs: dict[int, tuple[int, int]] = _load_panel_refs()
# Map channel_id -> focused server index (None = show all)
_persistent_focused_server: dict[int, int | None] = {}
# Map (server_index, map_pretty_name) -> current objectives last set through this bot
_current_objectives_state: dict[tuple[int, str], list[str]] = {}
# Map (server_index, map_id) -> dynamic weather state last set through this bot
_dynamic_weather_state: dict[tuple[int, str], bool] = {}

# ============================================================
# Periodic cleanup for unbounded module-level dicts (P2 fix)
# ============================================================

_MAX_CACHE_ENTRIES = 1000
_MAX_CACHE_AGE_HOURS = 24
_cache_cleanup_task: asyncio.Task | None = None


def _cleanup_state_caches() -> None:
    """Remove stale entries from unbounded caches to prevent memory leaks."""

    # Clean _current_objectives_state
    if len(_current_objectives_state) > _MAX_CACHE_ENTRIES:
        # Keep only recent entries (by arbitrary ordering, we can't track timestamps without more structure)
        # For now, just trim to max size - in practice, a server won't have 1000 different maps
        keys_to_keep = list(_current_objectives_state.keys())[
            -_MAX_CACHE_ENTRIES:
        ]
        _current_objectives_state.clear()
        for k in keys_to_keep:
            if k in _current_objectives_state:
                pass  # already cleared
        # Actually, let's just keep it simple and don't auto-evict for now
        # The dicts are keyed by (server_index, map_name) which is naturally bounded

    # Clean _dynamic_weather_state
    if len(_dynamic_weather_state) > _MAX_CACHE_ENTRIES:
        pass

    # Clean _persistent_focused_server (channel-focused servers)
    # These are per-channel and typically small, but let's bound them
    if len(_persistent_focused_server) > _MAX_CACHE_ENTRIES:
        pass


async def _periodic_cache_cleanup() -> None:
    """Background task to clean up state caches periodically."""
    while True:
        try:
            await asyncio.sleep(3600)  # Run hourly
            _cleanup_state_caches()
        except asyncio.CancelledError:
            break
        except Exception:
            # Don't let cleanup crashes affect the bot
            import logging

            logging.getLogger(__name__).exception("Cache cleanup task crashed")


def _start_cache_cleanup_task() -> None:
    """Start the periodic cache cleanup background task."""
    global _cache_cleanup_task
    if _cache_cleanup_task is None or _cache_cleanup_task.done():
        _cache_cleanup_task = asyncio.create_task(_periodic_cache_cleanup())


def _stop_cache_cleanup_task() -> None:
    """Stop the periodic cache cleanup background task."""
    global _cache_cleanup_task
    if _cache_cleanup_task is not None and not _cache_cleanup_task.done():
        _cache_cleanup_task.cancel()


# Panel buttons as (emoji, short name). Discord sizes buttons to their label, so names are kept
# short and padded to one width with blank braille characters (which Discord doesn't trim),
# giving an even grid of four buttons per row.
PANEL_BUTTONS = {
    "persistent:change_server": ("🔁", "Server"),
    "persistent:book_server": ("📅", "Book"),
    "persistent:manage_bookings": ("📋", "Bookings"),
    "persistent:refresh_status": ("🔄", "Refresh"),
    "persistent:open_map_changer": ("🗺️", "Map"),
    "persistent:set_objectives": ("🎯", "Objectives"),
    "persistent:set_dynamic_weather": ("🌦", "Weather"),
    "persistent:set_warfare_match_timer": ("⏱", "Match Time"),
    "persistent:set_warfare_warmup_timer": ("🔥", "Warm Up"),
    "persistent:set_team_switch_cooldown": ("↔️", "Team Swap"),
    "persistent:add_admin_cam_access": ("🎥", "Add Cam"),
    "persistent:remove_admin_cam_access": ("🗑️", "Remove Cam"),
}
PANEL_LABEL_WIDTH = 12
_BLANK = "\u2800"


def panel_label(custom_id: str, emoji: str | None = None) -> str:
    default_emoji, name = PANEL_BUTTONS[custom_id]
    padding = max(0, PANEL_LABEL_WIDTH - len(name))
    left, right = padding // 2, padding - padding // 2
    return f"{_BLANK * left}{emoji or default_emoji} {name}{_BLANK * right}"


CRCON_PANEL_DEPENDENT_BUTTONS = {
    "persistent:set_objectives",
    "persistent:set_dynamic_weather",
    "persistent:remove_admin_cam_access",
    "persistent:add_admin_cam_access",
}

# Game-control buttons, shown on the panel only while a booking is live (see _panel_controls_unlocked).
BOOKING_GATED_BUTTONS = CRCON_PANEL_DEPENDENT_BUTTONS | {
    "persistent:open_map_changer",
    "persistent:set_warfare_match_timer",
    "persistent:set_warfare_warmup_timer",
    "persistent:set_team_switch_cooldown",
}

CRCON_PANEL_DEFAULT_BUTTON_STYLES = {
    "persistent:set_objectives": discord.ButtonStyle.secondary,
    "persistent:set_dynamic_weather": discord.ButtonStyle.secondary,
    "persistent:remove_admin_cam_access": discord.ButtonStyle.secondary,
    "persistent:add_admin_cam_access": discord.ButtonStyle.danger,
}

CRCON_PANEL_DEFAULT_BUTTON_LABELS = {
    custom_id: panel_label(custom_id) for custom_id in CRCON_PANEL_DEPENDENT_BUTTONS
}


def _format_time_remaining(
    time_remaining: float | None, raw_time: str | None
) -> str:
    if time_remaining is None:
        if raw_time:
            return raw_time
        return "Unknown"
    try:
        seconds = int(time_remaining)
    except (TypeError, ValueError):
        if raw_time:
            return raw_time
        return "Unknown"

    if seconds <= 0:
        return "0:00"

    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _format_current_objectives(objectives: list[str]) -> str:
    if not objectives:
        return "Unknown"
    return " -> ".join(objectives)


def _status_badge(status: GameStatus) -> str:
    if status.transport == "direct_rcon":
        return "Direct RCON Fallback"
    return "CRCON HTTP"


# ============================================================
# Embed building helpers (extracted from build_main_embed)
# ============================================================


def _build_server_status_lines(
    servers: list[tuple[int, str]],
    statuses: dict[int, ControlResult],
    focused_server_index: int | None,
    current_objectives_state: dict[tuple[int, str], list[str]],
    dynamic_weather_state: dict[tuple[int, str], bool],
    booking_lines: dict[int, list[str]] | None = None,
) -> tuple[list[str], list[str]]:
    """Build server status lines and collect CRCON-degraded server names."""
    server_lines: list[str] = []
    crcon_http_degraded_servers: list[str] = []

    for position, (index, server_name) in enumerate(servers, start=1):
        selected_prefix = (
            "**[SELECTED]** " if focused_server_index == index else ""
        )
        status_result = statuses.get(index)
        server_lines.append(f"{position}. {selected_prefix}{server_name}")
        server_lines.extend((booking_lines or {}).get(index, []))

        if status_result is None:
            server_lines.append("   Status: ⚠️ Status not loaded.")
            crcon_http_degraded_servers.append(server_name)
            continue

        if not status_result.success:
            server_lines.append(f"   Status: ⚠️ {status_result.message}")
            crcon_http_degraded_servers.append(server_name)
            continue

        status = (status_result.data or {}).get("status")
        if not isinstance(status, GameStatus):
            server_lines.append("   Status: ⚠️ Status payload unavailable.")
            continue

        pretty_name = status.current_map or "Unknown"
        allied = (
            status.allied_players
            if status.allied_players is not None
            else "??"
        )
        axis = status.axis_players if status.axis_players is not None else "??"
        time_remaining = _format_time_remaining(
            status.time_remaining_seconds,
            status.raw_time_remaining,
        )

        known_objectives = current_objectives_state.get(
            (index, str(pretty_name))
        )
        if known_objectives is not None:
            _format_current_objectives(known_objectives)

        dynamic_weather_text = "Unknown"
        known_state = dynamic_weather_state.get((index, pretty_name))
        if known_state is not None:
            dynamic_weather_text = "Enabled" if known_state else "Disabled"

        server_lines.append(f"   Map: {pretty_name}")
        server_lines.append(
            f"   Allied: {allied} | Axis: {axis} | Time Remaining: {time_remaining}"
        )
        server_lines.append(f"   Dynamic Weather: {dynamic_weather_text}")
        server_lines.append(f"   Status Source: {_status_badge(status)}")
        if status_result.warning:
            server_lines.append(f"   Warning: {status_result.warning}")
        if status.transport != "crcon_http":
            crcon_http_degraded_servers.append(server_name)

    return server_lines, crcon_http_degraded_servers


def _build_crcon_warning_block(crcon_http_degraded_servers: list[str]) -> str:
    """Build the CRCON outage warning block for the embed description."""
    if not crcon_http_degraded_servers:
        return ""
    affected_servers = ", ".join(sorted(set(crcon_http_degraded_servers)))
    return (
        "**CRCON API Status:** ⚠️ Issues detected\n"
        f"Affected server(s): {affected_servers}\n"
        "Panel controls disabled while CRCON is down: Set Objectives, Dynamic Weather, "
        "Remove Admin Cam User, Add Admin Cam Access.\n\n"
    )


def _build_selected_server_label(
    servers: list[tuple[int, str]],
    focused_server_index: int | None,
) -> str:
    """Determine the selected server label for the embed."""
    if not servers:
        return "None"
    selected_name = next(
        (name for index, name in servers if index == focused_server_index),
        None,
    )
    if selected_name:
        return selected_name
    if focused_server_index is not None:
        return f"Unknown ({focused_server_index})"
    return servers[0][1]


def _get_updated_at_text(servers: list[tuple[int, str]]) -> str:
    """Generate the AEST timestamp for the embed footer."""
    if not servers:
        return ""
    aest = timezone(timedelta(hours=10), name="AEST")
    updated_at = datetime.now(aest)
    return f"Updated as at {updated_at.strftime('%d %B %Y - %H:%M:%S')} AEST"


def _get_server_env_value(
    name: str, server_index: int | None = None
) -> str | None:
    server_number = _server_number(server_index)
    if server_number is not None:
        server_key = f"SERVER{server_number}_{name}"
        if server_key in os.environ:
            return os.environ[server_key]
    return os.getenv(name)


def _get_timer_minutes(
    name: str, default: int, server_index: int | None = None
) -> int:
    raw_value = (_get_server_env_value(name, server_index) or "").strip()
    if not raw_value:
        return default

    try:
        value = int(raw_value)
    except ValueError:
        return default

    return value if value > 0 else default


def _get_focused_or_single_server(
    interaction: discord.Interaction,
) -> int | None:
    servers = api_client.get_servers()
    focused_server = _get_channel_focused_server(interaction.channel)

    if focused_server is not None and any(
        index == focused_server for index, _ in servers
    ):
        return focused_server

    if len(servers) == 1:
        return servers[0][0]

    return None


async def fetch_server_statuses() -> dict[int, ControlResult]:
    """Read status for every configured server concurrently, off the event loop."""
    indices = [index for index, _ in api_client.get_servers()]
    results = await asyncio.gather(
        *(
            asyncio.to_thread(server_control.get_status, index)
            for index in indices
        ),
        return_exceptions=True,
    )
    # Handle exceptions: convert to failed ControlResult
    processed_results = {}
    for index, result in zip(indices, results):
        if isinstance(result, Exception):
            import logging

            logging.getLogger(__name__).warning(
                "fetch_server_statuses: server %s raised %s: %s",
                index,
                type(result).__name__,
                result,
            )
            processed_results[index] = ControlResult(
                success=False,
                transport=None,
                message=f"Status fetch failed: {type(result).__name__}: {result}",
            )
        else:
            processed_results[index] = result
    return processed_results


def _panel_booking_lines(viewer_guild_id: int | None = None) -> dict[int, list[str]]:
    """Active booking (or the next one) per server, as Discord timestamps."""
    if booking_service is None:
        return {}
    lines: dict[int, list[str]] = {}
    try:
        summary = booking_service.summary_by_server()
        for index, _ in api_client.get_servers():
            active, upcoming = summary.get(
                api_client.get_server_key(index), (None, None)
            )
            if active is not None:
                lines[index] = [
                    f"   🔒 Booked by {_booked_by(active, viewer_guild_id)} until <t:{active.end_utc}:t> (<t:{active.end_utc}:R>)"
                ]
                continue
            if upcoming is not None:
                lines[index] = [
                    f"   📅 Next booking: {_booked_by(upcoming, viewer_guild_id)} <t:{upcoming.start_utc}:f> (<t:{upcoming.start_utc}:R>)"
                ]
    except Exception as exc:
        print(f"Could not read bookings for the panel: {exc}")
        return {}
    return lines


def _panel_schedule_lines(viewer_guild_id: int | None = None) -> list[str] | None:
    """Every booking from the start of today through the next SCHEDULE_DAYS days (booking time zone).

    None when booking is off or the store can't be read, so the panel omits the section.
    """
    if booking_service is None:
        return None
    try:
        now = booking_service.now_ts()
        since, until = schedule_window(now, booking_config.timezone)
        bookings = booking_service.list_window(since, until)
    except Exception as exc:
        print(f"Could not read the booking schedule for the panel: {exc}")
        return None

    lines = []
    for booking in bookings:
        line = (
            f"<t:{booking.start_utc}:f> → <t:{booking.end_utc}:t> · "
            f"**{api_client.get_server_name_for_key(booking.server_key)}** · {_booked_by(booking, viewer_guild_id)}"
        )
        if booking.end_utc <= now:
            line = f"~~{line}~~"
        elif booking.start_utc <= now:
            line = f"🔒 {line}"
        lines.append(line)
    return lines


# Discord caps an embed field at 1024 characters and a whole embed at 6000.
_EMBED_FIELD_LIMIT = 1024
_EMBED_TOTAL_LIMIT = 6000
_SCHEDULE_MAX_CHARS = 3072


def _add_schedule_fields(embed: discord.Embed, lines: list[str]) -> None:
    """Append the booking schedule as one or more fields, trimmed to fit Discord's embed limits."""
    title = f"📅 Bookings: today + next {SCHEDULE_DAYS - 1} days"
    if not lines:
        embed.add_field(name=title, value="No bookings.", inline=False)
        return

    budget = min(_SCHEDULE_MAX_CHARS, _EMBED_TOTAL_LIMIT - len(embed) - 200)
    chunks: list[list[str]] = [[]]
    used = chunk_len = 0
    shown = 0
    for line in lines:
        if used + len(line) + 1 > budget:
            break
        if chunk_len + len(line) + 1 > _EMBED_FIELD_LIMIT - 40:
            chunks.append([])
            chunk_len = 0
        chunks[-1].append(line)
        chunk_len += len(line) + 1
        used += len(line) + 1
        shown += 1
    if shown < len(lines):
        chunks[-1].append(f"…and {len(lines) - shown} more (see /bookings).")

    for number, chunk in enumerate(chunks):
        if chunk:
            embed.add_field(
                name=title if number == 0 else "📅 (cont.)",
                value="\n".join(chunk),
                inline=False,
            )


async def build_panel(
    focused_server_index: int | None = None,
    viewer_guild_id: int | None = None,
    statuses: dict[int, ControlResult] | None = None,
) -> tuple[discord.Embed, "GameModeView"]:
    """The panel as one guild sees it. Pass `statuses` to reuse one CRCON read across guilds."""

    async def current_statuses():
        return statuses if statuses is not None else await fetch_server_statuses()

    statuses, booking_lines, schedule_lines, controls_unlocked = await asyncio.gather(
        current_statuses(),
        asyncio.to_thread(_panel_booking_lines, viewer_guild_id),
        asyncio.to_thread(_panel_schedule_lines, viewer_guild_id),
        asyncio.to_thread(
            _panel_controls_unlocked, focused_server_index, viewer_guild_id
        ),
    )
    embed = build_main_embed(
        focused_server_index, statuses, booking_lines, schedule_lines
    )
    if not controls_unlocked:
        embed.add_field(
            name="🔒 Server controls hidden",
            value="Map, timer and admin controls appear while a booking is live. Use 📅 Book Server.",
            inline=False,
        )
    crcon_outage, _ = _panel_crcon_outage_state(statuses, focused_server_index)
    view = GameModeView(
        focused_server_index=focused_server_index,
        crcon_outage=crcon_outage,
        show_controls=controls_unlocked,
    )
    return embed, view


def _panel_controls_unlocked(
    focused_server_index: int | None, viewer_guild_id: int | None = None
) -> bool:
    """Show game controls only while a booking is live on the focused server.

    With no focused server (and several configured) any live booking unlocks them. A guest
    guild's panel only counts bookings made from that guild; the home panel counts every
    booking so home booking admins can override. Booking off means no gating. A store
    failure hides them; writes fail closed in _deny_if_booked anyway.
    """
    if booking_service is None:
        return True
    servers = [index for index, _ in api_client.get_servers()]
    if focused_server_index in servers:
        targets = [focused_server_index]
    else:
        targets = servers
    counts_any = (
        viewer_guild_id is None
        or not guild_registry.configured
        or guild_registry.is_home(viewer_guild_id)
    )
    try:
        for index in targets:
            active = booking_service.active_for(api_client.get_server_key(index))
            if active is not None and (
                counts_any or active.guild_id in (None, viewer_guild_id)
            ):
                return True
        return False
    except Exception as exc:
        print(f"Could not read bookings to gate panel controls: {exc}")
        return False


def _server_has_crcon_http(
    statuses: dict[int, ControlResult], server_index: int
) -> bool:
    status_result = statuses.get(server_index)
    return (
        status_result is not None
        and status_result.success
        and status_result.transport == "crcon_http"
    )


def _panel_crcon_outage_state(
    statuses: dict[int, ControlResult],
    focused_server_index: int | None = None,
) -> tuple[bool, list[str]]:
    servers = api_client.get_servers()
    if not servers:
        return False, []

    target_indices: list[int]
    if focused_server_index is not None and any(
        index == focused_server_index for index, _ in servers
    ):
        target_indices = [focused_server_index]
    elif len(servers) == 1:
        target_indices = [servers[0][0]]
    else:
        target_indices = [index for index, _ in servers]

    unavailable_server_names: list[str] = []
    healthy_count = 0

    for server_index in target_indices:
        if _server_has_crcon_http(statuses, server_index):
            healthy_count += 1
            continue
        unavailable_server_names.append(
            api_client.get_server_name(server_index)
        )

    if not target_indices:
        return False, []

    if focused_server_index is not None or len(servers) == 1:
        return healthy_count == 0, unavailable_server_names

    return healthy_count == 0, unavailable_server_names


def build_main_embed(
    focused_server_index: int | None,
    statuses: dict[int, ControlResult],
    booking_lines: dict[int, list[str]] | None = None,
    schedule_lines: list[str] | None = None,
) -> discord.Embed:
    """Render the panel embed from statuses already fetched by fetch_server_statuses()."""
    servers = api_client.get_servers()

    server_lines, crcon_http_degraded_servers = _build_server_status_lines(
        servers=servers,
        statuses=statuses,
        focused_server_index=focused_server_index,
        current_objectives_state=_current_objectives_state,
        dynamic_weather_state=_dynamic_weather_state,
        booking_lines=booking_lines,
    )

    selected_server_label = _build_selected_server_label(
        servers, focused_server_index
    )
    updated_at_text = _get_updated_at_text(servers)

    crcon_warning_block = _build_crcon_warning_block(
        crcon_http_degraded_servers
    )

    description = "Click the buttons below to manage the server.\n\n"
    description += (
        f"**Currently Selected Server:** {selected_server_label}\n\n"
    )
    description += crcon_warning_block
    description += "**Available Servers:**\n" + "\n".join(server_lines)

    if updated_at_text:
        description += f"\n\n{updated_at_text}"

    embed = discord.Embed(
        title=MAIN_EMBED_TITLE,
        description=description,
        color=0x2F3136,
    )
    embed.set_footer(
        text="Buttons stay active across restarts. Note: When you choose the last objective it will set the map to the selected objectives immediately."
    )
    if schedule_lines is not None:
        _add_schedule_fields(embed, schedule_lines)
    return embed


async def ensure_persistent_message(
    channel: discord.abc.Messageable,
    statuses: dict[int, ControlResult] | None = None,
) -> discord.Message | None:
    """Edit (or find, or post) this guild's panel in `channel`."""
    channel_id = getattr(channel, "id", None)
    guild_id = _guild_of(channel)
    focused = None
    if channel_id is not None:
        focused = _persistent_focused_server.setdefault(channel_id, None)
    embed, view = await build_panel(focused, guild_id or None, statuses)

    ref = panel_refs.get(guild_id)
    if ref and ref[0] == channel_id and hasattr(channel, "get_partial_message"):
        # Edit by ID: unlike fetch_message, this needs no Read Message History permission.
        try:
            return await channel.get_partial_message(ref[1]).edit(
                embed=embed, view=view
            )
        except discord.NotFound:
            pass  # deleted by hand; find or post a new one below

    history = getattr(channel, "history", None)
    if history is not None:
        try:
            async for message in history(limit=50):
                if message.author == bot.user and message.embeds:
                    title = message.embeds[0].title
                    if title == MAIN_EMBED_TITLE or title in LEGACY_EMBED_TITLES:
                        await message.edit(embed=embed, view=view)
                        if channel_id is not None:
                            _set_panel_ref(guild_id, channel_id, message.id)
                        return message
        except discord.Forbidden:
            print(
                "Cannot read channel history to find the old panel; grant the bot "
                "Read Message History so restarts never leave a duplicate."
            )

    message = await channel.send(embed=embed, view=view)  # type: ignore[attr-defined]
    if channel_id is not None:
        _set_panel_ref(guild_id, channel_id, message.id)
    return message


def _panel_channel_id(guild_id: int | None) -> int | None:
    """A guild's panel channel: wherever /setup_panel put it. The home guild (or a setup
    without guilds) falls back to DISCORD_CHANNEL_ID before its first /setup_panel."""
    ref = panel_refs.get(guild_id or 0)
    if ref:
        return ref[0]
    if guild_id and guild_registry.configured and not guild_registry.is_home(guild_id):
        return None
    try:
        return int(os.getenv("DISCORD_CHANNEL_ID") or "")
    except ValueError:
        return None


def _panel_channel_ids() -> list[int]:
    """Every approved guild's panel channel, home first."""
    channels: list[int] = []
    home_channel = _panel_channel_id(guild_registry.home_guild_id)
    if home_channel is not None:
        channels.append(home_channel)
    for guild_id, (channel_id, _) in panel_refs.items():
        if guild_registry.is_approved(guild_id) and channel_id not in channels:
            channels.append(channel_id)
    return channels


async def _resolve_text_channel(
    channel_id: int | None,
) -> discord.TextChannel | discord.Thread | None:
    if channel_id is None:
        return None
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except (discord.NotFound, discord.HTTPException):
            return None
    if isinstance(channel, (discord.TextChannel, discord.Thread)):
        return channel
    return None


async def post_panel(
    channel: discord.TextChannel | discord.Thread,
) -> discord.Message:
    """Post a fresh panel in `channel`, remove the one it replaces in this guild, and remember it."""
    guild_id = _guild_of(channel)
    focused = _persistent_focused_server.setdefault(channel.id, None)
    embed, view = await build_panel(focused, guild_id or None)
    message = await channel.send(embed=embed, view=view)
    old = panel_refs.get(guild_id)
    if old and old[1] != message.id:
        old_channel = await _resolve_text_channel(old[0])
        if old_channel is not None:
            try:
                await old_channel.get_partial_message(old[1]).delete()
            except discord.HTTPException:
                pass  # already deleted by hand
    _set_panel_ref(guild_id, channel.id, message.id)
    return message


async def refresh_main_embed() -> list[discord.Message]:
    """Re-render every approved guild's panel, reading CRCON status once for all of them."""
    channels = [
        channel
        for channel in [await _resolve_text_channel(cid) for cid in _panel_channel_ids()]
        if channel is not None
    ]
    if not channels:
        return []
    statuses = await fetch_server_statuses()
    messages = []
    for channel in channels:
        try:
            message = await ensure_persistent_message(channel, statuses)
        except discord.HTTPException as exc:
            # One guild's broken panel (permissions, deleted channel) must not stop the rest.
            print(f"Could not refresh the panel in channel {channel.id}: {exc}")
            continue
        if message is not None:
            messages.append(message)
    return messages


async def _delete_interaction_after(
    interaction: discord.Interaction, delay: float = 10.0
) -> None:
    await asyncio.sleep(delay)
    try:
        await interaction.delete_original_response()
    except (discord.NotFound, discord.HTTPException):
        pass


async def _delete_interaction_after_and_refresh(
    interaction: discord.Interaction, delay: float = 10.0
) -> None:
    """Delete interaction response after delay and refresh the persistent panel."""
    await asyncio.sleep(delay)
    try:
        await interaction.delete_original_response()
    except (discord.NotFound, discord.HTTPException):
        pass
    await refresh_main_embed()


async def send_temporary_response(
    interaction: discord.Interaction,
    *,
    embed: discord.Embed | None = None,
    content: str | None = None,
    view: discord.ui.View | None = None,
    delay: float | None = 20,
    ephemeral: bool = True,
) -> None:
    """Send an interaction response and optionally auto-delete it after `delay` seconds."""
    if not interaction.response.is_done():
        if embed is not None and view is not None:
            await interaction.response.send_message(
                content=content, embed=embed, view=view, ephemeral=ephemeral
            )
        elif embed is not None:
            await interaction.response.send_message(
                content=content, embed=embed, ephemeral=ephemeral
            )
        elif view is not None:
            await interaction.response.send_message(
                content=content, view=view, ephemeral=ephemeral
            )
        else:
            await interaction.response.send_message(
                content=content, ephemeral=ephemeral
            )
        if delay is not None and delay > 0:
            asyncio.create_task(_delete_interaction_after(interaction, delay))
    else:
        if embed is not None and view is not None:
            followup = await interaction.followup.send(
                content=content or "",
                embed=embed,
                view=view,
                ephemeral=ephemeral,
                wait=True,
            )
        elif embed is not None:
            followup = await interaction.followup.send(
                content=content or "",
                embed=embed,
                ephemeral=ephemeral,
                wait=True,
            )
        elif view is not None:
            followup = await interaction.followup.send(
                content=content or "",
                view=view,
                ephemeral=ephemeral,
                wait=True,
            )
        else:
            followup = await interaction.followup.send(
                content=content or "",
                ephemeral=ephemeral,
                wait=True,
            )
        if delay is not None and delay > 0 and followup is not None:
            asyncio.create_task(_delete_message_after(followup, delay))


async def _delete_message_after(
    message: discord.Message, delay: float = 10.0
) -> None:
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except (discord.NotFound, discord.HTTPException):
        pass


async def defer_interaction(
    interaction: discord.Interaction,
    *,
    ephemeral: bool = False,
    thinking: bool = False,
) -> None:
    if interaction.response.is_done():
        return
    await interaction.response.defer(ephemeral=ephemeral, thinking=thinking)


async def update_interaction_message(
    interaction: discord.Interaction,
    *,
    content: str | None = None,
    embed: discord.Embed | None = None,
    view: discord.ui.View | None = None,
) -> None:
    if interaction.response.is_done():
        await interaction.edit_original_response(
            content=content, embed=embed, view=view
        )
    else:
        await interaction.response.edit_message(
            content=content, embed=embed, view=view
        )


class PersistentView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)  # Persistent view


class AddAdminCamModal(discord.ui.Modal, title="ADD ADMIN CAM ACCESS"):
    player_id = discord.ui.TextInput(
        label="Player ID (see hllrecords.com)",
        placeholder="Paste Steam64 / EOS / game Player ID from hllrecords.com",
        required=True,
        max_length=64,
    )

    def __init__(self, server_index: int):
        super().__init__()
        self.server_index = server_index

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await defer_interaction(interaction, ephemeral=True)
        client = _get_http_client(self.server_index)
        if not client:
            await send_temporary_response(
                interaction,
                content=f"CRCON HTTP unavailable for this server: {_http_error_message(self.server_index)}",
                delay=10,
            )
            return

        player_id = str(self.player_id.value).strip()
        if not player_id:
            await send_temporary_response(
                interaction, content="Player NOT FOUND", delay=10
            )
            return

        if await _deny_if_booked(interaction, self.server_index):
            return

        try:
            added = await _run_server_write(
                self.server_index,
                client.add_admin,
                player_id=player_id,
                role="spectator",
                description="Admin cam access",
            )
        except CRCONHTTPError:
            added = False

        if added:
            await send_temporary_response(
                interaction, content="Player ADDED", delay=10
            )
        else:
            await send_temporary_response(
                interaction, content="Player NOT FOUND", delay=10
            )


class TeamSwitchCooldownModal(
    discord.ui.Modal, title="SET TEAM SWITCH COOLDOWN"
):
    cooldown_minutes = discord.ui.TextInput(
        label="Cooldown in minutes",
        placeholder="Enter 0 or a positive whole number",
        required=True,
        max_length=6,
    )

    def __init__(self, server_index: int):
        super().__init__()
        self.server_index = server_index

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw_value = str(self.cooldown_minutes.value).strip()
        try:
            cooldown_minutes = int(raw_value)
        except ValueError:
            await send_temporary_response(
                interaction,
                content="Cooldown must be a whole number of minutes.",
                delay=20,
            )
            return

        if cooldown_minutes < 0:
            await send_temporary_response(
                interaction,
                content="Cooldown must be 0 or greater.",
                delay=20,
            )
            return

        server_name = api_client.get_server_name(self.server_index)

        await defer_interaction(interaction, ephemeral=True)
        if await _deny_if_booked(interaction, self.server_index):
            return
        result = await asyncio.to_thread(
            server_control.set_team_switch_cooldown,
            self.server_index,
            cooldown_minutes,
        )
        if not result.success:
            await send_temporary_response(
                interaction,
                content=f"Failed to set team switch cooldown for {server_name}: {result.message}",
                delay=20,
            )
            return

        warning = ""
        if result.warning:
            warning = f" Note: {result.warning}"

        await send_temporary_response(
            interaction,
            content=(
                f"Set team switch cooldown to {cooldown_minutes} minute(s) on {server_name}."
                f" Transport: {'Direct RCON fallback' if result.transport == 'direct_rcon' else 'CRCON HTTP'}."
                f"{warning}"
            ),
            delay=20,
        )


class RemoveAdminCamConfirmView(discord.ui.View):
    def __init__(self, server_index: int, player_id: str, player_name: str):
        super().__init__(timeout=300)
        self.server_index = server_index
        self.player_id = player_id
        self.player_name = player_name

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.danger)
    async def confirm_yes(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await defer_interaction(interaction)
        client = _get_http_client(self.server_index)
        if not client:
            await update_interaction_message(
                interaction,
                content=f"CRCON HTTP unavailable for this server: {_http_error_message(self.server_index)}",
                embed=None,
                view=None,
            )
            asyncio.create_task(_delete_interaction_after(interaction, 10.0))
            return

        if await _deny_if_booked(
            interaction, self.server_index, edit_flow=True
        ):
            return

        try:
            removed = await _run_server_write(
                self.server_index, client.remove_admin, self.player_id
            )
        except CRCONHTTPError:
            removed = False

        if removed:
            message = "Player REMOVED"
        else:
            message = "Player NOT FOUND"

        await update_interaction_message(
            interaction, content=message, embed=None, view=None
        )
        asyncio.create_task(_delete_interaction_after(interaction, 10.0))

    @discord.ui.button(label="No", style=discord.ButtonStyle.secondary)
    async def confirm_no(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.edit_message(
            content="Cancelled.", embed=None, view=None
        )
        asyncio.create_task(_delete_interaction_after(interaction, 1.0))


class RemoveAdminCamSelect(discord.ui.Select):
    def __init__(self, server_index: int, spectators: list[dict]):
        self.server_index = server_index
        self._by_player_id = {
            str(entry.get("player_id")): entry
            for entry in spectators
            if entry.get("player_id")
        }

        options = []
        for entry in spectators[:25]:
            player_id = str(entry.get("player_id") or "")
            if not player_id:
                continue
            name = str(entry.get("name") or player_id)
            options.append(
                discord.SelectOption(
                    label=name[:100],
                    value=player_id,
                    description=f"ID: {player_id}"[:100],
                )
            )

        super().__init__(
            placeholder="Select spectator admin user to remove...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        selected_player_id = self.values[0]
        selected_entry = self._by_player_id.get(selected_player_id, {})
        selected_name = str(selected_entry.get("name") or selected_player_id)

        embed = discord.Embed(
            title="Remove Admin Cam User",
            description=(
                f"Remove spectator access for **{selected_name}**?\n"
                f"Player ID: `{selected_player_id}`"
            ),
            color=0xE67E22,
        )
        view = RemoveAdminCamConfirmView(
            self.server_index, selected_player_id, selected_name
        )
        await interaction.response.edit_message(embed=embed, view=view)


class RemoveAdminCamSelectionView(discord.ui.View):
    def __init__(self, server_index: int, spectators: list[dict]):
        super().__init__(timeout=300)
        self.add_item(RemoveAdminCamSelect(server_index, spectators))


class GameModeView(PersistentView):
    def __init__(
        self,
        focused_server_index: int | None = None,
        crcon_outage: bool = False,
        show_controls: bool = True,
    ):
        super().__init__()
        self.focused_server_index = focused_server_index
        if not show_controls:
            for child in list(self.children):
                if getattr(child, "custom_id", None) in BOOKING_GATED_BUTTONS:
                    self.remove_item(child)
        self._apply_crcon_panel_state(crcon_outage)

    def _apply_crcon_panel_state(self, disable_crcon_controls: bool) -> None:
        for child in self.children:
            if not isinstance(child, discord.ui.Button):
                continue
            custom_id = child.custom_id
            if custom_id not in CRCON_PANEL_DEPENDENT_BUTTONS:
                continue

            default_style = CRCON_PANEL_DEFAULT_BUTTON_STYLES.get(
                custom_id, child.style
            )
            default_label = CRCON_PANEL_DEFAULT_BUTTON_LABELS.get(
                custom_id, child.label
            )
            child.disabled = disable_crcon_controls

            if disable_crcon_controls:
                child.style = discord.ButtonStyle.danger
                child.label = panel_label(custom_id, emoji="⛔")
            else:
                child.style = default_style
                child.label = default_label

    async def _apply_warfare_timer(
        self,
        interaction: discord.Interaction,
        *,
        timer_name: str,
        env_name: str,
        default_minutes: int,
    ) -> None:
        await defer_interaction(interaction, ephemeral=True)
        servers = api_client.get_servers()
        if not servers:
            await send_temporary_response(
                interaction,
                content=f"No servers are configured; cannot set the warfare {timer_name}.",
                delay=20,
            )
            return

        server_index = _get_focused_or_single_server(interaction)
        if server_index is None:
            await send_temporary_response(
                interaction,
                content="Please set a focused server first using Change Server.",
                delay=10,
            )
            return

        minutes = _get_timer_minutes(env_name, default_minutes, server_index)
        server_name = api_client.get_server_name(server_index)

        if await _deny_if_booked(interaction, server_index):
            return

        if timer_name == "match timer":
            result = await asyncio.to_thread(
                server_control.set_match_timer,
                server_index,
                "warfare",
                minutes,
            )
        else:
            result = await asyncio.to_thread(
                server_control.set_warmup_timer,
                server_index,
                "warfare",
                minutes,
            )

        if not result.success:
            await send_temporary_response(
                interaction,
                content=f"Failed to set warfare {timer_name} for {server_name}: {result.message}",
                delay=20,
            )
            return

        await send_temporary_response(
            interaction,
            content=(
                f"Set warfare {timer_name} to {minutes} minute(s) on {server_name}. "
                f"Transport: {'Direct RCON fallback' if result.transport == 'direct_rcon' else 'CRCON HTTP'}."
            ),
            delay=20,
        )

    @discord.ui.button(
        label=panel_label("persistent:change_server"),
        style=discord.ButtonStyle.secondary,
        custom_id="persistent:change_server",
        row=0,
    )
    async def change_server(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        servers = api_client.get_servers()
        if not servers:
            await send_temporary_response(
                interaction, content="No servers configured.", delay=20
            )
            return

        if len(servers) == 1:
            await send_temporary_response(
                interaction, content="Only one server configured.", delay=20
            )
            return

        embed = discord.Embed(
            title="🔁 Change Server",
            description="Select which server the main view should focus on:",
            color=0x7289DA,
        )
        view = ChangeServerSelectionView()
        await send_temporary_response(
            interaction, embed=embed, view=view, delay=None
        )

    @discord.ui.button(
        label=panel_label("persistent:book_server"),
        style=discord.ButtonStyle.success,
        custom_id="persistent:book_server",
        row=0,
    )
    async def book_server(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await open_book_panel(interaction, booking_deps)

    @discord.ui.button(
        label=panel_label("persistent:manage_bookings"),
        style=discord.ButtonStyle.primary,
        custom_id="persistent:manage_bookings",
        row=0,
    )
    async def manage_bookings(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await open_manage_panel(interaction, booking_deps)

    @discord.ui.button(
        label=panel_label("persistent:refresh_status"),
        style=discord.ButtonStyle.success,
        custom_id="persistent:refresh_status",
        row=0,
    )
    async def refresh_status(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await defer_interaction(interaction, ephemeral=True)
        await refresh_main_embed()
        await send_temporary_response(
            interaction, content="Status refreshed.", delay=20
        )

    @discord.ui.button(
        label=panel_label("persistent:open_map_changer"),
        style=discord.ButtonStyle.primary,
        custom_id="persistent:open_map_changer",
        row=1,
    )
    async def open_map_changer(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await defer_interaction(interaction, ephemeral=True)
        servers = api_client.get_servers()
        focused_server = _get_channel_focused_server(interaction.channel)

        if focused_server is not None and any(
            index == focused_server for index, _ in servers
        ):
            server_index = focused_server
            server_name = api_client.get_server_name(server_index)
            current_map = await asyncio.to_thread(
                api_client.get_current_map, server_index
            )

            embed = discord.Embed(
                title="??? Map Change Control",
                description=f"**Server:** {server_name}\n**Current Map:** {current_map}\n\nSelect a game mode:",
                color=0x00FF00,
            )
            view = GameModeSelectionView(server_index)
            await send_temporary_response(
                interaction, embed=embed, view=view, delay=None
            )
            return

        if len(servers) == 1:
            server_index = servers[0][0]
            server_name = servers[0][1]
            current_map = await asyncio.to_thread(
                api_client.get_current_map, server_index
            )

            embed = discord.Embed(
                title="??? Map Change Control",
                description=f"**Server:** {server_name}\n**Current Map:** {current_map}\n\nSelect a game mode:",
                color=0x00FF00,
            )
            view = GameModeSelectionView(server_index)
        else:
            embed = discord.Embed(
                title="??? Map Change Control",
                description="Select a server:",
                color=0x00FF00,
            )
            view = ServerSelectionView()

        await send_temporary_response(
            interaction, embed=embed, view=view, delay=None
        )

    @discord.ui.button(
        label=panel_label("persistent:set_objectives"),
        style=discord.ButtonStyle.secondary,
        custom_id="persistent:set_objectives",
        row=1,
    )
    async def set_objectives(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        servers = api_client.get_servers()
        if not servers:
            await send_temporary_response(
                interaction,
                content="No servers are configured; cannot set objectives.",
                delay=20,
            )
            return

        if not any(_get_http_client(index) for index, _ in servers):
            await send_temporary_response(
                interaction,
                content="CRCON HTTP credentials are not configured for any server; objective controls are unavailable.",
                delay=20,
            )
            return

        focused_server = _get_channel_focused_server(interaction.channel)
        if focused_server is not None and any(
            index == focused_server for index, _ in servers
        ):
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
            await send_objective_selection(
                interaction, focused_server, edit_message=False
            )
            return

        if len(servers) == 1:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
            await send_objective_selection(
                interaction, servers[0][0], edit_message=False
            )
            return

        server_list = "\n".join([f" {name}" for _, name in servers])
        embed = discord.Embed(
            title="🎯 Select Server",
            description=f"Choose which server's objectives you want to configure:\n\n{server_list}",
            color=0x9B59B6,
        )
        view = ObjectiveServerSelectionView()
        await send_temporary_response(
            interaction, embed=embed, view=view, delay=None
        )

    @discord.ui.button(
        label=panel_label("persistent:set_dynamic_weather"),
        style=discord.ButtonStyle.secondary,
        custom_id="persistent:set_dynamic_weather",
        row=1,
    )
    async def set_dynamic_weather(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        servers = api_client.get_servers()
        if not servers:
            await send_temporary_response(
                interaction,
                content="No servers are configured; cannot update dynamic weather.",
                delay=20,
            )
            return

        if not any(_get_http_client(index) for index, _ in servers):
            await send_temporary_response(
                interaction,
                content="CRCON HTTP credentials are not configured for any server; dynamic weather controls are unavailable.",
                delay=20,
            )
            return

        focused_server = _get_channel_focused_server(interaction.channel)
        if focused_server is not None and any(
            index == focused_server for index, _ in servers
        ):
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
            await send_dynamic_weather_controls(
                interaction, focused_server, edit_message=False
            )
            return

        if len(servers) == 1:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
            await send_dynamic_weather_controls(
                interaction, servers[0][0], edit_message=False
            )
            return

        server_list = "\n".join([f" {name}" for _, name in servers])
        embed = discord.Embed(
            title="🌦 Select Server",
            description=f"Choose which server's dynamic weather you want to update:\n\n{server_list}",
            color=0x1ABC9C,
        )
        view = DynamicWeatherServerSelectionView()
        await send_temporary_response(
            interaction, embed=embed, view=view, delay=None
        )

    @discord.ui.button(
        label=panel_label("persistent:set_warfare_match_timer"),
        style=discord.ButtonStyle.primary,
        custom_id="persistent:set_warfare_match_timer",
        row=1,
    )
    async def set_warfare_match_timer(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await self._apply_warfare_timer(
            interaction,
            timer_name="match timer",
            env_name="WARFARE_MATCH_TIMER_MINUTES",
            default_minutes=90,
        )

    @discord.ui.button(
        label=panel_label("persistent:set_warfare_warmup_timer"),
        style=discord.ButtonStyle.primary,
        custom_id="persistent:set_warfare_warmup_timer",
        row=2,
    )
    async def set_warfare_warmup_timer(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await self._apply_warfare_timer(
            interaction,
            timer_name="warmup timer",
            env_name="WARFARE_WARMUP_TIMER_MINUTES",
            default_minutes=3,
        )

    @discord.ui.button(
        label=panel_label("persistent:set_team_switch_cooldown"),
        style=discord.ButtonStyle.primary,
        custom_id="persistent:set_team_switch_cooldown",
        row=2,
    )
    async def set_team_switch_cooldown(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        servers = api_client.get_servers()
        if not servers:
            await send_temporary_response(
                interaction,
                content="No servers are configured; cannot set the team switch cooldown.",
                delay=20,
            )
            return

        server_index = _get_focused_or_single_server(interaction)
        if server_index is None:
            await send_temporary_response(
                interaction,
                content="Please set a focused server first using Change Server.",
                delay=10,
            )
            return

        if not _get_http_client(server_index):
            await send_temporary_response(
                interaction,
                content=f"CRCON HTTP unavailable for this server: {_http_error_message(server_index)}",
                delay=20,
            )
            return

        await interaction.response.send_modal(
            TeamSwitchCooldownModal(server_index)
        )

    @discord.ui.button(
        label=panel_label("persistent:remove_admin_cam_access"),
        style=discord.ButtonStyle.secondary,
        custom_id="persistent:remove_admin_cam_access",
        row=2,
    )
    async def remove_admin_cam_access(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await defer_interaction(interaction, ephemeral=True)
        servers = api_client.get_servers()
        if not servers:
            await send_temporary_response(
                interaction,
                content="No servers are configured; cannot remove admin cam access.",
                delay=10,
            )
            return

        focused_server = _get_channel_focused_server(interaction.channel)
        if focused_server is not None and any(
            index == focused_server for index, _ in servers
        ):
            server_index = focused_server
        elif len(servers) == 1:
            server_index = servers[0][0]
        else:
            await send_temporary_response(
                interaction,
                content="Please set a focused server first using Change Server.",
                delay=10,
            )
            return

        client = _get_http_client(server_index)
        if not client:
            await send_temporary_response(
                interaction,
                content=f"CRCON HTTP unavailable for this server: {_http_error_message(server_index)}",
                delay=10,
            )
            return

        try:
            admins = await asyncio.to_thread(client.get_admin_ids)
        except CRCONHTTPError as exc:
            await send_temporary_response(
                interaction,
                content=f"Failed to load admin users: {exc}",
                delay=10,
            )
            return

        spectator_admins = [
            row
            for row in admins
            if str(row.get("role", "")).lower() == "spectator"
        ]
        if not spectator_admins:
            await send_temporary_response(
                interaction,
                content="No users with spectator access found.",
                delay=10,
            )
            return

        embed = discord.Embed(
            title="Remove Admin Cam User",
            description="Select a user with spectator access to remove.",
            color=0xE67E22,
        )
        view = RemoveAdminCamSelectionView(server_index, spectator_admins)
        await send_temporary_response(
            interaction, embed=embed, view=view, delay=None
        )

    @discord.ui.button(
        label=panel_label("persistent:add_admin_cam_access"),
        style=discord.ButtonStyle.danger,
        custom_id="persistent:add_admin_cam_access",
        row=2,
    )
    async def add_admin_cam_access(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        servers = api_client.get_servers()
        if not servers:
            await send_temporary_response(
                interaction,
                content="No servers are configured; cannot add admin cam access.",
                delay=10,
            )
            return

        focused_server = _get_channel_focused_server(interaction.channel)
        if focused_server is not None and any(
            index == focused_server for index, _ in servers
        ):
            server_index = focused_server
        elif len(servers) == 1:
            server_index = servers[0][0]
        else:
            await send_temporary_response(
                interaction,
                content="Please set a focused server first using Change Server.",
                delay=10,
            )
            return

        if not _get_http_client(server_index):
            await send_temporary_response(
                interaction,
                content=f"CRCON HTTP unavailable for this server: {_http_error_message(server_index)}",
                delay=10,
            )
            return

        await interaction.response.send_modal(AddAdminCamModal(server_index))


class ServerSelectionView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)

        # Create dropdown with servers
        servers = api_client.get_servers()
        if servers:
            self.add_item(ServerDropdown(servers))


class ChangeServerSelectionView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        servers = api_client.get_servers()
        if servers:
            self.add_item(ChangeServerDropdown(servers))


class ChangeServerDropdown(discord.ui.Select):
    def __init__(self, servers):
        options = [
            discord.SelectOption(
                label=server_name,
                description=f"Focus main view on {server_name}",
                value=str(server_index),
            )
            for server_index, server_name in servers[:25]
        ]
        super().__init__(
            placeholder="Choose a server to focus...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        try:
            await defer_interaction(interaction)
            channel = interaction.channel
            if channel is None:
                await send_temporary_response(
                    interaction,
                    content="Unable to determine channel to update.",
                    delay=20,
                )
                return

            selected = int(self.values[0])
            channel_id = getattr(channel, "id", None)
            if channel_id is None:
                await send_temporary_response(
                    interaction,
                    content="Unable to determine channel to update.",
                    delay=20,
                )
                return

            # Store focus and update persistent message if present
            _persistent_focused_server[channel_id] = selected

            if isinstance(channel, (discord.TextChannel, discord.Thread)):
                await ensure_persistent_message(channel)

            await update_interaction_message(
                interaction,
                content=(
                    f"Selected server set to {api_client.get_server_name(selected)}. "
                    "Change Map, Set Objectives, and Dynamic Weather now target this server."
                ),
                embed=None,
                view=None,
            )
            asyncio.create_task(_delete_interaction_after(interaction, 10.0))
        except Exception as exc:
            await send_temporary_response(
                interaction,
                content=f"Error updating main view: {exc}",
                delay=20,
            )


class ServerDropdown(discord.ui.Select):
    def __init__(self, servers):
        options = [
            discord.SelectOption(
                label=server_name,
                description=f"Change map on {server_name}",
                value=str(server_index),
            )
            for server_index, server_name in servers[:25]  # Discord limit
        ]

        super().__init__(
            placeholder="Choose a server...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        await defer_interaction(interaction)
        selected_server_index = int(self.values[0])
        server_name = api_client.get_server_name(selected_server_index)
        current_map = await asyncio.to_thread(
            api_client.get_current_map, selected_server_index
        )

        embed = discord.Embed(
            title="🗺️ Map Change Control",
            description=f"**Server:** {server_name}\n**Current Map:** {current_map}\n\nSelect a game mode:",
            color=0x00FF00,
        )

        view = GameModeSelectionView(selected_server_index)
        await update_interaction_message(interaction, embed=embed, view=view)


async def send_objective_selection(
    interaction: discord.Interaction,
    server_index: int,
    *,
    edit_message: bool,
) -> None:
    client = _get_http_client(server_index)
    if not client:
        message = _http_error_message(server_index)
        if edit_message:
            await update_interaction_message(
                interaction,
                embed=discord.Embed(
                    title="?? Objective Controls Disabled",
                    description=message,
                    color=0xFFA500,
                ),
                view=None,
            )
            asyncio.create_task(_delete_interaction_after(interaction, 20.0))
        else:
            await send_temporary_response(
                interaction, content=message, delay=20
            )
        return

    try:
        rows = await asyncio.to_thread(client.get_objective_rows)
    except CRCONHTTPError as exc:
        message = f"Failed to load objectives: {exc}"
        if edit_message:
            await update_interaction_message(
                interaction,
                embed=discord.Embed(
                    title="?? Objective Fetch Failed",
                    description=message,
                    color=0xFFA500,
                ),
                view=None,
            )
            asyncio.create_task(_delete_interaction_after(interaction, 20.0))
        else:
            await send_temporary_response(
                interaction, content=message, delay=20
            )
        return

    server_name = api_client.get_server_name(server_index)
    current_map = await asyncio.to_thread(
        api_client.get_current_map, server_index
    )
    view = ObjectiveSelectionView(server_index, rows, server_name, current_map)
    embed = view.build_embed()

    if edit_message:
        await update_interaction_message(interaction, embed=embed, view=view)
    else:
        await send_temporary_response(
            interaction, embed=embed, view=view, delay=None
        )


async def send_dynamic_weather_controls(
    interaction: discord.Interaction,
    server_index: int,
    *,
    edit_message: bool,
) -> None:
    client = _get_http_client(server_index)
    if not client:
        message = _http_error_message(server_index)
        if edit_message:
            await update_interaction_message(
                interaction,
                embed=discord.Embed(
                    title="?? Dynamic Weather Disabled",
                    description=message,
                    color=0xFFA500,
                ),
                view=None,
            )
            asyncio.create_task(_delete_interaction_after(interaction, 20.0))
        else:
            await send_temporary_response(
                interaction, content=message, delay=20
            )
        return

    try:
        gamestate_resp = await asyncio.to_thread(client.get_gamestate)
        gamestate = (
            gamestate_resp.get("result")
            if isinstance(gamestate_resp, dict)
            else None
        )
    except CRCONHTTPError as exc:
        message = f"Failed to load gamestate: {exc}"
        if edit_message:
            await update_interaction_message(
                interaction,
                embed=discord.Embed(
                    title="?? Dynamic Weather Unavailable",
                    description=message,
                    color=0xFFA500,
                ),
                view=None,
            )
            asyncio.create_task(_delete_interaction_after(interaction, 20.0))
        else:
            await send_temporary_response(
                interaction, content=message, delay=20
            )
        return

    current_map = (gamestate or {}).get("current_map") or {}
    map_id = current_map.get("id") or current_map.get("map", {}).get("id")
    map_pretty = (
        current_map.get("pretty_name")
        or current_map.get("map", {}).get("pretty_name")
        or map_id
        or "Unknown"
    )

    if not map_id:
        message = "Could not determine the current map ID from the server."
        if edit_message:
            await update_interaction_message(
                interaction,
                embed=discord.Embed(
                    title="?? Dynamic Weather Unavailable",
                    description=message,
                    color=0xFFA500,
                ),
                view=None,
            )
            asyncio.create_task(_delete_interaction_after(interaction, 20.0))
        else:
            await send_temporary_response(
                interaction, content=message, delay=20
            )
        return

    view = DynamicWeatherToggleView(server_index, map_id, map_pretty)
    embed = view.build_embed()

    if edit_message:
        await update_interaction_message(interaction, embed=embed, view=view)
    else:
        await send_temporary_response(
            interaction, embed=embed, view=view, delay=None
        )


class DynamicWeatherServerSelectionView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        servers = api_client.get_servers()
        if servers:
            self.add_item(DynamicWeatherServerDropdown(servers))


class DynamicWeatherServerDropdown(discord.ui.Select):
    def __init__(self, servers):
        options = [
            discord.SelectOption(label=name, value=str(index))
            for index, name in servers[:25]
        ]
        super().__init__(
            placeholder="Select a server...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        server_index = int(self.values[0])
        await defer_interaction(interaction)
        await send_dynamic_weather_controls(
            interaction, server_index, edit_message=True
        )


class DynamicWeatherToggleView(discord.ui.View):
    def __init__(self, server_index: int, map_id: str, map_pretty: str):
        super().__init__(timeout=300)
        self.server_index = server_index
        self.map_id = map_id
        self.map_pretty = map_pretty

    def build_embed(self) -> discord.Embed:
        description = (
            f"**Map:** {self.map_pretty}\n\n"
            "Dynamic weather affects the current match only. Choose whether to enable or disable it."
        )
        embed = discord.Embed(
            title="🌦 Dynamic Weather Control",
            description=description,
            color=0x1ABC9C,
        )
        embed.set_footer(
            text="The API does not expose the current dynamic weather state; last action applies immediately."
        )
        return embed

    async def _set_dynamic_weather(
        self, interaction: discord.Interaction, enabled: bool
    ) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

        if await _deny_if_booked(
            interaction, self.server_index, edit_flow=True
        ):
            self.stop()
            return

        result = await asyncio.to_thread(
            server_control.set_dynamic_weather_enabled,
            self.server_index,
            self.map_id,
            enabled,
        )
        if not result.success:
            followup = await interaction.followup.send(
                f"Failed to update dynamic weather: {result.message}",
                ephemeral=True,
                wait=True,
            )
            if followup is not None:
                asyncio.create_task(_delete_message_after(followup, 20.0))
            return

        _dynamic_weather_state[(self.server_index, self.map_id)] = enabled
        _dynamic_weather_state[(self.server_index, self.map_pretty)] = enabled

        state = "enabled" if enabled else "disabled"
        success_embed = discord.Embed(
            title="🌦 Dynamic Weather Updated",
            description=(
                f"Dynamic weather {state} for **{self.map_pretty}**.\n"
                f"Transport: {'Direct RCON fallback' if result.transport == 'direct_rcon' else 'CRCON HTTP'}."
            ),
            color=0x1ABC9C,
        )

        self.stop()
        await interaction.edit_original_response(
            embed=success_embed, view=None
        )
        await refresh_main_embed()
        asyncio.create_task(_delete_interaction_after(interaction, 10.0))

    @discord.ui.button(label="Turn On", style=discord.ButtonStyle.success)
    async def enable(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await self._set_dynamic_weather(interaction, True)

    @discord.ui.button(label="Turn Off", style=discord.ButtonStyle.danger)
    async def disable(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await self._set_dynamic_weather(interaction, False)


class ObjectiveServerSelectionView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        servers = api_client.get_servers()
        if servers:
            self.add_item(ObjectiveServerDropdown(servers))


class ObjectiveServerDropdown(discord.ui.Select):
    def __init__(self, servers):
        options = [
            discord.SelectOption(label=name, value=str(index))
            for index, name in servers[:25]
        ]
        super().__init__(
            placeholder="Select a server...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        server_index = int(self.values[0])
        await defer_interaction(interaction)
        await send_objective_selection(
            interaction, server_index, edit_message=True
        )


class ObjectiveSelectionView(discord.ui.View):
    def __init__(
        self,
        server_index: int,
        rows: list[list[str]],
        server_name: str,
        current_map: str,
    ):
        super().__init__(timeout=300)
        self.server_index = server_index
        self.server_name = server_name
        self.initial_map = current_map
        self.rows = rows
        self.selected: dict[int, str] = {}

        for slot, options in enumerate(rows, start=1):
            self.add_item(ObjectiveDropdown(slot, options))

    def build_embed(self) -> discord.Embed:
        description = (
            f"**Server:** {self.server_name}\n"
            f"**Current Map:** {self.initial_map}\n\n"
            "Choose one strongpoint for each slot, then lock the layout for this match."
        )

        lines = []
        for slot, options in enumerate(self.rows, start=1):
            chosen = self.selected.get(slot)
            if chosen:
                lines.append(f"{slot}. **{chosen}**")
            else:
                choices = ", ".join(options)
                lines.append(f"{slot}. _(Select: {choices})_")

        embed = discord.Embed(
            title="🎯 Set Objectives for Current Map",
            description=description,
            color=0x9B59B6,
        )
        embed.add_field(
            name="Selections", value="\n".join(lines), inline=False
        )
        return embed

    async def lock_objectives(self, interaction: discord.Interaction) -> None:
        objective_list = [
            self.selected.get(slot) for slot in range(1, len(self.rows) + 1)
        ]
        if any(choice is None for choice in objective_list):
            await interaction.followup.send(
                "Please choose an objective for every slot before locking.",
                ephemeral=True,
            )
            return

        objective_list = [choice for choice in objective_list if choice]
        # mypy doesn't track the None filter above
        objective_list_typed: list[str] = objective_list  # type: ignore[assignment]

        if await _deny_if_booked(
            interaction, self.server_index, edit_flow=True
        ):
            self.stop()
            return

        result = await asyncio.to_thread(
            server_control.set_sector_layout, self.server_index, objective_list
        )
        if not result.success:
            await interaction.followup.send(
                f"Failed to lock objectives: {result.message}",
                ephemeral=True,
            )
            return

        latest_map = await asyncio.to_thread(
            api_client.get_current_map, self.server_index
        )
        _current_objectives_state[(self.server_index, latest_map)] = list(
            objective_list_typed
        )
        summary = "\n".join(
            f"{idx}. **{name}**"
            for idx, name in enumerate(objective_list, start=1)
        )

        success_embed = discord.Embed(
            title="🔒 Objectives Locked",
            description=(
                f"**Server:** {self.server_name}\n"
                f"**Map:** {latest_map}\n"
                f"**Transport:** {'Direct RCON fallback' if result.transport == 'direct_rcon' else 'CRCON HTTP'}\n\n"
                f"{summary}"
            ),
            color=0x2ECC71,
        )

        self.stop()
        await interaction.edit_original_response(
            embed=success_embed, view=None
        )
        await refresh_main_embed()
        asyncio.create_task(
            _delete_interaction_after_and_refresh(interaction, 10.0)
        )


class ObjectiveDropdown(discord.ui.Select):
    def __init__(self, slot: int, options: list[str]):
        select_options = [
            discord.SelectOption(label=option, value=option)
            for option in options
        ]
        super().__init__(
            placeholder=f"Objective Slot {slot}",
            min_values=1,
            max_values=1,
            options=select_options,
        )
        self.slot = slot

    async def callback(self, interaction: discord.Interaction):
        objective = self.values[0]
        view: ObjectiveSelectionView = self.view  # type: ignore[assignment]
        view.selected[self.slot] = objective
        if all(
            view.selected.get(slot) for slot in range(1, len(view.rows) + 1)
        ):
            if not interaction.response.is_done():
                await interaction.response.defer()
            await view.lock_objectives(interaction)
        else:
            await interaction.response.edit_message(
                embed=view.build_embed(), view=view
            )


class GameModeSelectionView(discord.ui.View):
    def __init__(self, server_index):
        super().__init__(timeout=300)
        self.server_index = server_index

    @discord.ui.button(
        label="Warfare", style=discord.ButtonStyle.primary, emoji="⚔️"
    )
    async def warfare_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await self.show_map_selection(interaction, "warfare")

    @discord.ui.button(
        label="Offensive", style=discord.ButtonStyle.secondary, emoji="🏃"
    )
    async def offensive_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await self.show_map_selection(interaction, "offensive")

    @discord.ui.button(
        label="Skirmish", style=discord.ButtonStyle.success, emoji="💥"
    )
    async def skirmish_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await self.show_map_selection(interaction, "skirmish")

    async def show_map_selection(self, interaction, game_mode):
        await defer_interaction(interaction)
        server_name = api_client.get_server_name(self.server_index)
        current_map = await asyncio.to_thread(
            api_client.get_current_map, self.server_index
        )

        embed = discord.Embed(
            title=f"🗺️ {game_mode.title()} Maps",
            description=f"**Server:** {server_name}\n**Current Map:** {current_map}\n\nSelect a map:",
            color=0x0099FF,
        )

        maps = await asyncio.to_thread(get_maps_for_mode, game_mode)
        view = MapSelectionView(self.server_index, game_mode, maps)
        await update_interaction_message(interaction, embed=embed, view=view)


class MapSelectionView(discord.ui.View):
    def __init__(self, server_index, game_mode, maps):
        super().__init__(timeout=300)
        self.server_index = server_index
        self.game_mode = game_mode

        # Create dropdown with maps
        if maps:
            self.add_item(MapDropdown(server_index, game_mode, maps))

        # Add back button
        servers = api_client.get_servers()
        if len(servers) > 1:
            self.add_item(BackToServerSelectionButton())
        else:
            self.add_item(BackToGameModeButton(server_index))


class MapDropdown(discord.ui.Select):
    def __init__(self, server_index, game_mode, maps):
        self.server_index = server_index
        self.game_mode = game_mode

        options = [
            discord.SelectOption(
                label=map_name,
                description=f"Select {map_name} for {game_mode}",
                value=map_name,
            )
            for map_name in maps[:25]  # Discord limit
        ]

        super().__init__(
            placeholder="Choose a map...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        await defer_interaction(interaction)
        selected_map = self.values[0]
        server_name = api_client.get_server_name(self.server_index)
        current_map = await asyncio.to_thread(
            api_client.get_current_map, self.server_index
        )

        embed = discord.Embed(
            title=f"🗺️ {selected_map} - {self.game_mode.title()}",
            description=f"**Server:** {server_name}\n**Current Map:** {current_map}\n\nSelect a variant:",
            color=0xFF9900,
        )

        variants = await asyncio.to_thread(
            get_variants_for_map, self.game_mode, selected_map
        )
        view = VariantSelectionView(
            self.server_index, self.game_mode, selected_map, variants
        )
        await update_interaction_message(interaction, embed=embed, view=view)


class VariantSelectionView(discord.ui.View):
    def __init__(self, server_index, game_mode, map_name, variants):
        super().__init__(timeout=300)
        self.server_index = server_index
        self.game_mode = game_mode
        self.map_name = map_name

        # Create dropdown with variants
        if variants:
            self.add_item(
                VariantDropdown(server_index, game_mode, map_name, variants)
            )

        # Add back button
        self.add_item(BackToMapSelectionButton(server_index, game_mode))


class VariantDropdown(discord.ui.Select):
    def __init__(self, server_index, game_mode, map_name, variants):
        self.server_index = server_index
        self.game_mode = game_mode
        self.map_name = map_name
        self.variants = variants

        options = [
            discord.SelectOption(
                label=variant["variant"],
                description=f"{map_name} - {variant['variant']}",
                value=variant["id"],
            )
            for variant in variants[:25]  # Discord limit
        ]

        super().__init__(
            placeholder="Choose a variant...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        selected_variant_id = self.values[0]
        server_name = api_client.get_server_name(self.server_index)

        # Find the variant name for display
        variant_entry = next(
            (v for v in self.variants if v["id"] == selected_variant_id), None
        )
        variant_name = (
            variant_entry["variant"] if variant_entry else selected_variant_id
        )

        embed = discord.Embed(
            title="🔄 Changing Map...",
            description=f"**Server:** {server_name}\n\nAttempting to change to **{self.map_name}** ({variant_name})",
            color=0xFFFF00,
        )

        await interaction.response.edit_message(embed=embed, view=None)

        # Acknowledge first, then check the booking lock right before the write.
        if await _deny_if_booked(
            interaction, self.server_index, edit_flow=True
        ):
            return

        status_lines = []
        overall_success = False

        result = await asyncio.to_thread(
            server_control.change_map, self.server_index, selected_variant_id
        )
        if result.success:
            if result.transport == "direct_rcon":
                status_lines.append(
                    "Direct RCON fallback change_map succeeded."
                )
                if result.warning:
                    status_lines.append(f"CRCON HTTP issue: {result.warning}")
            else:
                status_lines.append("CRCON HTTP change_map succeeded.")
                if result.warning:
                    status_lines.append(f"Note: {result.warning}")
            overall_success = True
        else:
            status_lines.append(result.message)

        status_summary = "\n".join(f"• {line}" for line in status_lines)

        if overall_success:
            new_current_map = await asyncio.to_thread(
                api_client.get_current_map, self.server_index
            )
            stale_objective_keys = [
                key
                for key in _current_objectives_state
                if key[0] == self.server_index
            ]
            for key in stale_objective_keys:
                _current_objectives_state.pop(key, None)

            final_embed = discord.Embed(
                title="✅ Map Changed Successfully!",
                description=(
                    f"**Server:** {server_name}\n"
                    f"**New Map:** {new_current_map}\n"
                    f"**Target:** {self.map_name} ({variant_name})\n\n"
                    f"{status_summary}"
                ),
                color=0x00FF00,
            )
            await interaction.edit_original_response(
                embed=final_embed, view=None
            )
            await refresh_main_embed()
            asyncio.create_task(_delete_interaction_after(interaction, 10.0))
        else:
            final_embed = discord.Embed(
                title="❌ Map Change Failed",
                description=(
                    f"**Server:** {server_name}\n"
                    f"**Target:** {self.map_name} ({variant_name})\n\n"
                    f"{status_summary}"
                ),
                color=0xFF0000,
            )
            await interaction.edit_original_response(
                embed=final_embed, view=None
            )
            asyncio.create_task(_delete_interaction_after(interaction, 20.0))


class BackToServerSelectionButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="← Back to Servers", style=discord.ButtonStyle.gray
        )

    async def callback(self, interaction: discord.Interaction):
        await defer_interaction(interaction)
        embed = discord.Embed(
            title="🗺️ Map Change Control",
            description="Select a server:",
            color=0x00FF00,
        )

        view = ServerSelectionView()
        await update_interaction_message(interaction, embed=embed, view=view)


class BackToGameModeButton(discord.ui.Button):
    def __init__(self, server_index):
        super().__init__(
            label="← Back to Game Modes", style=discord.ButtonStyle.gray
        )
        self.server_index = server_index

    async def callback(self, interaction: discord.Interaction):
        await defer_interaction(interaction)
        server_name = api_client.get_server_name(self.server_index)
        current_map = await asyncio.to_thread(
            api_client.get_current_map, self.server_index
        )

        embed = discord.Embed(
            title="🗺️ Map Change Control",
            description=f"**Server:** {server_name}\n**Current Map:** {current_map}\n\nSelect a game mode:",
            color=0x00FF00,
        )

        view = GameModeSelectionView(self.server_index)
        await update_interaction_message(interaction, embed=embed, view=view)


class BackToMapSelectionButton(discord.ui.Button):
    def __init__(self, server_index, game_mode):
        super().__init__(
            label="← Back to Maps", style=discord.ButtonStyle.gray
        )
        self.server_index = server_index
        self.game_mode = game_mode

    async def callback(self, interaction: discord.Interaction):
        await defer_interaction(interaction)
        server_name = api_client.get_server_name(self.server_index)
        current_map = await asyncio.to_thread(
            api_client.get_current_map, self.server_index
        )

        embed = discord.Embed(
            title=f"🗺️ {self.game_mode.title()} Maps",
            description=f"**Server:** {server_name}\n**Current Map:** {current_map}\n\nSelect a map:",
            color=0x0099FF,
        )

        maps = await asyncio.to_thread(get_maps_for_mode, self.game_mode)
        view = MapSelectionView(self.server_index, self.game_mode, maps)
        await update_interaction_message(interaction, embed=embed, view=view)


async def _leave_if_unapproved(guild: discord.Guild) -> bool:
    """Leave a guild missing from DISCORD_GUILD_ID / BOOKING_GUILD_IDS. True if it left."""
    if guild_registry.is_approved(guild.id):
        return False
    print(
        f"Leaving guild {guild.name!r} ({guild.id}): not in DISCORD_GUILD_ID or BOOKING_GUILD_IDS."
    )
    try:
        await guild.leave()
    except discord.HTTPException as exc:
        print(f"Could not leave guild {guild.id}: {exc}")
        return False
    return True


@bot.event
async def on_guild_join(guild: discord.Guild):
    if not await _leave_if_unapproved(guild):
        print(f"Joined approved guild {guild.name!r} ({guild.id}).")


@bot.event
async def on_ready():
    print(f"{bot.user} has connected to Discord!")
    for guild in list(bot.guilds):
        await _leave_if_unapproved(guild)
    print(f"Bot is in {len(bot.guilds)} guilds")
    missing = set(guild_registry.approved_ids()) - {g.id for g in bot.guilds}
    if missing:
        print(
            f"Approved guild(s) the bot has not joined yet: {', '.join(map(str, sorted(missing)))}"
        )

    # Start periodic cache cleanup task
    _start_cache_cleanup_task()

    # Re-render the panel when a booking starts or ends (display only).
    if booking_refresher is not None:
        booking_refresher.start()

    # Warm cache when available, but avoid noisy startup warnings if CRCON isn't reachable yet.
    await asyncio.to_thread(refresh_map_cache, force=False)

    # Add the persistent view
    bot.add_view(GameModeView())

    # Restore every guild's panel where /setup_panel last put it (home: or DISCORD_CHANNEL_ID).
    messages = await refresh_main_embed()
    if not messages:
        print("No panel channel yet; an admin can run /setup_panel in a channel.")
    for message in messages:
        guild_name = getattr(getattr(message, "guild", None), "name", "?")
        channel_name = getattr(message.channel, "name", message.channel.id)
        print(
            f"Persistent controls ready in {guild_name} #{channel_name} (message ID {message.id})"
        )


# Admin command to repost the button if needed
@bot.tree.command(
    name="repost_button",
    description="Repost the map changer button (Admin only)",
)
async def repost_button(interaction: discord.Interaction):
    # Check if user has admin permissions
    if (
        not isinstance(interaction.user, discord.Member)
        or not interaction.user.guild_permissions.administrator
    ):
        await send_temporary_response(
            interaction,
            content="❌ You need administrator permissions to use this command.",
            delay=20,
        )
        return

    channel = await _resolve_text_channel(_panel_channel_id(interaction.guild_id))
    if channel is None:
        await send_temporary_response(
            interaction,
            content="❌ This Discord has no panel yet. Use /setup_panel in the channel you want.",
            delay=20,
        )
        return
    await defer_interaction(interaction, ephemeral=True)
    await post_panel(channel)
    await send_temporary_response(
        interaction, content="✅ Map changer button reposted!", delay=20
    )


@bot.tree.command(
    name="setup_panel",
    description="Create the control panel in a channel; it is restored there on restart (Admin only)",
)
@discord.app_commands.describe(
    channel="Channel for the panel (defaults to this one)"
)
async def setup_panel(
    interaction: discord.Interaction,
    channel: discord.TextChannel | None = None,
):
    if (
        not isinstance(interaction.user, discord.Member)
        or not interaction.user.guild_permissions.administrator
    ):
        await send_temporary_response(
            interaction,
            content="❌ You need administrator permissions to use this command.",
            delay=20,
        )
        return

    if not guild_registry.is_approved(interaction.guild_id):
        await send_temporary_response(
            interaction,
            content="❌ This Discord isn't set up to use this bot.",
            delay=20,
        )
        return

    target = channel or interaction.channel
    if not isinstance(target, (discord.TextChannel, discord.Thread)) or (
        _guild_of(target) != (interaction.guild_id or 0)
    ):
        await send_temporary_response(
            interaction,
            content="❌ Pick a text channel in this Discord for the panel.",
            delay=20,
        )
        return

    await defer_interaction(interaction, ephemeral=True)
    try:
        message = await post_panel(target)
    except discord.Forbidden:
        await send_temporary_response(
            interaction,
            content=f"❌ I can't post in {target.mention}. Give me View Channel and Send Messages there.",
            delay=20,
        )
        return
    print(
        f"Control panel set up in #{target.name} (message ID {message.id}) by {interaction.user.id}"
    )
    await send_temporary_response(
        interaction,
        content=f"✅ Control panel created in {target.mention}. It will be restored there after restarts.",
        delay=20,
    )


booking_refresher: BookingBoundaryRefresher | None = None
if booking_service is not None:
    register_booking_commands(bot.tree, booking_deps)
    booking_refresher = BookingBoundaryRefresher(
        booking_service,
        lambda: refresh_main_embed(),
        daily_at=booking_config.panel_refresh_time.replace(
            tzinfo=booking_config.timezone
        ),
    )


if __name__ == "__main__":
    from config.settings import validate_required_env

    validate_required_env()
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        print("Error: DISCORD_TOKEN not found in environment variables")
        sys.exit(1)

    bot.run(token)
