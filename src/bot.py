import discord
from discord.ext import commands
import os
from dotenv import load_dotenv
from utils.map_data import get_maps_for_mode, get_variants_for_map, get_map_id
from utils.api_client import HLLAPIClient

# Load environment variables
load_dotenv()

# Bot configuration
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# API client
api_client = HLLAPIClient()

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
        variant_name = next(v["variant"] for v in variants if v["id"] == selected_variant_id)
        
        embed = discord.Embed(
            title="🔄 Changing Map...",
            description=f"**Server:** {server_name}\n\nAttempting to change to **{self.map_name}** ({variant_name})",
            color=0xffff00
        )
        
        await interaction.response.edit_message(embed=embed, view=None)
        
        # Call the API to change the map
        success, message = api_client.set_map(self.server_index, selected_variant_id)
        
        if success:
            # Get the new current map after change
            new_current_map = api_client.get_current_map(self.server_index)
            
            final_embed = discord.Embed(
                title="✅ Map Changed Successfully!",
                description=f"**Server:** {server_name}\n**New Map:** {new_current_map}\n\nMap changed to **{self.map_name}** ({variant_name})",
                color=0x00ff00
            )
        else:
            final_embed = discord.Embed(
                title="❌ Map Change Failed",
                description=f"**Server:** {server_name}\n\nFailed to change map: {message}",
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