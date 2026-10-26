"""Booking views opened from the persistent panel's 📅 Book Server and 📋 Manage Bookings buttons.

Creation and cancellation go through the same helpers as /book and /cancel_booking, so the
panel and the slash commands validate, refuse, and reply identically.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import date, datetime, timedelta

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
from services.booking import MAX_OCCURRENCES, Booking

logger = logging.getLogger(__name__)

RECURRENCE_CHOICES = [
    ("none", "Does not repeat"),
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
    view = BookingPickerView(deps)
    await interaction.response.send_message(
        embed=view.embed(), view=view, ephemeral=True
    )


DATE_PAGE_DAYS = 14
SLOT_MINUTES = 30
DURATION_CHOICES = [30, 60, 90, 120, 150, 180, 240, 300, 360, 480, 720, 1440]


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60 % 24:02d}:{minutes % 60:02d}"


def _duration_label(minutes: int) -> str:
    hours, mins = divmod(minutes, 60)
    if not hours:
        return f"{mins} min"
    return f"{hours} h {mins:02d}" if mins else f"{hours} h"


class _StateSelect(discord.ui.Select):
    """Stores its pick on the picker view and redraws it, like editing a calendar field."""

    def __init__(self, field: str, cast, **kwargs):
        super().__init__(**kwargs)
        self.field = field
        self.cast = cast

    async def callback(self, interaction: discord.Interaction):
        setattr(self.view, self.field, self.cast(self.values[0]))
        await self.view.redraw(interaction)


class _ActionButton(discord.ui.Button):
    def __init__(self, action, **kwargs):
        super().__init__(**kwargs)
        self.action = action

    async def callback(self, interaction: discord.Interaction):
        await self.action(interaction)


class BookingPickerView(discord.ui.View):
    """Google-Calendar-style picker: server, date, start time and length as dropdowns.

    The embed shows the slot as it will be booked and updates on every pick. 🔁 swaps in a
    repeat panel, like Calendar's "Does not repeat" menu. Discord allows 5 rows and 25
    options per select, so dates page two weeks at a time and start times show one half
    of the day (AM/PM toggle) in 30-minute steps.
    """

    def __init__(self, deps: BookingCommandDeps):
        super().__init__(timeout=600)
        self.deps = deps
        self.tz = deps.config.timezone
        now = datetime.now(self.tz)
        self.today = now.date()
        servers = deps.servers()
        self.server_index: int | None = servers[0][0] if len(servers) == 1 else None
        # Default slot: the next half hour, one hour long, like a new Calendar event.
        minute_of_day = now.hour * 60 + now.minute
        next_slot = (minute_of_day // SLOT_MINUTES + 1) * SLOT_MINUTES
        self.date = self.today + timedelta(days=next_slot // 1440)
        self.start_minutes = next_slot % 1440
        self.pm = self.start_minutes >= 720
        self.page = (self.date - self.today).days // DATE_PAGE_DAYS
        self.duration = 60
        self.recurrence = "none"
        self.count = 1
        self.editing_repeat = False
        self.max_days = max(1, deps.config.max_lead_seconds // 86400)
        self.max_minutes = deps.config.max_duration_seconds // 60 or 1440
        self._render()

    # ------------------------------------------------------------ display

    def embed(self) -> discord.Embed:
        start = datetime.combine(self.date, datetime.min.time(), tzinfo=self.tz)
        start += timedelta(minutes=self.start_minutes)
        end = start + timedelta(minutes=self.duration)
        end_text = end.strftime("%H:%M")
        if end.date() != start.date():
            end_text += f" ({end.strftime('%a %d %b')})"
        server = dict(self.deps.servers()).get(self.server_index, "*Pick a server*")
        repeat = (
            "Does not repeat"
            if self.recurrence == "none"
            else f"{dict(RECURRENCE_CHOICES)[self.recurrence]}, {self.count} times"
        )
        return discord.Embed(
            title="📅 Book Server",
            description="\n".join(
                [
                    f"🕒 **{start.strftime('%A, %d %B')}** · **{start.strftime('%H:%M')} – {end_text}**",
                    f"🖥️ {server}",
                    f"🔁 {repeat}",
                    f"🌐 Times in {self.tz.key}",
                ]
            ),
            color=0x5EA9F5,
        )

    def _render(self) -> None:
        self.clear_items()
        if self.editing_repeat:
            self._render_repeat()
        else:
            self._render_picker()

    def _render_picker(self) -> None:
        self.add_item(
            _StateSelect(
                "server_index",
                int,
                placeholder="Select a server...",
                options=[
                    discord.SelectOption(
                        label=name[:100],
                        value=str(index),
                        default=index == self.server_index,
                    )
                    for index, name in self.deps.servers()[:25]
                ],
                row=0,
            )
        )

        first = self.page * DATE_PAGE_DAYS
        last = min(first + DATE_PAGE_DAYS, self.max_days + 1)
        self.add_item(
            _StateSelect(
                "date",
                date.fromisoformat,
                placeholder="Date",
                options=[
                    discord.SelectOption(
                        label=d.strftime("%a %d %b"),
                        value=d.isoformat(),
                        description={0: "Today", 1: "Tomorrow"}.get(
                            (d - self.today).days
                        ),
                        default=d == self.date,
                    )
                    for d in (
                        self.today + timedelta(days=n) for n in range(first, last)
                    )
                ],
                row=1,
            )
        )

        base = 720 if self.pm else 0
        self.add_item(
            _StateSelect(
                "start_minutes",
                int,
                placeholder="Start time",
                options=[
                    discord.SelectOption(
                        label=_hhmm(m), value=str(m), default=m == self.start_minutes
                    )
                    for m in range(base, base + 720, SLOT_MINUTES)
                ],
                row=2,
            )
        )

        self.add_item(
            _StateSelect(
                "duration",
                int,
                placeholder="Length",
                options=[
                    discord.SelectOption(
                        label=_duration_label(m),
                        value=str(m),
                        description=f"Ends {_hhmm(self.start_minutes + m)}",
                        default=m == self.duration,
                    )
                    for m in DURATION_CHOICES
                    if m <= self.max_minutes
                ],
                row=3,
            )
        )

        last_page = self.max_days // DATE_PAGE_DAYS
        buttons = [
            _ActionButton(self._shift_page(-1), label="◀", disabled=self.page == 0),
            _ActionButton(
                self._shift_page(1), label="▶", disabled=self.page >= last_page
            ),
            _ActionButton(
                self._toggle_half, label="AM times" if self.pm else "PM times"
            ),
            _ActionButton(self._open_repeat, label="🔁 Repeat"),
            _ActionButton(
                self._book,
                label="Book",
                style=discord.ButtonStyle.success,
                disabled=self.server_index is None,
            ),
        ]
        for button in buttons:
            button.row = 4
            self.add_item(button)

    def _render_repeat(self) -> None:
        self.add_item(
            _StateSelect(
                "recurrence",
                str,
                options=[
                    discord.SelectOption(
                        label=label, value=value, default=value == self.recurrence
                    )
                    for value, label in RECURRENCE_CHOICES
                ],
                row=0,
            )
        )
        self.add_item(
            _StateSelect(
                "count",
                int,
                placeholder="How many times",
                disabled=self.recurrence == "none",
                options=[
                    discord.SelectOption(
                        label=f"{n} times", value=str(n), default=n == self.count
                    )
                    for n in range(2, MAX_OCCURRENCES + 1)
                ],
                row=1,
            )
        )
        self.add_item(
            _ActionButton(
                self._close_repeat,
                label="Done",
                style=discord.ButtonStyle.primary,
                row=2,
            )
        )

    async def redraw(self, interaction: discord.Interaction) -> None:
        if self.recurrence != "none" and self.count < 2:
            self.count = 2
        self._render()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    # ------------------------------------------------------------ actions

    def _shift_page(self, step: int):
        async def action(interaction: discord.Interaction):
            self.page += step
            self.date = self.today + timedelta(days=self.page * DATE_PAGE_DAYS)
            await self.redraw(interaction)

        return action

    async def _toggle_half(self, interaction: discord.Interaction):
        self.pm = not self.pm
        self.start_minutes = (self.start_minutes + 720) % 1440
        await self.redraw(interaction)

    async def _open_repeat(self, interaction: discord.Interaction):
        self.editing_repeat = True
        await self.redraw(interaction)

    async def _close_repeat(self, interaction: discord.Interaction):
        self.editing_repeat = False
        await self.redraw(interaction)

    async def _book(self, interaction: discord.Interaction):
        await book_and_reply(
            interaction,
            self.deps,
            self.server_index,
            f"{self.date.isoformat()} {_hhmm(self.start_minutes)}",
            str(self.duration),
            self.recurrence,
            self.count if self.recurrence != "none" else 1,
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
