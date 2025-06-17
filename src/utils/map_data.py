from typing import List, Dict
import requests
import os

API_BASE_URL = os.getenv("API_BASE_URL")

# Map data organized by game mode and map name
MAPS_DATA = {
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

def get_maps() -> List[str]:
    response = requests.get(f"{API_BASE_URL}/maps")
    if response.status_code == 200:
        return response.json().get("maps", [])
    return []

def get_variants(map_name: str) -> List[str]:
    response = requests.get(f"{API_BASE_URL}/maps/{map_name}/variants")
    if response.status_code == 200:
        return response.json().get("variants", [])
    return []

def get_map_details(map_name: str, variant: str) -> Dict:
    response = requests.get(f"{API_BASE_URL}/maps/{map_name}/variants/{variant}")
    if response.status_code == 200:
        return response.json()
    return {}

def get_maps_for_mode(game_mode):
    """Get all map names for a specific game mode"""
    return list(MAPS_DATA.get(game_mode, {}).keys())

def get_variants_for_map(game_mode, map_name):
    """Get all variants for a specific map in a game mode"""
    return MAPS_DATA.get(game_mode, {}).get(map_name, [])

def get_map_id(game_mode, map_name, variant):
    """Get the map ID for a specific combination"""
    variants = get_variants_for_map(game_mode, map_name)
    for v in variants:
        if v["variant"] == variant:
            return v["id"]
    return None