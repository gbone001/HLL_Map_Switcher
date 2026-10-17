---
planning: true
shaping: true
---

# Multi-Guild Booking — Breadboard (shape A)

## Mode and authority
- **Mode:** selected-design
- **Authority:** **accepted** selected intent, 2026-10-16. Ready for slicing.
- **Selected shape:** A — fixed guild list in `.env`, parts A1–A8, with A6 revised for G1 ([multi-guild-shaping.md](multi-guild-shaping.md))
- **Requirements:** M0–M8 accepted; M9 below the cut line.
- **Appetite:** accepted, 1 week.
- **Builds on:** the accepted booking breadboard ([booking-breadboard.md](booking-breadboard.md)). Its IDs (N2–N14, N20–N26, S1, U1–U15) are reused where behavior carries over; changed rows are marked *(changed)*. Code evidence: `feat/server-booking` @ 7235865.

## Control table (the behavior M2 and M7 must produce)
"Guild role" means the booking role configured for that guild: `BOOKING_ROLE_ID` for the owner, `GUILD{N}_BOOKING_ROLE_ID` for guests.

| # | Situation | Owner member (no role) | Owner member with guild role | Owner booking admin | Booking guest guild: role member | Booking guest guild: no role | Any other guest guild |
|---|---|:---:|:---:|:---:|:---:|:---:|:---:|
| C1 | No active booking | ✅ | ✅ | ✅ | ❌ not booked | ❌ | ❌ |
| C2 | Guest guild G holds the booking | ❌ | ❌ | ✅ override | ✅ | ❌ | ❌ |
| C3 | Owner guild holds the booking | ❌ | ✅ | ✅ | — | — | ❌ |
| C4 | Admin cam add/remove | follows C1–C3 | follows C1–C3 | ✅ | ❌ owner-only | ❌ | ❌ |

C1's first column keeps today's behavior: anyone who can see the owner panel can make changes when the server is unbooked.

## Design decisions made while breadboarding
| ID | Decision | Why |
|---|---|---|
| D1 | The guard still runs right before each of the 8 write sites (unchanged from the first bet). It now takes `interaction.guild_id` and an `action` (`control` or `admin_cam`) | One decision point for M2 and M7 |
| D2 | The panel is built **per viewing guild**: booking-line wording (A7) and the admin cam buttons (A6) differ by guild | Guest panels must not offer admin cam; guilds see guild names, not other guilds' members |
| D3 | Guest panels leave out the two admin cam buttons. The guard still denies `admin_cam` from guest guilds if an old message or forged interaction gets through | Defense in depth for M7 |
| D4 | A guest guild's Administrators can `/book` in their own guild even without the role, matching how admins can book in the owner guild today | Consistent with M4's own-guild admin scope |
| D5 | Bookings by guilds no longer listed in `.env` are **ignored**: they don't lock, show, or block overlaps | Accepted uncertainty "stranded bookings ignored"; avoids phantom locks |
| D6 | Overlap rejection shown to another guild names the guild, not the member: "❌ Overlaps a booking by **Guild A** <t:…>" | M6 privacy |
| D7 | Interactions from a DM, or from a guild not in the registry, are refused before any booking or control logic runs | M1; `interaction.guild_id` may be `None` |

## Places
| ID | Place | Description |
|---|---|---|
| P1 | Panel message, one per listed guild *(changed)* | Persistent embed + `GameModeView` in each guild's panel channel |
| P2–P5 | Map, objectives, weather and cooldown flows *(current)* | Unchanged; reachable from every guild's panel |
| P6–P7 | Add/remove admin cam flows *(current)* | Reachable only from the owner panel (D3) |
| P8–P10 | `/book`, `/bookings`, `/cancel_booking` *(changed)* | Registered in every listed guild; guild-aware replies |
| P11 | Booking store *(changed)* | `bookings` gains `guild_id` |
| P12 | Discord guild membership *(new, system)* | Guilds the bot is in; joining an unlisted guild triggers leaving it |
| P13 | `/repost_button` *(changed)* | Reposts the calling guild's panel only |

