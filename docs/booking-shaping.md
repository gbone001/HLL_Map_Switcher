# Server Booking — Shaping

## Working mode
- Profile: collaborative
- Current move: slices accepted (V1 → V2 → V3 → V4) → next is building V1
- Status: **selected** (shape B), 2026-10-16. B8 added 2026-10-16 to resolve breadboard issue G1

## Frame source
Conversation with the repo owner on 2026-10-16; repo state `main` @ 73a1eca.

- **x — current situation:** Anyone who can see the panel channel can change any server's map, objectives, weather, timers, cooldown, and admin cam access at any time. There is no record of who intends to use a server when, so groups (scrims, training, events) can collide and admins sort it out by hand. All bot state (focus, objectives, weather) is in memory and lost on restart.
- **f() — solution variable:** a way to reserve a server for a time slot.
- **y — desired outcome:** users can see when a server is free, reserve a slot, and trust that the slot is theirs; admins can override.
- **gap:** no reservation record, no conflict detection, no restriction of panel writes to the reserving user.
- **boundaries:**
  - AGENTS.md layout: Discord commands in `src/commands/`, events/schedulers in `src/events/`, business logic in `src/services/`.
  - Interactions acknowledge before slow work; secrets and per-server config via environment variables.
  - Must not break single-server or multi-server configurations, or unrelated Discord features when booking config is missing.
- **M — operating model:**
  - Deployed via **Docker** (`docker-compose.yml`), a single container. Railway files (`Procfile`, `railway.json`) are legacy for this feature.
  - The bot restarts on deploys and crashes; bookings must survive that.
  - Users are in different time zones; the panel currently hard-codes AEST (+10, no DST).
  - CRCON can be down; booking itself must not depend on CRCON being reachable.

## Project language and decisions
- Canonical terms: *panel* (persistent `GameModeView` message), *focused server* (per-channel selection), *server_index* (position in `api_client.get_servers()`), *server_number* (the `N` in `SERVER{N}_*` env vars), *control write* (any mutation through `ServerControlService` or `_run_server_write`).
- Relevant decisions: `ServerControlService` owns HTTP-first / RCON-fallback writes and holds a per-server `mutation_lock`; `_run_server_write` shares that lock for direct CRCON admin writes.
- Existing seams to reuse:
  - Panel write handlers in `src/bot.py` (map change, objectives, weather, warfare timers, team switch cooldown, admin cam add/remove) — the place to apply the booking lock.
  - `build_panel()` / `build_main_embed()` — the place to show booking state.
  - `data/` directory, bind-mounted by Docker Compose — the place to persist bookings.

## Requirements
| ID | Requirement | Status | Authority | Origin |
|---|---|---|---|---|
| R0 | A user can reserve a specific server for a start and end time | Core goal | Accepted | FROM_Y |
| R1 | Overlapping bookings on the same server are rejected | Must-have | Accepted | FROM_GAP |
| R2 | Anyone can see upcoming bookings, each in their own time zone | Must-have | Accepted | FROM_M |
| R3 | Bookings survive bot restarts and redeploys | Must-have | Accepted | FROM_M |
| R4 | The booker and admins can cancel; admins can override any booking | Must-have | Accepted | FROM_BOUNDARY |
| R5 | Who may book is restricted | Must-have | Accepted | FROM_GAP |
| R5.1 | Only members with a configured booking role (or admins) can create bookings | Must-have | Accepted — **above cut** | FROM_GAP |
| R5.2 | Per-user limits (max duration, max future bookings) prevent hoarding | Must-have | Accepted — **below cut** | FROM_GAP |
| R6 | During an active booking, other users cannot change that server through the bot | Must-have | Accepted | FROM_GAP |
| R7 | Server auto-configures to a preset at booking start | Nice-to-have | Out (this bet) | FROM_Y |
| R8 | Reminders and start/end notices | Nice-to-have | Out (this bet) | FROM_Y |

R5 was split on 2026-10-16: without a role check, booking plus the R6 lock would let any channel member lock everyone else out of a server, so the single role check moved above the cut line. Per-user limits stay below it.

## Appetite
- Authority: **Accepted**
- Budget: 1–2 weeks, one developer.
- Review point: end of week 1 — **slices V1 and V2 demoed**: book, list, persist, and the lock during a booking. Revised 2026-10-16 from "R0–R4 incl. cancel" so the riskiest slice (V2, the lock) goes first; cancel (V4) now lands in week 2.
- Cut line: **R0–R4, R5.1, R6.** Below the line: R5.2 (per-user limits). Out of this bet: R7, R8.
- Accepted uncertainty:
  - The lock only governs the bot. CRCON's web UI and in-game admins can still change the server during a booking.
  - Times are entered in one configured time zone, not per user.
