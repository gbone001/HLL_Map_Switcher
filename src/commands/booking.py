"""Booking slash commands: /book, /bookings, /cancel_booking.

All replies are ephemeral. Times are entered in BOOKING_TIMEZONE and shown as Discord
timestamps, which each viewer's client renders in their own time zone.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import discord
from discord import app_commands

from services.guild_registry import GuildRegistry
from services.booking import (
    BookingGroup,
    group_by_series,
    DURATION_HINT,
    EVENT_NAME_MAX_LENGTH,
    START_FORMAT_HINT,
    Booking,
    BookingConfig,
    BookingError,
    BookingOverlapError,
    BookingService,
    format_local,
    parse_duration,
    parse_start,
)

NOT_CONFIGURED_MESSAGE = "Server booking is not configured on this bot."
WRONG_GUILD_MESSAGE = "This Discord isn't set up to use server booking."
NOT_A_MEMBER_MESSAGE = "You must be a member of an approved Discord to book a server."
STORE_ERROR_MESSAGE = "❌ Could not reach the booking store. Nothing was changed; try again shortly."
# Booking replies are ephemeral and delete themselves after this long.
REPLY_EXPIRY_SECONDS = 90
# Discord rejects messages over 2000 characters; leave room for the "…and N more." line.
MAX_REPLY_CHARS = 1900

logger = logging.getLogger(__name__)


@dataclass
class BookingCommandDeps:
    service: BookingService | None
    config: BookingConfig
    servers: Callable[[], list[tuple[int, str]]]
    server_key: Callable[[int], str]
    server_name_for_key: Callable[[str], str]
    on_change: Callable[[], Awaitable[None]]
    # Approved guilds (multi-guild). None means single-guild: only config.guild_id.
    registry: GuildRegistry | None = None
    # Display name for a guild ID, used when showing other guilds' bookings.
    guild_name: Callable[[int | None], str] = lambda guild_id: "another Discord"

    def is_approved(self, guild_id: int | None) -> bool:
        if self.registry is not None:
            return self.registry.configured and self.registry.is_approved(guild_id)
        return guild_id is not None and guild_id == self.config.guild_id


def _member_of_booking_guild(user: object, config: BookingConfig) -> bool:
    return (
        isinstance(user, discord.Member)
        and config.guild_id is not None
        and getattr(user.guild, "id", None) == config.guild_id
    )


def is_booking_admin(user: object, config: BookingConfig) -> bool:
    """Administrator permission or BOOKING_ADMIN_ROLE_ID, counted only inside DISCORD_GUILD_ID."""
    if not _member_of_booking_guild(user, config):
        return False
    if user.guild_permissions.administrator:
        return True
    return config.admin_role_id is not None and any(
        role.id == config.admin_role_id for role in user.roles
    )


def can_book(user: object, deps: BookingCommandDeps) -> bool:
    """Any member of an approved guild can book; home booking admins always can."""
    if is_booking_admin(user, deps.config):
        return True
    return isinstance(user, discord.Member) and deps.is_approved(
        getattr(user.guild, "id", None)
    )


def guild_admin_id(user: object) -> int | None:
    """The guild this user administers in this interaction, if any (manages that guild's bookings)."""
    if isinstance(user, discord.Member) and user.guild_permissions.administrator:
        return getattr(user.guild, "id", None)
    return None


def booker_text(
    booking: Booking,
    viewer_guild_id: int | None,
    guild_name: Callable[[int | None], str],
) -> str:
    """Mention the booker inside their own guild; elsewhere show only the guild's name."""
    if booking.guild_id is None or booking.guild_id == viewer_guild_id:
        return f"<@{booking.user_id}>"
    return f"**{guild_name(booking.guild_id)}**"


def fit_lines(lines: list[str], limit: int = MAX_REPLY_CHARS) -> str:
    """Join lines, stopping before the Discord message limit and noting how many were left out."""
    kept: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > limit:
            break
        kept.append(line)
        used += len(line) + 1
    if len(kept) < len(lines):
        kept.append(f"…and {len(lines) - len(kept)} more.")
    return "\n".join(kept)


def timestamp(ts: int, style: str = "F") -> str:
    return f"<t:{ts}:{style}>"


def describe_booking(
    booking: Booking, server_name: str, booked_by: str | None = None
) -> str:
    named = f" *{discord.utils.escape_markdown(booking.event_name)}*" if booking.event_name else ""
    return (
        f"#{booking.id} **{server_name}**{named} — {booked_by or f'<@{booking.user_id}>'} "
        f"{timestamp(booking.start_utc)} → {timestamp(booking.end_utc, 't')} ({timestamp(booking.start_utc, 'R')})"
    )


async def _reply(interaction: discord.Interaction, content: str) -> None:
    """Ephemeral reply that removes itself after REPLY_EXPIRY_SECONDS so the channel stays clean."""
    mentions = discord.AllowedMentions.none()
    if interaction.response.is_done():
        message = await interaction.followup.send(
            content, ephemeral=True, allowed_mentions=mentions, wait=True
        )
        if message is not None:
            await message.delete(delay=REPLY_EXPIRY_SECONDS)
    else:
        await interaction.response.send_message(
            content,
            ephemeral=True,
            allowed_mentions=mentions,
            delete_after=REPLY_EXPIRY_SECONDS,
        )


async def _notify_change(deps: BookingCommandDeps) -> None:
    """Refresh the panel after a booking change; a failed refresh must not fail the booking."""
    try:
        await deps.on_change()
    except Exception:
        logger.exception("Panel refresh after a booking change failed")


async def _defer(interaction: discord.Interaction) -> None:
    """Acknowledge before touching the store: a locked SQLite file can wait longer than Discord's 3s deadline."""
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True, thinking=True)


