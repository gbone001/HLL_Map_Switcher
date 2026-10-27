"""Booking views opened from the persistent panel's 📅 Book Server and 📋 Manage Bookings buttons.

Creation and cancellation go through the same helpers as /book and /cancel_booking, so the
panel and the slash commands validate, refuse, and reply identically.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta

import discord

from commands.booking import (
    NOT_A_MEMBER_MESSAGE,
    STORE_ERROR_MESSAGE,
    BookingCommandDeps,
    _defer,
    _reply,
    book_and_reply,
    edit_and_reply,
    booker_text,
    can_book,
    cancel_and_reply,
    describe_group,
    group_label,
    fit_lines,
    guild_admin_id,
    is_booking_admin,
    refuse_unavailable,
)
from services.booking import (
    EVENT_NAME_MAX_LENGTH,
    MAX_OCCURRENCES,
    Booking,
    BookingError,
    BookingGroup,
    group_by_series,
    parse_time_range,
)

logger = logging.getLogger(__name__)

class _DeferSelect(discord.ui.Select):
    """A select whose value is read later by a button; picking just acknowledges."""

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()


class _CloseButton(discord.ui.Button):
    def __init__(self, row: int):
        super().__init__(label="Close", style=discord.ButtonStyle.danger, row=row)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        self.view.close = interaction.delete_original_response
        await self.view.finish()


# ---------------------------------------------------------------- creation


async def open_book_panel(
    interaction: discord.Interaction, deps: BookingCommandDeps
) -> None:
    if await refuse_unavailable(interaction, deps):
        return
    if not can_book(interaction.user, deps):
        await _reply(interaction, NOT_A_MEMBER_MESSAGE)
        return
    await interaction.response.send_modal(BookingModal(deps))


class _SelfClosingView(discord.ui.View):
    """An ephemeral panel that deletes its own message when it times out or is finished."""

    close = None  # coroutine function that deletes the panel message; set once it is sent

    async def finish(self) -> None:
        self.stop()
        if self.close is not None:
            try:
                await self.close()
            except discord.HTTPException:
                pass  # already dismissed, or the interaction token expired

    async def on_timeout(self) -> None:
        await self.finish()


# Discord allows 25 options per dropdown, so the date list covers 25 days.
DATE_DAYS = 25
REPEAT_CHOICES = (
    [("none:1", "Does not repeat")]
    + [(f"daily:{n}", f"Daily · {n} days") for n in (2, 3, 5, 7, 14)]
    + [(f"weekly:{n}", f"Weekly · {n} weeks") for n in (2, 3, 4, 6, 8, 10, 12, 16, 20)]
    + [(f"biweekly:{n}", f"Every 2 weeks · {n} times") for n in (2, 3, 4, 6, 8, 10)]
)


class BookingModal(discord.ui.Modal):
    """Pop-up booking form: event name, server, date, time range and repeat.

    With `editing`, the same form edits a booking (or a whole series, every remaining
    date moving together) and leaves out Repeat. A Discord modal holds at most 5 fields.
    """

    def __init__(self, deps: BookingCommandDeps, *, editing: BookingGroup | None = None, on_saved=None):
        super().__init__(title="Edit booking" if editing else "Book a server", timeout=900)
        self.deps = deps
        self.editing = editing
        self.on_saved = on_saved
        tz = deps.config.timezone
        today = datetime.now(tz).date()
        first = editing.first if editing else None
        start = datetime.fromtimestamp(first.start_utc, tz) if first else None
        end = datetime.fromtimestamp(first.end_utc, tz) if first else None

        self.event = discord.ui.TextInput(
            required=False,
            max_length=EVENT_NAME_MAX_LENGTH,
            placeholder="Weekly Tank Battle",
            default=first.event_name if first else None,
        )
        self.add_item(discord.ui.Label(text="Event name", description="Optional. Shown on the schedule.", component=self.event))

        servers = deps.servers()[:25]
        if first:
            current = next((i for i, _ in servers if deps.server_key(i) == first.server_key), None)
        else:
            current = servers[0][0] if len(servers) == 1 else None
        self.server = discord.ui.Select(
            placeholder="Pick a server",
            options=[
                discord.SelectOption(label=name[:100], value=str(index), default=index == current)
                for index, name in servers
            ],
        )
        self.add_item(discord.ui.Label(text="Server", component=self.server))

        chosen = start.date() if start else today
        max_days = max(1, deps.config.max_lead_seconds // 86400)
        window_start = today if (chosen - today).days < DATE_DAYS else chosen - timedelta(days=DATE_DAYS // 2)
        days = [
            window_start + timedelta(days=n)
            for n in range(DATE_DAYS)
            if (window_start + timedelta(days=n) - today).days <= max_days
        ]
        self.date = discord.ui.Select(
            placeholder="Pick a date",
            options=[
                discord.SelectOption(
                    label=d.strftime("%a %d %b"),
                    value=d.isoformat(),
                    description={0: "Today", 1: "Tomorrow"}.get((d - today).days),
                    default=d == chosen,
                )
                for d in days
            ],
        )
        self.add_item(discord.ui.Label(text="Date", component=self.date))

        self.time = discord.ui.TextInput(
            max_length=15,
            placeholder="19:30-22:00",
            default=f"{start:%H:%M}-{end:%H:%M}" if start else None,
        )
        self.add_item(
            discord.ui.Label(
                text="Time",
                description=f"Start and end, 24-hour, {tz.key}"[:100],
                component=self.time,
            )
        )

        self.repeat = None
        if editing is None:
            self.repeat = discord.ui.Select(
                options=[
                    discord.SelectOption(label=label, value=value, default=value == "none:1")
                    for value, label in REPEAT_CHOICES
                    if int(value.split(":")[1]) <= MAX_OCCURRENCES
                ],
            )
            self.add_item(discord.ui.Label(text="Repeat", component=self.repeat))

    async def on_submit(self, interaction: discord.Interaction):
        if not self.server.values or not self.date.values:
            await _reply(interaction, "❌ Pick a server and a date.")
            return
        try:
            start_minute, length = parse_time_range(self.time.value)
        except BookingError as exc:
            await _reply(interaction, f"❌ {exc}")
            return
        server_index = int(self.server.values[0])
        start = f"{self.date.values[0]} {start_minute // 60:02d}:{start_minute % 60:02d}"
        event_name = self.event.value or None

        if self.editing is not None:
            # Deferring keeps the Manage Bookings message as this interaction's original
            # response, so it can be redrawn once the edit is saved.
            await interaction.response.defer()
            saved = await edit_and_reply(
                interaction,
                self.deps,
                self.editing.first.id,
                server_index=server_index,
                start=start,
                duration=str(length),
                event_name=event_name,
                whole_series=self.editing.is_series,
            )
            if saved and self.on_saved is not None:
                await self.on_saved(interaction)
            return

        recurrence, count = (self.repeat.values or ["none:1"])[0].split(":")
        await book_and_reply(
            interaction,
            self.deps,
            server_index,
            start,
            str(length),
            recurrence,
            int(count),
            event_name=event_name,
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
        viewer_guild_id=interaction.guild_id,
        admin_guild_id=guild_admin_id(interaction.user),
    )
    message = await interaction.followup.send(
        embed=view.embed,
        view=view,
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
        wait=True,
    )
    view.close = message.delete


class ManageBookingsView(_SelfClosingView):
    def __init__(
        self,
        deps: BookingCommandDeps,
        bookings: list[Booking],
        user_id: int,
        is_admin: bool,
        viewer_guild_id: int | None = None,
        admin_guild_id: int | None = None,
    ):
        super().__init__(timeout=300)
        self.deps = deps
        self.user_id = user_id
        self.is_admin = is_admin
        self.viewer_guild_id = viewer_guild_id
        self.admin_guild_id = admin_guild_id
        self._show(bookings)

    def _show(self, bookings: list[Booking]) -> None:
        groups = group_by_series(bookings)
        lines = [
            describe_group(
                g,
                self.deps.server_name_for_key(g.first.server_key),
                booker_text(g.first, self.viewer_guild_id, self.deps.guild_name),
            )
            for g in groups
        ]
        self.embed = discord.Embed(
            title="📋 Manage Bookings",
            description=fit_lines(lines) or "No active or upcoming bookings.",
            color=0x5865F2,
        )
        self.embed.set_footer(text="🔁 = repeating series. Editing or cancelling a series changes all its remaining dates.")
        manageable = [
            g
            for g in groups
            if g.first.manageable_by(self.user_id, self.is_admin, self.admin_guild_id)
        ][:25]
        self.groups = {str(g.first.id): g for g in manageable}
        self.booking_select = _DeferSelect(
            placeholder="Select a booking or series to edit or cancel...",
            options=[
                discord.SelectOption(
                    label=group_label(g, self.deps, self.viewer_guild_id),
                    value=str(g.first.id),
                    description=(
                        f"Edit or cancel all {len(g.bookings)} remaining dates" if g.is_series else None
                    ),
                )
                for g in manageable
            ],
            row=0,
        )
        self.clear_items()
        if manageable:
            self.add_item(self.booking_select)
            self.add_item(self.edit_selected)
            self.add_item(self.cancel_selected)
        self.add_item(self.refresh)
        self.add_item(_CloseButton(row=1))

    @discord.ui.button(label="✏️ Edit selected", style=discord.ButtonStyle.primary, row=1)
    async def edit_selected(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if not self.booking_select.values:
            await _reply(interaction, "Select a booking first.")
            return
        group = self.groups[self.booking_select.values[0]]
        await interaction.response.send_modal(
            BookingModal(self.deps, editing=group, on_saved=self._reload)
        )

    @discord.ui.button(
        label="Cancel selected", style=discord.ButtonStyle.danger, row=1
    )
    async def cancel_selected(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if not self.booking_select.values:
            await _reply(interaction, "Select a booking first.")
            return
        await interaction.response.defer()
        selected = self.groups[self.booking_select.values[0]]
        if await cancel_and_reply(
            interaction, self.deps, selected.first.id, whole_series=selected.is_series
        ):
            await self._reload(interaction)

    @discord.ui.button(label="🔄 Refresh", style=discord.ButtonStyle.secondary, row=1)
    async def refresh(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.defer()
        await self._reload(interaction)

    async def _reload(self, interaction: discord.Interaction) -> None:
        bookings = await _load_live(interaction, self.deps)
        if bookings is None:
            return
        self._show(bookings)
        await interaction.edit_original_response(embed=self.embed, view=self)
