# Multi-Guild Booking — Shaping

Extends the server booking bet ([booking-shaping.md](booking-shaping.md), branch `feat/server-booking`).

**Terms used here:** *guild* = a Discord server; *game server* = an HLL server; *owner guild* = the Discord that runs the bot today; *guest guild* = any other approved Discord.

## Working mode
- Profile: collaborative
- Current move: selected-design breadboard accepted ([multi-guild-breadboard.md](multi-guild-breadboard.md)) → next is slicing
- Status: **selected** (shape A), 2026-10-16. A6 revised 2026-10-16 to resolve breadboard issue G1

## Frame
- **x — current situation:** the bot assumes one guild:
  - one panel channel (`DISCORD_CHANNEL_ID`) and one in-memory `persistent_message_ref`;
  - slash commands synced to one `DISCORD_GUILD_ID`;
  - one `BOOKING_ROLE_ID`;
  - bookings with no guild.

  `is_booking_admin` trusts the Discord *Administrator* permission. If the bot were invited to a second guild, that guild's admins would bypass the booking lock and could cancel anyone's booking.
- **f() — solution variable:** a way for one bot to serve several guilds, where only the guild holding a booking can control that game server.
- **y — desired outcome:** several communities share the HLL servers through one bot. Each books from its own Discord. Only the booking guild can change a game server during its slot; the owner guild keeps control otherwise.
- **gap:** no guild approval list, no per-guild configuration, no guild on bookings, and a lock and admin model that ignore which guild a request comes from.
- **boundaries:**
  - An existing single-guild `.env` works unchanged.
  - One shared CRCON token, so guest guilds act through the owner's CRCON access.
  - One Docker container.
  - AGENTS.md module layout.
  - Every control write still goes through the guard right before it runs.
- **M — operating model:**
  - Anyone with the invite link can add the bot to a guild unless "Public Bot" is off in the developer portal.
  - Role and user IDs only mean something inside their own guild.
  - Admin cam access is stored on the game server and outlives a booking.
  - Discord rate-limits command syncs; per-guild sync is fine for a handful of guilds.

## Project language and decisions
- Builds on the accepted booking bet: `BookingService`, `_deny_if_booked` before all 8 control writes, the display-only refresh loop, and SQLite in `data/bookings.db`.
- Existing patterns to reuse:
  - `SERVER{N}_*` numbered env vars → mirror them as `GUILD{N}_*`.
  - Persistent `GameModeView` custom IDs dispatch the same way in every guild.
  - Focused server is already keyed per channel.

## Requirements
| ID | Requirement | Status | Authority | Origin |
|---|---|---|---|---|
| M0 | The bot runs in several approved guilds, each with its own panel and booking commands | Core goal | Accepted | FROM_Y |
| M1 | Only owner-approved guilds can use the bot; any other guild is left | Must-have | Accepted | FROM_M |
| M2 | Control rules by guild. With no active booking, only the owner guild can make control changes. During a booking, only the booking guild's members with its booking role can, plus owner booking admins (override) | Must-have | Accepted | FROM_Y |
| M3 | One booking calendar per game server across all guilds; bookings from different guilds cannot overlap | Must-have | Accepted | FROM_GAP |
| M4 | Admin power is scoped. Owner booking admins can override or cancel any guild's booking. A guest guild's Administrators can manage only their own guild's bookings | Must-have | Accepted | FROM_M |
| M5 | Each guild sets its own panel channel and booking role without code changes (a `.env` edit plus restart is acceptable) | Must-have | Accepted | FROM_GAP |
| M6 | Every guild can see when each game server is booked and by which guild, without exposing other guilds' members | Must-have | Accepted | FROM_GAP |
| M7 | Admin cam add and remove stay owner-guild only at all times, and remain subject to M2 | Must-have | Accepted | FROM_M |
| M8 | Existing single-guild deployments behave as they do on `feat/server-booking` | Must-have | Accepted | FROM_BOUNDARY |
| M9 | Fair-use limits per guild | Nice-to-have | Accepted — **below cut** | FROM_GAP |

Decisions behind M2 and M7 were made by the repo owner on 2026-10-16:
- unbooked → owner guild only;
- the booking guild's role members get control, not only the booker;
- admin cam → owner guild only;
- onboarding → fixed `.env` list.

Effect on the first booking bet: during an owner-guild booking, control now goes to **owner members with the owner booking role**, not only the booker. That makes the lock consistent across guilds.

## Appetite
- Authority: **Accepted**
- Budget: 1 week, one developer.
- Review point: two guilds book the same game server; the lock holds across them; the owner guild keeps control when nothing is booked.
- Cut line: **M0–M8.** Below the line: M9.
- Accepted uncertainty:
  - Guilds removed from `.env` leave their future bookings stranded. Those bookings are ignored and hidden, with no revoke workflow.
  - One panel channel per guild.
  - A panel pushed past the last 50 messages in its channel isn't found after a restart; `/repost_button` recovers it (G1).
  - Per-guild command sync assumes a handful of guilds.
- Revisit conditions:
  - More than about 10 guilds (switch to a global command sync).
  - Guest guilds need admin cam (would need an auto-revoke scheduler — a separate bet).

## Shapes

### CURRENT (`feat/server-booking`)
| Part | Mechanism |
|---|---|
| CURRENT1 | One guild, one panel channel, one booking role |
| CURRENT2 | Lock allows only the booker and booking admins (Discord Administrator, in any guild) |

