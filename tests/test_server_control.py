import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from services.server_control import GameStatus, ServerControlService
from utils.crcon_http import CRCONHTTPError
from utils.hll_rcon import HLLRCONError


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
                session={
                    "session": {
                        "MapName": "carentan_warfare",
                        "AlliedPlayerCount": 20,
                        "AxisPlayerCount": 21,
                        "RemainingMatchTime": 1500,
                    }
                }
            ),
        )

        result = service.get_status(0)

        self.assertTrue(result.success)
        self.assertEqual(result.transport, "direct_rcon")
        self.assertEqual(result.data["status"].current_map, "carentan_warfare")
        self.assertIn("CRCON HTTP", result.warning)

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


if __name__ == "__main__":
    unittest.main()
