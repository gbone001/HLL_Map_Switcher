---
planning: true
shaping: true
---

# Server Booking — Breadboard (shape B)

## Mode and authority
- **Mode:** selected-design
- **Authority:** **accepted** selected intent, 2026-10-16. Ready for slicing.
- **Selected shape:** B — bookings plus panel lock, parts B1–B6, B7′ and B8 ([booking-shaping.md](booking-shaping.md)). B8 was added to resolve G1.
- **Requirements:** R0–R4, R5.1, R6 — Accepted, all above the cut line.
- **Appetite:** Accepted, 1–2 weeks. Cut: R5.2 (per-user limits) is below the line; R7 and R8 are out of this bet.
- **Candidate breadboard reconciled:** none existed.
- **Current-state evidence:** `src/bot.py` @ 73a1eca. Rows marked *(current)* describe existing behavior and are included only where new behavior connects to it.

## Design decisions made while breadboarding
These follow from the selected parts without changing them. Each one is flagged so a reviewer can push back.

| ID | Decision | Why |
|---|---|---|
| D1 | The guard (N9) is checked **when the write is committed**, at each of the 8 write sites, not only when the button is first clicked | Ephemeral flows stay open for up to 300s, so a flow started before a booking began could otherwise finish during it (trace T12) |
| D2 | Panel buttons stay enabled for everyone; a blocked user gets a private denial message when they click through | The panel is one shared message, so buttons can't be disabled for some users and not others |
| D3 | Booking is enabled only when `BOOKING_ROLE_ID` is set. Otherwise the slash commands reply "not configured" and the guard always allows | Meets the requirement that missing booking config must not break the panel |
| D4 | "Override" (R4) means admins pass the guard and can cancel any booking. To replace a booking, an admin cancels it and books again; there is no force option | Smallest mechanism that satisfies R4 |
| D5 | Time input format is `YYYY-MM-DD HH:MM` in `BOOKING_TIMEZONE`. Duration is minutes or `NhMm` | One unambiguous format. Discord timestamps take care of showing each user their local time (R2) |
| D6 | Bookings are half-open ranges `[start, end)`, so back-to-back bookings don't overlap | Standard interval semantics |
| D7 | Booking uses the existing data directory (`/app/data` under Docker) | Persistence spike; avoids adding a second hard-coded path |
| D8 | N14 runs every 60s and refreshes the panel only if a booking start or end has passed since its previous run. It runs only while booking is enabled, and never calls `ServerControlService` | Avoids an extra refresh every minute when nothing changed, picks up boundaries missed while the bot was down, and keeps B8 display-only |

## Places
| ID | Place | Description |
|---|---|---|
| P1 | Panel message *(current, modified)* | Persistent embed with `GameModeView` buttons in `DISCORD_CHANNEL_ID` |
| P2 | Map change flow *(current)* | Ephemeral: game mode → map → variant (`GameModeSelectionView` → `VariantDropdown`) |
| P3 | Objectives flow *(current)* | Ephemeral: five slot dropdowns; choosing the last slot applies the layout (`ObjectiveSelectionView`) |
| P4 | Dynamic weather flow *(current)* | Ephemeral Turn On / Turn Off (`DynamicWeatherToggleView`) |
| P5 | Team switch cooldown modal *(current)* | `TeamSwitchCooldownModal` |
| P6 | Add admin cam modal *(current)* | `AddAdminCamModal` |
| P7 | Remove admin cam flow *(current)* | Ephemeral select → confirm (`RemoveAdminCamConfirmView`) |
| P8 | `/book` *(new)* | Slash command with an ephemeral reply |
| P9 | `/bookings` *(new)* | Slash command; ephemeral list |
| P10 | `/cancel_booking` *(new)* | Slash command with an ephemeral reply |
| P11 | Booking store *(new, system)* | SQLite file `data/bookings.db` on the Docker bind mount |

