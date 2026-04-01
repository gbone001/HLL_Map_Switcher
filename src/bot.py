import asyncio
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple

import discord
from discord.ext import commands
from dotenv import load_dotenv
from utils.map_data import (
    get_maps_for_mode,
    get_variants_for_map,
    refresh_map_cache,
)
from utils.api_client import HLLAPIClient
from utils.crcon_http import CRCONHttpClient, CRCONHTTPError

# Load environment variables
load_dotenv()

# Bot configuration
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# API client
api_client = HLLAPIClient()
_http_clients: Dict[Optional[int], CRCONHttpClient] = {}
_http_client_errors: Dict[Optional[int], str] = {}


def _server_number(server_index: Optional[int]) -> Optional[int]:
    if server_index is None:
        return None
    return server_index + 1


def _get_http_client(server_index: Optional[int] = None) -> Optional[CRCONHttpClient]:
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


def _http_error_message(server_index: Optional[int]) -> str:
    return _http_client_errors.get(server_index) or "HTTP API credentials are not configured for this server."


def _get_channel_focused_server(channel: Optional[object]) -> Optional[int]:
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
persistent_message_ref: Optional[Tuple[int, int]] = None
# Map channel_id -> focused server index (None = show all)
_persistent_focused_server: Dict[int, Optional[int]] = {}
# Map (server_index, map_pretty_name) -> current objectives last set through this bot
_current_objectives_state: Dict[Tuple[int, str], list[str]] = {}
# Map (server_index, map_id) -> dynamic weather state last set through this bot
_dynamic_weather_state: Dict[Tuple[int, str], bool] = {}


def _format_time_remaining(time_remaining: Optional[float], raw_time: Optional[str]) -> str:
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


def _get_server_env_value(name: str, server_index: Optional[int] = None) -> Optional[str]:
    server_number = _server_number(server_index)
    if server_number is not None:
        server_key = f"SERVER{server_number}_{name}"
        if server_key in os.environ:
            return os.environ[server_key]
    return os.getenv(name)


def _get_timer_minutes(name: str, default: int, server_index: Optional[int] = None) -> int:
    raw_value = (_get_server_env_value(name, server_index) or "").strip()
    if not raw_value:
        return default

    try:
        value = int(raw_value)
    except ValueError:
        return default

    return value if value > 0 else default


def _get_focused_or_single_server(interaction: discord.Interaction) -> Optional[int]:
    servers = api_client.get_servers()
    focused_server = _get_channel_focused_server(interaction.channel)

    if focused_server is not None and any(index == focused_server for index, _ in servers):
        return focused_server

    if len(servers) == 1:
        return servers[0][0]

    return None





def build_main_embed(focused_server_index: Optional[int] = None) -> discord.Embed:
    servers = api_client.get_servers()
    server_lines: list[str] = []
    updated_at_text = ""
    selected_server_label = "None"

    if servers:
        selected_name = next((name for index, name in servers if index == focused_server_index), None)
        if selected_name:
            selected_server_label = selected_name
        elif focused_server_index is not None:
            selected_server_label = f"Unknown ({focused_server_index})"

        for position, (index, server_name) in enumerate(servers, start=1):
            client = _get_http_client(index)
            selected_prefix = "**[SELECTED]** " if focused_server_index == index else ""

            if not client:
                server_lines.append(f"{position}. {selected_prefix}{server_name}")
                server_lines.append(f"   Status: ⚠️ {_http_error_message(index)}")
                continue

            try:
                gamestate_resp = client.get_gamestate()
            except CRCONHTTPError as exc:
                server_lines.append(f"{position}. {selected_prefix}{server_name}")
                server_lines.append(f"   Status: ⚠️ {exc}")
                continue

            gamestate_data = gamestate_resp.get("result") if isinstance(gamestate_resp, dict) else None
            if gamestate_data:
                current_map = gamestate_data.get("current_map", {}) or {}
                map_id = str(current_map.get("id") or "")
                pretty_name = current_map.get("pretty_name") or current_map.get("id") or "Unknown"
                allied = gamestate_data.get("num_allied_players", "??")
                axis = gamestate_data.get("num_axis_players", "??")
                time_remaining = _format_time_remaining(
                    gamestate_data.get("time_remaining"),
                    gamestate_data.get("raw_time_remaining"),
                )

                objective_rows_text = "Unknown"
                known_objectives = _current_objectives_state.get((index, str(pretty_name)))
                if known_objectives is not None:
                    objective_rows_text = _format_current_objectives(known_objectives)

                dynamic_weather_text = "Unknown"
                if map_id:
                    known_state = _dynamic_weather_state.get((index, map_id))
                    if known_state is not None:
                        dynamic_weather_text = "Enabled" if known_state else "Disabled"

                server_lines.append(f"{position}. {selected_prefix}{server_name}")
                server_lines.append(f"   Map: {pretty_name}")
                server_lines.append(f"   Allied: {allied} | Axis: {axis} | Time Remaining: {time_remaining}")
                server_lines.append(f"   Dynamic Weather: {dynamic_weather_text}")
                aest = timezone(timedelta(hours=10), name="AEST")
                updated_at = datetime.now(aest)
                updated_at_text = f"Updated as at {updated_at.strftime('%d %B %Y - %H:%M:%S')} AEST"
            else:
                server_lines.append(f"{position}. {selected_prefix}{server_name}")
                server_lines.append("   Status: ⚠️ Gamestate unavailable.")
    else:
        server_lines.append("- No servers configured.")

    description = "Click the buttons below to manage the server.\n\n"
    description += f"**Currently Selected Server:** {selected_server_label}\n\n"
    description += "**Available Servers:**\n" + "\n".join(server_lines)

    if updated_at_text:
        description += f"\n\n{updated_at_text}"

    embed = discord.Embed(
        title=MAIN_EMBED_TITLE,
        description=description,
        color=0x2f3136,
    )
    embed.set_footer(text="Buttons stay active across restarts. Note: When you choose the last objective it will set the map to the selected objectives immediately.")
    return embed