- Must-resolve unknowns: none blocking (the persistence spike below is resolved).
- Revisit conditions:
  - More than one bot container is ever run against the same `data/`.
  - Users ask for automatic server setup at booking start (R7) — that is a separate bet (shape C).

## Shapes

### CURRENT
| Part | Mechanism | Flag |
|---|---|:---:|
| CURRENT1 | No booking concept; any panel user can write to any server at any time | |
| CURRENT2 | All bot state in memory; lost on restart | |

### A: Calendar-only bookings — *not selected*
Booking service, SQLite storage, slash commands, panel display, admin override (A1–A5). Advisory only; fails R5 and R6.

### B: Bookings plus panel lock — **SELECTED**
| Part | Mechanism | Flag |
|---|---|:---:|
| B1 | `BookingService` in `src/services/booking.py`: create, cancel, list, and find the active booking; overlap check on half-open UTC ranges `[start, end)` | |
| B2 | SQLite store at `data/bookings.db` (default rollback journal, not WAL). Bookings keyed by `server_number` (or the server name in single-server mode), never by the position-based `server_index` | |
| B3 | Slash commands in `src/commands/booking.py`: `/book server start duration`, `/bookings`, `/cancel_booking`. Input parsed in `BOOKING_TIMEZONE` (an IANA name, e.g. `Australia/Sydney`); output as Discord `<t:unix:F>` / `<t:unix:R>` timestamps | |
| B4 | Panel embed shows each server's active booking ("Booked by @X until …") and next booking | |
| B5 | Admin override: Discord Administrator permission or `BOOKING_ADMIN_ROLE_ID` can cancel or replace any booking | |
| B6 | One booking guard that every panel and modal **control write** goes through: during an active booking, only the booker and admins pass. Read-only actions (Refresh Status, Change Server focus) are not gated | |
| B7′ | `BOOKING_ROLE_ID` check on `/book` (R5.1 only; per-user limits from the original B7 are cut) | |
| B8 | Panel boundary refresh: a minimal `discord.ext.tasks` loop that re-renders the panel when any booking starts or ends. **Display only** — no presets, no server writes. Added to resolve breadboard issue G1 | |

### C: B plus automated session start and end — *not selected (candidate follow-up bet)*
Adds presets, a scheduler that applies them through `ServerControlService`, reminders, and an optional restore at end. Covers R7 and R8, which are out of this bet.

### D: Discord Scheduled Events as the store — *rejected*
Fails R1: Discord does not prevent overlapping events, and events created directly in Discord's UI bypass the bot's overlap and role checks.

## Candidate evidence
| Candidate | Evidence | Question resolved | R/S implication | Appetite implication | Remaining uncertainty |
|---|---|---|---|---|---|
| A, B, C | Persistence spike (below) | Does a file in `data/` survive restarts and redeploys? | R3 passes for B via B2 | None | Windows/macOS Docker Desktop bind mounts: fine for a single writer; avoid WAL |
| D | Discord Scheduled Events behaviour | Can the bot stop overlapping events? | No → fails R1 | — | — |

### Spike: persistence under Docker (2026-10-16)
- Railway check abandoned: Railway is not used for this deployment (the Railway CLI session was also unauthorized).
- `docker-compose.yml` bind-mounts `./data:/app/data`. Bind mounts live on the host, so files survive `restart`, rebuilds, and `docker compose down -v` (which removes named volumes only).
- `container_name` is fixed and there is one service, so there is a single SQLite writer.
- Findings to carry into the design:
  - `Dockerfile` runs `COPY data/ ./data/`, so a host `bookings.db` would be baked into the image at build time. The runtime bind mount hides it, but `data/*.db` should be added to `.dockerignore`.
  - `ENV MAP_CACHE_DIR=/app/data` is set but unused: `map_data.py` hard-codes `<repo>/data/map_cache.json`. Bookings should use one shared data-directory setting rather than add a second hard-coded path.
- Implications: R3 passes for B (via B2). No new requirements. No appetite change.

## Fit check
- Authority: **Decision-ready** (requirements and appetite accepted)

| Req | Requirement | Status | A | B | C | D |
|---|---|---|:---:|:---:|:---:|:---:|
| R0 | Reserve a server for a time slot | Core goal | ✅ | ✅ | ✅ | ✅ |
| R1 | No overlapping bookings | Must-have | ✅ | ✅ | ✅ | ❌ |
| R2 | Visible, in each user's own time zone | Must-have | ✅ | ✅ | ✅ | ✅ |
| R3 | Survives restarts | Must-have | ✅ | ✅ | ✅ | ✅ |
| R4 | Cancel and admin override | Must-have | ✅ | ✅ | ✅ | ✅ |
| R5.1 | Booking role required | Must-have | ❌ | ✅ | ✅ | ❌ |
| R6 | Locked to booker during slot | Must-have | ❌ | ✅ | ✅ | ✅ |