## UI affordances
| ID | Place | Component | Affordance | Control | Wires Out | Returns To |
|---|---|---|---|---|---|---|
| U1 | P1 | embed | Per-server booking line: "🔒 Booked by @X <t:start:t>–<t:end:t>" when active; "Next: @Y <t:start:F> (<t:start:R>)" when one is upcoming | display | — | — |
| U2 | P1 | buttons *(current)* | Change Map, Set Objectives, Dynamic Weather, Match Timer, Warm Up Timer, Team Switch Cooldown, Remove Admin Cam, Add Admin Cam | click | → P2…P7; the timer buttons are their own commit step (U3) | — |
| U3 | P1–P7 | flow commit *(current)* | The final step of each flow: variant pick, last objective slot, Turn On/Off, modal submit, Match/Warm Up Timer button, Yes confirm | click/submit | → N9 | — |
| U4 | P2–P7 | ephemeral | **Denial:** "🔒 {server} is booked by @X until <t:end:t>. Only they or an admin can change it." | display | — | — |
| U5 | P2–P7 | ephemeral *(current)* | Existing success / failure result messages | display | — | — |
| U6 | P8 | options | `server` (choices = configured servers), `start` (`YYYY-MM-DD HH:MM`), `duration` | input | → N2 | — |
| U7 | P8 | reply | **Booked:** "✅ {server} booked <t:start:F> → <t:end:t> (ID #n)" | display | — | — |
| U8 | P8 | reply | **Rejected (overlap):** "❌ Overlaps @Z's booking <t:start:F>–<t:end:t> (#m)" | display | — | — |
| U9 | P8 | reply | **Rejected (no role):** "You need the @BookingRole role to book." | display | — | — |
| U10 | P8 | reply | **Rejected (bad time):** unparseable input, start in the past, or zero duration — echoes the expected format | display | — | — |
| U11 | P9 | options | `server` (optional) | input | → N5 | — |
| U12 | P9 | reply | List of active and upcoming bookings: ID, server, @user, <t:start:F>–<t:end:t>, <t:start:R> | display | — | — |
| U13 | P10 | options | `booking_id` (autocomplete: your own bookings; admins see all) | input | → N6 | — |
| U14 | P10 | reply | **Cancelled** / **Not yours** / **Not found or already ended** | display | — | — |
| U15 | P8–P10 | reply | **Booking not configured** (D3) | display | — | — |

