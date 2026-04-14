# Project: HLL CRCON Discord Automation

## Scope
- This repository is a Hell Let Loose Discord automation bot for CRCON-backed server control.
- The live app is a persistent Discord control panel that can change maps, switch focused servers, set objective layouts, toggle dynamic weather, manage admin cam access, and apply Warfare timer defaults.
- Railway deploys the bot with `python src/bot.py`.

## Active Entry Point
- `src/bot.py` -> main Discord bot runtime, persistent panel views, interaction handlers, and the `/repost_button` admin slash command.

## Live Service Modules
- `src/utils/crcon_http.py` -> authoritative CRCON HTTP client. Handles token auth, gamestate, map changes, objective layout changes, dynamic weather, timer updates, and admin access operations.
- `src/utils/hll_rcon.py` -> direct Hell Let Loose RCON v2 client for TCP fallback when CRCON HTTP is unavailable.
- `src/utils/api_client.py` -> server discovery and lightweight CRCON-backed server metadata wrapper used by the Discord layer.
- `src/utils/map_data.py` -> CRCON map catalog loading, normalization, cache file persistence, and fallback map data.
- `src/services/server_control.py` -> HTTP-first control service with direct RCON fallback for status reads, map changes, sector layout changes, dynamic weather, and timer updates.

## Supporting Modules
- `src/config/settings.py` -> older environment-variable loader; not the main runtime configuration path today.
- `src/commands/map_commands.py` -> legacy sample interaction code; not the primary command surface for the deployed bot.
- `src/handlers/button_handlers.py` -> legacy sample button handler code; not the primary handler path for the deployed bot.

## Discord Controls In Production
- Slash commands:
- `/repost_button` -> reposts the persistent control panel for admins.
- Persistent panel actions:
- `Change Server`
- `Refresh Status`
- `Change Map`
- `Set Objectives`
- `Dynamic Weather`
- `Warfare Match Timer`
- `Warfare Warm Up Timer`
- `Remove Admin Cam User`
- `Add Admin Cam Access`

## Interaction And Event Surfaces
- Discord lifecycle:
- `on_ready`
- Discord interaction flows:
- persistent button presses
- select menu callbacks
- modal submit callbacks
- slash command execution
- HLL/CRCON actions currently driven from Discord interactions:
- map change
- objective layout lock
- dynamic weather toggle
- warfare timer update
- spectator admin add/remove
- multi-server focus switching
- direct RCON fallback for selected control-plane actions when CRCON HTTP fails

## Current Architecture Rules
- Treat `src/bot.py` as the live orchestration layer until the bot is split further.
- Treat `src/utils/crcon_http.py` as the single source of truth for CRCON HTTP endpoints.
- Treat `src/utils/hll_rcon.py` as the single source of truth for direct game-server RCON transport details.
- Do not add raw `requests` calls directly inside Discord UI handlers when extending CRCON functionality.
- Do not duplicate CRCON auth, token, or endpoint logic outside `src/utils/crcon_http.py`.
- Do not duplicate direct RCON packet, XOR, or login logic outside `src/utils/hll_rcon.py`.
- Keep map catalog parsing and caching inside `src/utils/map_data.py`.
- Keep server discovery and server-name resolution inside `src/utils/api_client.py` or a future dedicated service module.
- Keep HTTP-first / direct-RCON-fallback orchestration inside `src/services/server_control.py`.
- Keep Discord responses admin-friendly and short. Panel interactions should acknowledge quickly before doing slow network work.
- Any new networked Discord interaction should be async-safe and avoid the `This interaction failed` pattern by deferring or responding immediately before slow work.

## Preferred Backend Direction For Future HLL Work
- New work should move toward this module layout:
- `src/commands/` -> slash commands and admin command cogs
- `src/events/` -> Discord event listeners and event routing
- `src/services/` -> CRCON communication, alerting, player state, rule enforcement, deduplication, cooldown logic
- `src/utils/` -> shared helpers only
- New HLL features should separate Discord concerns from game-server concerns.
- No direct CRCON business logic should live inside command definitions once service modules exist.
- New alerting logic should go through a dedicated alert service instead of being embedded in Discord handlers.

## Service Responsibilities To Preserve
- CRCON service responsibilities:
- authenticate with token-based CRCON HTTP
- read gamestate and map catalog
- mutate map, objective, weather, timer, and admin access state
- normalize and surface CRCON errors clearly
- Player/server state responsibilities:
- resolve configured servers from environment variables
- track focused server by Discord channel
- track current objective selections and last-known dynamic weather decisions
- cache map metadata and fall back safely if CRCON map loading fails
- Discord service responsibilities:
- maintain the persistent panel
- gate admin-only actions
- keep ephemeral interactions clean and non-spammy
- refresh the main status embed after successful state changes

## Operational Rules
- Use environment variables for all secrets and per-server configuration.
- Support both single-server and multi-server CRCON configuration.
- Assume CRCON APIs can timeout or return partial failures.
- Prefer explicit error messages over silent fallbacks.
- Log critical control-plane actions and cache refresh failures.
- Preserve idempotency where possible for repeated button clicks and duplicate interaction attempts.
- Handle missing or incomplete CRCON configuration without crashing unrelated Discord features.

## Event Patterns To Design For In Future HLL Development
- player join
- player leave
- admin ping
- rule violation
- duplicate event suppression
- cooldown-based alerting
- retry-safe transient CRCON/API failure handling

## Implementation Notes For Future Contributors
- If you add new Hell Let Loose automation, document the new command, event, and service path here.
- If you introduce a real `src/services/` package, migrate CRCON-facing business logic there instead of expanding `src/bot.py`.
- Legacy sample files in `src/commands/` and `src/handlers/` should not be treated as the deployed architecture unless they are actively wired into `src/bot.py`.
