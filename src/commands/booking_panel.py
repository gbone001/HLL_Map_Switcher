"""Booking views opened from the persistent panel's 📅 Book Server and 📋 Manage Bookings buttons.

Creation and cancellation go through the same helpers as /book and /cancel_booking, so the
panel and the slash commands validate, refuse, and reply identically.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

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
    BookingGroup,
    group_by_series,
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
    await interaction.response.send_modal(BookingModal(deps, BookingDraft.new(deps)))


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
MINUTE_CHOICES = (0, 15, 30, 45)
LENGTH_CHOICES = range(30, 6 * 60 + 1, 15)
FREQUENCY_CHOICES = [
    ("none", "Does not repeat"),
    ("daily", "Daily"),
    ("weekly", "Weekly"),
    ("biweekly", "Every 2 weeks"),
]


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60 % 24:02d}:{minutes % 60:02d}"


def _length_label(minutes: int) -> str:
    hours, mins = divmod(minutes, 60)
    if not hours:
        return f"{mins} min"
    return f"{hours} h {mins:02d}" if mins else f"{hours} h"


@dataclass
class BookingDraft:
    """What the two booking steps are building: the pop-up fills the when/where/what,
    the confirm step the length and repeat. Editing starts from the booking's values."""

    event_name: str | None = None
    server_index: int | None = None
    day: date | None = None
    start_minute: int = 19 * 60
    length: int = 120
    recurrence: str = "none"
    count: int = 2
    editing: BookingGroup | None = None
    on_saved: Callable[[], Awaitable[None]] | None = None

    @classmethod
    def new(cls, deps: BookingCommandDeps) -> BookingDraft:
        servers = deps.servers()
        now = datetime.now(deps.config.timezone)
        next_hour = (now.hour + 1) % 24
        return cls(
            server_index=servers[0][0] if len(servers) == 1 else None,
            day=now.date() + timedelta(days=1 if next_hour == 0 else 0),
            start_minute=next_hour * 60,
        )

    @classmethod
    def from_group(cls, group: BookingGroup, deps: BookingCommandDeps, on_saved=None) -> BookingDraft:
        first = group.first
        tz = deps.config.timezone
        start = datetime.fromtimestamp(first.start_utc, tz)
        return cls(
            event_name=first.event_name,
            server_index=next(
                (i for i, _ in deps.servers() if deps.server_key(i) == first.server_key), None
            ),
            day=start.date(),
            start_minute=start.hour * 60 + start.minute,
            length=(first.end_utc - first.start_utc) // 60,
            recurrence=first.recurrence if group.is_series else "none",
            count=min(len(group.bookings), MAX_OCCURRENCES) if group.is_series else 2,
            editing=group,
            on_saved=on_saved,
        )

    @property
    def dates(self) -> int:
        return 1 if self.recurrence == "none" else self.count


