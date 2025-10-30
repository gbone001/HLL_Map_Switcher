from discord import ButtonStyle, Interaction, SelectOption, ui
from discord.ext import commands

from utils.map_data import get_maps_for_mode, get_variants_for_map

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
        maps = get_maps_for_mode(mode)
        if not maps:
            maps = ["No maps available"]
        options = [
            SelectOption(label=map_name, value=map_name)
            for map_name in maps[:25]
        ]
        return MapSelect(mode, options)

class MapSelect(ui.Select):
    def __init__(self, game_mode, options):
        self.game_mode = game_mode
        super().__init__(placeholder="Choose a map...", options=options)

    async def callback(self, interaction: Interaction):
        selected_map = self.values[0]
        await interaction.response.send_message(
            f"You selected the map: {selected_map}. Now select a variant:",
            view=self.variant_select(selected_map),
        )

    def variant_select(self, map_name):
        variants = get_variants_for_map(self.game_mode, map_name)
        if not variants:
            variants = [{"id": "unknown", "variant": "Unavailable"}]
        options = [
            SelectOption(
                label=variant["variant"],
                value=variant["id"],
                description=variant["id"],
            )
            for variant in variants[:25]
        ]
        return VariantSelect(self.game_mode, map_name, variants, options)

class VariantSelect(ui.Select):
    def __init__(self, game_mode, map_name, variants, options):
        self.game_mode = game_mode
        self.map_name = map_name
        self.variants = variants
        super().__init__(placeholder="Choose a variant...", options=options)

    async def callback(self, interaction: Interaction):
        selected_id = self.values[0]
        label = next(
            (variant["variant"] for variant in self.variants if variant["id"] == selected_id),
            selected_id,
        )
        await interaction.response.send_message(
            f"You selected the variant: {label} (ID: {selected_id})."
        )

async def setup(bot: commands.Bot):
    bot.add_view(MapSelectView())