## Non-UI affordances
| ID | Place | Component | Affordance | Control | Wires Out | Returns To |
|---|---|---|---|---|---|---|
| N1 | — | booking | Resolve server key: `server_index` → `server_number`, or the single-server key | call | — | → N4, N8 |
| N2 | P8 | booking | **Can book?** Member has `BOOKING_ROLE_ID`, or is a booking admin (N7) | call | yes → N3 · no → U9 | — |
| N3 | P8 | booking | **Parse and validate time:** `start` in `BOOKING_TIMEZONE` → UTC; `end = start + duration`; reject a past start or `end ≤ start` | call | ok → N1 → N4 · bad → U10 | — |
| N4 | P8 | `BookingService.create` | **Overlap check and insert** in one transaction (`BEGIN IMMEDIATE`): any live booking on the same key with `start < new_end AND end > new_start`? | call | overlap → U8 · free → S1 write → N12 | → U7 |
| N5 | P9 | `BookingService.list` | Live bookings (not cancelled, `end > now`), optionally for one server | call | — | → U12 |
| N6 | P10 | `BookingService.cancel` | Allowed only for the booker or a booking admin (N7). Sets `cancelled_at` and `cancelled_by` | call | ok → S1 write → N12 · denied/missing → U14 | → U14 |
| N7 | — | booking | **Is booking admin?** Discord Administrator permission, or `BOOKING_ADMIN_ROLE_ID` | call | — | → N2, N6, N9 |
| N8 | — | `BookingService.active_for` | Live booking on a key where `start ≤ now < end` | call | — | → N9, N10 |
| N9 | P1–P7 | **booking guard** | Decide whether this user may write to this server now: booking disabled → allow · no active booking (N8) → allow · user is the booker → allow · booking admin (N7) → allow · otherwise → deny | call | allow → N20–N27 · deny → U4 | — |
| N10 | P1 | panel | Build booking lines for each server (active via N8, next via N5) | call | — | → U1 |
| N11 | P1 | `build_panel` *(current, modified)* | Also call N10 when building the embed | call | → N10 | → P1 |
| N12 | — | `refresh_main_embed` *(current)* | Re-render the panel after a booking is created or cancelled | call | → N11 | — |
| N13 | — | startup | Open or create `bookings.db` and schema; read `BOOKING_*` env vars; set "booking enabled" (D3); start N14 when enabled | call | → S1, S2, N14 | → N2, N9, U15 |
| N14 | — | **panel boundary refresh** (B8) | `tasks.loop(60s)`: did any live booking's `start` or `end` fall in (previous run, now]? If yes → N12. Remembers the time of its previous run; on the first run after startup it compares against startup time | timer | crossed → N12 · none → (idle) | — |
| N20 | P2 | `VariantDropdown.callback` → `change_map` *(current)* | Write | call | → server | → U5 |
| N21 | P1 | `_apply_warfare_timer` → `set_match_timer` / `set_warmup_timer` *(current)* | Write (two buttons) | call | → server | → U5 |
| N22 | P3 | `lock_objectives` → `set_sector_layout` *(current)* | Write | call | → server | → U5 |
| N23 | P4 | `_set_dynamic_weather` → `set_dynamic_weather_enabled` *(current)* | Write | call | → server | → U5 |
| N24 | P5 | `TeamSwitchCooldownModal.on_submit` → `set_team_switch_cooldown` *(current)* | Write | call | → server | → U5 |
| N25 | P6 | `AddAdminCamModal.on_submit` → `_run_server_write(add_admin)` *(current)* | Write | call | → server | → U5 |
| N26 | P7 | `RemoveAdminCamConfirmView.confirm_yes` → `_run_server_write(remove_admin)` *(current)* | Write | call | → server | → U5 |

N20–N26 cover all **8 write sites**: N21 has two (match and warm-up timers) and the other six have one each. N9 must sit in front of every one of them; the realized-conformance check for R6 is "8 of 8 sites are guarded".

## Stores
| ID | Place | Store | Description |
|---|---|---|---|
| S1 | P11 | `bookings` table | `id`, `server_key`, `user_id`, `user_name`, `start_utc`, `end_utc`, `created_at`, `cancelled_at`, `cancelled_by`. Written by N4 and N6; read by N5 and N8. Survives restarts through the Docker bind mount |
| S2 | — | Booking config (env) | `BOOKING_ROLE_ID`, `BOOKING_ADMIN_ROLE_ID`, `BOOKING_TIMEZONE` (IANA name); read by N13 |
| S3 | — | Server config *(current)* | `api_client` server list; read by N1 and U6 choices |

