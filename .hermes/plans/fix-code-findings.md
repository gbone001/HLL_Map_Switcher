# Fix Code Findings Plan

## Baseline
- **Repository**: HLL CRCON Discord Automation
- **Tests**: 90 passing
- **Files changed**: 13 Python files in `src/`
- **Goal**: Fix all bugs and style issues without breaking tests

---

## Findings Categorized

### 🔴 Critical Bugs (Must Fix)
| ID | File | Line | Issue |
|----|------|------|-------|
| B1 | `src/handlers/button_handlers.py` | 24,29,33,37 | Undefined `discord` — dead code not wired into bot |
| B2 | `src/bot.py` | 260-261 | Unused `now`, `max_age_seconds` in `_cleanup_state_caches()` |
| B3 | `src/bot.py` | 1867 | `list[str \| None]` passed where `Iterable[str]` expected |
| B4 | `src/utils/api_client.py` | 148 | Unused `exc` in except block |

### 🟡 Type Issues (mypy)
| ID | File | Issue |
|----|------|-------|
| T1 | `src/services/booking.py` | Dataclass constructor type mismatch |
| T2 | `src/bot.py:889,933` | Modal field type annotations missing |
| T3 | `src/bot.py:1231` | Incompatible assignment `ServerSelectionView` → `GameModeSelectionView` |
| T4 | `src/bot.py:1520` | Union attr access on channel type |
| T5 | Various | Import resolution (modules not on mypy path) |

### 🟢 Style Issues (flake8 ~400 violations)
| ID | Category | Files Affected |
|----|----------|----------------|
| S1 | E501 Line too long (>79) | All 13 files |
| S2 | W293 Blank line with whitespace | All 13 files |
| S3 | E302/E303/E305 Blank line counts | All 13 files |
| S4 | F401/F841 Unused imports/vars | Multiple files |

---

## Task Breakdown

### Phase 1: Critical Bugs (4 tasks, ~30 min)

#### Task 1.1: Remove dead code `button_handlers.py`
- **File**: `src/handlers/button_handlers.py`
- **Action**: Delete file (not wired into bot.py, legacy sample per AGENTS.md)
- **Verify**: Tests still pass, no import errors

#### Task 1.2: Fix `_cleanup_state_caches()` unused variables
- **File**: `src/bot.py` lines 258-261
- **Action**: Either implement cache cleanup logic or remove unused vars
- **Verify**: Tests pass, no F841 warnings

#### Task 1.3: Fix `list[str \| None]` type error at line 1867
- **File**: `src/bot.py` line 1867
- **Action**: Filter `None` from `objective_list` before `list()` call
- **Verify**: Tests pass, mypy error resolved

#### Task 1.4: Fix unused `exc` in api_client.py
- **File**: `src/utils/api_client.py` line 148
- **Action**: Change `except Exception as exc:` to `except Exception:`
- **Verify**: Tests pass, flake8 F841 resolved

---

### Phase 2: Type Fixes (5 tasks, ~45 min)

#### Task 2.1: Fix BookingConfig dataclass constructor types
- **File**: `src/services/booking.py` lines 123, 140
- **Action**: Explicitly type the `**base` dict or construct with named args
- **Verify**: mypy passes on booking.py

#### Task 2.2: Add modal field type annotations
- **File**: `src/bot.py` lines 889 (`player_id`), 933 (`cooldown_minutes`)
- **Action**: Add `discord.ui.TextInput` type annotations
- **Verify**: mypy var-annotated errors resolved

#### Task 2.3: Fix view assignment type mismatch
- **File**: `src/bot.py` line 1231
- **Action**: Ensure consistent view type or add type ignore
- **Verify**: mypy assignment error resolved

#### Task 2.4: Fix union attr access on channel
- **File**: `src/bot.py` line 1520
- **Action**: Add type guard or cast before `.fetch_message()`
- **Verify**: mypy union-attr error resolved

#### Task 2.5: Configure mypy path for local imports
- **File**: `mypy.ini` or `pyproject.toml`
- **Action**: Add `src` to `MYPYPATH` or use `[tool.mypy]` config
- **Verify**: Import-not-found errors resolved

---

### Phase 3: Style Cleanup (1 task, automated)

#### Task 3.1: Run black + ruff to auto-fix style
- **Action**: 
  ```bash
  .venv/Scripts/python -m black src/
  .venv/Scripts/python -m ruff check --fix src/
  ```
- **Verify**: flake8 passes, tests still pass
- **Note**: This fixes S1-S4 in one pass

---

### Phase 4: Verification (1 task)

#### Task 4.1: Full regression test
- **Action**: Run all tests, mypy, flake8
- **Verify**: 
  - 90 tests pass
  - mypy: 0 errors (or only external stub warnings)
  - flake8: 0 violations

---

## Dependencies

```
Phase 1 (Critical) ──────► Phase 2 (Types) ──────► Phase 3 (Style) ──────► Phase 4 (Verify)
   │                           │                       │                      │
   ▼                           ▼                       ▼                      ▼
B1-B4                        T1-T5                    S1-S4                  All clean
```

---

## Non-Goals
- No architectural changes
- No new features
- No config/schema changes
- No test modifications (tests already pass)

---

## Rollback
- Each task is a single-file change or auto-format
- Git commit per phase for easy revert
- `git stash` before auto-format if needed

---

## Acceptance Criteria
- [ ] All 90 tests pass
- [ ] `mypy src/` shows 0 errors on project code (external stub warnings OK)
- [ ] `flake8 src/` shows 0 violations
- [ ] No dead code remains
- [ ] No unused variables/imports