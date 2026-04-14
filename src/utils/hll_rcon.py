import base64
import json
import os
import socket
import struct
import threading
from dataclasses import dataclass
from typing import Any, Dict, Optional


HEADER_MAGIC = 0xDE450508
HEADER_SIZE = 12
RCON_VERSION = 2


class HLLRCONError(Exception):
    """Raised when the HLL direct RCON connection or command fails."""


@dataclass(frozen=True)
class HLLRCONCredentials:
    host: str
    port: int
    password: str

    @staticmethod
    def _env_value(name: str, server_number: Optional[int]) -> Optional[str]:
        if server_number is not None:
            server_key = f"SERVER{server_number}_{name}"
            if server_key in os.environ:
                return os.environ[server_key]
        return os.environ.get(name)

    @classmethod
    def is_configured(cls, server_number: Optional[int] = None) -> bool:
        host = (cls._env_value("RCON_HOST", server_number) or "").strip()
        port = (cls._env_value("RCON_PORT", server_number) or "").strip()
        password = (cls._env_value("RCON_PASSWORD", server_number) or "").strip()
        return bool(host and port and password)

    @classmethod
    def from_env(cls, server_number: Optional[int] = None) -> "HLLRCONCredentials":
        host = (cls._env_value("RCON_HOST", server_number) or "").strip()
        port_value = (cls._env_value("RCON_PORT", server_number) or "").strip()
        password = (cls._env_value("RCON_PASSWORD", server_number) or "").strip()

        if not host or not port_value or not password:
            if server_number is not None:
                raise HLLRCONError(
                    f"Direct RCON credentials are incomplete for server #{server_number}. "
                    f"Set SERVER{server_number}_RCON_HOST, SERVER{server_number}_RCON_PORT, "
                    f"and SERVER{server_number}_RCON_PASSWORD."
                )
            raise HLLRCONError(
                "Direct RCON credentials are not configured. "
                "Set RCON_HOST, RCON_PORT, and RCON_PASSWORD, or per-server SERVER{N}_RCON_* variables."
            )

        try:
            port = int(port_value)
        except ValueError as exc:
            raise HLLRCONError(f"Invalid RCON port value: {port_value}") from exc

        if port <= 0 or port > 65535:
            raise HLLRCONError(f"RCON port must be between 1 and 65535. Got: {port}")

        return cls(host=host, port=port, password=password)