## Behavior traces
| ID | Scenario | Trace | Observable consequence |
|---|---|---|---|
| T1 | Role member books a free slot | U6 → N2 ✓ → N3 ✓ → N1 → N4 free → S1 → N12 → N11 → N10 | U7, then U1 updates on the panel |
| T2 | Overlapping booking | U6 → N2 ✓ → N3 ✓ → N1 → N4 overlap | U8 names the conflicting booking; S1 unchanged |
| T3 | Back-to-back booking (new start = existing end) | … → N4: `existing.end > new.start` is false → free → S1 | U7 (D6) |
| T4 | Member without the role tries to book | U6 → N2 ✗ | U9 |
| T5 | Bad or past time | U6 → N2 ✓ → N3 ✗ | U10 |
| T6 | Another user changes the map during a booking | U2 → P2 → U3 (variant) → N9: N8 finds a booking; not the booker; N7 ✗ → deny | U4; **no CRCON/RCON call** |
| T7 | The booker writes during their booking | U3 → N9 → N8 finds a booking; user is the booker → N20–N26 | U5 (existing result) |
| T8 | An admin writes during someone else's booking | U3 → N9 → N7 ✓ → N20–N26 | U5 |
| T9 | Write with no active booking | U3 → N9 → N8 none → N20–N26 | U5 (behaves as today) |
| T10 | Flow opened before a booking started, finished after | U2 (before start) → … → U3 (after start) → N9 deny | U4 (D1) |
| T11 | Booker cancels | U13 → N6 ✓ → S1 → N12 | U14 "Cancelled"; U1 updates |
| T12 | Non-booker tries to cancel | U13 → N6 ✗ | U14 "Not yours" |
| T13 | Admin cancels any booking (override) | U13 → N6 → N7 ✓ → S1 → N12 | U14 "Cancelled"; U1 updates |
| T14 | Restart or redeploy | container restart → N13 opens S1 → U11 → N5 | U12 still lists future bookings; N9 still enforces |
| T15 | Users in different time zones view bookings | U11 → N5 → U12 using `<t:…>` tokens | Each user sees their local time (R2) |
| T16 | Booking not configured | N13: no `BOOKING_ROLE_ID` → disabled; U6/U11/U13 → U15; N9 → allow | Commands reply U15; panel works as today |
| T17 | A booking starts or ends with no one using the bot | N14 tick → reads S1 (N5/N8) → boundary crossed → N12 → N11 → N10 | U1 switches between "Next: …", "🔒 Booked …" and nothing within ≤60s. N9 has always allowed writes once `end` passes |
| T18 | Bot was down across a boundary | restart → N13 → N14 first run compares against startup time and boundaries are re-evaluated → N12 | U1 is correct after the first run (≤60s) |

### Reverse trace (every observable consequence → its sources)
| Consequence | Incoming wires | OK? |
|---|---|:---:|
| U1 | N10 ← N11 ← N12 ← N4 / N6 / **N14**; also ← existing refresh paths (Refresh Status, successful writes) | ✅ |
| U4 | N9 deny | ✅ |
| U5 | N20–N26 ← N9 allow | ✅ |
| U7 | N4 | ✅ |
| U8 | N4 | ✅ |
| U9 | N2 | ✅ |
| U10 | N3 | ✅ |
| U12 | N5 ← S1 | ✅ |
| U14 | N6 | ✅ |
| U15 | N13 (disabled) | ✅ |

## Requirement realization map
| Req | Selected part(s) | Breadboard IDs | Observable consequence |
|---|---|---|---|
| R0 | B1, B3 | U6, N3, N4, S1 | U7 |
| R1 | B1 | N4 (atomic overlap check), D6 | U8; back-to-back accepted (T3) |
| R2 | B3, B4 | U12, U1, D5, `<t:…>` tokens | T15 |
| R3 | B2 | S1, N13 | T14 |
| R4 | B1, B3, B5 | N6, N7, U13, U14, D4 | T11–T13 |
| R5.1 | B7′ | N2, U9, S2 | T4 |
| R6 | B6, B4, B8 | N9 at N20–N26, U4, U1, N14 | T6–T10, T17; 8 of 8 write sites guarded |

Every selected part is represented: B1 → N4, N5, N6, N8 · B2 → S1, N13 · B3 → P8–P10 · B4 → U1, N10 · B5 → N7 · B6 → N9 · B7′ → N2 · B8 → N14.

## Visual breadboard

Read left to right along the primary journey: book a slot, then try to change the server during it. Visible places are on top; system behavior is on the rail underneath.

