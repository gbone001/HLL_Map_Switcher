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


if __name__ == "__main__":
    unittest.main()
