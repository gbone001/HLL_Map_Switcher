import asyncio
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from services.kill_feed import KillFeedService
from utils.crcon_http import CRCONHTTPError


class FakeAPIClient:
    def get_servers(self):
        return [(0, "Test Server")]


class FakeHTTPClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def get_recent_logs(self, filter_action, end):
        self.calls += 1
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def empty_logs_payload():
    return {"result": {"logs": []}}


class KillFeedServiceTests(unittest.TestCase):
    def test_poll_failure_backoff_skips_repeated_crcon_failures(self):
        http_client = FakeHTTPClient(
            [
                CRCONHTTPError("get_recent_logs failed with status 500: <html></html>"),
                empty_logs_payload(),
            ]
        )
        service = KillFeedService(
            api_client=FakeAPIClient(),
            http_client_factory=lambda _server_index: http_client,
            poll_interval_seconds=1.0,
            failure_backoff_initial_seconds=30.0,
            failure_backoff_max_seconds=60.0,
        )

        asyncio.run(service._poll_server(server_index=0, server_name="Test Server"))
        asyncio.run(service._poll_server(server_index=0, server_name="Test Server"))

        self.assertEqual(http_client.calls, 1)
        self.assertIn(0, service._failure_states)

    def test_success_clears_previous_poll_failure_state(self):
        http_client = FakeHTTPClient(
            [
                CRCONHTTPError("get_recent_logs failed with status 500: <html></html>"),
                empty_logs_payload(),
            ]
        )
        service = KillFeedService(
            api_client=FakeAPIClient(),
            http_client_factory=lambda _server_index: http_client,
            poll_interval_seconds=1.0,
            failure_backoff_initial_seconds=1.0,
            failure_backoff_max_seconds=2.0,
        )

        asyncio.run(service._poll_server(server_index=0, server_name="Test Server"))
        service._failure_states[0].next_poll_after_monotonic = 0.0
        asyncio.run(service._poll_server(server_index=0, server_name="Test Server"))

        self.assertEqual(http_client.calls, 2)
        self.assertNotIn(0, service._failure_states)

    def test_html_error_summary_keeps_logs_short(self):
        message = (
            "get_recent_logs failed with status 500: "
            "<!doctype html><html><head><title>Server Error (500)</title></head>"
            "<body><h1>Server Error (500)</h1><p></p></body></html>"
        )

        summary = KillFeedService._summarize_exception(CRCONHTTPError(message))

        self.assertEqual(
            summary,
            "get_recent_logs failed with status 500: HTML error page: Server Error (500)",
        )
        self.assertNotIn("<html", summary)


if __name__ == "__main__":
    unittest.main()