## UI affordances
| ID | Place | Component | Affordance | Control | Wires Out | Returns To |
|---|---|---|---|---|---|---|
| U1 | P1 | embed | Booking line per game server. Inside the booking guild: "🔒 Booked by @user until …". Elsewhere: "🔒 Booked by **{Guild}** until …" / "📅 Next: **{Guild}** <t:…>" *(changed)* | display | — | — |
| U2 | P1 | buttons | Owner panel: all buttons. Guest panel: everything except Remove/Add Admin Cam *(changed)* | click | → P2…P7 | — |
| U3 | P1–P7 | commit step *(current)* | Final step of each flow | click/submit | → N40 | — |
| U4 | P1–P7 | ephemeral | Denial, worded by reason (N40): "🔒 Booked by **{Guild}** until …" · "Your Discord can change {server} only during its own bookings." · "Admin cam is managed by the owner Discord." · "You need the @role role to change {server} during this booking." *(changed)* | display | — | — |
| U5 | P2–P7 | ephemeral *(current)* | Existing write results | display | — | — |
| U6–U7, U9–U10, U12–U15 | P8–P10 | — *(current)* | `/book`, `/bookings`, `/cancel_booking` inputs and replies. The role named in U9 is the calling guild's. U12 lists guild names; members are mentioned only for the caller's own guild | — | as before | — |
| U8 | P8 | reply *(changed)* | Overlap: names the guild for cross-guild conflicts (D6), the member for same-guild conflicts | display | — | — |
| U16 | any | ephemeral *(new)* | "This Discord isn't set up to use this bot." (D7) | display | — | — |
| U17 | P13 | reply *(changed)* | "✅ Panel reposted in #channel" for the caller's guild only | display | — | — |

## Non-UI affordances
| ID | Place | Component | Affordance | Control | Wires Out | Returns To |
|---|---|---|---|---|---|---|
| N30 | — | **guild registry** (A1) | Load the owner guild from `DISCORD_GUILD_ID`, `DISCORD_CHANNEL_ID`, `BOOKING_ROLE_ID` and `BOOKING_ADMIN_ROLE_ID`, plus `GUILD{N}_ID`, `GUILD{N}_CHANNEL_ID` and `GUILD{N}_BOOKING_ROLE_ID`. `lookup(guild_id)` returns the guild's config (`is_owner`, `channel_id`, `role_id`) or `None` | call | → S20 | → N31, N32, N40, N41, N42, N43 |
| N31 | P12 | **leave unlisted guilds** (A2) | On `on_guild_join`, and once at startup over `bot.guilds`: if N30 has no entry, log it and `guild.leave()` | event | → P12 | — |
| N32 | — | **per-guild command sync** (A8) | `copy_global_to` and `sync` for each listed guild at `setup_hook` *(replaces the single-guild sync)* | call | → Discord | — |
| N33 | — | **request context** | Resolve `interaction.guild_id` → N30. If missing → U16 (D7) | call | missing → U16 | → N40, N41, N42 |
| N41 | P8 | can book *(changed N2)* | Member has the **calling guild's** role, or is that guild's Administrator (D4), or is an owner booking admin | call | yes → N3 · no → U9 | — |
| N4 | P8 | create *(changed)* | Stores `guild_id`. Overlap check across all **listed** guilds' bookings on the game server (D5) | call | overlap → U8 · free → S1 → N44 | → U7 |
| N42 | P10 | cancel *(changed N6)* | Booker, or an Administrator **of the booking's guild**, or an owner booking admin (A5) | call | ok → S1 → N44 · denied → U14 | → U14 |
| N43 | — | **admin scope** (A5, *changed N7*) | `owner_admin`: Administrator or `BOOKING_ADMIN_ROLE_ID` *in the owner guild*. `guild_admin`: Administrator in the calling guild. Otherwise `none` | call | — | → N40, N41, N42 |
| N8 | — | active booking *(current)* | Active booking on a game server; ignores unlisted guilds (D5) | call | — | → N40, N10 |
| N40 | P1–P7 | **control guard** (A4, *changed N9*) | `can_control(server, ctx, member, action)`, in this order: owner admin → allow · `action == admin_cam` and not the owner guild → deny (C4) · active booking (N8)? **none** → allow if owner guild, else deny (C1) · **booked** → allow if `ctx.guild == booking.guild` **and** the member has that guild's role, else deny (C2/C3) | call | allow → N20–N26 · deny → U4 | — |
| N10 | P1 | booking lines *(changed)* | Build U1 for one **viewing guild**: guild name for other guilds' bookings, a user mention for its own | call | — | → U1 |
| N11 | P1 | build panel *(changed)* | `build_panel(focused, viewing_guild)`. CRCON statuses are fetched once per refresh and reused for every guild. Guest view leaves out the admin cam buttons (D3) | call | → N10 | → P1 |
| N44 | — | **refresh all panels** (A6, *replaces N12*) | For each listed guild: get its panel channel → N11 → find or edit the panel message (N45) | call | → N11, N45 | — |
| N45 | — | panel message locator *(changed, G1)* | Look up `channel_id` in S21; on a miss or `NotFound`, run the existing history scan (last 50 messages, panel title) and cache the result; if still not found, post a new panel | call | → S21 | → N44 |
| N14 | — | boundary refresh *(current)* | Display-only loop; now calls N44 | timer | → N44 | — |
| N46 | P13 | repost *(changed)* | Admin of the **calling** guild → send a new panel to that guild's channel → S21 | call | → N11, S21 | → U17 |
| N20–N26 | P2–P7 | 8 write sites *(current)* | Unchanged; each now calls N40 with `action=control`, except N25/N26 with `action=admin_cam` | call | → game server | → U5 |

