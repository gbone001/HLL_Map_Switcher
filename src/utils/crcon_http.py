import os
from dataclasses import dataclass
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import requests
from dotenv import load_dotenv

load_dotenv()


class CRCONHTTPError(Exception):
    """Raised when the CRCON HTTP API returns an error."""


@dataclass
class CRCONCredentials:
    base_url: str
    token: Optional[str] = None

    @staticmethod
    def _env_value(name: str, server_number: Optional[int]) -> Optional[str]:
        if server_number is not None:
            server_key = f"SERVER{server_number}_{name}"
            if server_key in os.environ:
                return os.environ[server_key]
        return os.environ.get(name)

    @staticmethod
    def _configured_server_numbers(max_servers: int = 25) -> List[int]:
        numbers: List[int] = []
        for index in range(1, max_servers + 1):
            host_key = f"SERVER{index}_HOST"
            crcon_keys = [
                f"SERVER{index}_CRCON_BASE_URL",
                f"SERVER{index}_CRCON_TOKEN",
            ]
            if os.environ.get(host_key) is None and not any(key in os.environ for key in crcon_keys):
                break
            numbers.append(index)
        return numbers

    @classmethod
    def from_env(cls, server_number: Optional[int] = None) -> "CRCONCredentials":
        candidate_numbers: List[Optional[int]]
        if server_number is not None:
            candidate_numbers = [server_number]
        else:
            candidate_numbers = [None]
            candidate_numbers.extend(cls._configured_server_numbers())

        for candidate in candidate_numbers:
            base_url = (cls._env_value("CRCON_BASE_URL", candidate) or "").strip()
            token = cls._env_value("CRCON_TOKEN", candidate)

            if base_url and token:
                return cls(base_url=base_url.rstrip("/"), token=token)

        if server_number is not None:
            raise CRCONHTTPError(
                f"CRCON HTTP API credentials are incomplete for server #{server_number}. "
                f"Set SERVER{server_number}_CRCON_BASE_URL and SERVER{server_number}_CRCON_TOKEN."
            )

        raise CRCONHTTPError(
            "CRCON HTTP API credentials are not configured. "
            "Set CRCON_BASE_URL and CRCON_TOKEN, or per-server SERVER{N}_CRCON_* variables."
        )


