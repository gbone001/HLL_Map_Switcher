import discord
from discord.ext import commands
import os
from dotenv import load_dotenv
from utils.map_data import (
    get_maps_for_mode,
    get_variants_for_map,
    get_map_id,
    refresh_map_cache,
    get_last_map_cache_error,
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
try:
    http_client = CRCONHttpClient.from_env()
except CRCONHTTPError as exc:
    http_client = None
    print(f"CRCON HTTP client disabled: {exc}")

class PersistentView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)  # Persistent view

class GameModeView(PersistentView):
    def __init__(self):
        super().__init__()
    
    @discord.ui.button(label='🗺️ Change Map', style=discord.ButtonStyle.primary, custom_id='persistent:open_map_changer')
    async def open_map_changer(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Check if there are multiple servers
        servers = api_client.get_servers()
        
        if len(servers) == 1:
            # Single server, go directly to game mode selection
            server_index = servers[0][0]
            server_name = servers[0][1]
            current_map = api_client.get_current_map(server_index)
            
            embed = discord.Embed(
                title="🗺️ Map Change Control",
                description=f"**Server:** {server_name}\n**Current Map:** {current_map}\n\nSelect a game mode:",
                color=0x00ff00
            )
            view = GameModeSelectionView(server_index)
        else:
            # Multiple servers, show server selection first
            embed = discord.Embed(
                title="🗺️ Map Change Control",
                description="Select a server:",
                color=0x00ff00
            )
            view = ServerSelectionView()
        
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(label='🎯 Set Objectives', style=discord.ButtonStyle.secondary, custom_id='persistent:set_objectives')
    async def set_objectives(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not http_client:
            await interaction.response.send_message(
                "HTTP API credentials are not configured; objective controls are unavailable.",
                ephemeral=True,
            )
            return

        servers = api_client.get_servers()
        if not servers:
            await interaction.response.send_message(
                "No servers are configured; cannot set objectives.",
                ephemeral=True,
            )
            return

        if len(servers) == 1:
            await send_objective_selection(interaction, servers[0][0], edit_message=False)
            return

        server_list = "\n".join([f"• {name}" for _, name in servers])
        embed = discord.Embed(
            title="🎯 Select Server",
            description=f"Choose which server's objectives you want to configure:\n\n{server_list}",
            color=0x9b59b6,
        )
        view = ObjectiveServerSelectionView()
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

class ServerSelectionView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        
        # Create dropdown with servers
        servers = api_client.get_servers()
        if servers:
            self.add_item(ServerDropdown(servers))

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
        selected_server_index = int(self.values[0])
        server_name = api_client.get_server_name(selected_server_index)
        current_map = api_client.get_current_map(selected_server_index)
        
        embed = discord.Embed(
            title="🗺️ Map Change Control",
            description=f"**Server:** {server_name}\n**Current Map:** {current_map}\n\nSelect a game mode:",
            color=0x00ff00
        )
        
        view = GameModeSelectionView(selected_server_index)
        await interaction.response.edit_message(embed=embed, view=view)


async def send_objective_selection(
    interaction: discord.Interaction,
    server_index: int,
    *,
    edit_message: bool,
) -> None:
    if not http_client:
        message = "HTTP API credentials are not configured; objective controls are unavailable."
        if edit_message:
            await interaction.response.edit_message(
                embed=discord.Embed(title="⚠️ Objective Controls Disabled", description=message, color=0xffa500),
                view=None,
            )
        else:
            await interaction.response.send_message(message, ephemeral=True)
        return

    try:
        rows = http_client.get_objective_rows()
    except CRCONHTTPError as exc:
        message = f"Failed to load objectives: {exc}"
        if edit_message:
            await interaction.response.edit_message(
                embed=discord.Embed(title="⚠️ Objective Fetch Failed", description=message, color=0xffa500),
                view=None,
            )
        else:
            await interaction.response.send_message(message, ephemeral=True)
        return

    server_name = api_client.get_server_name(server_index)
    current_map = api_client.get_current_map(server_index)
    view = ObjectiveSelectionView(server_index, rows, server_name, current_map)
    embed = view.build_embed()

    if edit_message:
        await interaction.response.edit_message(embed=embed, view=view)
    else:
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


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

        self.add_item(LockObjectivesButton())

    def build_embed(self) -> discord.Embed:
        current_map = api_client.get_current_map(self.server_index)
        description = (
            f"**Server:** {self.server_name}\n"
            f"**Current Map:** {current_map}\n\n"
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
        await interaction.response.edit_message(embed=view.build_embed(), view=view)


class LockObjectivesButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="🔒 Lock Objectives for Match", style=discord.ButtonStyle.success)

    async def callback(self, interaction: discord.Interaction):
        view: ObjectiveSelectionView = self.view  # type: ignore[assignment]
        if not http_client:
            await interaction.response.send_message(
                "HTTP API unavailable; cannot lock objectives.",
                ephemeral=True,
            )
            return

        selections = [view.selected.get(slot) for slot in range(1, len(view.rows) + 1)]
        if any(choice is None for choice in selections):
            await interaction.response.send_message(
                "Please choose an objective for every slot before locking.",
                ephemeral=True,
            )
            return

        objective_list = [choice for choice in selections if choice]

        try:
            http_client.set_game_layout(objective_list)
        except CRCONHTTPError as exc:
            await interaction.response.send_message(
                f"Failed to lock objectives: {exc}",
                ephemeral=True,
            )
            return

        latest_map = api_client.get_current_map(view.server_index)
        summary = "\n".join(
            f"{idx}. **{name}**" for idx, name in enumerate(objective_list, start=1)
        )

        success_embed = discord.Embed(
            title="🔒 Objectives Locked",
            description=(
                f"**Server:** {view.server_name}\n"
                f"**Map:** {latest_map}\n\n"
                f"{summary}"
            ),
            color=0x2ecc71,
        )

        view.stop()
        await interaction.response.edit_message(embed=success_embed, view=None)

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
        server_name = api_client.get_server_name(self.server_index)
        current_map = api_client.get_current_map(self.server_index)
        
        embed = discord.Embed(
            title=f"🗺️ {game_mode.title()} Maps",
            description=f"**Server:** {server_name}\n**Current Map:** {current_map}\n\nSelect a map:",
            color=0x0099ff
        )
        
        view = MapSelectionView(self.server_index, game_mode)
        await interaction.response.edit_message(embed=embed, view=view)

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
        selected_map = self.values[0]
        server_name = api_client.get_server_name(self.server_index)
        current_map = api_client.get_current_map(self.server_index)
        
        embed = discord.Embed(
            title=f"🗺️ {selected_map} - {self.game_mode.title()}",
            description=f"**Server:** {server_name}\n**Current Map:** {current_map}\n\nSelect a variant:",
            color=0xff9900
        )
        
        view = VariantSelectionView(self.server_index, self.game_mode, selected_map)
        await interaction.response.edit_message(embed=embed, view=view)

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
        http_attempted = False

        if http_client:
            http_attempted = True
            try:
                http_client.set_map(selected_variant_id)
                status_lines.append("✅ HTTP API change_map succeeded.")
                overall_success = True
            except CRCONHTTPError as exc:
                status_lines.append(f"⚠️ HTTP API change_map failed: {exc}")
        else:
            status_lines.append("ℹ️ HTTP API not configured; skipping.")

        if not overall_success:
            rcon_success, rcon_message = api_client.set_map(self.server_index, selected_variant_id)
            if rcon_success:
                status_lines.append("✅ RCON fallback succeeded.")
                overall_success = True
            else:
                status_lines.append(f"❌ RCON fallback failed: {rcon_message}")
        elif http_attempted:
            status_lines.append("ℹ️ RCON fallback not required.")

        status_summary = "\n".join(f"• {line}" for line in status_lines)
        
        if overall_success:
            new_current_map = api_client.get_current_map(self.server_index)
            
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
        
        await interaction.edit_original_response(embed=final_embed)

class BackToServerSelectionButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="← Back to Servers", style=discord.ButtonStyle.gray)
    
    async def callback(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="🗺️ Map Change Control",
            description="Select a server:",
            color=0x00ff00
        )
        
        view = ServerSelectionView()
        await interaction.response.edit_message(embed=embed, view=view)

class BackToGameModeButton(discord.ui.Button):
    def __init__(self, server_index):
        super().__init__(label="← Back to Game Modes", style=discord.ButtonStyle.gray)
        self.server_index = server_index
    
    async def callback(self, interaction: discord.Interaction):
        server_name = api_client.get_server_name(self.server_index)
        current_map = api_client.get_current_map(self.server_index)
        
        embed = discord.Embed(
            title="🗺️ Map Change Control",
            description=f"**Server:** {server_name}\n**Current Map:** {current_map}\n\nSelect a game mode:",
            color=0x00ff00
        )
        
        view = GameModeSelectionView(self.server_index)
        await interaction.response.edit_message(embed=embed, view=view)

class BackToMapSelectionButton(discord.ui.Button):
    def __init__(self, server_index, game_mode):
        super().__init__(label="← Back to Maps", style=discord.ButtonStyle.gray)
        self.server_index = server_index
        self.game_mode = game_mode
    
    async def callback(self, interaction: discord.Interaction):
        server_name = api_client.get_server_name(self.server_index)
        current_map = api_client.get_current_map(self.server_index)
        
        embed = discord.Embed(
            title=f"🗺️ {self.game_mode.title()} Maps",
            description=f"**Server:** {server_name}\n**Current Map:** {current_map}\n\nSelect a map:",
            color=0x0099ff
        )
        
        view = MapSelectionView(self.server_index, self.game_mode)
        await interaction.response.edit_message(embed=embed, view=view)

@bot.event
async def on_ready():
    print(f'{bot.user} has connected to Discord!')
    print(f'Bot is in {len(bot.guilds)} guilds')

    refresh_map_cache(force=True)
    cache_error = get_last_map_cache_error()
    if cache_error:
        print(f"Warning: failed to refresh map cache via CRCON API ({cache_error})")
    
    # Add the persistent view
    bot.add_view(GameModeView())
    
    # Post the persistent button to the specified channel
    channel_id = int(os.getenv('DISCORD_CHANNEL_ID'))
    channel = bot.get_channel(channel_id)
    
    if channel:
        # Check if there's already a message with our button
        async for message in channel.history(limit=50):
            if message.author == bot.user and message.embeds:
                if "Hell Let Loose Map Changer" in message.embeds[0].title:
                    print("Map changer button already exists in channel")
                    return
        
        # Post the persistent button
        servers = api_client.get_servers()
        server_list = "\n".join([f"• {name}" for _, name in servers])
        
        embed = discord.Embed(
            title="🎮 Hell Let Loose Map Changer",
            description=f"Click the button below to change the server map.\n\n**Available Servers:**\n{server_list}\n\n**Available Game Modes:**\n⚔️ Warfare\n🏃 Offensive\n💥 Skirmish",
            color=0x2f3136
        )
        embed.set_footer(text="This button never expires and will work even after bot restarts")
        
        view = GameModeView()
        await channel.send(embed=embed, view=view)
        print(f"Posted persistent map changer button to channel: {channel.name}")
    else:
        print(f"Could not find channel with ID: {channel_id}")

# Admin command to repost the button if needed
@bot.tree.command(name="repost_button", description="Repost the map changer button (Admin only)")
async def repost_button(interaction: discord.Interaction):
    # Check if user has admin permissions
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ You need administrator permissions to use this command.", ephemeral=True)
        return
    
    channel_id = int(os.getenv('DISCORD_CHANNEL_ID'))
    channel = bot.get_channel(channel_id)
    
    if channel:
        servers = api_client.get_servers()
        server_list = "\n".join([f"• {name}" for _, name in servers])
        
        embed = discord.Embed(
            title="🎮 Hell Let Loose Map Changer",
            description=f"Click the button below to change the server map.\n\n**Available Servers:**\n{server_list}\n\n**Available Game Modes:**\n⚔️ Warfare\n🏃 Offensive\n💥 Skirmish",
            color=0x2f3136
        )
        embed.set_footer(text="This button never expires and will work even after bot restarts")
        
        view = GameModeView()
        await channel.send(embed=embed, view=view)
        await interaction.response.send_message("✅ Map changer button reposted!", ephemeral=True)
    else:
        await interaction.response.send_message("❌ Could not find the configured channel.", ephemeral=True)

if __name__ == '__main__':
    token = os.getenv('DISCORD_TOKEN')
    if not token:
        print("Error: DISCORD_TOKEN not found in environment variables")
        exit(1)
    
    bot.run(token)
