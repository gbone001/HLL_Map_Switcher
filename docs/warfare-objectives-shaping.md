# Warfare Objectives Without CRCON — Shaping

## Working mode
- Profile: collaborative (S-first: the bundled table already exists)
- Current move: **selected and built** — see Decision
- Status: **selected** (shape D, revised), 2026-11-14

## Frame source
Session on 2026-11-14, branch `feat/server-booking` @ 8661b5e plus uncommitted CRCON read retry.
Live tests on both servers (US West Events, Aus/JAP Events Server) the same day.

- **x — current situation:** 🎯 Set Objectives builds its 5×3 picker only from CRCON
  `get_objective_rows` (`send_objective_selection` in `src/bot.py`). The read is retried once
  (`_crcon_read_with_retry`), then the user sees "Failed to load objectives". Without a CRCON HTTP
  client the picker refuses outright, and during a panel-level CRCON outage the button is disabled
  (`CRCON_PANEL_DEPENDENT_BUTTONS`). The *write* already falls back to RCON `SetSectorLayout`.
- **f() — solution variable:** where the picker gets the objective choices for the loaded map.
- **y — desired outcome:** on a warfare map, an authorised user can open the picker and lock a
  layout whenever the bot can identify the loaded map and reach the server by either transport.
- **gap:** the only source of choices is the least reliable call in the flow.
- **boundaries:**
  - AGENTS.md: RCON transport only in `src/utils/hll_rcon.py`; HTTP-first/RCON-fallback
    orchestration in `src/services/server_control.py`; map catalog data in `src/utils/map_data.py`.
  - Existing guards stay in force: `_deny_if_booked`, `_deny_if_map_changing` (incl. stale pickers),
    `_audit`.
  - Interactions acknowledge before slow work.
- **M — operating model (observed 2026-11-14):**
  - CRCON (`get_gamestate`, `get_objective_rows`) times out for 30 s+ or returns
    `500 BrokenPipeError` for about a minute around map changes; RCON on the same servers kept
    answering.
  - RCON v2 exposes the loaded map's choices via `GetClientReferenceData("SetSectorLayout")`
    (5 `Combo` params, comma-separated `valueMember`) and the loaded layer via the session `mapId`.
    It cannot list choices for maps that are not loaded.
  - Objectives are per map, not per time-of-day layer (verified Remagen day = night; assumed for
    the rest).
  - Game updates add maps and occasionally rename strongpoints; the table was collected once.
  - Both production servers now have RCON configured.

## Project language and decisions
- Terms: *layer* = map ID such as `REM_L_1945_WarfareNight`; *sector* = one of five rows;
  *objective* / *strongpoint* = one of three choices in a sector; *layout* = five chosen objectives.
- Existing seams: `ServerControlService.get_status` (`GameStatus.map_id` from CRCON or RCON),
  `ServerControlService.set_sector_layout` (HTTP → RCON), `HLLRCONClient.execute`,
  `utils/map_data.py` (map catalog + cache).
- Data artifact: `src/data/warfare_objectives.json` (20 maps, 44 warfare layers, exact game
  strings; `PHL` "ROAD TO CARENTAN " keeps a trailing space). Lives under `src/` because Railway
  mounts a volume over `/app/data`.

## Requirements
| ID | Requirement | Status | Authority | Origin | Evidence refs |
|---|---|---|---|---|---|
| R0 | On a warfare map, the picker opens and a layout can be locked when CRCON objective reads fail (timeout, 500, not configured), provided the loaded map can be identified and the server reached | Core goal | Working | FROM_GAP | live tests 2026-11-14 |
| R1 | Choices offered are exactly the strings the game accepts for the **currently loaded** map; never another map's | Must-have | Working | FROM_BOUNDARY | trailing-space case; stale-picker guard |
| R2 | When the source of choices is wrong or out of date (game patch), the bot fails safely and the drift is visible to maintainers | Must-have | Working | FROM_M | table collected once |
| R3 | Offensive and skirmish behave no worse than today | Must-have | Working | FROM_BOUNDARY | — |
| R4 | Booking lock, map-change hold, stale-picker refusal and audit apply unchanged | Must-have | Working | FROM_BOUNDARY | AGENTS.md |
| R5 | The 🎯 button is not disabled when the picker could actually work | Nice-to-have | Working | FROM_X | `CRCON_PANEL_DEPENDENT_BUTTONS` |
| R6 | Opening the picker stays fast in the common case | Nice-to-have | Working | FROM_Y | — |
| R7 | Refreshing the data after a game update is a repeatable, documented step | Nice-to-have | Working | FROM_M | collector script in temp dir today |

