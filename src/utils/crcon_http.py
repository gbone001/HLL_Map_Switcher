import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Union

import requests
from dotenv import load_dotenv

load_dotenv()


class CRCONHTTPError(Exception):
    """Raised when the CRCON HTTP API returns an error."""


@dataclass
class CRCONCredentials:
    base_url: str
    username: str
    password: str

    @classmethod
    def from_env(cls) -> "CRCONCredentials":
        base_url = (os.getenv("CRCON_BASE_URL") or "").strip()
        username = (os.getenv("CRCON_USERNAME") or "").strip()
        password = os.getenv("CRCON_PASSWORD")

        if not base_url:
            raise CRCONHTTPError("Environment variable CRCON_BASE_URL is required for HTTP API access.")
        if not username:
            raise CRCONHTTPError("Environment variable CRCON_USERNAME is required for HTTP API access.")
        if password is None:
            raise CRCONHTTPError("Environment variable CRCON_PASSWORD is required for HTTP API access.")

        return cls(
            base_url=base_url.rstrip("/"),
            username=username,
            password=password,
        )


class CRCONHttpClient:
    """Minimal client for interacting with CRCON's HTTP API."""

    def __init__(self, credentials: Optional[CRCONCredentials] = None, timeout: float = 10.0):
        self.credentials = credentials or CRCONCredentials.from_env()
        self.timeout = timeout
        self.session = requests.Session()
        self._token: Optional[str] = None

    @classmethod
    def from_env(cls, timeout: float = 10.0) -> "CRCONHttpClient":
        return cls(credentials=CRCONCredentials.from_env(), timeout=timeout)

    def login(self) -> str:
        """Authenticate with CRCON and store the bearer token."""
        url = f"{self.credentials.base_url}/login"
        payload = {"username": self.credentials.username, "password": self.credentials.password}
        response = self.session.post(url, json=payload, timeout=self.timeout)

        if response.status_code != 200:
            raise CRCONHTTPError(f"Login failed with status {response.status_code}: {response.text}")

        data = self._parse_json(response)
        token = data.get("result") or data.get("token") or data.get("access_token")
        if not token:
            raise CRCONHTTPError("Login response did not include a token.")

        self._token = token
        return token

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