async def ensure_persistent_message(channel: discord.abc.Messageable) -> Optional[discord.Message]:
    global persistent_message_ref
    channel_id = getattr(channel, "id", None)
    focused = None
    if channel_id is not None:
        focused = _persistent_focused_server.get(channel_id)
    embed = build_main_embed(focused_server_index=focused)
    view = GameModeView()
    

    if persistent_message_ref and channel_id is not None:
        ref_channel_id, message_id = persistent_message_ref
        if ref_channel_id != channel_id:
            persistent_message_ref = None
        else:
            try:
                message = await channel.fetch_message(message_id)  # type: ignore[attr-defined]
                await message.edit(embed=embed, view=view)
                return message
            except (discord.NotFound, AttributeError):
                persistent_message_ref = None

    try:
        history = channel.history  # type: ignore[attr-defined]
    except AttributeError:
        message = await channel.send(embed=embed, view=view)  # type: ignore[attr-defined]
        if channel_id is not None:
            persistent_message_ref = (channel_id, message.id)
            # initialize focus map
            _persistent_focused_server[channel_id] = None
        return message

    async for message in history(limit=50):
        if message.author == bot.user and message.embeds:
            title = message.embeds[0].title
            if title == MAIN_EMBED_TITLE or title in LEGACY_EMBED_TITLES:
                if channel_id is not None:
                    persistent_message_ref = (channel_id, message.id)
                await message.edit(embed=embed, view=view)
                # ensure focus map initialized
                if channel_id is not None and channel_id not in _persistent_focused_server:
                    _persistent_focused_server[channel_id] = None
                return message

    message = await channel.send(embed=embed, view=view)  # type: ignore[attr-defined]
    if channel_id is not None:
        persistent_message_ref = (channel_id, message.id)
    return message


async def refresh_main_embed() -> None:
    channel_id = os.getenv("DISCORD_CHANNEL_ID")
    if not channel_id:
        return

    try:
        channel_id_int = int(channel_id)
    except ValueError:
        return

    channel = bot.get_channel(channel_id_int)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id_int)
        except (discord.NotFound, discord.HTTPException):
            return

    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        return

    await ensure_persistent_message(channel)


async def _delete_interaction_after(interaction: discord.Interaction, delay: float = 10.0) -> None:
    await asyncio.sleep(delay)
    try:
        await interaction.delete_original_response()
    except (discord.NotFound, discord.HTTPException):
        pass


async def _delete_interaction_after_and_refresh(interaction: discord.Interaction, delay: float = 10.0) -> None:
    """Delete interaction response after delay and refresh the persistent panel."""
    await asyncio.sleep(delay)
    try:
        await interaction.delete_original_response()
    except (discord.NotFound, discord.HTTPException):
        pass
    await refresh_main_embed()


async def send_temporary_response(interaction: discord.Interaction, *, embed: Optional[discord.Embed] = None, content: Optional[str] = None, view: Optional[discord.ui.View] = None, delay: Optional[float] = 20, ephemeral: bool = True) -> None:
    """Send an interaction response and optionally auto-delete it after `delay` seconds."""
    if not interaction.response.is_done():
        if embed is not None and view is not None:
            await interaction.response.send_message(content=content, embed=embed, view=view, ephemeral=ephemeral)
        elif embed is not None:
            await interaction.response.send_message(content=content, embed=embed, ephemeral=ephemeral)
        elif view is not None:
            await interaction.response.send_message(content=content, view=view, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(content=content, ephemeral=ephemeral)
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


async def _delete_message_after(message: discord.Message, delay: float = 10.0) -> None:
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
    content: Optional[str] = None,
    embed: Optional[discord.Embed] = None,
    view: Optional[discord.ui.View] = None,
) -> None:
    if interaction.response.is_done():
        await interaction.edit_original_response(content=content, embed=embed, view=view)
    else:
        await interaction.response.edit_message(content=content, embed=embed, view=view)

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
            await send_temporary_response(interaction, content="Player NOT FOUND", delay=10)
            return

        try:
            added = client.add_admin(player_id=player_id, role="spectator", description="Admin cam access")
        except CRCONHTTPError:
            added = False

        if added:
            await send_temporary_response(interaction, content="Player ADDED", delay=10)
        else:
            await send_temporary_response(interaction, content="Player NOT FOUND", delay=10)


