import os
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .crcon_http import CRCONCredentials, CRCONHTTPError, CRCONHttpClient


@dataclass
class ServerConfig:
    name: str
    server_number: Optional[int]


class HLLAPIClient:
    """CRCON HTTP-only helper for server metadata and map actions."""

    def __init__(self, timeout: float = 10.0):
        self.timeout = timeout
        self.servers: List[ServerConfig] = self._load_servers()
        if not self.servers:
            raise ValueError(
                "No CRCON servers configured. "
                "Set CRCON_BASE_URL / CRCON_USERNAME / CRCON_PASSWORD for a single server, "
                "or SERVER{N}_CRCON_BASE_URL / _USERNAME / _PASSWORD for multiple servers."
            )
        self._clients: Dict[Optional[int], CRCONHttpClient] = {}

    def _discover_server_numbers(self, max_servers: int = 25) -> List[int]:
        numbers: List[int] = []
        for index in range(1, max_servers + 1):
            keys = (
                f"SERVER{index}_NAME",
                f"SERVER{index}_CRCON_BASE_URL",
                f"SERVER{index}_CRCON_USERNAME",
                f"SERVER{index}_CRCON_PASSWORD",
            )
            if any(key in os.environ for key in keys):
                numbers.append(index)
        return numbers

    def _load_servers(self) -> List[ServerConfig]:
        servers: List[ServerConfig] = []
        numbers = self._discover_server_numbers()

        if numbers:
            for number in numbers:
                try:
                    CRCONCredentials.from_env(server_number=number)
                except CRCONHTTPError:
                    # Leave partially configured slots out of active controls.
                    continue

                server_name = os.getenv(f"SERVER{number}_NAME") or f"HLL Server {number}"
                servers.append(ServerConfig(name=server_name, server_number=number))
            return servers

        # Fall back to shared single-server CRCON configuration.
        CRCONCredentials.from_env()
        server_name = os.getenv("SERVER_NAME") or "HLL Server"
        return [ServerConfig(name=server_name, server_number=None)]

    def _client_for_index(self, server_index: int) -> CRCONHttpClient:
        if server_index < 0 or server_index >= len(self.servers):
            raise CRCONHTTPError("Invalid server index")

        server_number = self.servers[server_index].server_number
        if server_number in self._clients:
            return self._clients[server_number]

        client = CRCONHttpClient.from_env(timeout=self.timeout, server_number=server_number)
        self._clients[server_number] = client
        return client

    def get_servers(self) -> List[Tuple[int, str]]:
        return [(index, server.name) for index, server in enumerate(self.servers)]

    def get_server_name(self, server_index: int) -> str:
        if 0 <= server_index < len(self.servers):
            return self.servers[server_index].name
        return "Unknown Server"

    def get_current_map(self, server_index: int) -> str:
        try:
            client = self._client_for_index(server_index)
            response = client.get_gamestate()
        except Exception:
            return "Unknown"

        gamestate = response.get("result") if isinstance(response, dict) else None
        current_map = gamestate.get("current_map", {}) if isinstance(gamestate, dict) else {}
        return current_map.get("pretty_name") or current_map.get("id") or "Unknown"

    def set_map(self, server_index: int, map_id: str) -> Tuple[bool, str]:
        try:
            client = self._client_for_index(server_index)
            response = client.set_map(map_id)
            if isinstance(response, dict) and response.get("failed"):
                return False, str(response.get("error") or "CRCON set_map failed")
            return True, f"Successfully set map to {map_id}"
        except CRCONHTTPError as exc:
            return False, str(exc)
        except Exception as exc:
            return False, f"Unexpected error calling set_map: {exc}"
