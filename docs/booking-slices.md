---
planning: true
shaping: true
---

# Server Booking — Slices (shape B)

## Source and authority
- Slices are drawn from the **accepted** selected-design breadboard [booking-breadboard.md](booking-breadboard.md) (shape B: B1–B6, B7′, B8).
- Requirements in scope: R0–R4, R5.1, R6 (accepted). Appetite: 1–2 weeks (accepted).
- **Status: accepted** 2026-10-16 — sequence V1 → V2 → V3 → V4. The week-1 review point is now "V1 + V2 demoed".
- No code; this is a planning artifact only.

## Affinity groups (raw material)
| Group | Breadboard elements | Biggest unknown |
|---|---|---|
| Booking core | S1, S2, N13, N1, N2, N3, N4, U6–U10, U15 | Time parsing in `BOOKING_TIMEZONE` across daylight-saving changes |
| Visibility | P9, N5, U11, U12; U1, N10, N11, N12 | — (routine) |
| Lock | N7, N8, N9 in front of N20–N26, U4 | Guarding all 8 write sites at commit time (D1) without missing one |
| Boundary refresh | N14 | Loop timing, missed boundaries, catching up after a restart |
| Cancel / override | P10, U13, U14, N6 | — (routine) |

## Slices

Each element is assigned to the first slice that needs it. A wire to an element in a later slice is allowed when it describes the finished system and that element isn't built yet.

### V1 — Book a slot, see it, and keep it across restarts
- **Mechanisms:**
  - S1, S2, N13 (the store, config, and the booking-enabled flag; N14 is not started yet).
  - `/book`: N1, N2, N3, N4 including the overlap branch, plus U6, U7, U8, U9, U10.
  - `/bookings`: P9, N5, U11, U12.
  - U15, shown when booking is disabled.
  - Add `data/*.db` to `.dockerignore`.
- **Why overlap rejection is in V1, not a later slice:** V2's guard (N8) assumes **at most one active booking per server**. Working right to left, V1 must deliver that rule, not a store that allows overlaps.
- **Demo:**
  1. A member with `BOOKING_ROLE_ID` runs `/book Server1 2026-10-20 19:00 2h` → ✅ with ID #1.
  2. A second user books 20:00–21:00 on the same server → ❌ "Overlaps @X's booking" (U8).
  3. A third user books 21:00–22:00 → accepted (back-to-back, D6).
  4. A member without the role → U9.
  5. `/bookings` shows times in each viewer's local time zone (two accounts in different zones).
  6. `docker compose down && docker compose up -d` → `/bookings` still lists everything.
  7. Remove `BOOKING_ROLE_ID` → the commands reply U15, and the panel still works as before.
- **Requirements demonstrated:** R0, R1, R2 (in the `/bookings` list), R3, R5.1.
- **Produces:** live bookings in S1 with no overlaps, plus `BookingService.create` and `list`, the server-key resolver N1, and the booking-enabled flag. V2 needs these to find "the active booking" for a server.

### V2 — Lock the server during a booking
- **Mechanisms:**
  - N8 (`active_for`) and N7 (is booking admin).
  - N9, the booking guard, wired in front of **all 8 write sites**: N20 change map, N21 match timer, N21 warm-up timer, N22 objectives, N23 weather, N24 team switch cooldown, N25 add admin cam, N26 remove admin cam.
  - U4 denial message.
- **Demo** (with a booking active now for user A):
  1. User B tries each of the 8 writes → U4 every time, and **no CRCON/RCON call** is made (visible in the logs).
  2. User A does a map change → it works (U5).
  3. An admin (Administrator permission or `BOOKING_ADMIN_ROLE_ID`) changes objectives → it works.
  4. User B opens the map flow before a booking starts and picks the variant after it starts → U4 (T10).
  5. After the booking ends, user B's write works.
- **Requirements demonstrated:** R6 (enforcement), R4 (the admin-bypass half).
- **Produces:** N8 `active_for` and N7 `is_booking_admin` as reusable checks. V3 needs N8 to render "🔒 Booked by …", and V4 needs N7 to authorise admin cancels.