class BookingModal(discord.ui.Modal):
    """Step 1, a pop-up: event name, server, date and start time (hour, then minute).

    A Discord modal holds at most 5 fields, so length and repeat follow in step 2
    (BookingDetailsView). With `details` set, the pop-up was reopened from step 2 and
    updates that message instead of sending a new one.
    """

    def __init__(self, deps: BookingCommandDeps, draft: BookingDraft, details: BookingDetailsView | None = None):
        super().__init__(title="Edit booking" if draft.editing else "Book a server", timeout=900)
        self.deps = deps
        self.draft = draft
        self.details = details
        today = datetime.now(deps.config.timezone).date()

        self.event = discord.ui.TextInput(
            required=False,
            max_length=EVENT_NAME_MAX_LENGTH,
            placeholder="Weekly Tank Battle",
            default=draft.event_name,
        )
        self.add_item(discord.ui.Label(text="Event name", description="Optional. Shown on the schedule.", component=self.event))

        self.server = discord.ui.Select(
            placeholder="Pick a server",
            options=[
                discord.SelectOption(label=name[:100], value=str(index), default=index == draft.server_index)
                for index, name in deps.servers()[:25]
            ],
        )
        self.add_item(discord.ui.Label(text="Server", component=self.server))

        chosen = draft.day or today
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

        hour, minute = divmod(draft.start_minute, 60)
        self.hour = discord.ui.Select(
            options=[
                discord.SelectOption(label=f"{h:02d}:00", value=str(h), default=h == hour)
                for h in range(24)
            ],
        )
        self.add_item(
            discord.ui.Label(
                text="Start hour",
                description=f"24-hour, {deps.config.timezone.key}"[:100],
                component=self.hour,
            )
        )
        minute = max(m for m in MINUTE_CHOICES if m <= minute)
        self.minute = discord.ui.Select(
            options=[
                discord.SelectOption(label=f":{m:02d}", value=str(m), default=m == minute)
                for m in MINUTE_CHOICES
            ],
        )
        self.add_item(discord.ui.Label(text="Start minute", component=self.minute))

    async def on_submit(self, interaction: discord.Interaction):
        if not self.server.values or not self.date.values:
            await _reply(interaction, "❌ Pick a server and a date.")
            return
        draft = self.draft
        draft.event_name = self.event.value or None
        draft.server_index = int(self.server.values[0])
        draft.day = date.fromisoformat(self.date.values[0])
        draft.start_minute = int((self.hour.values or ["19"])[0]) * 60 + int((self.minute.values or ["0"])[0])

        if self.details is not None:
            self.details.render()
            await interaction.response.edit_message(embed=self.details.embed(), view=self.details)
            self.details.close = interaction.delete_original_response
            return
        details = BookingDetailsView(self.deps, draft)
        await interaction.response.send_message(embed=details.embed(), view=details, ephemeral=True)
        details.close = interaction.delete_original_response


