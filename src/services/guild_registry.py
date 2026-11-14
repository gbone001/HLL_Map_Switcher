"""Which Discord guilds may use the bot (docs/multi-guild-slices.md, slice 1).

The home guild comes from DISCORD_GUILD_ID. Approved guest guilds come from BOOKING_GUILD_IDS
(comma or space separated). No Discord dependency, so it can be tested on its own.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class GuildRegistry:
    home_guild_id: int | None
    guest_guild_ids: frozenset[int] = field(default_factory=frozenset)
    invalid_entries: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, environ: dict | None = None) -> GuildRegistry:
        env = os.environ if environ is None else environ
        home = _parse_id((env.get("DISCORD_GUILD_ID") or "").strip())
        guests: set[int] = set()
        invalid: list[str] = []
        for token in re.split(r"[\s,]+", env.get("BOOKING_GUILD_IDS") or ""):
            if not token:
                continue
            guild_id = _parse_id(token)
            if guild_id is None:
                invalid.append(token)
            elif guild_id != home:
                guests.add(guild_id)
        return cls(home, frozenset(guests), tuple(invalid))

    @property
    def configured(self) -> bool:
        """False without a home guild: the bot then serves any guild, as before multi-guild."""
        return self.home_guild_id is not None

    def approved_ids(self) -> list[int]:
        """Home guild first, then guests in a stable order."""
        if self.home_guild_id is None:
            return []
        return [self.home_guild_id, *sorted(self.guest_guild_ids)]

    def is_approved(self, guild_id: int | None) -> bool:
        if not self.configured:
            return True
        return guild_id is not None and (
            guild_id == self.home_guild_id or guild_id in self.guest_guild_ids
        )

    def is_home(self, guild_id: int | None) -> bool:
        return guild_id is not None and guild_id == self.home_guild_id


def _parse_id(text: str) -> int | None:
    try:
        value = int(text)
    except ValueError:
        return None
    return value if value > 0 else None
