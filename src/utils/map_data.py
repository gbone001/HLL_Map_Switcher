from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from pathlib import Path
import json
from typing import Dict, List, Optional

from .crcon_http import CRCONHttpClient, CRCONHTTPError

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 300
_FILE_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60  # one week
_map_cache: Dict[str, Dict[str, List[Dict[str, str]]]] = {}
_cache_timestamp: float = 0.0
_cache_lock = threading.Lock()
_last_error: Optional[str] = None
MAP_CACHE_FILE = (Path(__file__).resolve().parents[2] / "data" / "map_cache.json").resolve()

_ENV_LABELS = {
    "day": "Day",
    "night": "Night",
    "dusk": "Dusk",
    "dawn": "Dawn",
    "morning": "Dawn",
    "evening": "Evening",
    "overcast": "Overcast",
    "rain": "Rain",
    "storm": "Storm",
    "snow": "Snow",
    "fog": "Fog",
}

_FACTION_LABELS = {
    "us": "US",
    "usa": "US",
    "ger": "GER",
    "deu": "GER",
    "gb": "GB",
    "gbr": "GB",
    "rus": "RUS",
    "sov": "RUS",
    "cwu": "CW",
    "cw": "CW",
    "axis": "Axis",
    "allies": "Allies",
}


def _env_label(environment: Optional[str]) -> str:
    if not environment:
        return "Standard"
    normalized = environment.lower()
    return _ENV_LABELS.get(normalized, normalized.replace("_", " ").title())


def _attacker_label(attacker: Optional[str]) -> str:
    if not attacker:
        return "Attack"
    normalized = attacker.lower()
    return _FACTION_LABELS.get(normalized, normalized.upper())


def _variant_from_entry(entry: Dict[str, any]) -> str:
    mode = (entry.get("game_mode") or "").lower()
    environment = _env_label(entry.get("environment"))

    if mode == "offensive":
        attacker = _attacker_label(entry.get("attackers"))
        if environment not in ("Standard", "Day"):
            return f"{attacker} Attack ({environment})"
        return f"{attacker} Attack"

    if environment == "Standard":
        # Fall back to parsing the pretty name (e.g. "Foy Warfare (Night)").
        pretty_name = entry.get("pretty_name", "")
        base_name = entry.get("map", {}).get("pretty_name", "")
        if base_name and pretty_name.startswith(base_name):
            suffix = pretty_name[len(base_name):].strip(" -")
            if suffix:
                suffix = suffix.replace(entry.get("game_mode", "").title(), "").strip(" -()")
                if suffix:
                    return suffix.title()
        return "Standard"

    return environment


def _load_cache_file() -> Optional[Dict[str, Dict[str, List[Dict[str, str]]]]]:
    if not MAP_CACHE_FILE.exists():
        return None

    try:
        global _cache_timestamp
        data = json.loads(MAP_CACHE_FILE.read_text(encoding="utf-8"))
        maps = data.get("maps")
        timestamp = float(data.get("updated_at", 0.0))

        if isinstance(maps, dict):
            for mode, mode_maps in list(maps.items()):
                if not isinstance(mode_maps, dict):
                    return None
                for map_name, variants in list(mode_maps.items()):
                    if not isinstance(variants, list):
                        return None
                    for variant in variants:
                        if not isinstance(variant, dict) or "id" not in variant or "variant" not in variant:
                            return None
            global _cache_timestamp
            _cache_timestamp = timestamp
            return maps
    except Exception as exc:  # pragma: no cover - defensive logging
        logger.warning("Failed to read cached map file %s: %s", MAP_CACHE_FILE, exc)
    return None


def _write_cache_file(maps: Dict[str, Dict[str, List[Dict[str, str]]]]) -> None:
    try:
        MAP_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": time.time(),
            "maps": maps,
        }
        MAP_CACHE_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:  # pragma: no cover - defensive logging
        logger.warning("Failed to write map cache file %s: %s", MAP_CACHE_FILE, exc)