class RemoveAdminCamConfirmView(discord.ui.View):
    def __init__(self, server_index: int, player_id: str, player_name: str):
        super().__init__(timeout=300)
        self.server_index = server_index
        self.player_id = player_id
        self.player_name = player_name

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.danger)
    async def confirm_yes(self, interaction: discord.Interaction, button: discord.ui.Button):
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

        try:
            removed = client.remove_admin(self.player_id)
        except CRCONHTTPError:
            removed = False

        if removed:
            message = "Player REMOVED"
        else:
            message = "Player NOT FOUND"

        await update_interaction_message(interaction, content=message, embed=None, view=None)
        asyncio.create_task(_delete_interaction_after(interaction, 10.0))

    @discord.ui.button(label="No", style=discord.ButtonStyle.secondary)
    async def confirm_no(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Cancelled.", embed=None, view=None)
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
            color=0xe67e22,
        )
        view = RemoveAdminCamConfirmView(self.server_index, selected_player_id, selected_name)
        await interaction.response.edit_message(embed=embed, view=view)


class RemoveAdminCamSelectionView(discord.ui.View):
    def __init__(self, server_index: int, spectators: list[dict]):
        super().__init__(timeout=300)
        self.add_item(RemoveAdminCamSelect(server_index, spectators))


class GameModeView(PersistentView):
    def __init__(self):
        super().__init__()

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

        client = _get_http_client(server_index)
        if not client:
            await send_temporary_response(
                interaction,
                content=f"CRCON HTTP unavailable for this server: {_http_error_message(server_index)}",
                delay=20,
            )
            return

        minutes = _get_timer_minutes(env_name, default_minutes, server_index)
        server_name = api_client.get_server_name(server_index)

        try:
            if timer_name == "match timer":
                client.set_match_timer("warfare", minutes)
            else:
                client.set_warmup_timer("warfare", minutes)
        except CRCONHTTPError as exc:
            await send_temporary_response(
                interaction,
                content=f"Failed to set warfare {timer_name} for {server_name}: {exc}",
                delay=20,
            )
            return

        await send_temporary_response(
            interaction,
            content=f"Set warfare {timer_name} to {minutes} minute(s) on {server_name}.",
            delay=20,
        )

    @discord.ui.button(label='🔁 Change Server', style=discord.ButtonStyle.secondary, custom_id='persistent:change_server', row=0)
    async def change_server(self, interaction: discord.Interaction, button: discord.ui.Button):
        servers = api_client.get_servers()
        if not servers:
            await send_temporary_response(interaction, content="No servers configured.", delay=20)
            return

        if len(servers) == 1:
            await send_temporary_response(interaction, content="Only one server configured.", delay=20)
            return

        embed = discord.Embed(
            title="🔁 Change Server",
            description="Select which server the main view should focus on:",
            color=0x7289da,
        )
        view = ChangeServerSelectionView()
        await send_temporary_response(interaction, embed=embed, view=view, delay=None)

    @discord.ui.button(label='🔄 Refresh Status', style=discord.ButtonStyle.success, custom_id='persistent:refresh_status', row=0)
    async def refresh_status(self, interaction: discord.Interaction, button: discord.ui.Button):
        await defer_interaction(interaction, ephemeral=True)
        await refresh_main_embed()
        await send_temporary_response(interaction, content="Status refreshed.", delay=20)

    @discord.ui.button(label='🗺️ Change Map', style=discord.ButtonStyle.primary, custom_id='persistent:open_map_changer', row=1)
    async def open_map_changer(self, interaction: discord.Interaction, button: discord.ui.Button):
        await defer_interaction(interaction, ephemeral=True)
        servers = api_client.get_servers()
        focused_server = _get_channel_focused_server(interaction.channel)

        if focused_server is not None and any(index == focused_server for index, _ in servers):
            server_index = focused_server
            server_name = api_client.get_server_name(server_index)
            current_map = api_client.get_current_map(server_index)

            embed = discord.Embed(
                title="??? Map Change Control",
                description=f"**Server:** {server_name}\n**Current Map:** {current_map}\n\nSelect a game mode:",
                color=0x00ff00
            )
            view = GameModeSelectionView(server_index)
            await send_temporary_response(interaction, embed=embed, view=view, delay=None)
            return

        if len(servers) == 1:
            server_index = servers[0][0]
            server_name = servers[0][1]
            current_map = api_client.get_current_map(server_index)

            embed = discord.Embed(
                title="??? Map Change Control",
                description=f"**Server:** {server_name}\n**Current Map:** {current_map}\n\nSelect a game mode:",
                color=0x00ff00
            )
            view = GameModeSelectionView(server_index)
        else:
            embed = discord.Embed(
                title="??? Map Change Control",
                description="Select a server:",
                color=0x00ff00
            )
            view = ServerSelectionView()

        await send_temporary_response(interaction, embed=embed, view=view, delay=None)

    @discord.ui.button(label='🎯 Set Objectives', style=discord.ButtonStyle.secondary, custom_id='persistent:set_objectives', row=1)
    async def set_objectives(self, interaction: discord.Interaction, button: discord.ui.Button):
        servers = api_client.get_servers()
        if not servers:
            await send_temporary_response(interaction, content="No servers are configured; cannot set objectives.", delay=20)
            return

        if not any(_get_http_client(index) for index, _ in servers):
            await send_temporary_response(
                interaction,
                content="CRCON HTTP credentials are not configured for any server; objective controls are unavailable.",
                delay=20,
            )
            return

        focused_server = _get_channel_focused_server(interaction.channel)
        if focused_server is not None and any(index == focused_server for index, _ in servers):
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
            await send_objective_selection(interaction, focused_server, edit_message=False)
            return

        if len(servers) == 1:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
            await send_objective_selection(interaction, servers[0][0], edit_message=False)
            return

        server_list = "\n".join([f" {name}" for _, name in servers])
        embed = discord.Embed(
            title="🎯 Select Server",
            description=f"Choose which server's objectives you want to configure:\n\n{server_list}",
            color=0x9b59b6,
        )
        view = ObjectiveServerSelectionView()
        await send_temporary_response(interaction, embed=embed, view=view, delay=None)

    @discord.ui.button(label='🌦 Dynamic Weather', style=discord.ButtonStyle.secondary, custom_id='persistent:set_dynamic_weather', row=1)
    async def set_dynamic_weather(self, interaction: discord.Interaction, button: discord.ui.Button):
        servers = api_client.get_servers()
        if not servers:
            await send_temporary_response(interaction, content="No servers are configured; cannot update dynamic weather.", delay=20)
            return

        if not any(_get_http_client(index) for index, _ in servers):
            await send_temporary_response(
                interaction,
                content="CRCON HTTP credentials are not configured for any server; dynamic weather controls are unavailable.",
                delay=20,
            )
            return

        focused_server = _get_channel_focused_server(interaction.channel)
        if focused_server is not None and any(index == focused_server for index, _ in servers):
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
            await send_dynamic_weather_controls(interaction, focused_server, edit_message=False)
            return

        if len(servers) == 1:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
            await send_dynamic_weather_controls(interaction, servers[0][0], edit_message=False)
            return

        server_list = "\n".join([f" {name}" for _, name in servers])
        embed = discord.Embed(
            title="🌦 Select Server",
            description=f"Choose which server's dynamic weather you want to update:\n\n{server_list}",
            color=0x1abc9c,
        )
        view = DynamicWeatherServerSelectionView()
        await send_temporary_response(interaction, embed=embed, view=view, delay=None)

    @discord.ui.button(
        label='⏱ Warfare Match Timer',
        style=discord.ButtonStyle.primary,
        custom_id='persistent:set_warfare_match_timer',
        row=2,
    )
    async def set_warfare_match_timer(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._apply_warfare_timer(
            interaction,
            timer_name="match timer",
            env_name="WARFARE_MATCH_TIMER_MINUTES",
            default_minutes=90,
        )

    @discord.ui.button(
        label='🔥 Warfare Warm Up Timer',
        style=discord.ButtonStyle.primary,
        custom_id='persistent:set_warfare_warmup_timer',
        row=2,
    )
    async def set_warfare_warmup_timer(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._apply_warfare_timer(
            interaction,
            timer_name="warmup timer",
            env_name="WARFARE_WARMUP_TIMER_MINUTES",
            default_minutes=3,
        )

    @discord.ui.button(
        label='🗑️ REMOVE ADMIN CAM USER',
        style=discord.ButtonStyle.secondary,
        custom_id='persistent:remove_admin_cam_access',
        row=3,
    )
    async def remove_admin_cam_access(self, interaction: discord.Interaction, button: discord.ui.Button):
        await defer_interaction(interaction, ephemeral=True)
        servers = api_client.get_servers()
        if not servers:
            await send_temporary_response(interaction, content="No servers are configured; cannot remove admin cam access.", delay=10)
            return

        focused_server = _get_channel_focused_server(interaction.channel)
        if focused_server is not None and any(index == focused_server for index, _ in servers):
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
            admins = client.get_admin_ids()
        except CRCONHTTPError as exc:
            await send_temporary_response(
                interaction,
                content=f"Failed to load admin users: {exc}",
                delay=10,
            )
            return

        spectator_admins = [
            row for row in admins
            if str(row.get("role", "")).lower() == "spectator"
        ]
        if not spectator_admins:
            await send_temporary_response(interaction, content="No users with spectator access found.", delay=10)
            return

        embed = discord.Embed(
            title="Remove Admin Cam User",
            description="Select a user with spectator access to remove.",
            color=0xe67e22,
        )
        view = RemoveAdminCamSelectionView(server_index, spectator_admins)
        await send_temporary_response(interaction, embed=embed, view=view, delay=None)

    @discord.ui.button(
        label='🎥 ADD ADMIN CAM ACCESS',
        style=discord.ButtonStyle.danger,
        custom_id='persistent:add_admin_cam_access',
        row=3,
    )
    async def add_admin_cam_access(self, interaction: discord.Interaction, button: discord.ui.Button):
        servers = api_client.get_servers()
        if not servers:
            await send_temporary_response(interaction, content="No servers are configured; cannot add admin cam access.", delay=10)
            return

        focused_server = _get_channel_focused_server(interaction.channel)
        if focused_server is not None and any(index == focused_server for index, _ in servers):
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
            discord.SelectOption(label=server_name, description=f"Focus main view on {server_name}", value=str(server_index))
            for server_index, server_name in servers[:25]
        ]
        super().__init__(
            placeholder="Choose a server to focus...",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        try:
            await defer_interaction(interaction)
            channel = interaction.channel
            if channel is None:
                await send_temporary_response(interaction, content="Unable to determine channel to update.", delay=20)
                return

            selected = int(self.values[0])
            channel_id = getattr(channel, "id", None)
            if channel_id is None:
                await send_temporary_response(interaction, content="Unable to determine channel to update.", delay=20)
                return

            # Store focus and update persistent message if present
            _persistent_focused_server[channel_id] = selected

            # Update persistent message if we have a reference for this channel
            if persistent_message_ref and persistent_message_ref[0] == channel_id:
                _, message_id = persistent_message_ref
                try:
                    msg = await channel.fetch_message(message_id)  # type: ignore[attr-defined]
                    embed = build_main_embed(focused_server_index=selected)
                    await msg.edit(embed=embed)
                except (discord.NotFound, discord.HTTPException):
                    # recreate persistent message
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
            await send_temporary_response(interaction, content=f"Error updating main view: {exc}", delay=20)

class ServerDropdown(discord.ui.Select):
    def __init__(self, servers):
        options = [
            discord.SelectOption(
                label=server_name,
                description=f"Change map on {server_name}",
                value=str(server_index)
            )
            for server_index, server_name in servers[:25]  # Discord limit
        ]
        
        super().__init__(
            placeholder="Choose a server...",
            min_values=1,
            max_values=1,
            options=options
        )
    
    async def callback(self, interaction: discord.Interaction):
        await defer_interaction(interaction)
        selected_server_index = int(self.values[0])
        server_name = api_client.get_server_name(selected_server_index)
        current_map = api_client.get_current_map(selected_server_index)
        
        embed = discord.Embed(
            title="🗺️ Map Change Control",
            description=f"**Server:** {server_name}\n**Current Map:** {current_map}\n\nSelect a game mode:",
            color=0x00ff00
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
                embed=discord.Embed(title="?? Objective Controls Disabled", description=message, color=0xffa500),
                view=None,
            )
            asyncio.create_task(_delete_interaction_after(interaction, 20.0))
        else:
            await send_temporary_response(interaction, content=message, delay=20)
        return

    try:
        rows = client.get_objective_rows()
    except CRCONHTTPError as exc:
        message = f"Failed to load objectives: {exc}"
        if edit_message:
            await update_interaction_message(
                interaction,
                embed=discord.Embed(title="?? Objective Fetch Failed", description=message, color=0xffa500),
                view=None,
            )
            asyncio.create_task(_delete_interaction_after(interaction, 20.0))
        else:
            await send_temporary_response(interaction, content=message, delay=20)
        return

    server_name = api_client.get_server_name(server_index)
    current_map = api_client.get_current_map(server_index)
    view = ObjectiveSelectionView(server_index, rows, server_name, current_map)
    embed = view.build_embed()

    if edit_message:
        await update_interaction_message(interaction, embed=embed, view=view)
    else:
        await send_temporary_response(interaction, embed=embed, view=view, delay=None)



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
                embed=discord.Embed(title="?? Dynamic Weather Disabled", description=message, color=0xffa500),
                view=None,
            )
            asyncio.create_task(_delete_interaction_after(interaction, 20.0))
        else:
            await send_temporary_response(interaction, content=message, delay=20)
        return

    try:
        gamestate_resp = client.get_gamestate()
        gamestate = gamestate_resp.get("result") if isinstance(gamestate_resp, dict) else None
    except CRCONHTTPError as exc:
        message = f"Failed to load gamestate: {exc}"
        if edit_message:
            await update_interaction_message(
                interaction,
                embed=discord.Embed(title="?? Dynamic Weather Unavailable", description=message, color=0xffa500),
                view=None,
            )
            asyncio.create_task(_delete_interaction_after(interaction, 20.0))
        else:
            await send_temporary_response(interaction, content=message, delay=20)
        return

    current_map = (gamestate or {}).get("current_map") or {}
    map_id = current_map.get("id") or current_map.get("map", {}).get("id")
    map_pretty = current_map.get("pretty_name") or current_map.get("map", {}).get("pretty_name") or map_id or "Unknown"

    if not map_id:
        message = "Could not determine the current map ID from the server."
        if edit_message:
            await update_interaction_message(
                interaction,
                embed=discord.Embed(title="?? Dynamic Weather Unavailable", description=message, color=0xffa500),
                view=None,
            )
            asyncio.create_task(_delete_interaction_after(interaction, 20.0))
        else:
            await send_temporary_response(interaction, content=message, delay=20)
        return

    view = DynamicWeatherToggleView(server_index, map_id, map_pretty)
    embed = view.build_embed()

    if edit_message:
        await update_interaction_message(interaction, embed=embed, view=view)
    else:
        await send_temporary_response(interaction, embed=embed, view=view, delay=None)


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
        await send_dynamic_weather_controls(interaction, server_index, edit_message=True)


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
            color=0x1abc9c,
        )
        embed.set_footer(
            text="The API does not expose the current dynamic weather state; last action applies immediately."
        )
        return embed

    async def _set_dynamic_weather(self, interaction: discord.Interaction, enabled: bool) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

        client = _get_http_client(self.server_index)
        if not client:
            followup = await interaction.followup.send(
                f"CRCON HTTP unavailable for this server: {_http_error_message(self.server_index)}",
                ephemeral=True,
                wait=True,
            )
            if followup is not None:
                asyncio.create_task(_delete_message_after(followup, 20.0))
            return

        try:
            client.set_dynamic_weather_enabled(self.map_id, enabled)
        except CRCONHTTPError as exc:
            followup = await interaction.followup.send(
                f"Failed to update dynamic weather: {exc}",
                ephemeral=True,
                wait=True,
            )
            if followup is not None:
                asyncio.create_task(_delete_message_after(followup, 20.0))
            return

        _dynamic_weather_state[(self.server_index, self.map_id)] = enabled

        state = "enabled" if enabled else "disabled"
        success_embed = discord.Embed(
            title="🌦 Dynamic Weather Updated",
            description=f"Dynamic weather {state} for **{self.map_pretty}**.",
            color=0x1abc9c,
        )

        self.stop()
        await interaction.edit_original_response(embed=success_embed, view=None)
        await refresh_main_embed()
        asyncio.create_task(_delete_interaction_after(interaction, 10.0))

    @discord.ui.button(label="Turn On", style=discord.ButtonStyle.success)
    async def enable(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._set_dynamic_weather(interaction, True)

    @discord.ui.button(label="Turn Off", style=discord.ButtonStyle.danger)
    async def disable(self, interaction: discord.Interaction, button: discord.ui.Button):
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
        await send_objective_selection(interaction, server_index, edit_message=True)


class ObjectiveSelectionView(discord.ui.View):
    def __init__(self, server_index: int, rows: list[list[str]], server_name: str, current_map: str):
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
            color=0x9b59b6,
        )
        embed.add_field(name="Selections", value="\n".join(lines), inline=False)
        return embed

    async def lock_objectives(self, interaction: discord.Interaction) -> None:
        client = _get_http_client(self.server_index)
        if not client:
            await interaction.followup.send(
                f"HTTP API unavailable for this server: {_http_error_message(self.server_index)}",
                ephemeral=True,
            )
            return

        objective_list = [self.selected.get(slot) for slot in range(1, len(self.rows) + 1)]
        if any(choice is None for choice in objective_list):
            await interaction.followup.send(
                "Please choose an objective for every slot before locking.",
                ephemeral=True,
            )
            return

        objective_list = [choice for choice in objective_list if choice]

        try:
            client.set_game_layout(objective_list)
        except CRCONHTTPError as exc:
            await interaction.followup.send(
                f"Failed to lock objectives: {exc}",
                ephemeral=True,
            )
            return

        latest_map = api_client.get_current_map(self.server_index)
        _current_objectives_state[(self.server_index, latest_map)] = list(objective_list)
        summary = "\n".join(
            f"{idx}. **{name}**" for idx, name in enumerate(objective_list, start=1)
        )

        success_embed = discord.Embed(
            title="🔒 Objectives Locked",
            description=(
                f"**Server:** {self.server_name}\n"
                f"**Map:** {latest_map}\n\n"
                f"{summary}"
            ),
            color=0x2ecc71,
        )

        self.stop()
        await interaction.edit_original_response(embed=success_embed, view=None)
        await refresh_main_embed()
        asyncio.create_task(_delete_interaction_after_and_refresh(interaction, 10.0))