## Stores
| ID | Place | Store | Description |
|---|---|---|---|
| S1 | P11 | `bookings` *(changed)* | Adds `guild_id INTEGER`. Migration: existing rows get the owner guild ID |
| S20 | — | Guild config (env) *(new)* | Owner guild from the existing variables, plus `GUILD{N}_ID`, `GUILD{N}_CHANNEL_ID` and `GUILD{N}_BOOKING_ROLE_ID` |
| S21 | — | Panel message map, in memory *(changed, G1)* | `channel_id → message_id`, replacing the single `persistent_message_ref`. Empty after a restart and refilled by N45's history scan; not persisted |
| S3 | — | Server config *(current)* | Game servers, shared by all guilds |

## Behavior traces
| ID | Scenario | Trace | Observable consequence |
|---|---|---|---|
| T1 | Owner member changes the map, nothing booked | U3 → N33 → N40: no booking + owner guild → allow → N20 | U5 (as today, C1) |
| T2 | Guest role member tries a change, nothing booked | U3 → N33 → N40: no booking + not owner → deny | U4 "only during its own bookings" |
| T3 | Guest guild books a slot | U6 → N33 → N41 ✓ → N3 → N4 (cross-guild overlap check) → S1 (guild_id = G) → N44 | U7; **every** guild's panel updates (U1: "Next: **G**" elsewhere, "Next: @user" in G) |
| T4 | During G's booking: a G role member changes the map | U3 → N40: booked, same guild, has role → allow → N20 | U5 (C2) |
| T5 | During G's booking: a G member without the role | U3 → N40: same guild, no role → deny | U4 "need the @role role" |
| T6 | During G's booking: an owner member without admin | U3 → N40: booked by another guild → deny | U4 "Booked by **G** until …" |
| T7 | During G's booking: an owner booking admin | U3 → N40 → N43 owner_admin → allow | U5 (override) |
| T8 | During G's booking: a third guild H's member | U3 → N40 → deny | U4 "Booked by **G** …" |
| T9 | Guest guild tries admin cam (old message or forged interaction) | U3 (P6/P7) → N40 action=admin_cam, not owner → deny | U4 "Admin cam is managed by the owner Discord" (C4) |
| T10 | Guild H books over G's slot | U6 → N4 overlap with G | U8 "Overlaps a booking by **G**" (no G member named, D6) |
| T11 | G's Administrator cancels G's booking; tries H's | U13 → N42 → N43 guild_admin, same guild ✓ / other guild ✗ | U14 "Cancelled" → N44 / "Not yours" |
| T12 | Owner admin cancels any guild's booking | U13 → N42 → N43 owner_admin ✓ → S1 → N44 | U14; all panels update |
| T13 | Bot invited to an unlisted guild | P12 join → N31 → N30 no entry → leave | The bot leaves; logged |
| T14 | Startup with three listed guilds | N30 → N32 (sync ×3) → N31 sweep → N44 (statuses fetched once, three panels) | Commands appear immediately in each guild; three panels current |
| T15 | Booking start or end | N14 → N44 | Every guild's U1 updates within 60s |
| T16 | Existing single-guild `.env` (no `GUILD{N}_*`) | N30: owner only → behaves as `feat/server-booking`; C3 role rule applies | Same commands and panel as today (M8) |
| T17 | Guild removed from `.env` with future bookings | N30 no entry → N4/N8/N10 skip those bookings (D5) | No phantom lock; its bookings aren't shown or enforced |
| T18 | DM or unknown guild uses a slash command | N33 → none | U16 |

### Reverse trace (consequence → sources)
| Consequence | Incoming | OK? |
|---|---|:---:|
| U1 | N10 ← N11 ← N44 ← N4 / N42 / N14 / startup / existing refresh paths | ✅ |
| U4 | N40 deny (C1, C2, C3, C4 reasons) | ✅ |
| U5 | N20–N26 ← N40 allow | ✅ |
| U8 | N4 | ✅ |
| U14 | N42 | ✅ |
| U16 | N33 | ✅ |
| U17 | N46 | ✅ |
| Bot leaves guild | N31 | ✅ |