## Appetite
- Authority: **Working** (needs acceptance)
- Budget: one focused session (≈ half a day) including tests; no Discord UI redesign.
- Cut line: R5 and R7 are cut first.
- Accepted uncertainty: day/night parity assumed beyond Remagen.
- Must-resolve unknowns: S1 below (Discord and trailing-space values) before build.
- Revisit if: the game stops exposing `GetClientReferenceData`, or CRCON gains a reliable per-map
  objectives endpoint.

## Shapes

### CURRENT: CRCON-only choices
| Part | Mechanism | Flag |
|---|---|:---:|
| C1 | `get_objective_rows` via `_crcon_read_with_retry` | |
| C2 | picker refused when no CRCON client; button disabled in panel CRCON outage | |
| C3 | layout write CRCON → RCON fallback | |

### A: Bundled table as fallback
| Part | Mechanism | Flag |
|---|---|:---:|
| A1 | `map_data` loads `src/data/warfare_objectives.json` once; index by layer ID | |
| A2 | Picker: CRCON rows first (as now); on failure or no client, read `map_id` from `get_status` and use the table row for that layer | |
| A3 | Unknown or non-warfare layer → today's error message | |
| A4 | Lock failure from the game ("invalid sector") logged as table drift with the layer ID | |

### B: Live RCON reference data as fallback (no table at runtime)
| Part | Mechanism | Flag |
|---|---|:---:|
| B1 | `HLLRCONClient.get_sector_options()` parses `GetClientReferenceData("SetSectorLayout")` | |
| B2 | `ServerControlService.get_objective_rows(i)`: CRCON → RCON (same pattern as status) | |
| B3 | Picker uses B2; works for every game mode RCON reports sectors for | ⚠️ |
| B4 | Table kept only as a test fixture/reference | |

### C: Live sources first, table last (hybrid)
| Part | Mechanism | Flag |
|---|---|:---:|
| C-1 | B1 + B2 (CRCON → live RCON) | |
| C-2 | If both live reads fail and `map_id` is a warfare layer in the table, use A1 rows | |
| C-3 | Whenever a live read succeeds for a warfare layer, compare with the table; log mismatch once per layer (drift signal) | |
| C-4 | `scripts/collect_warfare_objectives.py` regenerates the table (the 2026-11-14 collector, made repeatable) | |
| C-5 | Panel keeps 🎯 enabled during CRCON outage when RCON status succeeded | |

### D: Table primary for warfare
| Part | Mechanism | Flag |
|---|---|:---:|
| D1 | Warfare layer in table → no network read for choices at all | |
| D2 | Other layers → CRCON (as now) | |
| D3 | A4 drift logging on lock failure | |

## Candidate evidence
| Candidate | Evidence | Question resolved | R/S implication | Appetite implication | Remaining uncertainty |
|---|---|---|---|---|---|
| B, C | probe 2026-11-14 on Aus/JAP: `GetClientReferenceData("SetSectorLayout")` returned Remagen's 5×3 exactly as CRCON | live RCON can supply choices for the loaded map | B satisfies R1 without a table | small: one parser + one service method | output for offensive/skirmish layers not yet seen (flag B3) |
| A, C, D | 20-map collection, 62–64 s per load, every map 5×3, all different | table is complete for current warfare layers | supports A/C/D for R0 | none | parity beyond Remagen assumed |
| all | CRCON timeouts/500s observed while RCON answered | M confirmed | R0 is real | — | — |

## Fit check
- Authority: **Working** (R and appetite not yet accepted)

| Req | Requirement | Status | CURRENT | A | B | C | D |
|---|---|---|:---:|:---:|:---:|:---:|:---:|
| R0 | Works on warfare when CRCON objective reads fail | Core goal | ❌ | ✅ | ✅ | ✅ | ✅ |
| R1 | Exactly the loaded map's accepted strings | Must-have | ✅ | ✅ | ✅ | ✅ | ❌ |
| R2 | Fails safely, drift visible | Must-have | ✅ | ❌ | ✅ | ✅ | ❌ |
| R3 | Offensive/skirmish no worse | Must-have | ✅ | ✅ | ✅ | ✅ | ✅ |
| R4 | Guards unchanged | Must-have | ✅ | ✅ | ✅ | ✅ | ✅ |
| R5 | Button not disabled when picker could work | Nice-to-have | ❌ | ❌ | ❌ | ✅ | ❌ |
| R6 | Fast common case | Nice-to-have | ✅ | ✅ | ✅ | ✅ | ✅ |
| R7 | Repeatable data refresh | Nice-to-have | — | ❌ | — | ✅ | ❌ |