class ObjectiveDropdown(discord.ui.Select):
    def __init__(self, slot: int, options: list[str]):
        select_options = [
            discord.SelectOption(label=option, value=option) for option in options
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
        if all(view.selected.get(slot) for slot in range(1, len(view.rows) + 1)):
            if not interaction.response.is_done():
                await interaction.response.defer()
            await view.lock_objectives(interaction)
        else:
            await interaction.response.edit_message(embed=view.build_embed(), view=view)

class GameModeSelectionView(discord.ui.View):
    def __init__(self, server_index):
        super().__init__(timeout=300)
        self.server_index = server_index
    
    @discord.ui.button(label='Warfare', style=discord.ButtonStyle.primary, emoji='⚔️')
    async def warfare_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.show_map_selection(interaction, "warfare")
    
    @discord.ui.button(label='Offensive', style=discord.ButtonStyle.secondary, emoji='🏃')
    async def offensive_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.show_map_selection(interaction, "offensive")
    
    @discord.ui.button(label='Skirmish', style=discord.ButtonStyle.success, emoji='💥')
    async def skirmish_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.show_map_selection(interaction, "skirmish")
    
    async def show_map_selection(self, interaction, game_mode):
        await defer_interaction(interaction)
        server_name = api_client.get_server_name(self.server_index)
        current_map = api_client.get_current_map(self.server_index)
        
        embed = discord.Embed(
            title=f"🗺️ {game_mode.title()} Maps",
            description=f"**Server:** {server_name}\n**Current Map:** {current_map}\n\nSelect a map:",
            color=0x0099ff
        )
        
        view = MapSelectionView(self.server_index, game_mode)
        await update_interaction_message(interaction, embed=embed, view=view)

class MapSelectionView(discord.ui.View):
    def __init__(self, server_index, game_mode):
        super().__init__(timeout=300)
        self.server_index = server_index
        self.game_mode = game_mode
        
        # Create dropdown with maps
        maps = get_maps_for_mode(game_mode)
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
                value=map_name
            )
            for map_name in maps[:25]  # Discord limit
        ]
        
        super().__init__(
            placeholder="Choose a map...",
            min_values=1,
            max_values=1,
            options=options
        )
    
    async def callback(self, interaction: discord.Interaction):
        await defer_interaction(interaction)
        selected_map = self.values[0]
        server_name = api_client.get_server_name(self.server_index)
        current_map = api_client.get_current_map(self.server_index)
        
        embed = discord.Embed(
            title=f"🗺️ {selected_map} - {self.game_mode.title()}",
            description=f"**Server:** {server_name}\n**Current Map:** {current_map}\n\nSelect a variant:",
            color=0xff9900
        )
        
        view = VariantSelectionView(self.server_index, self.game_mode, selected_map)
        await update_interaction_message(interaction, embed=embed, view=view)

