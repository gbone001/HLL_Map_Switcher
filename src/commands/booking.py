"""Booking slash commands: /book, /bookings, /cancel_booking.

All replies are ephemeral. Times are entered in BOOKING_TIMEZONE and shown as Discord
timestamps, which each viewer's client renders in their own time zone.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

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
MAX_LISTED_BOOKINGS = 20


@dataclass
class BookingCommandDeps:
    service: Optional[BookingService]
    config: BookingConfig
    servers: Callable[[], list[tuple[int, str]]]
    server_key: Callable[[int], str]
    server_name_for_key: Callable[[str], str]
    on_change: Callable[[], Awaitable[None]]


def is_booking_admin(user: object, config: BookingConfig) -> bool:
    """Discord Administrator permission, or the configured BOOKING_ADMIN_ROLE_ID."""
    if not isinstance(user, discord.Member):
        return False
    if user.guild_permissions.administrator:
        return True
    return config.admin_role_id is not None and any(role.id == config.admin_role_id for role in user.roles)


def can_book(user: object, config: BookingConfig) -> bool:
    if is_booking_admin(user, config):
        return True
    if not isinstance(user, discord.Member) or config.role_id is None:
        return False
    return any(role.id == config.role_id for role in user.roles)


def timestamp(ts: int, style: str = "F") -> str:
    return f"<t:{ts}:{style}>"


def describe_booking(booking: Booking, server_name: str) -> str:
    return (
        f"#{booking.id} **{server_name}** — <@{booking.user_id}> "
        f"{timestamp(booking.start_utc)} → {timestamp(booking.end_utc, 't')} ({timestamp(booking.start_utc, 'R')})"
    )


async def _reply(interaction: discord.Interaction, content: str) -> None:
    await interaction.response.send_message(content, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


def register_booking_commands(tree: app_commands.CommandTree, deps: BookingCommandDeps) -> None:
    server_choices = [
        app_commands.Choice(name=name[:100], value=str(index)) for index, name in deps.servers()[:25]
    ]

    @tree.command(name="book", description="Book a server for a time slot")
    @app_commands.describe(
        server="Server to book",
        start=f"Start time ({START_FORMAT_HINT}, {deps.config.timezone.key})",
        duration=f"Length: {DURATION_HINT}",
    )
    @app_commands.choices(server=server_choices)
    async def book(interaction: discord.Interaction, server: app_commands.Choice[str], start: str, duration: str):
        if deps.service is None or not deps.config.enabled:
            await _reply(interaction, NOT_CONFIGURED_MESSAGE)
            return
        if not can_book(interaction.user, deps.config):
            role = f"<@&{deps.config.role_id}>" if deps.config.role_id else "the booking"
            await _reply(interaction, f"You need the {role} role to book a server.")
            return

        try:
            start_utc = parse_start(start, deps.config.timezone)
            end_utc = start_utc + parse_duration(duration)
            server_key = deps.server_key(int(server.value))
            booking = deps.service.create(
                server_key=server_key,
                user_id=interaction.user.id,
                user_name=interaction.user.display_name,
                start_utc=start_utc,
                end_utc=end_utc,
            )
        except BookingOverlapError as exc:
            conflict = exc.conflict
            await _reply(
                interaction,
                f"❌ That overlaps <@{conflict.user_id}>'s booking #{conflict.id}: "
                f"{timestamp(conflict.start_utc)} → {timestamp(conflict.end_utc, 't')}.",
            )
            return
        except BookingError as exc:
            await _reply(
                interaction,
                f"❌ {exc} Use start `{START_FORMAT_HINT}` in {deps.config.timezone.key} "
                f"and duration as {DURATION_HINT}.",
            )
            return

        await _reply(
            interaction,
            f"✅ **{server.name}** booked {timestamp(booking.start_utc)} → {timestamp(booking.end_utc, 't')} "
            f"(booking #{booking.id}). During your slot only you and booking admins can change this server.",
        )
        await deps.on_change()

    @tree.command(name="bookings", description="List active and upcoming server bookings")
    @app_commands.describe(server="Only show bookings for this server")
    @app_commands.choices(server=server_choices)
    async def bookings(interaction: discord.Interaction, server: Optional[app_commands.Choice[str]] = None):
        if deps.service is None or not deps.config.enabled:
            await _reply(interaction, NOT_CONFIGURED_MESSAGE)
            return
        server_key = deps.server_key(int(server.value)) if server else None
        live = deps.service.list_live(server_key)
        if not live:
            await _reply(interaction, "No active or upcoming bookings.")
            return
        lines = [describe_booking(b, deps.server_name_for_key(b.server_key)) for b in live[:MAX_LISTED_BOOKINGS]]
        if len(live) > MAX_LISTED_BOOKINGS:
            lines.append(f"…and {len(live) - MAX_LISTED_BOOKINGS} more.")
        await _reply(interaction, "\n".join(lines))

    @tree.command(name="cancel_booking", description="Cancel one of your bookings (admins can cancel any)")
    @app_commands.describe(booking_id="Booking to cancel")
    async def cancel_booking(interaction: discord.Interaction, booking_id: int):
        if deps.service is None or not deps.config.enabled:
            await _reply(interaction, NOT_CONFIGURED_MESSAGE)
            return
        try:
            booking = deps.service.cancel(
                booking_id, interaction.user.id, is_booking_admin(interaction.user, deps.config)
            )
        except BookingError as exc:
            await _reply(interaction, f"❌ {exc}")
            return
        await _reply(
            interaction,
            f"✅ Cancelled booking #{booking.id} on **{deps.server_name_for_key(booking.server_key)}**.",
        )
        await deps.on_change()

    @cancel_booking.autocomplete("booking_id")
    async def cancel_booking_autocomplete(interaction: discord.Interaction, current: str):
        if deps.service is None or not deps.config.enabled:
            return []
        admin = is_booking_admin(interaction.user, deps.config)
        choices = []
        for booking in deps.service.list_live():
            if not admin and booking.user_id != interaction.user.id:
                continue
            label = (
                f"#{booking.id} {deps.server_name_for_key(booking.server_key)} "
                f"{format_local(booking.start_utc, deps.config.timezone)} ({booking.user_name})"
            )
            if current and current not in label:
                continue
            choices.append(app_commands.Choice(name=label[:100], value=booking.id))
        return choices[:25]
