---
planning: true
shaping: true
---

# Multi-Guild Booking — Slices (shape A, revised 2026-10-27)

## Source and authority
- Drawn from [multi-guild-shaping.md](multi-guild-shaping.md) and its **Revision 2026-10-27** (decisions R1–R5, repo owner), plus the accepted breadboard [multi-guild-breadboard.md](multi-guild-breadboard.md) where it still applies.
- Builds on production `feat/server-booking`. That branch has:
  - the strict lock (`BookingService.control_check`)
  - the panel saved to the volume, and `/setup_panel`
  - two game servers (`SERVER1_*`, `SERVER2_*`)
- **Status: accepted** 2026-10-27. Sequence: 1 → 2 → 3 → 4 → 5. Each slice is deployable on its own.
- **Progress:** slice 1 deployed 2026-10-27 (`e5f61a6`); slice 2 deployed 2026-10-27 (`30c574b`); slice 3 deployed 2026-10-27 (`13fae7e`, admin cam follow-up `a654038`); slice 4 built 2026-10-27.

## Slices

### 1 — Guild list and commands
- `services/guild_registry.py`: the home guild comes from `DISCORD_GUILD_ID`; approved guests come from `BOOKING_GUILD_IDS` (comma or space separated).
- Slash commands are copied to every approved guild at startup, so they appear immediately.
- The bot leaves any guild that isn't approved:
  - when it joins one (`on_guild_join`)
  - in a sweep at startup
  - never when no home guild is configured, so unconfigured setups keep working
- `/setup_panel` and `/repost_button` stay home-guild only until slice 4. One panel exists today, and running them elsewhere would move it.
- **Demo:**
  1. Put guild B in `BOOKING_GUILD_IDS`; the commands appear there after a restart.
  2. Invite the bot to unlisted guild C; it leaves and logs why.
  3. `/setup_panel` in B → "home Discord only for now".

### 2 — Bookings know their guild
- The `bookings` table gains `guild_id`; existing rows get the home guild.
- Overlap checks stay per game server across all guilds.
- Anyone in an approved guild can book (`can_book` uses the registry, not one guild).
- Cancelling is allowed for:
  - the booker
  - an Administrator of the booking's guild
  - a home-guild booking admin
- A clash with another guild names the guild, not the member. `/bookings` lists guild names and mentions members only in the caller's own guild.
- **Demo:**
  1. B books US West 20:00–21:00.
  2. Home tries 20:30 → "Overlaps a booking by **B**".
  3. B's admin cancels B's booking, and is refused on home's booking.

### 3 — Lock by guild
- `control_check(server_key, guild_id, user, admin scope)` is decided in this order:
  1. home booking admin → allow
  2. no live booking → deny
  3. live booking → allow only the booker or an Administrator of the booking's guild
- Admin cam follows the same rule (R5, re-decided 2026-10-27).
- It stays at all 8 write sites; the AST coverage test still applies.
- A decision-table test covers every row of the rule above.
- **Demo:**
  1. During B's booking: B's booker changes the map ✅; a home member is denied with "Booked by **B**"; a home booking admin overrides ✅.
  2. B's booker adds admin cam during the slot ✅.

### 4 — One panel per guild
- `data/panel_message.json` becomes `guild_id → (channel_id, message_id)`. The existing single entry migrates to the home guild.
- `/setup_panel` and `/repost_button` act on the caller's guild only.
- Refreshes (booking changes, booking start/end, Refresh button) update every guild's panel, fetching CRCON status once.
- Guest panels:
  - show "Booked by **Guild**" for other guilds' bookings
  - show "@user" inside the booking guild
- **Demo:** panels in home and B; B books; both panels update, each with its own wording.

### 5 — Rollout
- Update the first bet's booking tests to the new rules. Update AGENTS.md and the docs.
- On Railway: set `BOOKING_GUILD_IDS`, deploy, and run `/setup_panel` in each guild.
- In the Discord developer portal: turn **Public Bot** off, so only the owner can invite the bot. Auto-leave is the safety net.

## Accepted limits
- Removing a guild strands its future bookings: they are hidden and ignored, with no transfer.
- One panel per guild.
- Admin cam access a guest adds outlives their booking until someone removes it (no auto-revoke).
- Per-guild command sync suits a handful of guilds; past about 10, switch to global sync.
- No per-guild fair-use limits (M9, below the cut).

## Rollout caution
Slice 1's sweep **leaves every unlisted guild at startup**. Before deploying, put every guild the bot should stay in into `BOOKING_GUILD_IDS`.