class HLLRCONClient:
    """Minimal Hell Let Loose RCON v2 client for direct server control."""

    def __init__(
        self,
        credentials: HLLRCONCredentials,
        timeout: float = 10.0,
        max_request_size: int = 1024 * 1024,
        max_response_size: int = 4 * 1024 * 1024,
    ) -> None:
        self.credentials = credentials
        self.timeout = timeout
        self.max_request_size = max_request_size
        self.max_response_size = max_response_size
        self._request_id = 0
        self._request_lock = threading.Lock()

    @classmethod
    def from_env(cls, server_number: Optional[int] = None, timeout: float = 10.0) -> "HLLRCONClient":
        return cls(credentials=HLLRCONCredentials.from_env(server_number=server_number), timeout=timeout)

    def get_session_info(self) -> Dict[str, Any]:
        result = self.execute("GetServerInformation", {"Name": "session", "Value": ""})
        return self._decode_json_content(result, "GetServerInformation(session)")

    def get_map_sequence(self) -> Dict[str, Any]:
        result = self.execute("GetServerInformation", {"Name": "mapsequence", "Value": ""})
        return self._decode_json_content(result, "GetServerInformation(mapsequence)")

    def change_map(self, map_name: str) -> Dict[str, Any]:
        return self.execute("ChangeMap", {"MapName": map_name})

    def set_sector_layout(self, sectors: list[str]) -> Dict[str, Any]:
        if len(sectors) != 5:
            raise HLLRCONError("SetSectorLayout requires exactly 5 sector values.")
        payload = {
            "Sector_1": sectors[0],
            "Sector_2": sectors[1],
            "Sector_3": sectors[2],
            "Sector_4": sectors[3],
            "Sector_5": sectors[4],
        }
        return self.execute("SetSectorLayout", payload)

    def add_map_to_sequence(self, map_name: str, index: int = 0) -> Dict[str, Any]:
        return self.execute("AddMapToSequence", {"MapName": map_name, "Index": index})

    def set_dynamic_weather_enabled(self, map_id: str, enable: bool) -> Dict[str, Any]:
        return self.execute("SetDynamicWeatherEnabled", {"MapId": map_id, "Enable": enable})

    def set_team_switch_cooldown(self, minutes: int) -> Dict[str, Any]:
        return self.execute("SetTeamSwitchCooldown", {"TeamSwitchTimer": minutes})

    def set_match_timer(self, game_mode: str, minutes: int) -> Dict[str, Any]:
        return self.execute("SetMatchTimer", {"GameMode": game_mode, "MatchLength": minutes})

    def set_warmup_timer(self, game_mode: str, minutes: int) -> Dict[str, Any]:
        return self.execute("SetWarmupTimer", {"GameMode": game_mode, "WarmupLength": minutes})

    def execute(self, command: str, content_body: Any) -> Dict[str, Any]:
        with socket.create_connection((self.credentials.host, self.credentials.port), timeout=self.timeout) as connection:
            connection.settimeout(self.timeout)

            xor_key_response = self._exchange(
                connection=connection,
                auth_token="",
                xor_key=b"",
                command="ServerConnect",
                content_body="",
            )
            xor_key = self._decode_xor_key(xor_key_response)

            login_response = self._exchange(
                connection=connection,
                auth_token="",
                xor_key=xor_key,
                command="Login",
                content_body=self.credentials.password,
            )
            auth_token = self._extract_auth_token(login_response)

            return self._exchange(
                connection=connection,
                auth_token=auth_token,
                xor_key=xor_key,
                command=command,
                content_body=content_body,
            )

    def _exchange(
        self,
        *,
        connection: socket.socket,
        auth_token: str,
        xor_key: bytes,
        command: str,
        content_body: Any,
    ) -> Dict[str, Any]:
        request_bytes, request_id = self._pack_request(auth_token, command, content_body)
        if len(request_bytes) > self.max_request_size:
            raise HLLRCONError(
                f"RCON request for {command} exceeds the maximum allowed size of {self.max_request_size} bytes."
            )

        if xor_key:
            encrypted_body = self._xor_bytes(request_bytes[HEADER_SIZE:], xor_key)
            request_bytes = request_bytes[:HEADER_SIZE] + encrypted_body

        connection.sendall(request_bytes)

        header = self._recv_exact(connection, HEADER_SIZE)
        magic, response_id, content_length = struct.unpack("<III", header)
        if magic != HEADER_MAGIC:
            raise HLLRCONError(
                f"Invalid RCON header magic for {command}: expected 0x{HEADER_MAGIC:08X}, got 0x{magic:08X}."
            )
        if content_length > self.max_response_size:
            raise HLLRCONError(
                f"RCON response for {command} exceeds the maximum allowed size of {self.max_response_size} bytes."
            )

        body = self._recv_exact(connection, content_length)
        if xor_key:
            body = self._xor_bytes(body, xor_key)

        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HLLRCONError(f"Failed to decode RCON response for {command}.") from exc

        if response_id != request_id:
            raise HLLRCONError(
                f"RCON response ID mismatch for {command}: expected {request_id}, got {response_id}."
            )
        if not isinstance(payload, dict):
            raise HLLRCONError(f"Unexpected RCON response type for {command}: {type(payload).__name__}.")

        status_code = payload.get("statusCode")
        if status_code != 200:
            status_message = payload.get("statusMessage") or "Unknown direct RCON error"
            raise HLLRCONError(f"{command} failed: {status_message}")

        return payload

    def _pack_request(self, auth_token: str, command: str, content_body: Any) -> tuple[bytes, int]:
        with self._request_lock:
            self._request_id += 1
            request_id = self._request_id

        if isinstance(content_body, str):
            content_body_value = content_body
        elif content_body is None:
            content_body_value = ""
        else:
            content_body_value = json.dumps(content_body, separators=(",", ":"))

        payload = {
            "authToken": auth_token,
            "version": RCON_VERSION,
            "name": command,
            "contentBody": content_body_value,
        }

        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        header = struct.pack("<III", HEADER_MAGIC, request_id, len(body))
        return header + body, request_id

    @staticmethod
    def _recv_exact(connection: socket.socket, expected_bytes: int) -> bytes:
        buffer = bytearray()
        while len(buffer) < expected_bytes:
            chunk = connection.recv(expected_bytes - len(buffer))
            if not chunk:
                raise HLLRCONError("Direct RCON connection closed unexpectedly.")
            buffer.extend(chunk)
        return bytes(buffer)

    @staticmethod
    def _xor_bytes(data: bytes, key: bytes) -> bytes:
        if not key:
            return data
        return bytes(byte ^ key[index % len(key)] for index, byte in enumerate(data))

    @staticmethod
    def _decode_xor_key(response: Dict[str, Any]) -> bytes:
        content_body = response.get("contentBody")
        if not isinstance(content_body, str):
            raise HLLRCONError("ServerConnect did not return a valid XOR key.")
        try:
            return base64.b64decode(content_body)
        except ValueError as exc:
            raise HLLRCONError("Failed to decode direct RCON XOR key.") from exc

    @staticmethod
    def _extract_auth_token(response: Dict[str, Any]) -> str:
        content_body = response.get("contentBody")
        if not isinstance(content_body, str) or not content_body:
            raise HLLRCONError("Login did not return a valid auth token.")
        return content_body

    @staticmethod
    def _decode_json_content(response: Dict[str, Any], command: str) -> Dict[str, Any]:
        content_body = response.get("contentBody")
        if isinstance(content_body, dict):
            return content_body
        if isinstance(content_body, str):
            try:
                decoded = json.loads(content_body)
            except json.JSONDecodeError as exc:
                raise HLLRCONError(f"{command} did not return valid JSON content.") from exc
            if isinstance(decoded, dict):
                return decoded
        raise HLLRCONError(f"{command} returned an unexpected content body.")