```text
   P8  /book                      P1  Panel message                P2–P7  Any control flow
   ─────────────                  ───────────────────              ──────────────────────────
   U6 server/start/duration ──┐   U1 "🔒 Booked by @X              U2 button ──► … ──► U3 commit
                              │       until <t:end:t>"  ◄─ ─ ┐                          │
   U9  no role  ◄──── ✗ ──┐   │                              ┆                          │
   U10 bad time ◄──── ✗ ─┐│   │   U2 buttons (stay enabled,  ┆                          ▼
   U8  overlap  ◄──── ✗ ┐││   │      D2)                     ┆            ┌──── N9 booking guard ────┐
   U7  ✅ booked ◄─ ─ ─ ┐│││   │                              ┆            │ disabled? → allow        │
                        ┆│││   │                              ┆            │ no active (N8)? → allow  │
 ═══════════════════════┆┆┆┆═══╪══════════ system rail ═══════┆════════════│ booker? → allow          │═══
                        ┆│││   ▼                              ┆            │ admin (N7)? → allow      │
                        ┆││└─ N2 can book? ── ✓ ──► N3 parse ─┤            │ else → deny              │
                        ┆│└──────────────────────── ✗ ───────┘            └───┬───────────────┬──────┘
                        ┆│                    ✓                                │ allow         │ deny
                        ┆│                    ▼                                ▼               ▼
                        ┆└────── overlap ── N4 create (atomic) ── free ──► S1   N20–N26     U4 🔒 "booked by
                        └─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┘        │            bookings  existing       @X until …"
                                                       ▼               ▲      write → U5    (no CRCON call)
                                              N12 refresh panel ─► N11 ─► N10 ─ ─► U1       │
                                                                               ▲             │
                                                                               └── N8 active_for ◄── N9
   N14 every 60s: did a booking start or end? ── yes ──► N12 ─► N11 ─► N10 ─ ─► U1   (B8, display only)

   P9 /bookings:   U11 ─► N5 ─► S1 ─ ─► U12 list (<t:…> = viewer's local time)
   P10 /cancel:    U13 ─► N6 ─(N7 / booker?)─► S1 ─► N12 ─► U1 ;  ─ ─► U14
```

Solid arrows are control flow; dashed arrows are returns or visible consequences.

**Finger-trace check:** for T1, follow U6 → N2 → N3 → N4 → S1 → N12 → U1. For T6, follow U2 → U3 → N9 (deny) → U4. Both can be followed without reading the tables.

No targeted sketch is needed: every place is a Discord message, modal or command, so nothing depends on spatial layout.

## Shaping issue G1 — resolved 2026-10-16 (option 1 → B8, N14)

- **Selected shape says:** B4 — the panel shows each server's active booking ("Booked by @X until …").
- **Concrete behavioral implication:** the panel embed only re-renders on bot actions (Refresh Status, successful writes, booking create or cancel). When a booking starts or ends on its own, U1 keeps its old state: it may show "Next: …" after the booking has started, or "🔒 Booked" after it has ended. Discord's `<t:…:R>` relative times keep ticking, but the line's wording doesn't change.
- **Conflict or appetite risk:** U1 is part of R6's visible consequence ("who holds the server now"). The guard (N9) is still correct, so enforcement isn't affected — only the display.
- **Evidence:** the trace for T17; `refresh_main_embed` callers in `src/bot.py`.
- **Options:**
  1. **Revise the shape:** add B8, a minimal `tasks.loop` that refreshes the panel at the next booking start or end. It does display only — no presets, no server writes — so it stays well clear of shape C. Small; fits the appetite.
  2. **Cut the behavior:** U1 shows only time-stamped facts that can't go stale ("@X <t:start:t>–<t:end:t>"), with no "active" or "next" wording. Smallest option, but the display is weaker.
  3. Accept stale wording until the next refresh (mitigated by Refresh Status).
  4. Reopen selection (C already has a scheduler).
- **Decision:** option 1, chosen by the repo owner on 2026-10-16. Added B8 to the shaping record and N14 to this breadboard (T17, T18).

## Notes and open questions
- **Race between check and write:** a booking created between N9 allowing a write and the write running would let that one write through. This is accepted as below the level of concern for a 1–2 week bet.
- N9 reads SQLite on the event loop. Reads are sub-millisecond for this data size; switch to `asyncio.to_thread` if profiling ever shows otherwise.
- `/repost_button` and Change Server focus are not writes and stay ungated.
- Lock scope is bot-only (accepted uncertainty): CRCON's web UI and in-game admins are not blocked.
- Breadboard accepted 2026-10-16. Carry into slicing: add `data/*.db` to `.dockerignore` (from the persistence spike).
- Slices: [booking-slices.md](booking-slices.md) (accepted 2026-10-16).