async def refuse_unavailable(
    interaction: discord.Interaction, deps: BookingCommandDeps
) -> bool:
    """Reply and return True when booking is off or the command came from another guild or a DM."""
    if deps.service is None or not deps.config.enabled:
        await _reply(interaction, NOT_CONFIGURED_MESSAGE)
        return True
    if not deps.is_approved(interaction.guild_id):
        await _reply(interaction, WRONG_GUILD_MESSAGE)
        return True
    return False


def describe_group(group: BookingGroup, server_name: str, booked_by: str) -> str:
    """One line per booking, or one line for a whole repeating series."""
    if not group.is_series:
        return describe_booking(group.first, server_name, booked_by)
    first = group.first
    named = f" *{discord.utils.escape_markdown(first.event_name)}*" if first.event_name else ""
    remaining = len(group.bookings)
    return (
        f"#{first.series_id} 🔁 **{server_name}**{named} — {booked_by} · "
        f"{group.recurrence_label}, {remaining} date{'s' if remaining != 1 else ''} left · "
        f"next {timestamp(first.start_utc)} → {timestamp(first.end_utc, 't')}"
    )


def group_label(
    group: BookingGroup, deps: BookingCommandDeps, viewer_guild_id: int | None = None
) -> str:
    """Plain-text label for a select option or autocomplete choice."""
    label = booking_label(group.first, deps, viewer_guild_id)
    if group.is_series:
        label = f"🔁 {label} · {group.recurrence_label.lower()}, {len(group.bookings)} left"
    return label[:100]


def booking_label(
    booking: Booking, deps: BookingCommandDeps, viewer_guild_id: int | None = None
) -> str:
    """Plain-text booking label for choices and select options, where Discord timestamps don't render."""
    same_guild = booking.guild_id is None or booking.guild_id == viewer_guild_id
    who = booking.user_name if same_guild else deps.guild_name(booking.guild_id)
    return (
        f"#{booking.id} {deps.server_name_for_key(booking.server_key)} "
        f"{format_local(booking.start_utc, deps.config.timezone)} ({who})"
    )[:100]


async def book_and_reply(
    interaction: discord.Interaction,
    deps: BookingCommandDeps,
    server_index: int,
    start: str,
    duration: str,
    recurrence: str = "none",
    count: int = 1,
    event_name: str | None = None,
) -> bool:
    """Create a booking (or recurring series) and reply; True if it was created. Shared by /book and the panel."""
    if not can_book(interaction.user, deps):
        await _reply(interaction, NOT_A_MEMBER_MESSAGE)
        return False

    await _defer(interaction)
    server_key = deps.server_key(server_index)
    try:
        start_utc = parse_start(start, deps.config.timezone)
        end_utc = start_utc + parse_duration(duration)
        bookings = await asyncio.to_thread(
            deps.service.create_series,
            server_key=server_key,
            user_id=interaction.user.id,
            user_name=interaction.user.display_name,
            start_utc=start_utc,
            end_utc=end_utc,
            recurrence=recurrence,
            count=count,
            guild_id=interaction.guild_id,
            event_name=event_name,
        )
    except BookingOverlapError as exc:
        await _reply(interaction, _overlap_message(exc.conflict, interaction, deps))
        return False
    except sqlite3.Error:
        logger.exception("Booking store failed while creating a booking")
        await _reply(interaction, STORE_ERROR_MESSAGE)
        return False
    except BookingError as exc:
        await _reply(
            interaction,
            f"❌ {exc} Use start `{START_FORMAT_HINT}` in {deps.config.timezone.key} "
            f"and duration as {DURATION_HINT}.",
        )
        return False

    first = bookings[0]
    series = f" (+{len(bookings) - 1} more, {recurrence})" if len(bookings) > 1 else ""
    named = f" for **{discord.utils.escape_markdown(first.event_name)}**" if first.event_name else ""
    await _reply(
        interaction,
        f"✅ **{deps.server_name_for_key(server_key)}** booked{named} {timestamp(first.start_utc)} → "
        f"{timestamp(first.end_utc, 't')} (booking #{first.id}){series}. "
        "During your slot only you and booking admins can change this server.",
    )
    await _notify_change(deps)
    return True