class CRCONHttpClient:
    """Minimal client for interacting with CRCON's HTTP API."""

    def __init__(self, credentials: Optional[CRCONCredentials] = None, timeout: float = 10.0, cache_ttl: float = 5.0):
        self.credentials = credentials or CRCONCredentials.from_env()
        self.timeout = timeout
        self.session = requests.Session()
        self._token: Optional[str] = None
        self.cache_ttl = cache_ttl
        self._cache: Dict[str, Tuple[Dict[str, Any], float]] = {}

    @classmethod
    def from_env(cls, timeout: float = 10.0, server_number: Optional[int] = None) -> "CRCONHttpClient":
        return cls(credentials=CRCONCredentials.from_env(server_number=server_number), timeout=timeout)

    def login(self) -> str:
        """Authenticate with CRCON and store the bearer token."""
        # Require a static API token; token-only auth is enforced.
        if getattr(self.credentials, "token", None):
            self._token = self.credentials.token
            return self._token

        raise CRCONHTTPError(
            "CRCON HTTP API requires an API token. Set CRCON_TOKEN or SERVER{N}_CRCON_TOKEN in the environment."
        )

    def get_maps(self) -> Dict[str, Any]:
        """Retrieve the full map list using the authenticated session."""
        if not self._token:
            self.login()

        url = f"{self.credentials.base_url}/get_maps"
        response = self.session.get(url, headers=self._auth_headers(), timeout=self.timeout)

        if response.status_code == 401:
            # Token expired or invalid; try once more after refreshing.
            self._token = None
            self.login()
            response = self.session.get(url, headers=self._auth_headers(), timeout=self.timeout)

        if response.status_code != 200:
            raise CRCONHTTPError(f"get_maps failed with status {response.status_code}: {response.text}")

        return self._parse_json(response)

    def get_objective_rows(self) -> List[List[str]]:
        """Fetch the current objectives matrix (5 rows x 3 options)."""
        if not self._token:
            self.login()

        url = f"{self.credentials.base_url}/get_objective_rows"
        response = self.session.get(url, headers=self._auth_headers(), timeout=self.timeout)

        if response.status_code == 401:
            self._token = None
            self.login()
            response = self.session.get(url, headers=self._auth_headers(), timeout=self.timeout)

        if response.status_code != 200:
            raise CRCONHTTPError(f"get_objective_rows failed with status {response.status_code}: {response.text}")

        payload = self._parse_json(response)
        rows = payload.get("result")
        if not isinstance(rows, list) or len(rows) != 5:
            raise CRCONHTTPError("Unexpected data returned from get_objective_rows.")
        return rows

    def get_gamestate(self) -> Dict[str, Any]:
        """Retrieve live game state information (map, scores, player counts)."""
        cache_key = "get_gamestate"
        cached = self._cache.get(cache_key)
        now = time.time()
        if cached:
            payload, timestamp = cached
            if now - timestamp < self.cache_ttl:
                return payload

        if not self._token:
            self.login()

        url = f"{self.credentials.base_url}/get_gamestate"
        response = self.session.get(url, headers=self._auth_headers(), timeout=self.timeout)

        if response.status_code == 401:
            self._token = None
            self.login()
            response = self.session.get(url, headers=self._auth_headers(), timeout=self.timeout)

        if response.status_code != 200:
            raise CRCONHTTPError(f"get_gamestate failed with status {response.status_code}: {response.text}")

        payload = self._parse_json(response)
        if isinstance(payload, dict) and payload.get("failed"):
            raise CRCONHTTPError(f"get_gamestate reported failure: {payload.get('error')}")

        self._cache[cache_key] = (payload, now)
        return payload

    def set_map(self, map_id: str) -> Dict[str, Any]:
        """Change the current map via the HTTP API."""
        if not self._token:
            self.login()

        url = f"{self.credentials.base_url}/set_map"
        payload = {"map_name": map_id}
        response = self.session.post(
            url,
            headers=self._auth_headers(),
            json=payload,
            timeout=self.timeout,
        )

        if response.status_code == 401:
            self._token = None
            self.login()
            response = self.session.post(
                url,
                headers=self._auth_headers(),
                json=payload,
                timeout=self.timeout,
            )

        if response.status_code != 200:
            raise CRCONHTTPError(f"set_map failed with status {response.status_code}: {response.text}")

        return self._parse_json(response)

    def set_game_layout(
        self,
        objectives: Sequence[Union[str, int]],
        random_constraints: int = 0,
    ) -> Dict[str, Any]:
        """Apply a custom objective layout for the current match."""
        if not self._token:
            self.login()

        url = f"{self.credentials.base_url}/set_game_layout"
        payload = {
            "objectives": list(objectives),
            "random_constraints": random_constraints,
        }

        response = self.session.post(
            url,
            headers=self._auth_headers(),
            json=payload,
            timeout=self.timeout,
        )

        if response.status_code == 401:
            self._token = None
            self.login()
            response = self.session.post(
                url,
                headers=self._auth_headers(),
                json=payload,
                timeout=self.timeout,
            )

        if response.status_code != 200:
            raise CRCONHTTPError(f"set_game_layout failed with status {response.status_code}: {response.text}")

        payload = self._parse_json(response)
        if payload.get("failed"):
            raise CRCONHTTPError(f"set_game_layout reported failure: {payload.get('error')}")
        return payload

    def set_dynamic_weather_enabled(self, map_name: str, enabled: bool) -> Dict[str, Any]:
        """Enable or disable dynamic weather for a map layer."""
        if not self._token:
            self.login()

        url = f"{self.credentials.base_url}/set_dynamic_weather_enabled"
        payload = {
            "map_name": map_name,
            "enabled": bool(enabled),
        }

        response = self.session.post(
            url,
            headers=self._auth_headers(),
            json=payload,
            timeout=self.timeout,
        )

        if response.status_code == 401:
            self._token = None
            self.login()
            response = self.session.post(
                url,
                headers=self._auth_headers(),
                json=payload,
                timeout=self.timeout,
            )

        if response.status_code != 200:
            raise CRCONHTTPError(
                f"set_dynamic_weather_enabled failed with status {response.status_code}: {response.text}"
            )

        payload = self._parse_json(response)
        if isinstance(payload, dict) and payload.get("failed"):
            raise CRCONHTTPError(f"set_dynamic_weather_enabled reported failure: {payload.get('error')}")
        return payload

    def set_match_timer(self, game_mode: str, length: int) -> Dict[str, Any]:
        """Set the configured match timer for a game mode."""
        if not self._token:
            self.login()

        url = f"{self.credentials.base_url}/set_match_timer"
        payload = {
            "game_mode": game_mode,
            "length": int(length),
        }

        response = self.session.post(
            url,
            headers=self._auth_headers(),
            json=payload,
            timeout=self.timeout,
        )

        if response.status_code == 401:
            self._token = None
            self.login()
            response = self.session.post(
                url,
                headers=self._auth_headers(),
                json=payload,
                timeout=self.timeout,
            )

        if response.status_code != 200:
            raise CRCONHTTPError(f"set_match_timer failed with status {response.status_code}: {response.text}")

        result = self._parse_json(response)
        if isinstance(result, dict) and result.get("failed"):
            raise CRCONHTTPError(f"set_match_timer reported failure: {result.get('error')}")
        return result

    def set_warmup_timer(self, game_mode: str, length: int) -> Dict[str, Any]:
        """Set the configured warmup timer for a game mode."""
        if not self._token:
            self.login()

        url = f"{self.credentials.base_url}/set_warmup_timer"
        payload = {
            "game_mode": game_mode,
            "length": int(length),
        }

        response = self.session.post(
            url,
            headers=self._auth_headers(),
            json=payload,
            timeout=self.timeout,
        )

        if response.status_code == 401:
            self._token = None
            self.login()
            response = self.session.post(
                url,
                headers=self._auth_headers(),
                json=payload,
                timeout=self.timeout,
            )

        if response.status_code != 200:
            raise CRCONHTTPError(f"set_warmup_timer failed with status {response.status_code}: {response.text}")

        result = self._parse_json(response)
        if isinstance(result, dict) and result.get("failed"):
            raise CRCONHTTPError(f"set_warmup_timer reported failure: {result.get('error')}")
        return result

    def add_admin(self, player_id: str, role: str = "spectator", description: str = "") -> bool:
        """Grant admin role to a player via CRCON."""
        if not self._token:
            self.login()

        url = f"{self.credentials.base_url}/add_admin"
        payload = {
            "player_id": player_id,
            "role": role,
            "description": description,
        }

        response = self.session.post(
            url,
            headers=self._auth_headers(),
            json=payload,
            timeout=self.timeout,
        )

        if response.status_code == 401:
            self._token = None
            self.login()
            response = self.session.post(
                url,
                headers=self._auth_headers(),
                json=payload,
                timeout=self.timeout,
            )

        if response.status_code != 200:
            raise CRCONHTTPError(f"add_admin failed with status {response.status_code}: {response.text}")

        result = self._parse_json(response)
        if isinstance(result, dict) and result.get("failed"):
            return False

        return bool(result.get("result"))

    def get_admin_ids(self) -> List[Dict[str, Any]]:
        """Fetch all configured admin users and their roles."""
        if not self._token:
            self.login()

        url = f"{self.credentials.base_url}/get_admin_ids"
        response = self.session.get(url, headers=self._auth_headers(), timeout=self.timeout)

        if response.status_code == 401:
            self._token = None
            self.login()
            response = self.session.get(url, headers=self._auth_headers(), timeout=self.timeout)

        if response.status_code != 200:
            raise CRCONHTTPError(f"get_admin_ids failed with status {response.status_code}: {response.text}")

        payload = self._parse_json(response)
        if payload.get("failed"):
            raise CRCONHTTPError(f"get_admin_ids reported failure: {payload.get('error')}")

        result = payload.get("result")
        if not isinstance(result, list):
            raise CRCONHTTPError("Unexpected data returned from get_admin_ids.")

        return [row for row in result if isinstance(row, dict)]

    def remove_admin(self, player_id: str) -> bool:
        """Remove admin role from a player via CRCON."""
        if not self._token:
            self.login()

        url = f"{self.credentials.base_url}/remove_admin"
        payload = {"player_id": player_id}

        response = self.session.post(
            url,
            headers=self._auth_headers(),
            json=payload,
            timeout=self.timeout,
        )

        if response.status_code == 401:
            self._token = None
            self.login()
            response = self.session.post(
                url,
                headers=self._auth_headers(),
                json=payload,
                timeout=self.timeout,
            )

        if response.status_code != 200:
            raise CRCONHTTPError(f"remove_admin failed with status {response.status_code}: {response.text}")

        result = self._parse_json(response)
        if isinstance(result, dict) and result.get("failed"):
            return False

        return bool(result.get("result"))

    def _auth_headers(self) -> Dict[str, str]:
        if not self._token:
            raise CRCONHTTPError("Missing bearer token; call login() first.")
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        }

    @staticmethod
    def _parse_json(response: requests.Response) -> Dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise CRCONHTTPError(f"Failed to parse JSON response: {response.text}") from exc

        if isinstance(data, dict):
            return data

        raise CRCONHTTPError("Unexpected response format; expected JSON object.")