## Requirement realization map
| Req | Parts | Breadboard IDs | Observable consequence |
|---|---|---|---|
| M0 | A1, A6, A8 | N30, N32, N44, P1 per guild | T14 |
| M1 | A2 | N31, N33, U16 | T13, T18 |
| M2 | A4 | N40 (C1–C3) at N20–N26 | T1, T2, T4–T8 |
| M3 | A3 | N4 cross-guild overlap, S1.guild_id | T10 |
| M4 | A5 | N43, N42, N40 owner-admin branch | T7, T11, T12 |
| M5 | A1 | S20, N30 | T14, T16 |
| M6 | A6, A7 | N10, U1, U8, U12, D6 | T3, T10 |
| M7 | A4, A6 | N40 C4 branch, N11 guest view without admin cam | T9 |
| M8 | A1, A3 | N30 owner-only path, S1 migration | T16 |

All parts are represented: A1 → N30, S20 · A2 → N31 · A3 → S1, N4 · A4 → N40 · A5 → N43, N42 · A6 → N44, N45, N11 · A7 → N10 · A8 → N32, N46.

## Visual breadboard

```text
   Guild G (guest)                    Owner guild                          Guild H (guest)
   ───────────────                    ───────────                          ───────────────
   /book U6 ──┐                       P1 panel (all buttons)               P1 panel (no admin cam)
              │                       U1 "🔒 Booked by **G** …"            U1 "🔒 Booked by **G** …"
   P1 panel   │                       U3 commit ──┐                        U3 commit ──┐
   U1 "🔒 @user"                                 │                                     │
   U3 commit ─┼──────────────┐                   │                                     │
              │              │                   │                                     │
 ═════════════╪══════════════╪═══ system rail ═══╪═════════════════════════════════════╪════
              ▼              ▼                   ▼                                     ▼
   N33 which guild? ── unknown ─► U16                                                  │
              │              └──────────────► N40 control guard ◄──────────────────────┘
              ▼                               owner admin?          → allow
   N41 G's role? ── ✗ ─► U9                   admin_cam & guest?    → deny "owner Discord"
              │ ✓                             no booking: owner?    → allow / deny "own bookings"
              ▼                               booked: same guild + role? → allow / deny "Booked by G"
   N3 parse ─► N4 overlap (all listed guilds) ─┐             │ allow                │ deny
              │ overlap → U8 "by **H**"        │             ▼                      ▼
              ▼ free                           │      N20–N26 write → U5      U4 (reason)
             S1 (guild_id = G) ─► N44 refresh ALL panels ─► N11 per guild ─► N10 ─ ─► U1 ×3
                                     ▲
              N14 every 60s ─────────┘          N30 registry ◄─ S20 .env     N31 leave unlisted ◄─ P12 join
```

**Finger-trace check:**
- T3: G's `/book` → N41 → N4 → S1 → N44 → U1 in all three guilds.
- T6: owner U3 → N40 "booked: same guild?" ✗ → U4.

No targeted sketch is needed: every place is a Discord message, modal, or command.

## Shaping issue G1 — panel message storage (A6) — resolved 2026-10-16
- **Selected shape says:** A6 — "panel message IDs stored per guild in SQLite".
- **Concrete implication:** the existing `ensure_persistent_message` already finds the panel after a restart by scanning the last 50 messages in the channel for the panel title ([bot.py:658](../src/bot.py)). A per-channel map in memory plus that scan covers restarts without a new table.
- **Conflict:** none with requirements. The SQLite part adds a table and a migration that no requirement needs.
- **Options:**
  1. **Revise A6:** use an in-memory `channel_id → message_id` map (S21) plus the existing history scan. Smaller.
  2. Keep SQLite storage as selected. It survives the panel being pushed past 50 messages in a busy channel.
- **Decision:** option 1, chosen by the repo owner. A6 was revised in the shaping record; N45 and S21 updated here. Accepted uncertainty: a panel buried past 50 messages is recovered with `/repost_button`.

## Notes and open questions
- **Performance:** N44 fetches CRCON statuses once and renders N panels from them. With three guilds and two game servers, that's one status round per refresh, not three.
- **Changed behavior from the first bet:** during an owner-guild booking, owner members with the owner role, not only the booker, can control (C3). The first bet's guard tests need updating to match.
- **Discord setup:**
  - Switch "Public Bot" off in the developer portal. N31 is the safety net, not the primary control.
  - Each guild must let the bot post in its panel channel.
- **Message Content intent:** still requested on `main`. In guest guilds, as in the owner guild, the bot won't connect unless that intent is enabled in the developer portal (from the earlier review).
- Breadboard accepted 2026-10-16; next is slicing.
