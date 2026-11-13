import socket
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from services.server_control import GameStatus, ServerControlService
from utils.crcon_http import CRCONCredentials, CRCONHTTPError, CRCONHttpClient
from utils.hll_rcon import HLLRCONClient, HLLRCONCredentials, HLLRCONError


class FakeHTTPClient:
    def __init__(self, *, gamestate=None, exception=None):
        self._gamestate = gamestate
        self._exception = exception

    def get_gamestate(self):
        if self._exception:
            raise self._exception
        return self._gamestate

    def set_map(self, map_id):
        return {"result": True, "map_id": map_id}

    def set_game_layout(self, sectors):
        return {"result": True, "sectors": sectors}

    def set_dynamic_weather_enabled(self, map_id, enabled):
        return {"result": True, "map_id": map_id, "enabled": enabled}

    def set_team_switch_cooldown(self, minutes):
        return {"result": True, "minutes": minutes}

    def set_match_timer(self, game_mode, minutes):
        return {"result": True, "game_mode": game_mode, "minutes": minutes}

    def set_warmup_timer(self, game_mode, minutes):
        return {"result": True, "game_mode": game_mode, "minutes": minutes}


class FakeRCONClient:
    def __init__(self, *, session=None, exception=None):
        self._session = session
        self._exception = exception

    def get_session_info(self):
        if self._exception:
            raise self._exception
        return self._session

    def change_map(self, map_id):
        return {"statusCode": 200, "map_id": map_id}

    def set_sector_layout(self, sectors):
        return {"statusCode": 200, "sectors": sectors}

    def set_dynamic_weather_enabled(self, map_id, enabled):
        return {"statusCode": 200, "map_id": map_id, "enabled": enabled}

    def set_team_switch_cooldown(self, minutes):
        return {"statusCode": 200, "minutes": minutes}

    def set_match_timer(self, game_mode, minutes):
        return {"statusCode": 200, "game_mode": game_mode, "minutes": minutes}

    def set_warmup_timer(self, game_mode, minutes):
        return {"statusCode": 200, "game_mode": game_mode, "minutes": minutes}

    def add_map_to_sequence(self, map_id, index=0):
        return {"statusCode": 200, "map_id": map_id, "index": index}


class TestableServerControlService(ServerControlService):
    def __init__(self, *, http_client=None, rcon_client=None):
        super().__init__(server_number_resolver=lambda _index: 1, http_client_factory=lambda _index: http_client)
        self._test_rcon_client = rcon_client

    def _rcon_client(self, server_index: int):
        return self._test_rcon_client


class ServerControlServiceTests(unittest.TestCase):
    def test_get_status_uses_http_when_available(self):
        service = TestableServerControlService(
            http_client=FakeHTTPClient(
                gamestate={
                    "result": {
                        "current_map": {"pretty_name": "Carentan", "id": "carentan_warfare"},
                        "num_allied_players": 22,
                        "num_axis_players": 24,
                        "time_remaining": 1800,
                        "raw_time_remaining": "30:00",
                    }
                }
            ),
            rcon_client=FakeRCONClient(),
        )

        result = service.get_status(0)

        self.assertTrue(result.success)
        self.assertEqual(result.transport, "crcon_http")
        self.assertIsInstance(result.data["status"], GameStatus)
        self.assertEqual(result.data["status"].current_map, "Carentan")

    def test_get_status_falls_back_to_direct_rcon(self):
        service = TestableServerControlService(
            http_client=FakeHTTPClient(exception=CRCONHTTPError("http down")),
            rcon_client=FakeRCONClient(
                # Shape of a live GetServerInformation(session) reply: flat and camelCase.
                session={
                    "mapName": "CARENTAN",
                    "mapId": "carentan_warfare",
                    "alliedPlayerCount": 20,
                    "axisPlayerCount": 21,
                    "remainingMatchTime": 1500,
                }
            ),
        )

        result = service.get_status(0)

        self.assertTrue(result.success)
        self.assertEqual(result.transport, "direct_rcon")
        status = result.data["status"]
        self.assertEqual(
            (status.current_map, status.allied_players, status.axis_players, status.time_remaining_seconds),
            ("CARENTAN", 20, 21, 1500),
        )
        self.assertIn("CRCON HTTP", result.warning)

    def test_rcon_status_still_reads_pascal_case_sessions(self):
        service = TestableServerControlService(
            http_client=FakeHTTPClient(exception=CRCONHTTPError("http down")),
            rcon_client=FakeRCONClient(session={"session": {"MapName": "carentan_warfare", "AlliedPlayerCount": 7}}),
        )

        status = service.get_status(0).data["status"]

        self.assertEqual((status.current_map, status.allied_players), ("carentan_warfare", 7))

    def test_change_map_falls_back_to_direct_rcon(self):
        class FailingHTTP(FakeHTTPClient):
            def set_map(self, map_id):
                raise CRCONHTTPError("http set_map failed")

        service = TestableServerControlService(
            http_client=FailingHTTP(),
            rcon_client=FakeRCONClient(),
        )

        result = service.change_map(0, "carentan_warfare")

        self.assertTrue(result.success)
        self.assertEqual(result.transport, "direct_rcon")
        self.assertIn("http set_map failed", result.warning)

    def test_add_map_to_sequence_uses_direct_rcon_only(self):
        service = TestableServerControlService(
            http_client=None,
            rcon_client=FakeRCONClient(),
        )

        result = service.add_map_to_sequence(0, "carentan_warfare", index=3)

        self.assertTrue(result.success)
        self.assertEqual(result.transport, "direct_rcon")
        self.assertEqual(result.data["index"], 3)

    def test_get_status_returns_failure_when_all_transports_fail(self):
        service = TestableServerControlService(
            http_client=FakeHTTPClient(exception=CRCONHTTPError("http down")),
            rcon_client=FakeRCONClient(exception=HLLRCONError("rcon down")),
        )

        result = service.get_status(0)

        self.assertFalse(result.success)
        self.assertIn("http down", result.message)
        self.assertIn("rcon down", result.message)


