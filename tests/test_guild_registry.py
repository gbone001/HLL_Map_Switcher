import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from services.guild_registry import GuildRegistry


class GuildRegistryTests(unittest.TestCase):
    def test_home_and_guest_guilds_from_env(self):
        registry = GuildRegistry.from_env(
            {"DISCORD_GUILD_ID": "100", "BOOKING_GUILD_IDS": "300, 200 300\n100"}
        )

        self.assertEqual(registry.approved_ids(), [100, 200, 300])
        self.assertTrue(registry.is_home(100))
        self.assertFalse(registry.is_home(200))
        self.assertTrue(registry.is_approved(200))
        self.assertFalse(registry.is_approved(999))
        self.assertFalse(registry.is_approved(None))

    def test_invalid_entries_are_reported_not_approved(self):
        registry = GuildRegistry.from_env({"DISCORD_GUILD_ID": "100", "BOOKING_GUILD_IDS": "abc,-5,200"})

        self.assertEqual(registry.approved_ids(), [100, 200])
        self.assertEqual(registry.invalid_entries, ("abc", "-5"))

    def test_without_home_guild_everything_is_allowed_and_nothing_listed(self):
        registry = GuildRegistry.from_env({"BOOKING_GUILD_IDS": "200"})

        self.assertFalse(registry.configured)
        self.assertEqual(registry.approved_ids(), [])
        self.assertTrue(registry.is_approved(12345))

    def test_single_guild_setup_needs_no_new_variables(self):
        registry = GuildRegistry.from_env({"DISCORD_GUILD_ID": "100"})

        self.assertEqual(registry.approved_ids(), [100])
        self.assertFalse(registry.is_approved(200))


if __name__ == "__main__":
    unittest.main()