def _overlap_message(
    conflict: Booking, interaction: discord.Interaction, deps: BookingCommandDeps
) -> str:
    if conflict.guild_id is None or conflict.guild_id == interaction.guild_id:
        holder = f"<@{conflict.user_id}>'s booking #{conflict.id}"
    else:
        holder = f"a booking by **{deps.guild_name(conflict.guild_id)}**"
    return (
        f"❌ That overlaps {holder}: "
        f"{timestamp(conflict.start_utc)} → {timestamp(conflict.end_utc, 't')}."
    )


async def edit_and_reply(
    interaction: discord.Interaction,
    deps: BookingCommandDeps,
    booking_id: int,
    *,
    server_index: int,
    start: str,
    duration: str,
    event_name: str | None,
    recurrence: str = "none",
    count: int = 1,
) -> bool:
    """Edit a booking or a whole series (any field, including how it repeats) and reply;
    True if saved. The old dates are replaced in one transaction."""
    await _defer(interaction)
    try:
        start_utc = parse_start(start, deps.config.timezone)
        end_utc = start_utc + parse_duration(duration)
        updated = await asyncio.to_thread(
            functools.partial(
                deps.service.rebook,
                booking_id,
                interaction.user.id,
                is_booking_admin(interaction.user, deps.config),
                guild_admin_id(interaction.user),
                server_key=deps.server_key(server_index),
                start_utc=start_utc,
                end_utc=end_utc,
                event_name=event_name,
                recurrence=recurrence,
                count=count,
            )
        )
    except BookingOverlapError as exc:
        await _reply(interaction, _overlap_message(exc.conflict, interaction, deps))
        return False
    except sqlite3.Error:
        logger.exception("Booking store failed while editing a booking")
        await _reply(interaction, STORE_ERROR_MESSAGE)
        return False
    except BookingError as exc:
        await _reply(interaction, f"❌ {exc}")
        return False
    first = updated[0]
    server = deps.server_name_for_key(first.server_key)
    if len(updated) > 1:
        message = (
            f"✅ Saved series #{first.series_id} on **{server}** ({len(updated)} dates). "
            f"Next: {timestamp(first.start_utc)} → {timestamp(first.end_utc, 't')}."
        )
    else:
        message = (
            f"✅ Saved booking #{first.id}: **{server}** "
            f"{timestamp(first.start_utc)} → {timestamp(first.end_utc, 't')}."
        )
    await _reply(interaction, message)
    await _notify_change(deps)
    return True


async def cancel_and_reply(
    interaction: discord.Interaction,
    deps: BookingCommandDeps,
    booking_id: int,
    whole_series: bool = False,
) -> bool:
    """Cancel a booking (or, with `whole_series`, every remaining date of its series) and
    reply; True if anything was cancelled. Shared by /cancel_booking and the panel."""
    await _defer(interaction)
    try:
        cancelled = await asyncio.to_thread(
            deps.service.cancel_series if whole_series else deps.service.cancel,
            booking_id,
            interaction.user.id,
            is_booking_admin(interaction.user, deps.config),
            guild_admin_id(interaction.user),
        )
    except sqlite3.Error:
        logger.exception("Booking store failed while cancelling a booking")
        await _reply(interaction, STORE_ERROR_MESSAGE)
        return False
    except BookingError as exc:
        await _reply(interaction, f"❌ {exc}")
        return False
    cancelled = cancelled if isinstance(cancelled, list) else [cancelled]
    first = cancelled[0]
    server = deps.server_name_for_key(first.server_key)
    if len(cancelled) > 1:
        message = f"✅ Cancelled series #{first.series_id} on **{server}** ({len(cancelled)} dates)."
    else:
        message = f"✅ Cancelled booking #{first.id} on **{server}**."
    await _reply(interaction, message)
    await _notify_change(deps)
    return True


