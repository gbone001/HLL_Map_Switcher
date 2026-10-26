"""Booking slash commands: /book, /bookings, /cancel_booking.

All replies are ephemeral. Times are entered in BOOKING_TIMEZONE and shown as Discord
timestamps, which each viewer's client renders in their own time zone.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import discord
from discord import app_commands

from services.booking import (
    DURATION_HINT,
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
WRONG_GUILD_MESSAGE = (
    "Server booking is only available in this bot's home Discord."
)
NOT_A_MEMBER_MESSAGE = "You must be a member of the booking guild to book a server."
STORE_ERROR_MESSAGE = "❌ Could not reach the booking store. Nothing was changed; try again shortly."
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


def can_book(user: object, config: BookingConfig) -> bool:
    if is_booking_admin(user, config):
        return True
    if not _member_of_booking_guild(user, config):
        return False
    return True  # any guild member can book


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


def describe_booking(booking: Booking, server_name: str) -> str:
    return (
        f"#{booking.id} **{server_name}** — <@{booking.user_id}> "
        f"{timestamp(booking.start_utc)} → {timestamp(booking.end_utc, 't')} ({timestamp(booking.start_utc, 'R')})"
    )


async def _reply(interaction: discord.Interaction, content: str) -> None:
    mentions = discord.AllowedMentions.none()
    if interaction.response.is_done():
        await interaction.followup.send(
            content, ephemeral=True, allowed_mentions=mentions
        )
    else:
        await interaction.response.send_message(
            content, ephemeral=True, allowed_mentions=mentions
        )


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
    if interaction.guild_id != deps.config.guild_id:
        await _reply(interaction, WRONG_GUILD_MESSAGE)
        return True
    return False


def booking_label(booking: Booking, deps: BookingCommandDeps) -> str:
    """Plain-text booking label for choices and select options, where Discord timestamps don't render."""
    return (
        f"#{booking.id} {deps.server_name_for_key(booking.server_key)} "
        f"{format_local(booking.start_utc, deps.config.timezone)} ({booking.user_name})"
    )[:100]


async def book_and_reply(
    interaction: discord.Interaction,
    deps: BookingCommandDeps,
    server_index: int,
    start: str,
    duration: str,
    recurrence: str = "none",
    count: int = 1,
) -> None:
    """Create a booking (or recurring series) and reply; shared by /book and the panel."""
    if not can_book(interaction.user, deps.config):
        await _reply(interaction, NOT_A_MEMBER_MESSAGE)
        return

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
        )
    except BookingOverlapError as exc:
        conflict = exc.conflict
        await _reply(
            interaction,
            f"❌ That overlaps <@{conflict.user_id}>'s booking #{conflict.id}: "
            f"{timestamp(conflict.start_utc)} → {timestamp(conflict.end_utc, 't')}.",
        )
        return
    except sqlite3.Error:
        logger.exception("Booking store failed while creating a booking")
        await _reply(interaction, STORE_ERROR_MESSAGE)
        return
    except BookingError as exc:
        await _reply(
            interaction,
            f"❌ {exc} Use start `{START_FORMAT_HINT}` in {deps.config.timezone.key} "
            f"and duration as {DURATION_HINT}.",
        )
        return

    first = bookings[0]
    series = f" (+{len(bookings) - 1} more, {recurrence})" if len(bookings) > 1 else ""
    await _reply(
        interaction,
        f"✅ **{deps.server_name_for_key(server_key)}** booked {timestamp(first.start_utc)} → "
        f"{timestamp(first.end_utc, 't')} (booking #{first.id}){series}. "
        "During your slot only you and booking admins can change this server.",
    )
    await deps.on_change()


async def cancel_and_reply(
    interaction: discord.Interaction, deps: BookingCommandDeps, booking_id: int
) -> None:
    """Cancel a booking and reply; shared by /cancel_booking and the panel."""
    await _defer(interaction)
    try:
        booking = await asyncio.to_thread(
            deps.service.cancel,
            booking_id,
            interaction.user.id,
            is_booking_admin(interaction.user, deps.config),
        )
    except sqlite3.Error:
        logger.exception("Booking store failed while cancelling a booking")
        await _reply(interaction, STORE_ERROR_MESSAGE)
        return
    except BookingError as exc:
        await _reply(interaction, f"❌ {exc}")
        return
    await _reply(
        interaction,
        f"✅ Cancelled booking #{booking.id} on **{deps.server_name_for_key(booking.server_key)}**.",
    )
    await deps.on_change()


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
    )
    @app_commands.choices(server=server_choices)
    async def book(
        interaction: discord.Interaction,
        server: app_commands.Choice[str],
        start: str,
        duration: str,
    ):
        if await refuse_unavailable(interaction, deps):
            return
        await book_and_reply(interaction, deps, int(server.value), start, duration)

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
            describe_booking(b, deps.server_name_for_key(b.server_key))
            for b in live
        ]
        await _reply(interaction, fit_lines(lines))

    @tree.command(
        name="cancel_booking",
        description="Cancel one of your bookings (admins can cancel any)",
    )
    @app_commands.describe(booking_id="Booking to cancel")
    async def cancel_booking(
        interaction: discord.Interaction, booking_id: int
    ):
        if await refuse_unavailable(interaction, deps):
            return
        await cancel_and_reply(interaction, deps, booking_id)

    @cancel_booking.autocomplete("booking_id")
    async def cancel_booking_autocomplete(
        interaction: discord.Interaction, current: str
    ):
        if (
            deps.service is None
            or not deps.config.enabled
            or interaction.guild_id != deps.config.guild_id
        ):
            return []
        admin = is_booking_admin(interaction.user, deps.config)
        try:
            live = await asyncio.to_thread(deps.service.list_live)
        except sqlite3.Error:
            logger.exception(
                "Booking store failed during /cancel_booking autocomplete"
            )
            return []
        needle = current.lower()
        choices = []
        for booking in live:
            if not admin and booking.user_id != interaction.user.id:
                continue
            label = booking_label(booking, deps)
            if needle and needle not in label.lower():
                continue
            choices.append(
                app_commands.Choice(name=label[:100], value=booking.id)
            )
        return choices[:25]