Notes:
- **A × R2 ❌:** a stale table offers choices the game rejects; A4 only notices after a user tries to
  lock, and a *renamed-but-similar* list would be offered silently.
- **D × R1 ❌:** D trusts the table over the live server even when CRCON/RCON are healthy, so a
  patch that changes a map's objectives would serve wrong choices until someone regenerates the
  file. D is the only shape that makes the table authoritative.
- **B × R0 ✅** depends on RCON being configured — true in production today. If RCON is not
  configured and CRCON reads fail, B behaves like CURRENT; A and C would still work in that case
  when `map_id` is known from CRCON status.
- **C × R2 ✅** because the table is only a last resort and C-3 compares it to live data on every
  successful live read.

## Reverse fit check (C, the most complete candidate)
| Shape part | Mechanism | Requirement(s) served | Justified? |
|---|---|---|:---:|
| C-1 | CRCON → live RCON choices | R0, R1, R3 | ✅ |
| C-2 | table as last resort | R0 (CRCON reads down + RCON unavailable) | ✅ narrow; only case B misses |
| C-3 | live vs table comparison logging | R2 | ✅ |
| C-4 | regeneration script | R7 | ✅ (cut first) |
| C-5 | keep 🎯 enabled on RCON-healthy outage | R5 | ✅ (cut first) |

Observation: with RCON configured, C-2 is rarely reached; most of C's value comes from C-1 (= B).
The table's durable value in C is as a drift reference and last-resort fallback.

## Appetite fit (working)
| Shape | Fits? | Required cuts | Uncertainty / spike |
|---|:---:|---|---|
| A | ✅ | — | R2 gap remains |
| B | ✅ | — | B3: non-warfare RCON output |
| C | ✅ if C-4/C-5 cut, else tight | C-4, C-5 | S1, B3 |
| D | ✅ | — | R1/R2 failures |

## Spikes needed before build
- **S1 — trailing whitespace through Discord:** does a `SelectOption` value of
  `"ROAD TO CARENTAN "` come back unchanged in `Select.values`? If Discord trims it, map values to
  indices instead of names (affects every shape that sends table strings).
- **S2 (only for B/C non-warfare):** what `GetClientReferenceData("SetSectorLayout")` returns on an
  offensive and a skirmish layer (needs one map load each).

## Omission check
Mechanism families considered: live CRCON (CURRENT), live RCON (B), static table (A/D), hybrid (C).
Also considered and rejected: caching the last successful CRCON rows per layer at runtime — it
helps only for layers already opened since start-up and adds state without beating B.
No other credible source of per-map objectives was found (RCON cannot list unloaded maps; CRCON's
API only returns the loaded map).

## Decision
- Status: **selected by the repo owner, 2026-11-14**
- Chosen direction: **D (table primary for warfare), revised** with the owner's constraint that the
  choices shown are *only* ever for the live map.
- Owner's rationale: warfare maps and their objectives are locked and stationary, so the bundled
  table is authoritative for warfare. This accepts the R1/R2 risk recorded above for D (a game patch
  that renames objectives needs the table regenerated).
- Parts as built:
  - D1 — warfare layer in the table → choices from `src/data/warfare_objectives.json`
    (`map_data.get_warfare_objectives`); no CRCON objectives read.
  - D2 — other layers → CRCON `get_objective_rows` with one retry (as before).
  - D4 (new, from the owner's constraint) — the layer ID comes from a fresh `get_status`
    (CRCON → RCON); if it can't be read, no choices are shown.
  - D5 (new) — before sending, the picker re-reads the live layer and refuses if it changed
    (covers map changes the bot didn't make).
  - D6 (new) — panel footer tells users to switch to a map first, then set its objectives.
  - S1 resolved by design: dropdown values are positions, so trailing spaces survive.
  - The 🎯 handler no longer requires CRCON to be configured.
- Cut / not built: A4/D3 drift logging on a rejected lock, C-3 live-vs-table comparison, C-4
  regeneration script, C-5 button enabled during a panel CRCON outage.
- Remaining unknowns: day/night parity beyond Remagen (assumed); the panel still disables 🎯
  during a full CRCON outage even though the table + RCON could serve it.

## Next useful move
- Optional follow-ups: keep 🎯 enabled when RCON status works (C-5); commit the collector as a
  script (C-4).