class UnreachableSession:
    def get(self, url, **kwargs):
        raise requests.ConnectionError("connection refused")

    post = get


def unreachable_http_client() -> CRCONHttpClient:
    client = CRCONHttpClient(CRCONCredentials(base_url="http://crcon.invalid/api", token="token"))
    client.session = UnreachableSession()
    return client


def closed_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class NetworkFailureFallbackTests(unittest.TestCase):
    def test_unreachable_crcon_falls_back_to_direct_rcon(self):
        service = TestableServerControlService(
            http_client=unreachable_http_client(),
            rcon_client=FakeRCONClient(session={"session": {"MapName": "carentan_warfare"}}),
        )

        result = service.get_status(0)

        self.assertTrue(result.success)
        self.assertEqual(result.transport, "direct_rcon")

    def test_change_map_falls_back_when_crcon_unreachable(self):
        service = TestableServerControlService(
            http_client=unreachable_http_client(),
            rcon_client=FakeRCONClient(),
        )

        result = service.change_map(0, "carentan_warfare")

        self.assertTrue(result.success)
        self.assertEqual(result.transport, "direct_rcon")
        self.assertIn("connection refused", result.warning)

    def test_both_transports_unreachable_reports_failure_instead_of_raising(self):
        rcon_client = HLLRCONClient(
            HLLRCONCredentials(host="127.0.0.1", port=closed_local_port(), password="secret"),
            timeout=1.0,
        )
        service = TestableServerControlService(http_client=unreachable_http_client(), rcon_client=rcon_client)

        result = service.get_status(0)

        self.assertFalse(result.success)
        self.assertIn("CRCON HTTP", result.message)
        self.assertIn("Direct RCON", result.message)


class OverlapTrackingHTTPClient(FakeHTTPClient):
    """Records how many set_map calls are in flight at once."""

    def __init__(self, delay: float = 0.2):
        super().__init__()
        self.delay = delay
        self.in_flight = 0
        self.max_in_flight = 0
        self.order = []
        self._guard = threading.Lock()

    def set_map(self, map_id):
        with self._guard:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        time.sleep(self.delay)
        with self._guard:
            self.in_flight -= 1
            self.order.append(map_id)
        return {"result": True, "map_id": map_id}


class MutationSerializationTests(unittest.TestCase):
    def test_same_server_mutations_do_not_overlap(self):
        http_client = OverlapTrackingHTTPClient()
        service = TestableServerControlService(http_client=http_client, rcon_client=FakeRCONClient())

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(service.change_map, 0, "carentan_warfare")
            time.sleep(0.05)
            second = pool.submit(service.change_map, 0, "foy_warfare")
            results = [first.result(), second.result()]

        self.assertTrue(all(result.success for result in results))
        self.assertEqual(http_client.max_in_flight, 1)
        self.assertEqual(http_client.order, ["carentan_warfare", "foy_warfare"])

    def test_different_servers_still_run_concurrently(self):
        http_client = OverlapTrackingHTTPClient()
        service = TestableServerControlService(http_client=http_client, rcon_client=FakeRCONClient())

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(service.change_map, index, "carentan_warfare") for index in (0, 1)]
            for future in futures:
                future.result()

        self.assertEqual(http_client.max_in_flight, 2)

    def test_status_reads_are_not_blocked_by_a_mutation(self):
        http_client = OverlapTrackingHTTPClient(delay=0.5)
        http_client._gamestate = {"result": {"current_map": {"pretty_name": "Carentan"}}}
        service = TestableServerControlService(http_client=http_client, rcon_client=FakeRCONClient())

        with ThreadPoolExecutor(max_workers=2) as pool:
            mutation = pool.submit(service.change_map, 0, "foy_warfare")
            time.sleep(0.05)
            started = time.monotonic()
            status = service.get_status(0)
            elapsed = time.monotonic() - started
            mutation.result()

        self.assertTrue(status.success)
        self.assertLess(elapsed, 0.25)


if __name__ == "__main__":
    unittest.main()


class RconNotConfiguredTests(unittest.TestCase):
    def test_missing_rcon_credentials_are_reported_plainly(self):
        import os
        from unittest import mock

        class DownHTTP(FakeHTTPClient):
            def set_map(self, map_id):
                raise CRCONHTTPError("http down")

        service = ServerControlService(
            server_number_resolver=lambda _index: 1,
            http_client_factory=lambda _index: DownHTTP(exception=CRCONHTTPError("http down")),
        )
        with mock.patch.dict(os.environ, {}, clear=True):
            result = service.change_map(0, "foy_warfare")
            status = service.get_status(0)

        for message in (result.message, status.message):
            self.assertIn("http down", message)
            self.assertIn("fallback not configured", message)
            self.assertNotIn("SERVER1_RCON_HOST", message)


class CurrentMapRconFallbackTests(unittest.TestCase):
    def test_get_current_map_reads_live_camel_case_session(self):
        import os
        from unittest import mock

        from utils import api_client as api_module

        env = {"SERVER1_NAME": "One", "SERVER1_RCON_HOST": "127.0.0.1", "SERVER1_RCON_PORT": "1", "SERVER1_RCON_PASSWORD": "p"}
        rcon = FakeRCONClient(session={"mapName": "EL ALAMEIN", "mapId": "elalamein_warfare"})
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(api_module.HLLRCONClient, "from_env", return_value=rcon):
            client = api_module.HLLAPIClient()
            self.assertEqual(client.get_current_map(0), "EL ALAMEIN")