### A: Fixed guild list in `.env` — **SELECTED**
| Part | Mechanism | Flag |
|---|---|:---:|
| A1 | Guild registry from env: `GUILD{N}_ID`, `GUILD{N}_CHANNEL_ID`, `GUILD{N}_BOOKING_ROLE_ID`. The existing `DISCORD_GUILD_ID` + `DISCORD_CHANNEL_ID` + `BOOKING_ROLE_ID` (+ `BOOKING_ADMIN_ROLE_ID`) define the owner guild | |
| A2 | Leave unlisted guilds: `on_guild_join`, plus a sweep at startup | |
| A3 | Bookings gain `guild_id`; existing rows are migrated to the owner guild. Overlap checks stay per game server across all guilds | |
| A4 | Lock decision `can_control(game server, guild, member, action)` implements M2 and M7. It stays at the existing 8 write sites | |
| A5 | Scoped admin: owner booking admin = Administrator or `BOOKING_ADMIN_ROLE_ID` *in the owner guild*. Guest guild admin = Administrator in that guild, for that guild's bookings only | |
| A6 | One panel per guild: an in-memory `channel_id → message_id` map, plus the existing history scan to find each guild's panel after a restart (revised from SQLite storage, G1). Booking changes and the refresh loop update every guild's panel. Guest panels leave out the admin cam buttons | |
| A7 | Panel and booking text shows the booking **guild's** name; the booker is shown only inside their own guild | |
| A8 | Slash commands are copied to each listed guild at startup; `/repost_button` reposts only the calling guild's panel | |

### B: Owner-managed guild registry — *not selected*
`/guild approve|revoke` plus `/setup` in each guild, with no restarts. Rejected for this bet: more commands and a revoke lifecycle, and no requirement needs live onboarding.

### C: Panel only for the guild holding the booking — *not selected*
Guest guilds get only `/book`, `/bookings`, and a private `/control` during their slot. Fails M6: guest guilds get no live availability panel.

## Fit check
- Authority: **Decision-ready**

| Req | A | B | C |
|---|:---:|:---:|:---:|
| M0 Multi-guild | ✅ | ✅ | ✅ |
| M1 Approved guilds only | ✅ | ✅ | ✅ |
| M2 Lock by guild | ✅ | ✅ | ✅ |
| M3 Shared calendar | ✅ | ✅ | ✅ |
| M4 Scoped admin | ✅ | ✅ | ✅ |
| M5 Per-guild config | ✅ | ✅ | ✅ |
| M6 Cross-guild visibility | ✅ | ✅ | ❌ |
| M7 Admin cam owner-only | ✅ | ✅ | ✅ |
| M8 Single-guild unchanged | ✅ | ✅ | ✅ |

## Reverse fit check (A)
| Part | Requirement(s) | Justified? |
|---|---|:---:|
| A1 | M0, M5, M8 | ✅ |
| A2 | M1 | ✅ |
| A3 | M3, M8 | ✅ |
| A4 | M2, M7 | ✅ |
| A5 | M4 | ✅ |
| A6 | M0, M6, M7 | ✅ |
| A7 | M6 | ✅ |
| A8 | M0 | ✅ |

A4 carries all of M2: missing a single write site breaks it. The existing AST test (every write must call the guard) still applies, and the guard's rules gain a decision-table test.

## Requirement coverage (selected shape A)
| Req | Parts | Coverage | Realized-conformance question |
|---|---|:---:|---|
| M0 | A1, A6, A8 | ✅ | Do two listed guilds each get a panel and the slash commands immediately after startup? |
| M1 | A2 | ✅ | Does inviting the bot to an unlisted guild make it leave within seconds? |
| M2 | A4 | ✅ | Does every cell of the control table in the breadboard behave as specified? |
| M3 | A3 | ✅ | Is a guild B booking that overlaps guild A's booking on the same game server rejected? |
| M4 | A5 | ✅ | Is a guest guild's Administrator refused when cancelling another guild's booking, and denied override during another guild's slot? |
| M5 | A1 | ✅ | Can a new guild be added by editing `.env` and restarting, with no code change? |
| M6 | A6, A7 | ✅ | Does guild B's panel show "Booked by **Guild A**" with no guild A member mentions? |
| M7 | A4, A6 | ✅ | Do guest panels lack the admin cam buttons, and is a guest admin cam attempt denied anyway? |
| M8 | A1, A3 | ✅ | Does the existing single-guild `.env` pass the current booking tests unchanged (apart from the role-member rule)? |

## Appetite fit
| Shape | Fits? | Required cuts | Uncertainty |
|---|:---:|---|---|
| A | ✅ | M9 | Per-guild command sync limits (fine for a handful of guilds) |

## Decision
- Status: **selected**
- Chosen direction: **A — fixed guild list in `.env`**, parts A1–A8.
- Rationale:
  - Cheapest shape that passes M0–M8.
  - B adds live onboarding that no requirement needs.
  - C fails M6.
- Cuts and non-goals:
  - M9 fair-use limits.
  - Live guild approval or revocation.
  - Guest admin cam, and auto-revoke at booking end.
  - More than one panel per guild.
  - Global command sync.
- Accepted uncertainty: see Appetite.
- Constraints the build must preserve:
  - `_deny_if_booked` (renamed or extended) runs immediately before every control write.
  - The refresh loop stays display-only.
  - Booking logic stays in `src/services/booking.py`.
  - Guild configuration resolution lives in a service or util module, not inline in `bot.py`.

## Next useful move
- Selected-design breadboard **accepted** 2026-10-16 ([multi-guild-breadboard.md](multi-guild-breadboard.md)). G1 resolved by revising A6 (option 1, repo owner).
- Next: slicing.
