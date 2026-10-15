import unittest
from pathlib import Path
import sys

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from utils.crcon_http import CRCONCredentials, CRCONHTTPError, CRCONHttpClient


class FakeSession:
    def __init__(self, response: requests.Response):
        self.response = response

    def get(self, url, headers=None, timeout=None):
        return self.response


def build_response(status_code: int, body: str) -> requests.Response:
    response = requests.Response()
    response.status_code = status_code
    response._content = body.encode("utf-8")
    response.encoding = "utf-8"
    return response


class CRCONHttpClientTests(unittest.TestCase):
    def test_get_maps_summarizes_html_error_body(self):
        html_error = """
        <!doctype html>
        <html lang="en">
        <head><title>Server Error (500)</title></head>
        <body><h1>Server Error (500)</h1><p></p></body>
        </html>
        """
        client = CRCONHttpClient(credentials=CRCONCredentials(base_url="https://crcon.example", token="token"))
        client.session = FakeSession(build_response(500, html_error))

        with self.assertRaises(CRCONHTTPError) as context:
            client.get_maps()

        self.assertEqual(
            str(context.exception),
            "get_maps failed with status 500: HTML error page: Server Error (500)",
        )
        self.assertNotIn("<html", str(context.exception))

    def test_status_error_keeps_plain_text_body(self):
        response = build_response(503, "upstream unavailable")

        message = CRCONHttpClient._status_error_message("get_gamestate", response)

        self.assertEqual(message, "get_gamestate failed with status 503: upstream unavailable")

    def test_json_parse_error_summarizes_html_body(self):
        response = build_response(200, "<html><head><title>Login Required</title></head></html>")

        with self.assertRaises(CRCONHTTPError) as context:
            CRCONHttpClient._parse_json(response)

        self.assertEqual(str(context.exception), "Failed to parse JSON response: HTML error page: Login Required")


class CRCONEnvelopeErrorTests(unittest.TestCase):
    """CRCON v12 returns HTTP 400 + {"failed": true, "error": ...} when a command fails."""

    def test_status_error_uses_crcon_error_field(self):
        body = (
            '{"result": null, "command": "set_map", "arguments": {"map_name": "nope"}, '
            '"failed": true, "error": "Unknown map nope", "forward_results": null, "version": "v12.3.0"}'
        )

        message = CRCONHttpClient._status_error_message("set_map", build_response(400, body))

        self.assertEqual(message, "set_map failed with status 400: Unknown map nope")

    def test_permission_denied_surfaces_crcon_message(self):
        body = '{"command": "/api/add_admin", "failed": true, "error": "You do not have the required permissions to use this"}'

        message = CRCONHttpClient._status_error_message("add_admin", build_response(403, body))

        self.assertIn("required permissions", message)
        self.assertNotIn("command", message)

    def test_json_without_error_falls_back_to_body(self):
        message = CRCONHttpClient._status_error_message("get_maps", build_response(500, '{"failed": true, "error": null}'))

        self.assertIn('"failed": true', message)


class RaisingSession:
    def __init__(self, exception: Exception):
        self.exception = exception

    def get(self, url, **kwargs):
        raise self.exception

    post = get


class CRCONHttpNetworkErrorTests(unittest.TestCase):
    def build_client(self, exception: Exception) -> CRCONHttpClient:
        client = CRCONHttpClient(CRCONCredentials(base_url="https://crcon.example/api", token="token"))
        client.session = RaisingSession(exception)
        return client

    def test_connection_error_is_wrapped(self):
        client = self.build_client(requests.ConnectionError("connection refused"))

        with self.assertRaises(CRCONHTTPError) as ctx:
            client.get_gamestate()

        self.assertIn("ConnectionError", str(ctx.exception))
        self.assertIsInstance(ctx.exception.__cause__, requests.ConnectionError)

    def test_timeout_is_wrapped_with_timeout_message(self):
        client = self.build_client(requests.ReadTimeout("read timed out"))

        with self.assertRaises(CRCONHTTPError) as ctx:
            client.set_map("carentan_warfare")

        self.assertIn("timed out", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
