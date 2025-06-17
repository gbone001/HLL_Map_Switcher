from discord import ButtonStyle, Interaction
from discord.ui import Button, View

# Sample map data structure
MAPS = {
    "warfare": {
        "maps": ["Map1", "Map2"],
        "variants": ["Day", "Night"]
    },
    "skirmish": {
        "maps": ["Map3", "Map4"],
        "variants": ["Day", "Night"]
    },
    "offensive": {
        "maps": ["Map5", "Map6"],
        "variants": ["Day", "Night"]
    }
}

class MapChangeView(View):
    def __init__(self):
        super().__init__()

    @discord.ui.button(label="Change Map", style=ButtonStyle.primary)
    async def change_map(self, button: Button, interaction: Interaction):
        # Logic to change map
        await interaction.response.send_message("Select mode: Warfare, Skirmish, or Offensive.")

    @discord.ui.button(label="Warfare", style=ButtonStyle.secondary)
    async def select_warfare(self, button: Button, interaction: Interaction):
        await self.select_map(interaction, "warfare")

    @discord.ui.button(label="Skirmish", style=ButtonStyle.secondary)
    async def select_skirmish(self, button: Button, interaction: Interaction):
        await self.select_map(interaction, "skirmish")

    @discord.ui.button(label="Offensive", style=ButtonStyle.secondary)
    async def select_offensive(self, button: Button, interaction: Interaction):
        await self.select_map(interaction, "offensive")

    async def select_map(self, interaction: Interaction, mode: str):
        maps = MAPS[mode]["maps"]
        await interaction.response.send_message(f"Select a map: {', '.join(maps)}")

    async def select_variant(self, interaction: Interaction, mode: str, map_name: str):
        variants = MAPS[mode]["variants"]
        await interaction.response.send_message(f"Select a variant for {map_name}: {', '.join(variants)}")