def register_booking_commands(
    tree: app_commands.CommandTree, deps: BookingCommandDeps
) -> None:
    server_choices = [
        app_commands.Choice(name=name[:100], value=str(index))
        for index, name in deps.servers()[:25]
    ]

    @tree.command(name="book", description="Book a server for a time slot")
    @app_commands.describe(
        server="Server to book",
        start=f"Start time ({START_FORMAT_HINT}, {deps.config.timezone.key})",
        duration=f"Length: {DURATION_HINT}",
        event="Optional event name shown on the schedule, e.g. Scrim vs 1st Para",
    )
    @app_commands.choices(server=server_choices)
    async def book(
        interaction: discord.Interaction,
        server: app_commands.Choice[str],
        start: str,
        duration: str,
        event: app_commands.Range[str, 1, EVENT_NAME_MAX_LENGTH] | None = None,
    ):
        if await refuse_unavailable(interaction, deps):
            return
        await book_and_reply(
            interaction, deps, int(server.value), start, duration, event_name=event
        )

    @tree.command(
        name="bookings", description="List active and upcoming server bookings"
    )
    @app_commands.describe(server="Only show bookings for this server")
    @app_commands.choices(server=server_choices)
    async def bookings(
        interaction: discord.Interaction,
        server: app_commands.Choice[str] | None = None,
    ):
        if await refuse_unavailable(interaction, deps):
            return
        server_key = deps.server_key(int(server.value)) if server else None
        await _defer(interaction)
        try:
            live = await asyncio.to_thread(deps.service.list_live, server_key)
        except sqlite3.Error:
            logger.exception("Booking store failed during /bookings")
            await _reply(interaction, STORE_ERROR_MESSAGE)
            return
        if not live:
            await _reply(interaction, "No active or upcoming bookings.")
            return
        lines = [
            describe_group(
                group,
                deps.server_name_for_key(group.first.server_key),
                booker_text(group.first, interaction.guild_id, deps.guild_name),
            )
            for group in group_by_series(live)
        ]
        await _reply(interaction, fit_lines(lines))

    @tree.command(
        name="cancel_booking",
        description="Cancel one of your bookings (admins can cancel any)",
    )
    @app_commands.describe(
        booking_id="Booking to cancel (🔁 marks a repeating series)",
        series="For a repeating booking: cancel every remaining date, not just this one",
    )
    async def cancel_booking(
        interaction: discord.Interaction, booking_id: int, series: bool = True
    ):
        if await refuse_unavailable(interaction, deps):
            return
        await cancel_and_reply(interaction, deps, booking_id, whole_series=series)

    @tree.command(
        name="clear_bookings",
        description="Cancel every active and upcoming booking (booking admins only)",
    )
    @app_commands.describe(confirm="Set to True to cancel every booking on every server")
    async def clear_bookings(interaction: discord.Interaction, confirm: bool = False):
        if await refuse_unavailable(interaction, deps):
            return
        if not is_booking_admin(interaction.user, deps.config):
            await _reply(interaction, "❌ Only booking admins can clear bookings.")
            return
        if not confirm:
            await _reply(
                interaction,
                "This cancels **every** active and upcoming booking on every server. "
                "Run `/clear_bookings confirm:True` to go ahead.",
            )
            return
        await _defer(interaction)
        try:
            cleared = await asyncio.to_thread(
                deps.service.cancel_all_live, interaction.user.id
            )
        except sqlite3.Error:
            logger.exception("Booking store failed during /clear_bookings")
            await _reply(interaction, STORE_ERROR_MESSAGE)
            return
        logger.warning("User %s cleared %s booking(s)", interaction.user.id, cleared)
        await _reply(interaction, f"✅ Cancelled {cleared} booking(s).")
        await _notify_change(deps)

    @cancel_booking.autocomplete("booking_id")
    async def cancel_booking_autocomplete(
        interaction: discord.Interaction, current: str
    ):
        if (
            deps.service is None
            or not deps.config.enabled
            or not deps.is_approved(interaction.guild_id)
        ):
            return []
        admin = is_booking_admin(interaction.user, deps.config)
        admin_guild = guild_admin_id(interaction.user)
        try:
            live = await asyncio.to_thread(deps.service.list_live)
        except sqlite3.Error:
            logger.exception(
                "Booking store failed during /cancel_booking autocomplete"
            )
            return []
        needle = current.lower()
        choices = []
        for group in group_by_series(live):
            if not group.first.manageable_by(interaction.user.id, admin, admin_guild):
                continue
            label = group_label(group, deps, interaction.guild_id)
            if needle and needle not in label.lower():
                continue
            choices.append(
                app_commands.Choice(name=label, value=group.first.id)
            )
        return choices[:25]