def _build_cache(force_refresh: bool = False) -> None:
    global _map_cache, _cache_timestamp, _last_error

    with _cache_lock:
        now = time.time()
        if not force_refresh and _map_cache and now - _cache_timestamp < _CACHE_TTL_SECONDS:
            return

        file_maps: Optional[Dict[str, Dict[str, List[Dict[str, str]]]]] = None
        if not _map_cache:
            file_maps = _load_cache_file()
            if file_maps:
                _map_cache = file_maps

        previous_maps: Optional[Dict[str, Dict[str, List[Dict[str, str]]]]] = None
        if _map_cache:
            previous_maps = json.loads(json.dumps(_map_cache))
        elif file_maps:
            previous_maps = json.loads(json.dumps(file_maps))

        file_age = now - _cache_timestamp if _cache_timestamp else _FILE_CACHE_TTL_SECONDS + 1
        needs_refresh = force_refresh or not _map_cache or file_age > _FILE_CACHE_TTL_SECONDS

        if not needs_refresh and _map_cache:
            return

        try:
            client = CRCONHttpClient.from_env()
            response = client.get_maps()
            entries = response.get("result") or []
            if not entries:
                raise CRCONHTTPError("CRCON get_maps returned no map entries.")

            structured: Dict[str, Dict[str, List[Dict[str, str]]]] = defaultdict(dict)

            for entry in entries:
                mode = (entry.get("game_mode") or "").lower()
                if mode not in {"warfare", "offensive", "skirmish"}:
                    continue

                map_meta = entry.get("map") or {}
                map_name = map_meta.get("pretty_name") or map_meta.get("name")
                map_id = entry.get("id")
                if not map_name or not map_id:
                    continue

                variant_label = _variant_from_entry(entry)
                variants = structured.setdefault(mode, {}).setdefault(map_name, [])

                if not any(v["variant"] == variant_label for v in variants):
                    variants.append({"id": map_id, "variant": variant_label})

            # Sort map names and variants for deterministic menus.
            ordered_maps: Dict[str, Dict[str, List[Dict[str, str]]]] = {}
            for mode, maps in structured.items():
                ordered_maps[mode] = {
                    map_name: sorted(variants, key=lambda v: (v["variant"], v["id"]))
                    for map_name, variants in sorted(maps.items(), key=lambda item: item[0])
            }

            if not ordered_maps:
                raise CRCONHTTPError("CRCON get_maps did not return any supported game modes.")

            if previous_maps:
                old_layers = {
                    (mode, map_name, variant["id"])
                    for mode, maps in previous_maps.items()
                    for map_name, variants in maps.items()
                    for variant in variants
                }
                new_layers = {
                    (mode, map_name, variant["id"])
                    for mode, maps in ordered_maps.items()
                    for map_name, variants in maps.items()
                    for variant in variants
                }
                added = new_layers - old_layers
                removed = old_layers - new_layers
                if added or removed:
                    logger.info(
                        "CRCON map list updated. Added: %s Removed: %s",
                        sorted(added),
                        sorted(removed),
                    )

            _map_cache = ordered_maps
            _cache_timestamp = now
            _last_error = None
            _write_cache_file(_map_cache)

        except CRCONHTTPError as exc:
            _last_error = str(exc)
            logger.warning("Failed to refresh map cache via CRCON API: %s", exc)
        except Exception as exc:  # pragma: no cover - defensive logging
            _last_error = str(exc)
            logger.exception("Unexpected error while refreshing map cache via CRCON API")

        if not _map_cache and file_maps:
            _map_cache = file_maps


def refresh_map_cache(force: bool = False) -> None:
    """Fetch the latest map data from the CRCON HTTP API."""
    _build_cache(force_refresh=force)


def get_last_map_cache_error() -> Optional[str]:
    """Return the most recent error encountered while refreshing the cache."""
    return _last_error


def _active_maps(force_refresh: bool = False) -> Dict[str, Dict[str, List[Dict[str, str]]]]:
    refresh_map_cache(force=force_refresh)
    if _map_cache:
        return _map_cache
    return LEGACY_MAPS_DATA

