"""Booking views opened from the persistent panel's 📅 Book Server and 📋 Manage Bookings buttons.

Creation and cancellation go through the same helpers as /book and /cancel_booking, so the
panel and the slash commands validate, refuse, and reply identically.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3

import discord

from commands.booking import (
    NOT_A_MEMBER_MESSAGE,
    STORE_ERROR_MESSAGE,
    BookingCommandDeps,
    _defer,
    _reply,
    book_and_reply,
    booking_label,
    can_book,
    cancel_and_reply,
    describe_booking,
    fit_lines,
    is_booking_admin,
    refuse_unavailable,
)
from services.booking import (
    DURATION_HINT,
    MAX_OCCURRENCES,
    START_FORMAT_HINT,
    Booking,
)

logger = logging.getLogger(__name__)

RECURRENCE_CHOICES = [
    ("none", "No recurrence (single booking)"),
    ("daily", "Daily"),
    ("weekly", "Weekly"),
    ("biweekly", "Every 2 weeks"),
]


class _DeferSelect(discord.ui.Select):
    """A select whose value is read later by a button; picking just acknowledges."""

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()


class _CloseButton(discord.ui.Button):
    def __init__(self, row: int):
        super().__init__(label="Close", style=discord.ButtonStyle.danger, row=row)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            content="Booking panel closed.", embed=None, view=None
        )


# ---------------------------------------------------------------- creation


async def open_book_panel(
    interaction: discord.Interaction, deps: BookingCommandDeps
) -> None:
    if await refuse_unavailable(interaction, deps):
        return
    if not can_book(interaction.user, deps.config):
        await _reply(interaction, NOT_A_MEMBER_MESSAGE)
        return
    embed = discord.Embed(
        title="📅 Book Server",
        description="Pick a server and recurrence, then press **Set time** to enter the slot.",
        color=0x00FF00,
    )
    await interaction.response.send_message(
        embed=embed, view=BookingCreationView(deps), ephemeral=True
    )


class BookingCreationView(discord.ui.View):
    def __init__(self, deps: BookingCommandDeps):
        super().__init__(timeout=300)
        self.deps = deps
        self.server_select = _DeferSelect(
            placeholder="Select a server...",
            options=[
                discord.SelectOption(label=name[:100], value=str(index))
                for index, name in deps.servers()[:25]
            ],
            row=0,
        )
        self.recurrence_select = _DeferSelect(
            placeholder="Recurrence (default: none)",
            options=[
                discord.SelectOption(label=label, value=value)
                for value, label in RECURRENCE_CHOICES
            ],
            row=1,
        )
        self.add_item(self.server_select)
        self.add_item(self.recurrence_select)
        self.add_item(_CloseButton(row=2))

    @discord.ui.button(label="Set time", style=discord.ButtonStyle.success, row=2)
    async def set_time(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if not self.server_select.values:
            await _reply(interaction, "Please select a server first.")
            return
        recurrence = (self.recurrence_select.values or ["none"])[0]
        await interaction.response.send_modal(
            BookingTimeModal(
                self.deps, int(self.server_select.values[0]), recurrence
            )
        )


class BookingTimeModal(discord.ui.Modal, title="Create Server Booking"):
    start_time = discord.ui.TextInput(
        label=f"Start ({START_FORMAT_HINT})", max_length=16
    )
    duration = discord.ui.TextInput(
        label="Duration", placeholder=DURATION_HINT[:100], max_length=10
    )
    occurrences = discord.ui.TextInput(
        label=f"Occurrences (recurring only, max {MAX_OCCURRENCES})",
        required=False,
        max_length=2,
        default="1",
    )

    def __init__(
        self, deps: BookingCommandDeps, server_index: int, recurrence: str
    ):
        super().__init__()
        self.deps = deps
        self.server_index = server_index
        self.recurrence = recurrence
        self.start_time.placeholder = f"Time zone: {deps.config.timezone.key}"

    async def on_submit(self, interaction: discord.Interaction):
        try:
            count = int(self.occurrences.value or "1")
        except ValueError:
            await _reply(interaction, "❌ Occurrences must be a whole number.")
            return
        await book_and_reply(
            interaction,
            self.deps,
            self.server_index,
            self.start_time.value,
            self.duration.value,
            self.recurrence,
            count,
        )


# ---------------------------------------------------------------- management


async def _load_live(
    interaction: discord.Interaction, deps: BookingCommandDeps
) -> list[Booking] | None:
    """Live bookings read off the event loop, or None (after telling the user) if the store failed."""
    try:
        return await asyncio.to_thread(deps.service.list_live)
    except sqlite3.Error:
        logger.exception("Booking store failed while opening Manage Bookings")
        await _reply(interaction, STORE_ERROR_MESSAGE)
        return None


async def open_manage_panel(
    interaction: discord.Interaction, deps: BookingCommandDeps
) -> None:
    if await refuse_unavailable(interaction, deps):
        return
    await _defer(interaction)
    bookings = await _load_live(interaction, deps)
    if bookings is None:
        return
    view = ManageBookingsView(
        deps,
        bookings,
        interaction.user.id,
        is_booking_admin(interaction.user, deps.config),
    )
    await interaction.followup.send(
        embed=view.embed,
        view=view,
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


class ManageBookingsView(discord.ui.View):
    def __init__(
        self,
        deps: BookingCommandDeps,
        bookings: list[Booking],
        user_id: int,
        is_admin: bool,
    ):
        super().__init__(timeout=300)
        self.deps = deps
        self.user_id = user_id
        self.is_admin = is_admin
        self._show(bookings)

    def _show(self, bookings: list[Booking]) -> None:
        lines = [
            describe_booking(b, self.deps.server_name_for_key(b.server_key))
            for b in bookings
        ]
        self.embed = discord.Embed(
            title="📋 Manage Bookings",
            description=fit_lines(lines) or "No active or upcoming bookings.",
            color=0x2F3136,
        )
        manageable = [
            b for b in bookings if self.is_admin or b.user_id == self.user_id
        ][:25]
        self.booking_select = _DeferSelect(
            placeholder="Select one of your bookings to cancel...",
            options=[
                discord.SelectOption(
                    label=booking_label(b, self.deps), value=str(b.id)
                )
                for b in manageable
            ],
            row=0,
        )
        self.clear_items()
        if manageable:
            self.add_item(self.booking_select)
            self.add_item(self.cancel_selected)
        self.add_item(self.refresh)
        self.add_item(_CloseButton(row=1))

    @discord.ui.button(
        label="Cancel selected booking", style=discord.ButtonStyle.danger, row=1
    )
    async def cancel_selected(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if not self.booking_select.values:
            await _reply(interaction, "Select a booking first.")
            return
        await cancel_and_reply(
            interaction, self.deps, int(self.booking_select.values[0])
        )

    @discord.ui.button(label="🔄 Refresh", style=discord.ButtonStyle.secondary, row=1)
    async def refresh(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.defer()
        bookings = await _load_live(interaction, self.deps)
        if bookings is None:
            return
        self._show(bookings)
        await interaction.edit_original_response(embed=self.embed, view=self)
