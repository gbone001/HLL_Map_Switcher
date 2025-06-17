from discord import ButtonStyle, Interaction, SelectOption, ui
from discord.ext import commands

class MapSelectView(ui.View):
    def __init__(self):
        super().__init__()

    @ui.button(label="Change Map", style=ButtonStyle.primary)
    async def change_map(self, button: ui.Button, interaction: Interaction):
        await interaction.response.send_message("Select a mode:", view=self.mode_select())

    def mode_select(self):
        options = [
            SelectOption(label="Warfare", value="warfare"),
            SelectOption(label="Skirmish", value="skirmish"),
            SelectOption(label="Offensive", value="offensive"),
        ]
        return MapModeSelect(options)

class MapModeSelect(ui.Select):
    def __init__(self, options):
        super().__init__(placeholder="Choose a mode...", options=options)

    async def callback(self, interaction: Interaction):
        selected_mode = self.values[0]
        await interaction.response.send_message(f"You selected: {selected_mode}. Now select a map:", view=self.map_select(selected_mode))

    def map_select(self, mode):
        # Here you would fetch the maps based on the selected mode
        maps = self.get_maps(mode)
        options = [SelectOption(label=map_name, value=map_name) for map_name in maps]
        return MapSelect(options)

    def get_maps(self, mode):
        # Placeholder for actual map fetching logic
        return ["Map1", "Map2", "Map3"]

class MapSelect(ui.Select):
    def __init__(self, options):
        super().__init__(placeholder="Choose a map...", options=options)

    async def callback(self, interaction: Interaction):
        selected_map = self.values[0]
        await interaction.response.send_message(f"You selected the map: {selected_map}. Now select a variant:", view=self.variant_select(selected_map))

    def variant_select(self, map_name):
        # Here you would fetch the variants based on the selected map
        variants = self.get_variants(map_name)
        options = [SelectOption(label=variant, value=variant) for variant in variants]
        return VariantSelect(options)

    def get_variants(self, map_name):
        # Placeholder for actual variant fetching logic
        return ["Day", "Night"]

class VariantSelect(ui.Select):
    def __init__(self, options):
        super().__init__(placeholder="Choose a variant...", options=options)

    async def callback(self, interaction: Interaction):
        selected_variant = self.values[0]
        await interaction.response.send_message(f"You selected the variant: {selected_variant}.")

async def setup(bot: commands.Bot):
    bot.add_view(MapSelectView())