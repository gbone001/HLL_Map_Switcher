import base64
import json
import socket
import struct
import threading
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from utils.hll_rcon import HEADER_MAGIC, HEADER_SIZE, HLLRCONClient, HLLRCONCredentials


class FakeRCONServer:
    def __init__(self):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(1)
        self.host, self.port = self.socket.getsockname()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.requests = []
        self._xor_key = b"test-key"
        self._auth_token = "auth-token"
        self._ready = threading.Event()
        self._done = threading.Event()

    def start(self):
        self.thread.start()
        self._ready.wait(timeout=2)

    def close(self):
        self._done.wait(timeout=2)
        self.socket.close()
        self.thread.join(timeout=2)

    def _serve(self):
        self._ready.set()
        conn, _ = self.socket.accept()
        with conn:
            xor_key = b""
            authed = False
            for _ in range(3):
                header = self._recv_exact(conn, HEADER_SIZE)
                magic, request_id, content_length = struct.unpack("<III", header)
                assert magic == HEADER_MAGIC
                body = self._recv_exact(conn, content_length)
                if xor_key:
                    body = self._xor(body, xor_key)
                payload = json.loads(body.decode("utf-8"))
                self.requests.append(payload)

                command = payload["name"]
                if command == "ServerConnect":
                    response_payload = {
                        "statusCode": 200,
                        "statusMessage": "ok",
                        "version": 2,
                        "name": command,
                        "contentBody": base64.b64encode(self._xor_key).decode("ascii"),
                    }
                elif command == "Login":
                    response_payload = {
                        "statusCode": 200,
                        "statusMessage": "ok",
                        "version": 2,
                        "name": command,
                        "contentBody": self._auth_token,
                    }
                    authed = True
                else:
                    assert authed
                    response_payload = {
                        "statusCode": 200,
                        "statusMessage": "ok",
                        "version": 2,
                        "name": command,
                        "contentBody": json.dumps(
                            {
                                "session": {
                                    "MapName": "carentan_warfare",
                                    "AlliedPlayerCount": 23,
                                    "AxisPlayerCount": 25,
                                    "RemainingMatchTime": 3120,
                                }
                            }
                        ),
                    }

                response_body = json.dumps(response_payload, separators=(",", ":")).encode("utf-8")
                should_encrypt_response = bool(xor_key)
                if should_encrypt_response:
                    response_body = self._xor(response_body, xor_key)
                response_header = struct.pack("<III", HEADER_MAGIC, request_id, len(response_body))
                conn.sendall(response_header + response_body)

                if command == "ServerConnect":
                    xor_key = self._xor_key

        self._done.set()

    @staticmethod
    def _recv_exact(conn, expected):
        data = bytearray()
        while len(data) < expected:
            chunk = conn.recv(expected - len(data))
            if not chunk:
                raise RuntimeError("socket closed")
            data.extend(chunk)
        return bytes(data)

    @staticmethod
    def _xor(data: bytes, key: bytes) -> bytes:
        return bytes(byte ^ key[index % len(key)] for index, byte in enumerate(data))


class HLLRCONClientTests(unittest.TestCase):
    def test_execute_performs_handshake_and_uses_lower_camel_case_request_keys(self):
        server = FakeRCONServer()
        server.start()
        try:
            client = HLLRCONClient(
                credentials=HLLRCONCredentials(host=server.host, port=server.port, password="password"),
                timeout=2.0,
            )
            session = client.get_session_info()
        finally:
            server.close()

        self.assertEqual(session["session"]["MapName"], "carentan_warfare")
        self.assertEqual(server.requests[0]["name"], "ServerConnect")
        self.assertIn("authToken", server.requests[0])
        self.assertIn("version", server.requests[0])
        self.assertIn("contentBody", server.requests[0])
        self.assertNotIn("AuthToken", server.requests[0])
        self.assertEqual(server.requests[2]["name"], "GetServerInformation")
        self.assertEqual(server.requests[2]["contentBody"], "{\"Name\":\"session\",\"Value\":\"\"}")


if __name__ == "__main__":
    unittest.main()
