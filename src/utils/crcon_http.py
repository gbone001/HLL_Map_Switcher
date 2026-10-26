import os
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv()


class CRCONHTTPError(Exception):
    """Raised when the CRCON HTTP API returns an error."""


@dataclass
class CRCONCredentials:
    base_url: str
    token: str | None = None

    @staticmethod
    def _env_value(name: str, server_number: int | None) -> str | None:
        if server_number is not None:
            server_key = f"SERVER{server_number}_{name}"
            if server_key in os.environ:
                return os.environ[server_key]
        return os.environ.get(name)

    @staticmethod
    def _configured_server_numbers(max_servers: int = 25) -> list[int]:
        numbers: list[int] = []
        for index in range(1, max_servers + 1):
            host_key = f"SERVER{index}_HOST"
            crcon_keys = [
                f"SERVER{index}_CRCON_BASE_URL",
                f"SERVER{index}_CRCON_TOKEN",
            ]
            if os.environ.get(host_key) is None and not any(
                key in os.environ for key in crcon_keys
            ):
                break
            numbers.append(index)
        return numbers

    @classmethod
    def from_env(cls, server_number: int | None = None) -> "CRCONCredentials":
        candidate_numbers: list[int | None]
        if server_number is not None:
            candidate_numbers = [server_number]
        else:
            candidate_numbers = [None]
            candidate_numbers.extend(cls._configured_server_numbers())

        for candidate in candidate_numbers:
            base_url = (
                cls._env_value("CRCON_BASE_URL", candidate) or ""
            ).strip()
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

    def __init__(
        self,
        credentials: CRCONCredentials | None = None,
        timeout: float = 10.0,
        cache_ttl: float = 5.0,
    ):
        self.credentials = credentials or CRCONCredentials.from_env()
        self.timeout = timeout
        self.session = requests.Session()
        self._token: str | None = None
        self.cache_ttl = cache_ttl
        self._cache: dict[str, tuple[dict[str, Any], float]] = {}

    @classmethod
    def from_env(
        cls, timeout: float = 10.0, server_number: int | None = None
    ) -> "CRCONHttpClient":
        return cls(
            credentials=CRCONCredentials.from_env(server_number=server_number),
            timeout=timeout,
        )

    def login(self) -> str:
        """Authenticate with CRCON and store the bearer token."""
        # Require a static API token; token-only auth is enforced.
        if getattr(self.credentials, "token", None):
            self._token = self.credentials.token
            return self._token

        raise CRCONHTTPError(
            "CRCON HTTP API requires an API token. Set CRCON_TOKEN or SERVER{N}_CRCON_TOKEN in the environment."
        )

    def get_maps(self) -> dict[str, Any]:
        """Retrieve the full map list using the authenticated session."""
        if not self._token:
            self.login()

        url = f"{self.credentials.base_url}/get_maps"
        response = self._get(
            url, headers=self._auth_headers(), timeout=self.timeout
        )

        if response.status_code == 401:
            # Token expired or invalid; try once more after refreshing.
            self._token = None
            self.login()
            response = self._get(
                url, headers=self._auth_headers(), timeout=self.timeout
            )

        if response.status_code != 200:
            raise CRCONHTTPError(
                self._status_error_message("get_maps", response)
            )

        return self._parse_json(response)

    def get_objective_rows(self) -> list[list[str]]:
        """Fetch the current objectives matrix (5 rows x 3 options)."""
        if not self._token:
            self.login()

        url = f"{self.credentials.base_url}/get_objective_rows"
        response = self._get(
            url, headers=self._auth_headers(), timeout=self.timeout
        )

        if response.status_code == 401:
            self._token = None
            self.login()
            response = self._get(
                url, headers=self._auth_headers(), timeout=self.timeout
            )

        if response.status_code != 200:
            raise CRCONHTTPError(
                self._status_error_message("get_objective_rows", response)
            )

        payload = self._parse_json(response)
        rows = payload.get("result")
        if not isinstance(rows, list) or len(rows) != 5:
            raise CRCONHTTPError(
                "Unexpected data returned from get_objective_rows."
            )
        return rows

    def get_gamestate(self) -> dict[str, Any]:
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
        response = self._get(
            url, headers=self._auth_headers(), timeout=self.timeout
        )

        if response.status_code == 401:
            self._token = None
            self.login()
            response = self._get(
                url, headers=self._auth_headers(), timeout=self.timeout
            )

        if response.status_code != 200:
            raise CRCONHTTPError(
                self._status_error_message("get_gamestate", response)
            )

        payload = self._parse_json(response)
        if isinstance(payload, dict) and payload.get("failed"):
            raise CRCONHTTPError(
                f"get_gamestate reported failure: {payload.get('error')}"
            )

        self._cache[cache_key] = (payload, now)
        return payload

    def set_map(self, map_id: str) -> dict[str, Any]:
        """Change the current map via the HTTP API."""
        if not self._token:
            self.login()

        url = f"{self.credentials.base_url}/set_map"
        payload = {"map_name": map_id}
        response = self._post(
            url,
            headers=self._auth_headers(),
            json=payload,
            timeout=self.timeout,
        )

        if response.status_code == 401:
            self._token = None
            self.login()
            response = self._post(
                url,
                headers=self._auth_headers(),
                json=payload,
                timeout=self.timeout,
            )

        if response.status_code != 200:
            raise CRCONHTTPError(
                self._status_error_message("set_map", response)
            )

        return self._parse_json(response)

    def set_game_layout(
        self,
        objectives: Sequence[str | int],
        random_constraints: int = 0,
    ) -> dict[str, Any]:
        """Apply a custom objective layout for the current match."""
        if not self._token:
            self.login()

        url = f"{self.credentials.base_url}/set_game_layout"
        payload = {
            "objectives": list(objectives),
            "random_constraints": random_constraints,
        }

        response = self._post(
            url,
            headers=self._auth_headers(),
            json=payload,
            timeout=self.timeout,
        )

        if response.status_code == 401:
            self._token = None
            self.login()
            response = self._post(
                url,
                headers=self._auth_headers(),
                json=payload,
                timeout=self.timeout,
            )

        if response.status_code != 200:
            raise CRCONHTTPError(
                self._status_error_message("set_game_layout", response)
            )

        payload = self._parse_json(response)
        if payload.get("failed"):
            raise CRCONHTTPError(
                f"set_game_layout reported failure: {payload.get('error')}"
            )
        return payload

    def set_dynamic_weather_enabled(
        self, map_name: str, enabled: bool
    ) -> dict[str, Any]:
        """Enable or disable dynamic weather for a map layer."""
        if not self._token:
            self.login()

        url = f"{self.credentials.base_url}/set_dynamic_weather_enabled"
        payload = {
            "map_name": map_name,
            "enabled": bool(enabled),
        }

        response = self._post(
            url,
            headers=self._auth_headers(),
            json=payload,
            timeout=self.timeout,
        )

        if response.status_code == 401:
            self._token = None
            self.login()
            response = self._post(
                url,
                headers=self._auth_headers(),
                json=payload,
                timeout=self.timeout,
            )

        if response.status_code != 200:
            raise CRCONHTTPError(
                self._status_error_message(
                    "set_dynamic_weather_enabled", response
                )
            )

        payload = self._parse_json(response)
        if isinstance(payload, dict) and payload.get("failed"):
            raise CRCONHTTPError(
                f"set_dynamic_weather_enabled reported failure: {payload.get('error')}"
            )
        return payload

    def get_team_switch_cooldown(self) -> int:
        """Retrieve the current team switch cooldown in minutes."""
        if not self._token:
            self.login()

        url = f"{self.credentials.base_url}/get_team_switch_cooldown"
        response = self._get(
            url, headers=self._auth_headers(), timeout=self.timeout
        )

        if response.status_code == 401:
            self._token = None
            self.login()
            response = self._get(
                url, headers=self._auth_headers(), timeout=self.timeout
            )

        if response.status_code != 200:
            raise CRCONHTTPError(
                self._status_error_message(
                    "get_team_switch_cooldown", response
                )
            )

        payload = self._parse_json(response)
        if isinstance(payload, dict) and payload.get("failed"):
            raise CRCONHTTPError(
                f"get_team_switch_cooldown reported failure: {payload.get('error')}"
            )

        result = payload.get("result")
        try:
            return int(result)
        except (TypeError, ValueError) as exc:
            raise CRCONHTTPError(
                "Unexpected data returned from get_team_switch_cooldown."
            ) from exc

    def set_team_switch_cooldown(
        self, cooldown_minutes: int
    ) -> dict[str, Any]:
        """Set the team switch cooldown in minutes."""
        if not self._token:
            self.login()

        url = f"{self.credentials.base_url}/set_team_switch_cooldown"
        payload = {"minutes": int(cooldown_minutes)}
        response = self._post(
            url,
            headers=self._auth_headers(),
            json=payload,
            timeout=self.timeout,
        )

        if response.status_code == 401:
            self._token = None
            self.login()
            response = self._post(
                url,
                headers=self._auth_headers(),
                json=payload,
                timeout=self.timeout,
            )

        if response.status_code != 200:
            raise CRCONHTTPError(
                self._status_error_message(
                    "set_team_switch_cooldown", response
                )
            )

        result = self._parse_json(response)
        if isinstance(result, dict) and result.get("failed"):
            raise CRCONHTTPError(
                f"set_team_switch_cooldown reported failure: {result.get('error')}"
            )
        return result

    def set_match_timer(self, game_mode: str, length: int) -> dict[str, Any]:
        """Set the configured match timer for a game mode."""
        if not self._token:
            self.login()

        url = f"{self.credentials.base_url}/set_match_timer"
        payload = {
            "game_mode": game_mode,
            "length": int(length),
        }

        response = self._post(
            url,
            headers=self._auth_headers(),
            json=payload,
            timeout=self.timeout,
        )

        if response.status_code == 401:
            self._token = None
            self.login()
            response = self._post(
                url,
                headers=self._auth_headers(),
                json=payload,
                timeout=self.timeout,
            )

        if response.status_code != 200:
            raise CRCONHTTPError(
                self._status_error_message("set_match_timer", response)
            )

        result = self._parse_json(response)
        if isinstance(result, dict) and result.get("failed"):
            raise CRCONHTTPError(
                f"set_match_timer reported failure: {result.get('error')}"
            )
        return result

    def set_warmup_timer(self, game_mode: str, length: int) -> dict[str, Any]:
        """Set the configured warmup timer for a game mode."""
        if not self._token:
            self.login()

        url = f"{self.credentials.base_url}/set_warmup_timer"
        payload = {
            "game_mode": game_mode,
            "length": int(length),
        }

        response = self._post(
            url,
            headers=self._auth_headers(),
            json=payload,
            timeout=self.timeout,
        )

        if response.status_code == 401:
            self._token = None
            self.login()
            response = self._post(
                url,
                headers=self._auth_headers(),
                json=payload,
                timeout=self.timeout,
            )

        if response.status_code != 200:
            raise CRCONHTTPError(
                self._status_error_message("set_warmup_timer", response)
            )

        result = self._parse_json(response)
        if isinstance(result, dict) and result.get("failed"):
            raise CRCONHTTPError(
                f"set_warmup_timer reported failure: {result.get('error')}"
            )
        return result

    def add_admin(
        self, player_id: str, role: str = "spectator", description: str = ""
    ) -> bool:
        """Grant admin role to a player via CRCON."""
        if not self._token:
            self.login()

        url = f"{self.credentials.base_url}/add_admin"
        payload = {
            "player_id": player_id,
            "role": role,
            "description": description,
        }

        response = self._post(
            url,
            headers=self._auth_headers(),
            json=payload,
            timeout=self.timeout,
        )

        if response.status_code == 401:
            self._token = None
            self.login()
            response = self._post(
                url,
                headers=self._auth_headers(),
                json=payload,
                timeout=self.timeout,
            )

        if response.status_code != 200:
            raise CRCONHTTPError(
                self._status_error_message("add_admin", response)
            )

        result = self._parse_json(response)
        if isinstance(result, dict) and result.get("failed"):
            return False

        return bool(result.get("result"))

    def get_admin_ids(self) -> list[dict[str, Any]]:
        """Fetch all configured admin users and their roles."""
        if not self._token:
            self.login()

        url = f"{self.credentials.base_url}/get_admin_ids"
        response = self._get(
            url, headers=self._auth_headers(), timeout=self.timeout
        )

        if response.status_code == 401:
            self._token = None
            self.login()
            response = self._get(
                url, headers=self._auth_headers(), timeout=self.timeout
            )

        if response.status_code != 200:
            raise CRCONHTTPError(
                self._status_error_message("get_admin_ids", response)
            )

        payload = self._parse_json(response)
        if payload.get("failed"):
            raise CRCONHTTPError(
                f"get_admin_ids reported failure: {payload.get('error')}"
            )

        result = payload.get("result")
        if not isinstance(result, list):
            raise CRCONHTTPError(
                "Unexpected data returned from get_admin_ids."
            )

        return [row for row in result if isinstance(row, dict)]

    def remove_admin(self, player_id: str) -> bool:
        """Remove admin role from a player via CRCON."""
        if not self._token:
            self.login()

        url = f"{self.credentials.base_url}/remove_admin"
        payload = {"player_id": player_id}

        response = self._post(
            url,
            headers=self._auth_headers(),
            json=payload,
            timeout=self.timeout,
        )

        if response.status_code == 401:
            self._token = None
            self.login()
            response = self._post(
                url,
                headers=self._auth_headers(),
                json=payload,
                timeout=self.timeout,
            )

        if response.status_code != 200:
            raise CRCONHTTPError(
                self._status_error_message("remove_admin", response)
            )

        result = self._parse_json(response)
        if isinstance(result, dict) and result.get("failed"):
            return False

        return bool(result.get("result"))

    def _get(self, url: str, **kwargs: Any) -> requests.Response:
        return self._send("get", url, **kwargs)

    def _post(self, url: str, **kwargs: Any) -> requests.Response:
        return self._send("post", url, **kwargs)

    def _send(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        """Issue a request, surfacing network failures as CRCONHTTPError so callers can fall back."""
        try:
            return getattr(self.session, method)(url, **kwargs)
        except requests.Timeout as exc:
            raise CRCONHTTPError(
                f"CRCON request to {url} timed out after {self.timeout}s."
            ) from exc
        except requests.RequestException as exc:
            raise CRCONHTTPError(
                f"CRCON request to {url} failed: {exc.__class__.__name__}: {exc}"
            ) from exc

    def _auth_headers(self) -> dict[str, str]:
        if not self._token:
            raise CRCONHTTPError("Missing bearer token; call login() first.")
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        }

    @staticmethod
    def _parse_json(response: requests.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            response_summary = CRCONHttpClient._summarize_response_text(
                response.text
            )
            raise CRCONHTTPError(
                f"Failed to parse JSON response: {response_summary}"
            ) from exc

        if isinstance(data, dict):
            return data

        raise CRCONHTTPError(
            "Unexpected response format; expected JSON object."
        )

    @staticmethod
    def _status_error_message(
        operation: str, response: requests.Response
    ) -> str:
        crcon_error = CRCONHttpClient._crcon_envelope_error(response)
        response_summary = (
            crcon_error
            or CRCONHttpClient._summarize_response_text(response.text)
        )
        return f"{operation} failed with status {response.status_code}: {response_summary}"

    @staticmethod
    def _crcon_envelope_error(response: requests.Response) -> str | None:
        """Pull the `error` field out of CRCON's JSON envelope.

        CRCON answers failed commands (and 401/403 auth failures) with
        {"result", "command", "arguments", "failed", "error", ...}; only the
        error text is useful to an admin.
        """
        try:
            data = response.json()
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        error = data.get("error")
        if error in (None, "", [], {}):
            return None
        return error if isinstance(error, str) else str(error)

    @staticmethod
    def _summarize_response_text(response_text: str) -> str:
        text = re.sub(r"\s+", " ", response_text or "").strip()
        if not text:
            return "empty response body"

        if "<html" not in text.lower():
            return text

        title_match = re.search(
            r"<title>\s*(.*?)\s*</title>", text, flags=re.IGNORECASE
        )
        heading_match = re.search(
            r"<h1>\s*(.*?)\s*</h1>", text, flags=re.IGNORECASE
        )
        html_summary_match = title_match or heading_match
        if html_summary_match:
            return f"HTML error page: {html_summary_match.group(1).strip()}"

        return "HTML error page returned"