class VariantSelectionView(discord.ui.View):
    def __init__(self, server_index, game_mode, map_name):
        super().__init__(timeout=300)
        self.server_index = server_index
        self.game_mode = game_mode
        self.map_name = map_name
        
        # Create dropdown with variants
        variants = get_variants_for_map(game_mode, map_name)
        if variants:
            self.add_item(VariantDropdown(server_index, game_mode, map_name, variants))
        
        # Add back button
        self.add_item(BackToMapSelectionButton(server_index, game_mode))

class VariantDropdown(discord.ui.Select):
    def __init__(self, server_index, game_mode, map_name, variants):
        self.server_index = server_index
        self.game_mode = game_mode
        self.map_name = map_name
        
        options = [
            discord.SelectOption(
                label=variant["variant"],
                description=f"{map_name} - {variant['variant']}",
                value=variant["id"]
            )
            for variant in variants[:25]  # Discord limit
        ]
        
        super().__init__(
            placeholder="Choose a variant...",
            min_values=1,
            max_values=1,
            options=options
        )
    
    async def callback(self, interaction: discord.Interaction):
        selected_variant_id = self.values[0]
        server_name = api_client.get_server_name(self.server_index)
        
        # Find the variant name for display
        variants = get_variants_for_map(self.game_mode, self.map_name)
        variant_entry = next((v for v in variants if v["id"] == selected_variant_id), None)
        variant_name = variant_entry["variant"] if variant_entry else selected_variant_id
        
        embed = discord.Embed(
            title="🔄 Changing Map...",
            description=f"**Server:** {server_name}\n\nAttempting to change to **{self.map_name}** ({variant_name})",
            color=0xffff00
        )
        
        await interaction.response.edit_message(embed=embed, view=None)
        


        status_lines = []
        overall_success = False

        client = _get_http_client(self.server_index)
        if client:
            try:
                client.set_map(selected_variant_id)
                status_lines.append("? HTTP API change_map succeeded.")
                overall_success = True
            except CRCONHTTPError as exc:
                status_lines.append(f"?? HTTP API change_map failed: {exc}")
        else:
            status_lines.append(f"?? HTTP API unavailable: {_http_error_message(self.server_index)}")

        status_summary = "\n".join(f"• {line}" for line in status_lines)
        
        if overall_success:
            new_current_map = api_client.get_current_map(self.server_index)
            stale_objective_keys = [
                key for key in _current_objectives_state if key[0] == self.server_index
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
                color=0x00ff00
            )
            await interaction.edit_original_response(embed=final_embed, view=None)
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
                color=0xff0000
            )
            await interaction.edit_original_response(embed=final_embed, view=None)
            asyncio.create_task(_delete_interaction_after(interaction, 20.0))

class BackToServerSelectionButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="← Back to Servers", style=discord.ButtonStyle.gray)
    
    async def callback(self, interaction: discord.Interaction):
        await defer_interaction(interaction)
        embed = discord.Embed(
            title="🗺️ Map Change Control",
            description="Select a server:",
            color=0x00ff00
        )
        
        view = ServerSelectionView()
        await update_interaction_message(interaction, embed=embed, view=view)

class BackToGameModeButton(discord.ui.Button):
    def __init__(self, server_index):
        super().__init__(label="← Back to Game Modes", style=discord.ButtonStyle.gray)
        self.server_index = server_index
    
    async def callback(self, interaction: discord.Interaction):
        await defer_interaction(interaction)
        server_name = api_client.get_server_name(self.server_index)
        current_map = api_client.get_current_map(self.server_index)
        
        embed = discord.Embed(
            title="🗺️ Map Change Control",
            description=f"**Server:** {server_name}\n**Current Map:** {current_map}\n\nSelect a game mode:",
            color=0x00ff00
        )
        
        view = GameModeSelectionView(self.server_index)
        await update_interaction_message(interaction, embed=embed, view=view)

class BackToMapSelectionButton(discord.ui.Button):
    def __init__(self, server_index, game_mode):
        super().__init__(label="← Back to Maps", style=discord.ButtonStyle.gray)
        self.server_index = server_index
        self.game_mode = game_mode
    
    async def callback(self, interaction: discord.Interaction):
        await defer_interaction(interaction)
        server_name = api_client.get_server_name(self.server_index)
        current_map = api_client.get_current_map(self.server_index)
        
        embed = discord.Embed(
            title=f"🗺️ {self.game_mode.title()} Maps",
            description=f"**Server:** {server_name}\n**Current Map:** {current_map}\n\nSelect a map:",
            color=0x0099ff
        )
        
        view = MapSelectionView(self.server_index, self.game_mode)
        await update_interaction_message(interaction, embed=embed, view=view)