R5.2 is below the cut line and excluded from this comparison. C passes everything B does but adds out-of-bet scope (R7, R8) and a scheduler, so it exceeds the appetite.

## Reverse fit check (B)
| Shape part | Mechanism | Requirement(s) served | Justified? |
|---|---|---|:---:|
| B1 | BookingService + overlap check | R0, R1, R4 | ✅ |
| B2 | SQLite in `data/` keyed by server_number | R3 | ✅ |
| B3 | Slash commands + Discord timestamps | R0, R2, R4 | ✅ |
| B4 | Panel shows active / next booking | R2, R6 | ✅ |
| B5 | Admin override role | R4 | ✅ |
| B6 | Booking guard on control writes | R6 | ✅ |
| B7′ | Booking role check | R5.1 | ✅ |
| B8 | Panel refresh at booking start/end | R6 (the visible "who holds the server now"), R2 | ✅ |

B6 carries all of R6 on its own: every control write must go through it, so missing a single write path breaks the requirement.

## Requirement coverage (selected shape B)
| Req | Supporting part(s) | Coverage | Realized-conformance question |
|---|---|:---:|---|
| R0 | B1, B3 | ✅ | Can a role member book a free slot with `/book` and see it in `/bookings`? |
| R1 | B1 | ✅ | Does a booking that overlaps by one minute get rejected, while back-to-back bookings (end = start) are accepted? |
| R2 | B3, B4 | ✅ | Do two users in different time zones see the same booking at the correct local times? |
| R3 | B2 | ✅ | After `docker compose down && up -d`, are all future bookings still listed? |
| R4 | B1, B3, B5 | ✅ | Can the booker and an admin cancel, can a non-booker not, and can an admin override an active booking? |
| R5.1 | B7′ | ✅ | Is a member without `BOOKING_ROLE_ID` refused at `/book` with a clear message? |
| R6 | B6, B4, B8 | ✅ | When a booking starts or ends with no one using the bot, does the panel line change within one loop tick? During an active booking, is every panel write (map, objectives, weather, both timers, cooldown, admin cam add/remove) refused for a non-booker and allowed for the booker and admins? |

## Appetite fit
| Shape | Fits? | Required cuts | Uncertainty / spike |
|---|:---:|---|---|
| B | ✅ | R5.2 (per-user limits) | None blocking. B8 is a small display-only loop and stays within the appetite |
| C | ❌ | Would need R7 and R8 cut, which reduces it to B | Scheduler idempotency, catch-up after restart |

## Decision
- Status: **selected**
- Chosen direction: **B — bookings plus panel lock**, parts B1–B6, B7′ and B8.
- Rationale: B is the cheapest shape that passes every accepted requirement above the cut line. A fails R5.1 and R6. D fails R1 and R5.1. C exceeds the appetite.
- Cuts / non-goals:
  - Per-user limits (R5.2).
  - Automatic server setup at booking start (R7). B8's loop must stay display-only; making it apply server changes would be shape C.
  - Reminders and start/end notices (R8).
  - Locking CRCON's web UI or in-game admin actions.
  - Server password or whitelist changes.
  - Running more than one bot container.
- Accepted uncertainty:
  - The lock is bot-only.
  - There is a single configured input time zone.
  - SQLite on a Docker Desktop bind mount (single writer, no WAL).
- Spikes or decisions still required:
  - Behaviour of an active booking when CRCON is down: the lock still applies, because booking state is local.
  - Whether `/repost_button` and Change Server should show a booking notice.
- Constraints the build must preserve:
  - `ServerControlService.mutation_lock` and `_run_server_write` stay the single path for control writes.
  - Keep booking logic out of `bot.py` beyond wiring.
  - A missing `BOOKING_*` configuration must disable booking cleanly without breaking the panel.
- Candidate breadboard reconciliation: none was produced. A selected-design breadboard is the next step.

## Next useful move
- Selected-design breadboard: [booking-breadboard.md](booking-breadboard.md) — **accepted** 2026-10-16.
- Shaping issue G1 (stale panel booking line) was resolved by adding B8 (option 1, chosen by the repo owner 2026-10-16).
- Slices: [booking-slices.md](booking-slices.md) — **accepted** 2026-10-16, V1 → V2 → V3 → V4. The week-1 review point was revised to "V1 + V2 demoed".
