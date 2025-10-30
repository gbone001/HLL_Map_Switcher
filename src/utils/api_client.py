import base64
import json
import os
import socket
import struct
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Union

from dotenv import load_dotenv

load_dotenv()

RCON_PROTOCOL_VERSION = 2
HEADER_STRUCT = struct.Struct("<II")


class RconV2Error(Exception):
    """Raised when an RCON V2 request fails."""


@dataclass
class ServerConfig:
    name: str
    host: str
    port: int
    password: str


def _as_int(value: Optional[str], default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


class RconV2Connection:
    """Minimal RCON V2 client implementing the spec from the V2 PDF."""

    def __init__(self, config: ServerConfig, timeout: float = 5.0):
        self.config = config
        self.timeout = timeout

        self._socket: Optional[socket.socket] = None
        self._xor_key: bytes = b""
        self._auth_token: str = ""
        self._message_id: int = 0

    def __enter__(self) -> "RconV2Connection":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def connect(self) -> None:
        if self._socket:
            return

        try:
            self._socket = socket.create_connection(
                (self.config.host, self.config.port), self.timeout
            )
            self._socket.settimeout(self.timeout)
        except OSError as exc:
            raise RconV2Error(f"Failed to connect to {self.config.host}:{self.config.port}") from exc

        self._perform_handshake()

    def close(self) -> None:
        if self._socket:
            try:
                self._socket.close()
            finally:
                self._socket = None
                self._xor_key = b""
                self._auth_token = ""

    def change_map(self, map_id: str) -> Dict[str, Any]:
        return self._run_command("ChangeMap", map_id)

    def server_information(self, name: str, value: str = "") -> Dict[str, Any]:
        response = self._run_command("ServerInformation", {"Name": name, "Value": value})
        content = response.get("content")
        return content if isinstance(content, dict) else {}

    def _perform_handshake(self) -> None:
        if not self._socket:
            raise RconV2Error("Socket not initialised")

        # ServerConnect is the only unencrypted command
        response = self._send_command(
            name="ServerConnect",
            content="",
            encrypt=False,
            include_auth=False,
        )
        xor_key_encoded = response.get("content")
        if not xor_key_encoded or not isinstance(xor_key_encoded, str):
            raise RconV2Error("ServerConnect did not return an XOR key")

        try:
            self._xor_key = base64.b64decode(xor_key_encoded)
        except (base64.binascii.Error, ValueError) as exc:
            raise RconV2Error("Failed to decode XOR key from ServerConnect") from exc

        if not self._xor_key:
            raise RconV2Error("Received empty XOR key from server")

        # Login shares the XOR key but does not use the auth token yet
        login_response = self._send_command(
            name="Login",
            content=self.config.password,
            include_auth=False,
        )
        auth_token = login_response.get("content")
        if not auth_token or not isinstance(auth_token, str):
            raise RconV2Error("Login did not return an auth token")

        self._auth_token = auth_token.strip()

    def _run_command(self, name: str, content: Union[str, Dict[str, Any], List[Any], None]) -> Dict[str, Any]:
        return self._send_command(name=name, content=content, include_auth=True)

    def _send_command(
        self,
        name: str,
        content: Union[str, Dict[str, Any], List[Any], None],
        *,
        encrypt: bool = True,
        include_auth: bool = True,
    ) -> Dict[str, Any]:
        if not self._socket:
            raise RconV2Error("Connection has been closed")

        payload = {
            "AuthToken": self._auth_token if include_auth else "",
            "Version": RCON_PROTOCOL_VERSION,
            "Name": name,
            "ContentBody": self._prepare_content(content),
        }

        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if encrypt:
            body = self._xor(body)

        message_id = self._write(body)
        response = self._read(decrypt=encrypt)

        if response["id"] != message_id:
            # The protocol states the same ID is echoed back; mismatches signal desync.
            raise RconV2Error(
                f"Response ID {response['id']} did not match request ID {message_id} for {name}"
            )

        status_code = response["status_code"]
        if status_code != 200:
            raise RconV2Error(
                f"{name} failed with status {status_code}: {response['status_message']}"
            )

        return response

    def _prepare_content(
        self, content: Union[str, Dict[str, Any], List[Any], None]
    ) -> Union[str, Dict[str, Any], List[Any]]:
        if content is None:
            return ""
        return content

    def _write(self, body: bytes) -> int:
        if not self._socket:
            raise RconV2Error("Connection has been closed")

        self._message_id = (self._message_id + 1) % (2**32)
        header = HEADER_STRUCT.pack(self._message_id, len(body))
        try:
            self._socket.sendall(header + body)
        except OSError as exc:
            raise RconV2Error("Failed to send data to server") from exc
        return self._message_id

    def _read(self, *, decrypt: bool) -> Dict[str, Any]:
        if not self._socket:
            raise RconV2Error("Connection has been closed")

        header_bytes = self._read_exact(HEADER_STRUCT.size)
        message_id, length = HEADER_STRUCT.unpack(header_bytes)

        body = self._read_exact(length)
        if decrypt:
            body = self._xor(body)

        try:
            payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise RconV2Error("Failed to decode response JSON") from exc

        return {
            "id": message_id,
            "status_code": payload.get("StatusCode", payload.get("statusCode", 0)),
            "status_message": payload.get("StatusMessage", payload.get("statusMessage", "")),
            "name": payload.get("Name", payload.get("name", "")),
            "content": self._parse_content_body(
                payload.get("ContentBody", payload.get("contentBody"))
            ),
        }

    def _read_exact(self, size: int) -> bytes:
        if not self._socket:
            raise RconV2Error("Connection has been closed")

        data = bytearray()
        while len(data) < size:
            try:
                chunk = self._socket.recv(size - len(data))
            except OSError as exc:
                raise RconV2Error("Failed to read response from server") from exc
            if not chunk:
                raise RconV2Error("Connection closed by remote host")
            data.extend(chunk)
        return bytes(data)

    def _xor(self, data: bytes) -> bytes:
        if not self._xor_key:
            raise RconV2Error("XOR key has not been initialised")
        key = self._xor_key
        key_len = len(key)
        return bytes(b ^ key[i % key_len] for i, b in enumerate(data))

    def _parse_content_body(self, body: Any) -> Any:
        if isinstance(body, str):
            stripped = body.strip().replace("\x00", "")
            if stripped and stripped[0] in ("{", "["):
                try:
                    return json.loads(stripped)
                except json.JSONDecodeError:
                    return stripped
            return stripped
        return body


class HLLAPIClient:
    """High-level helper for working with Hell Let Loose RCON v2 servers."""

    def __init__(self, timeout: float = 5.0):
        self.timeout = timeout
        self.servers: List[ServerConfig] = self._load_servers()
        if not self.servers:
            raise ValueError(
                "No RCON servers configured. "
                "Provide SERVER*_HOST / SERVER*_PORT / SERVER*_PASSWORD or RCON_HOST / RCON_PORT / RCON_PASSWORD."
            )
        self._fetch_server_names()

    def _load_servers(self) -> List[ServerConfig]:
        servers: List[ServerConfig] = []

        shared_password = os.getenv("RCON_PASSWORD", "")
        shared_port = _as_int(os.getenv("RCON_PORT"))

        index = 1
        while True:
            host = os.getenv(f"SERVER{index}_HOST")
            if not host:
                break

            name = os.getenv(f"SERVER{index}_NAME") or f"HLL Server {index}"
            port = _as_int(os.getenv(f"SERVER{index}_PORT"), shared_port)
            password = os.getenv(f"SERVER{index}_PASSWORD") or shared_password

            if not port or not password:
                raise ValueError(
                    f"SERVER{index}_HOST is defined but port or password is missing. "
                    f"Set SERVER{index}_PORT / SERVER{index}_PASSWORD or the shared RCON_PORT / RCON_PASSWORD."
                )

            servers.append(ServerConfig(name=name, host=host, port=port, password=password))
            index += 1

        if not servers:
            host = os.getenv("RCON_HOST")
            port = shared_port
            password = shared_password
            name = os.getenv("SERVER_NAME") or "HLL Server"

            if host and port and password:
                servers.append(ServerConfig(name=name, host=host, port=port, password=password))

        return servers

    def _fetch_server_names(self) -> None:
        for server in self.servers:
            try:
                with RconV2Connection(server, timeout=self.timeout) as conn:
                    session = conn.server_information("session")
                    server_name = session.get("ServerName") if isinstance(session, dict) else None
                    if server_name:
                        server.name = server_name.strip()
            except Exception as exc:
                print(f"Warning: failed to fetch server name for {server.host}:{server.port} ({exc})")

    def get_servers(self) -> List[Tuple[int, str]]:
        return [(index, server.name) for index, server in enumerate(self.servers)]

    def get_server_name(self, server_index: int) -> str:
        if 0 <= server_index < len(self.servers):
            return self.servers[server_index].name
        return "Unknown Server"

    def get_current_map(self, server_index: int) -> str:
        if server_index >= len(self.servers):
            return "Unknown"

        server = self.servers[server_index]
        try:
            with RconV2Connection(server, timeout=self.timeout) as conn:
                session = conn.server_information("session")
        except Exception as exc:
            print(f"Failed to get current map for {server.name}: {exc}")
            return "Unknown"

        if isinstance(session, dict):
            map_name = session.get("MapName")
            if map_name:
                return map_name.strip()

        return "Unknown"

    def set_map(self, server_index: int, map_id: str) -> Tuple[bool, str]:
        if server_index >= len(self.servers):
            return False, "Invalid server index"

        server = self.servers[server_index]
        try:
            with RconV2Connection(server, timeout=self.timeout) as conn:
                conn.change_map(map_id)

                new_map = ""
                try:
                    session = conn.server_information("session")
                    if isinstance(session, dict):
                        new_map = session.get("MapName", "")
                except RconV2Error:
                    # Map change succeeded but fetching the session failed (likely due to map reload).
                    new_map = ""

            pretty_map = new_map.strip() if isinstance(new_map, str) else map_id
            return True, f"Successfully issued ChangeMap to {pretty_map or map_id} on {server.name}"

        except RconV2Error as exc:
            return False, str(exc)
        except Exception as exc:
            return False, f"Unexpected error calling ChangeMap on {server.name}: {exc}"