# Map data organized by game mode and map name
LEGACY_MAPS_DATA = {
    "warfare": {
        "Carentan": [
            {"id": "carentan_warfare", "variant": "Day"},
            {"id": "carentan_warfare_night", "variant": "Night"}
        ],
        "Driel": [
            {"id": "driel_warfare", "variant": "Day"},
            {"id": "driel_warfare_night", "variant": "Night"}
        ],
        "El Alamein": [
            {"id": "elalamein_warfare", "variant": "Day"},
            {"id": "elalamein_warfare_night", "variant": "Dusk"}
        ],
        "Elsenborn Ridge": [
            {"id": "elsenbornridge_warfare_day", "variant": "Day"},
            {"id": "elsenbornridge_warfare_morning", "variant": "Dawn"},
            {"id": "elsenbornridge_warfare_night", "variant": "Night"}
        ],
        "Foy": [
            {"id": "foy_warfare", "variant": "Day"},
            {"id": "foy_warfare_night", "variant": "Night"}
        ],
        "Hill 400": [
            {"id": "hill400_warfare", "variant": "Day"}
        ],
        "Hurtgen Forest": [
            {"id": "hurtgenforest_warfare_V2", "variant": "Day"},
            {"id": "hurtgenforest_warfare_V2_night", "variant": "Night"}
        ],
        "Kharkov": [
            {"id": "kharkov_warfare", "variant": "Day"},
            {"id": "kharkov_warfare_night", "variant": "Night"}
        ],
        "Kursk": [
            {"id": "kursk_warfare", "variant": "Day"},
            {"id": "kursk_warfare_night", "variant": "Night"}
        ],
        "Mortain": [
            {"id": "mortain_warfare_day", "variant": "Day"},
            {"id": "mortain_warfare_dusk", "variant": "Dusk"},
            {"id": "mortain_warfare_overcast", "variant": "Overcast"}
        ],
        "Omaha Beach": [
            {"id": "omahabeach_warfare", "variant": "Day"},
            {"id": "omahabeach_warfare_night", "variant": "Dusk"}
        ],
        "Purple Heart Lane": [
            {"id": "PHL_L_1944_Warfare", "variant": "Rain"},
            {"id": "PHL_L_1944_Warfare_Night", "variant": "Night"}
        ],
        "Remagen": [
            {"id": "remagen_warfare", "variant": "Day"},
            {"id": "remagen_warfare_night", "variant": "Night"}
        ],
        "Stalingrad": [
            {"id": "stalingrad_warfare", "variant": "Day"},
            {"id": "stalingrad_warfare_night", "variant": "Night"}
        ],
        "St. Marie Du Mont": [
            {"id": "stmariedumont_warfare", "variant": "Day"},
            {"id": "stmariedumont_warfare_night", "variant": "Night"}
        ],
        "St. Mere Eglise": [
            {"id": "stmereeglise_warfare", "variant": "Day"},
            {"id": "stmereeglise_warfare_night", "variant": "Night"}
        ],
        "Tobruk": [
            {"id": "tobruk_warfare_day", "variant": "Day"},
            {"id": "tobruk_warfare_dusk", "variant": "Dusk"},
            {"id": "tobruk_warfare_morning", "variant": "Dawn"}
        ],
        "Utah Beach": [
            {"id": "utahbeach_warfare", "variant": "Day"},
            {"id": "utahbeach_warfare_night", "variant": "Night"}
        ]
    },
    "offensive": {
        "Carentan": [
            {"id": "carentan_offensive_ger", "variant": "GER Attack"},
            {"id": "carentan_offensive_us", "variant": "US Attack"}
        ],
        "Driel": [
            {"id": "driel_offensive_ger", "variant": "GER Attack"},
            {"id": "driel_offensive_us", "variant": "GB Attack"}
        ],
        "El Alamein": [
            {"id": "elalamein_offensive_CW", "variant": "GB Attack"},
            {"id": "elalamein_offensive_ger", "variant": "GER Attack"}
        ],
        "Elsenborn Ridge": [
            {"id": "elsenbornridge_offensiveUS_day", "variant": "US Attack (Day)"},
            {"id": "elsenbornridge_offensiveUS_morning", "variant": "US Attack (Dawn)"},
            {"id": "elsenbornridge_offensiveUS_night", "variant": "US Attack (Night)"},
            {"id": "elsenbornridge_offensiveger_day", "variant": "GER Attack (Day)"},
            {"id": "elsenbornridge_offensiveger_morning", "variant": "GER Attack (Dawn)"},
            {"id": "elsenbornridge_offensiveger_night", "variant": "GER Attack (Night)"}
        ],
        "Foy": [
            {"id": "foy_offensive_ger", "variant": "GER Attack"},
            {"id": "foy_offensive_us", "variant": "US Attack"}
        ],
        "Hill 400": [
            {"id": "hill400_offensive_US", "variant": "US Attack"},
            {"id": "hill400_offensive_ger", "variant": "GER Attack"}
        ],
        "Hurtgen Forest": [
            {"id": "hurtgenforest_offensive_US", "variant": "US Attack"},
            {"id": "hurtgenforest_offensive_ger", "variant": "GER Attack"}
        ],
        "Kharkov": [
            {"id": "kharkov_offensive_ger", "variant": "GER Attack"},
            {"id": "kharkov_offensive_rus", "variant": "RUS Attack"}
        ],
        "Kursk": [
            {"id": "kursk_offensive_ger", "variant": "GER Attack"},
            {"id": "kursk_offensive_rus", "variant": "RUS Attack"}
        ],
        "Mortain": [
            {"id": "mortain_offensiveUS_day", "variant": "US Attack (Day)"},
            {"id": "mortain_offensiveUS_dusk", "variant": "US Attack (Dusk)"},
            {"id": "mortain_offensiveUS_overcast", "variant": "US Attack (Overcast)"},
            {"id": "mortain_offensiveger_day", "variant": "GER Attack (Day)"},
            {"id": "mortain_offensiveger_dusk", "variant": "GER Attack (Dusk)"},
            {"id": "mortain_offensiveger_overcast", "variant": "GER Attack (Overcast)"}
        ],
        "Omaha Beach": [
            {"id": "omahabeach_offensive_ger", "variant": "GER Attack"},
            {"id": "omahabeach_offensive_us", "variant": "US Attack"}
        ],
        "Purple Heart Lane": [
            {"id": "PHL_L_1944_OffensiveGER", "variant": "GER Attack"},
            {"id": "PHL_L_1944_OffensiveUS", "variant": "US Attack"}
        ],
        "Remagen": [
            {"id": "remagen_offensive_ger", "variant": "GER Attack"},
            {"id": "remagen_offensive_us", "variant": "US Attack"}
        ],
        "Stalingrad": [
            {"id": "stalingrad_offensive_ger", "variant": "GER Attack"},
            {"id": "stalingrad_offensive_rus", "variant": "RUS Attack"}
        ],
        "St. Marie Du Mont": [
            {"id": "stmariedumont_off_ger", "variant": "GER Attack"},
            {"id": "stmariedumont_off_us", "variant": "US Attack"}
        ],
        "St. Mere Eglise": [
            {"id": "stmereeglise_offensive_ger", "variant": "GER Attack"},
            {"id": "stmereeglise_offensive_us", "variant": "US Attack"}
        ],
        "Tobruk": [
            {"id": "tobruk_offensivebritish_day", "variant": "GB Attack (Day)"},
            {"id": "tobruk_offensivebritish_dusk", "variant": "GB Attack (Dusk)"},
            {"id": "tobruk_offensivebritish_morning", "variant": "GB Attack (Dawn)"},
            {"id": "tobruk_offensiveger_day", "variant": "GER Attack (Day)"},
            {"id": "tobruk_offensiveger_dusk", "variant": "GER Attack (Dusk)"},
            {"id": "tobruk_offensiveger_morning", "variant": "GER Attack (Dawn)"}
        ],
        "Utah Beach": [
            {"id": "utahbeach_offensive_ger", "variant": "GER Attack"},
            {"id": "utahbeach_offensive_us", "variant": "US Attack"}
        ]
    },
    "skirmish": {
        "Carentan": [
            {"id": "CAR_S_1944_Day_P_Skirmish", "variant": "Day"},
            {"id": "CAR_S_1944_Dusk_P_Skirmish", "variant": "Dusk"},
            {"id": "CAR_S_1944_Rain_P_Skirmish", "variant": "Rain"}
        ],
        "Driel": [
            {"id": "DRL_S_1944_Day_P_Skirmish", "variant": "Day"},
            {"id": "DRL_S_1944_Night_P_Skirmish", "variant": "Night"},
            {"id": "DRL_S_1944_P_Skirmish", "variant": "Dawn"}
        ],
        "El Alamein": [
            {"id": "ELA_S_1942_Night_P_Skirmish", "variant": "Dusk"},
            {"id": "ELA_S_1942_P_Skirmish", "variant": "Day"}
        ],
        "Elsenborn Ridge": [
            {"id": "elsenbornridge_skirmish_day", "variant": "Day"},
            {"id": "elsenbornridge_skirmish_morning", "variant": "Dawn"},
            {"id": "elsenbornridge_skirmish_night", "variant": "Night"}
        ],
        "Hill 400": [
            {"id": "HIL_S_1944_Day_P_Skirmish", "variant": "Day"},
            {"id": "HIL_S_1944_Dusk_P_Skirmish", "variant": "Dusk"}
        ],
        "Mortain": [
            {"id": "mortain_skirmish_day", "variant": "Day"},
            {"id": "mortain_skirmish_dusk", "variant": "Dusk"},
            {"id": "mortain_skirmish_overcast", "variant": "Overcast"}
        ],
        "Purple Heart Lane": [
            {"id": "PHL_S_1944_Morning_P_Skirmish", "variant": "Dawn"},
            {"id": "PHL_S_1944_Night_P_Skirmish", "variant": "Night"},
            {"id": "PHL_S_1944_Rain_P_Skirmish", "variant": "Rain"}
        ],
        "St. Marie Du Mont": [
            {"id": "SMDM_S_1944_Day_P_Skirmish", "variant": "Day"},
            {"id": "SMDM_S_1944_Night_P_Skirmish", "variant": "Night"},
            {"id": "SMDM_S_1944_Rain_P_Skirmish", "variant": "Rain"}
        ],
        "St. Mere Eglise": [
            {"id": "SME_S_1944_Day_P_Skirmish", "variant": "Day"},
            {"id": "SME_S_1944_Morning_P_Skirmish", "variant": "Dawn"},
            {"id": "SME_S_1944_Night_P_Skirmish", "variant": "Night"}
        ],
        "Tobruk": [
            {"id": "tobruk_skirmish_day", "variant": "Day"},
            {"id": "tobruk_skirmish_dusk", "variant": "Dusk"},
            {"id": "tobruk_skirmish_morning", "variant": "Dawn"}
        ]
    }
}


def get_maps_for_mode(game_mode: str, force_refresh: bool = False) -> List[str]:
    """Get all map names for a specific game mode."""
    data = _active_maps(force_refresh)
    return list(data.get(game_mode, {}).keys())


def get_variants_for_map(
    game_mode: str, map_name: str, force_refresh: bool = False
) -> List[Dict[str, str]]:
    """Get all variants for a specific map in a game mode."""
    data = _active_maps(force_refresh)
    return data.get(game_mode, {}).get(map_name, [])


def get_map_id(game_mode: str, map_name: str, variant: str) -> Optional[str]:
    """Get the map ID for a specific combination."""
    variants = get_variants_for_map(game_mode, map_name)
    for entry in variants:
        if entry["variant"] == variant:
            return entry["id"]
    return None