### V3 — Panel shows bookings and stays current
- **Mechanisms:**
  - U1 booking lines and N10.
  - N11 extended to include them.
  - N12 called after `/book` succeeds (wired from N4).
  - N14 panel boundary refresh (B8, D8).
- **Demo:**
  1. Book a slot starting in 2 minutes → the panel shows "Next: @A <t:…> (in 2 minutes)".
  2. Wait without touching the bot → within 60s of the start, the panel shows "🔒 Booked by @A until …".
  3. Within 60s of the end, the line clears.
  4. Stop the container across a booking's start, then restart → the panel is correct after the first loop run (T18).
- **Requirements demonstrated:** R6 (who holds the server, visibly), R2 (on the panel).
- **Produces:** N12 refresh after a booking change, available to any later booking mutation. V4's cancel calls it so the panel updates immediately.

### V4 — Cancel, and admin override
- **Mechanisms:** P10 `/cancel_booking`, U13 (autocomplete: your own bookings; admins see all), N6, U14, and N12 called after a cancel.
- **Demo:**
  1. User A cancels #1 → "Cancelled", and the panel updates straight away.
  2. User B tries to cancel A's booking → "Not yours".
  3. An admin cancels B's active booking → "Cancelled", the lock lifts, and the panel updates (T13).
  4. Cancelling a booking that has already ended → "Not found or already ended".
- **Requirements demonstrated:** R4 (complete).
- **Produces:** nothing further — this completes the bet's scope above the cut line.

## Sequencing

### Pass 1 — dependencies
| Slice | Depends on | Reason |
|---|---|---|
| V1 | — | |
| V2 | V1 | N8 reads S1 and relies on V1's no-overlap rule |
| V3 | V1, V2 | U1 uses N8 from V2; N14 reads S1 |
| V4 | V1, V2 (V3 for the panel update) | N6 uses N7 from V2; calls N12 from V3 |

Valid orders: V1 → V2 → then V3 or V4.

### Pass 2 — unknowns
- V1 is the only valid starting point. Its unknown (time parsing across daylight saving) is small and contained.
- After V1, V2 carries the **biggest unknown**: guarding all 8 write sites at commit time and confirming none bypasses the guard. Do it second.
- Of V3 and V4, V3 carries the next unknown (loop timing and catching up after a restart). V4 is routine. So V3 comes before V4. That also lets V4 call N12 directly, so it doesn't have to be wired back in later.

### Final sequence
**V1 → V2 → V3 → V4.** Four slices, under the nine-slice limit, so nothing needs cutting.

## Requirement coverage by slice
| Req | V1 | V2 | V3 | V4 |
|---|:---:|:---:|:---:|:---:|
| R0 Reserve a slot | ✅ | | | |
| R1 No overlaps | ✅ | | | |
| R2 Visible in local time | ✅ (list) | | ✅ (panel) | |
| R3 Survives restart | ✅ | | ✅ (catch-up) | |
| R4 Cancel and override | | ◐ admin bypass | | ✅ |
| R5.1 Booking role | ✅ | | | |
| R6 Lock during slot | | ✅ enforce | ✅ display | |

Every accepted requirement is complete by the end of V4.

## Decision — review point vs. sequence (resolved 2026-10-16)
- **Accepted appetite says:** the end-of-week-1 review should show bookings created, listed, **cancelled**, and persisted (R0–R4).
- **The unknown-first sequence** puts cancel last (V4), so at the end of week 1 the expected demo is V1 + V2 (book, list, persist, **lock**), not cancel.
- **Options:**
  1. **Keep V1 → V2 → V3 → V4** and change the week-1 review to "V1 + V2 demoed". This puts the riskiest work first. (Recommended.)
  2. **Reorder to V1 → V4 → V2 → V3**, keeping the original review point. V4 would then build N7 itself and add the N12 call later, in V3. The lock risk is found later.
- **Decision:** option 1, chosen by the repo owner. The sequence is kept, and the shaping record's appetite review point is now "end of week 1: V1 + V2 demoed". Cancel (V4) lands in week 2.

## Cut
None. The four slices fit within the accepted appetite. R5.2 (per-user limits), R7 and R8 stay out of this bet, as recorded in the shaping record.