@bot.event
async def on_ready():
    print(f'{bot.user} has connected to Discord!')
    print(f'Bot is in {len(bot.guilds)} guilds')

    # Warm cache when available, but avoid noisy startup warnings if CRCON isn't reachable yet.
    refresh_map_cache(force=False)
    
    # Add the persistent view
    bot.add_view(GameModeView())
    
    # Post the persistent button to the specified channel
    channel_id_str = os.getenv('DISCORD_CHANNEL_ID')
    if not channel_id_str:
        print("DISCORD_CHANNEL_ID not configured; cannot post persistent controls.")
        return

    try:
        channel_id = int(channel_id_str)
    except ValueError:
        print(f"Invalid DISCORD_CHANNEL_ID value: {channel_id_str}")
        return

    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except (discord.NotFound, discord.HTTPException):
            channel = None

    if isinstance(channel, (discord.TextChannel, discord.Thread)):
        message = await ensure_persistent_message(channel)
        if message:
            print(f"Persistent controls ready in #{channel.name} (message ID {message.id})")
    else:
        print(f"Could not find text channel with ID: {channel_id}")

# Admin command to repost the button if needed
@bot.tree.command(name="repost_button", description="Repost the map changer button (Admin only)")
async def repost_button(interaction: discord.Interaction):
    # Check if user has admin permissions
    if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.administrator:
        await send_temporary_response(interaction, content="❌ You need administrator permissions to use this command.", delay=20)
        return

    channel_id_str = os.getenv('DISCORD_CHANNEL_ID')
    if not channel_id_str:
        await send_temporary_response(interaction, content="❌ DISCORD_CHANNEL_ID is not configured.", delay=20)
        return

    try:
        channel_id = int(channel_id_str)
    except ValueError:
        await send_temporary_response(interaction, content="❌ DISCORD_CHANNEL_ID is invalid.", delay=20)
        return

    channel = bot.get_channel(channel_id)

    if isinstance(channel, (discord.TextChannel, discord.Thread)):
        embed = build_main_embed()
        view = GameModeView()
        message = await channel.send(embed=embed, view=view)
        global persistent_message_ref
        persistent_message_ref = (channel.id, message.id)
        await send_temporary_response(interaction, content="✅ Map changer button reposted!", delay=20)
    else:
        await send_temporary_response(interaction, content="❌ Could not find the configured channel.", delay=20)

if __name__ == '__main__':
    token = os.getenv('DISCORD_TOKEN')
    if not token:
        print("Error: DISCORD_TOKEN not found in environment variables")
        exit(1)
    
    bot.run(token)