class BookingDetailsView(_SelfClosingView):
    """Step 2: length, how often it repeats, and how many dates, then Book (or Save)."""

    def __init__(self, deps: BookingCommandDeps, draft: BookingDraft):
        super().__init__(timeout=900)
        self.deps = deps
        self.draft = draft
        self.max_length = deps.config.max_duration_seconds // 60 or 24 * 60
        self.render()

    def embed(self) -> discord.Embed:
        draft = self.draft
        tz = self.deps.config.timezone
        start = datetime.combine(draft.day, datetime.min.time(), tzinfo=tz) + timedelta(minutes=draft.start_minute)
        end = start + timedelta(minutes=draft.length)
        end_text = end.strftime("%H:%M") + (f" ({end:%a %d %b})" if end.date() != start.date() else "")
        server = dict(self.deps.servers()).get(draft.server_index, "?")
        repeat = (
            "Does not repeat"
            if draft.recurrence == "none"
            else f"{dict(FREQUENCY_CHOICES)[draft.recurrence]} · {draft.count} dates"
        )
        lines = [
            f"🕒 **{start:%A %d %B}** · **{start:%H:%M} – {end_text}** ({_length_label(draft.length)})",
            f"🖥️ {server}",
            f"🔁 {repeat}",
        ]
        if draft.event_name:
            lines.insert(0, f"🏷️ **{discord.utils.escape_markdown(draft.event_name)}**")
        lines.append(f"-# Times in {tz.key}. Pick the length and repeat, then press {self.save_label}.")
        return discord.Embed(
            title="✏️ Edit booking" if draft.editing else "📅 Book a server",
            description="\n".join(lines),
            color=0x5865F2,
        )

    @property
    def save_label(self) -> str:
        return "Save changes" if self.draft.editing else "Book"

    def render(self) -> None:
        draft = self.draft
        self.clear_items()
        self.add_item(
            _DraftSelect(
                "length",
                placeholder="Length",
                options=[
                    discord.SelectOption(
                        label=_length_label(m),
                        value=str(m),
                        description=f"Ends {_hhmm(draft.start_minute + m)}",
                        default=m == draft.length,
                    )
                    for m in LENGTH_CHOICES
                    if m <= self.max_length
                ],
                row=0,
            )
        )
        self.add_item(
            _DraftSelect(
                "recurrence",
                cast=str,
                options=[
                    discord.SelectOption(label=label, value=value, default=value == draft.recurrence)
                    for value, label in FREQUENCY_CHOICES
                ],
                row=1,
            )
        )
        self.add_item(
            _DraftSelect(
                "count",
                placeholder="Number of dates",
                disabled=draft.recurrence == "none",
                options=[
                    discord.SelectOption(label=f"{n} dates", value=str(n), default=n == draft.count)
                    for n in range(2, MAX_OCCURRENCES + 1)
                ],
                row=2,
            )
        )
        for button in (
            _DraftButton(self.save, label=self.save_label, style=discord.ButtonStyle.success),
            _DraftButton(self.change_details, label="✏️ Change details"),
            _DraftButton(self.cancel, label="Cancel", style=discord.ButtonStyle.danger),
        ):
            button.row = 3
            self.add_item(button)

    async def redraw(self, interaction: discord.Interaction) -> None:
        self.render()
        await interaction.response.edit_message(embed=self.embed(), view=self)
        self.close = interaction.delete_original_response

    async def change_details(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(BookingModal(self.deps, self.draft, details=self))

    async def cancel(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        self.close = interaction.delete_original_response
        await self.finish()

    async def save(self, interaction: discord.Interaction) -> None:
        draft = self.draft
        # Deferring keeps this message as the interaction's original response, so it can
        # be deleted once the booking is saved.
        await interaction.response.defer()
        start = f"{draft.day.isoformat()} {_hhmm(draft.start_minute)}"
        if draft.editing is not None:
            saved = await edit_and_reply(
                interaction,
                self.deps,
                draft.editing.first.id,
                server_index=draft.server_index,
                start=start,
                duration=str(draft.length),
                event_name=draft.event_name,
                recurrence=draft.recurrence,
                count=draft.dates,
            )
        else:
            saved = await book_and_reply(
                interaction,
                self.deps,
                draft.server_index,
                start,
                str(draft.length),
                draft.recurrence,
                draft.dates,
                event_name=draft.event_name,
            )
        if not saved:
            return
        self.close = interaction.delete_original_response
        await self.finish()
        if draft.on_saved is not None:
            await draft.on_saved()


class _DraftSelect(discord.ui.Select):
    """Stores its pick on the draft and redraws the confirm step."""

    def __init__(self, field: str, cast=int, **kwargs):
        super().__init__(**kwargs)
        self.field = field
        self.cast = cast

    async def callback(self, interaction: discord.Interaction):
        setattr(self.view.draft, self.field, self.cast(self.values[0]))
        await self.view.redraw(interaction)


class _DraftButton(discord.ui.Button):
    def __init__(self, action, **kwargs):
        super().__init__(**kwargs)
        self.action = action

    async def callback(self, interaction: discord.Interaction):
        await self.action(interaction)


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
    view.message = message


class ManageBookingsView(_SelfClosingView):
    message = None  # the sent Manage Bookings message, redrawn after edits
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
            BookingModal(self.deps, BookingDraft.from_group(group, self.deps, on_saved=self.reload_message))
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

    async def reload_message(self) -> None:
        """Redraw this Manage Bookings message after an edit made from it."""
        if self.message is None:
            return
        try:
            bookings = await asyncio.to_thread(self.deps.service.list_live)
            self._show(bookings)
            await self.message.edit(embed=self.embed, view=self)
        except (sqlite3.Error, discord.HTTPException):
            logger.exception("Could not redraw Manage Bookings after an edit")

    async def _reload(self, interaction: discord.Interaction) -> None:
        bookings = await _load_live(interaction, self.deps)
        if bookings is None:
            return
        self._show(bookings)
        await interaction.edit_original_response(embed=self.embed, view=self)
