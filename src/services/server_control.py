from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from utils.crcon_http import CRCONHTTPError, CRCONHttpClient
from utils.hll_rcon import HLLRCONClient, HLLRCONError


@dataclass(frozen=True)
class ControlResult:
    success: bool
    transport: Optional[str]
    message: str
    warning: Optional[str] = None
    data: Optional[dict[str, Any]] = None


@dataclass(frozen=True)
class GameStatus:
    transport: str
    current_map: str
    allied_players: Optional[int]
    axis_players: Optional[int]
    time_remaining_seconds: Optional[int]
    raw_time_remaining: Optional[str]


class ServerControlService:
    """HTTP-first server control with direct RCON fallback for critical actions."""

    def __init__(
        self,
        server_number_resolver: Callable[[int], Optional[int]],
        http_client_factory: Callable[[int], Optional[CRCONHttpClient]],
        timeout: float = 10.0,
    ) -> None:
        self.server_number_resolver = server_number_resolver
        self.http_client_factory = http_client_factory
        self.timeout = timeout

    def get_status(self, server_index: int) -> ControlResult:
        http_client = self.http_client_factory(server_index)
        errors: list[str] = []

        if http_client is not None:
            try:
                payload = http_client.get_gamestate()
                return ControlResult(
                    success=True,
                    transport="crcon_http",
                    message="Status read through CRCON HTTP.",
                    data={"status": self._status_from_http(payload)},
                )
            except CRCONHTTPError as exc:
                errors.append(f"CRCON HTTP: {exc}")

        try:
            rcon_client = self._rcon_client(server_index)
            session_info = rcon_client.get_session_info()
            return ControlResult(
                success=True,
                transport="direct_rcon",
                message="Status read through direct RCON fallback.",
                warning="CRCON HTTP was unavailable." if errors else None,
                data={"status": self._status_from_rcon(session_info)},
            )
        except HLLRCONError as exc:
            errors.append(f"Direct RCON: {exc}")

        return ControlResult(
            success=False,
            transport=None,
            message=" ; ".join(errors) if errors else "No status transport is configured for this server.",
        )

    def change_map(self, server_index: int, map_id: str) -> ControlResult:
        return self._run_with_fallback(
            server_index=server_index,
            action_name="ChangeMap",
            http_action=lambda client: client.set_map(map_id),
            rcon_action=lambda client: client.change_map(map_id),
            success_http_message=f"Map changed through CRCON HTTP to {map_id}.",
            success_rcon_message=f"Map changed through direct RCON fallback to {map_id}.",
        )

    def set_sector_layout(self, server_index: int, sectors: list[str]) -> ControlResult:
        return self._run_with_fallback(
            server_index=server_index,
            action_name="SetSectorLayout",
            http_action=lambda client: client.set_game_layout(sectors),
            rcon_action=lambda client: client.set_sector_layout(sectors),
            success_http_message="Sector layout applied through CRCON HTTP.",
            success_rcon_message="Sector layout applied through direct RCON fallback.",
        )

    def set_dynamic_weather_enabled(self, server_index: int, map_id: str, enabled: bool) -> ControlResult:
        state = "enabled" if enabled else "disabled"
        return self._run_with_fallback(
            server_index=server_index,
            action_name="SetDynamicWeatherEnabled",
            http_action=lambda client: client.set_dynamic_weather_enabled(map_id, enabled),
            rcon_action=lambda client: client.set_dynamic_weather_enabled(map_id, enabled),
            success_http_message=f"Dynamic weather {state} through CRCON HTTP.",
            success_rcon_message=f"Dynamic weather {state} through direct RCON fallback.",
        )

    def set_team_switch_cooldown(self, server_index: int, minutes: int) -> ControlResult:
        return self._run_with_fallback(
            server_index=server_index,
            action_name="SetTeamSwitchCooldown",
            http_action=lambda client: client.set_team_switch_cooldown(minutes),
            rcon_action=lambda client: client.set_team_switch_cooldown(minutes),
            success_http_message=f"Team switch cooldown set to {minutes} minute(s) through CRCON HTTP.",
            success_rcon_message=f"Team switch cooldown set to {minutes} minute(s) through direct RCON fallback.",
        )

    def set_match_timer(self, server_index: int, game_mode: str, minutes: int) -> ControlResult:
        return self._run_with_fallback(
            server_index=server_index,
            action_name="SetMatchTimer",
            http_action=lambda client: client.set_match_timer(game_mode, minutes),
            rcon_action=lambda client: client.set_match_timer(game_mode, minutes),
            success_http_message=f"{game_mode.title()} match timer set through CRCON HTTP.",
            success_rcon_message=f"{game_mode.title()} match timer set through direct RCON fallback.",
        )

    def set_warmup_timer(self, server_index: int, game_mode: str, minutes: int) -> ControlResult:
        return self._run_with_fallback(
            server_index=server_index,
            action_name="SetWarmupTimer",
            http_action=lambda client: client.set_warmup_timer(game_mode, minutes),
            rcon_action=lambda client: client.set_warmup_timer(game_mode, minutes),
            success_http_message=f"{game_mode.title()} warmup timer set through CRCON HTTP.",
            success_rcon_message=f"{game_mode.title()} warmup timer set through direct RCON fallback.",
        )

    def add_map_to_sequence(self, server_index: int, map_id: str, index: int = 0) -> ControlResult:
        return self._run_with_fallback(
            server_index=server_index,
            action_name="AddMapToSequence",
            http_action=None,
            rcon_action=lambda client: client.add_map_to_sequence(map_id, index=index),
            success_http_message="",
            success_rcon_message=f"Added {map_id} to the map sequence through direct RCON fallback.",
        )

    def _run_with_fallback(
        self,
        *,
        server_index: int,
        action_name: str,
        http_action: Optional[Callable[[CRCONHttpClient], dict[str, Any]]],
        rcon_action: Callable[[HLLRCONClient], dict[str, Any]],
        success_http_message: str,
        success_rcon_message: str,
    ) -> ControlResult:
        errors: list[str] = []
        http_warning: Optional[str] = None

        if http_action is not None:
            http_client = self.http_client_factory(server_index)
            if http_client is not None:
                try:
                    payload = http_action(http_client)
                    warning = self._extract_warning(payload)
                    return ControlResult(
                        success=True,
                        transport="crcon_http",
                        message=success_http_message,
                        warning=warning,
                        data=payload if isinstance(payload, dict) else None,
                    )
                except CRCONHTTPError as exc:
                    http_warning = str(exc)
                    errors.append(f"CRCON HTTP: {exc}")
            else:
                http_warning = "CRCON HTTP is not configured for this server."
                errors.append(f"CRCON HTTP: {http_warning}")

        try:
            rcon_client = self._rcon_client(server_index)
            payload = rcon_action(rcon_client)
            warning = http_warning or None
            return ControlResult(
                success=True,
                transport="direct_rcon",
                message=success_rcon_message,
                warning=warning,
                data=payload if isinstance(payload, dict) else None,
            )
        except HLLRCONError as exc:
            errors.append(f"Direct RCON: {exc}")

        return ControlResult(
            success=False,
            transport=None,
            message=f"{action_name} failed. " + " ; ".join(errors),
        )

    def _rcon_client(self, server_index: int) -> HLLRCONClient:
        server_number = self.server_number_resolver(server_index)
        return HLLRCONClient.from_env(server_number=server_number, timeout=self.timeout)

    @staticmethod
    def _extract_warning(payload: dict[str, Any]) -> Optional[str]:
        if isinstance(payload, dict):
            warning = payload.get("warning")
            if warning:
                return str(warning)
        return None

    @staticmethod
    def _status_from_http(payload: dict[str, Any]) -> GameStatus:
        result = payload.get("result")
        if not isinstance(result, dict):
            raise CRCONHTTPError("CRCON HTTP get_gamestate returned an invalid payload.")

        current_map = result.get("current_map")
        current_map_name = "Unknown"
        if isinstance(current_map, dict):
            current_map_name = str(current_map.get("pretty_name") or current_map.get("id") or "Unknown")

        return GameStatus(
            transport="crcon_http",
            current_map=current_map_name,
            allied_players=ServerControlService._optional_int(result.get("num_allied_players")),
            axis_players=ServerControlService._optional_int(result.get("num_axis_players")),
            time_remaining_seconds=ServerControlService._optional_int(result.get("time_remaining")),
            raw_time_remaining=ServerControlService._optional_str(result.get("raw_time_remaining")),
        )

    @staticmethod
    def _status_from_rcon(payload: dict[str, Any]) -> GameStatus:
        session_data = ServerControlService._unwrap_session_payload(payload)

        return GameStatus(
            transport="direct_rcon",
            current_map=str(session_data.get("MapName") or "Unknown"),
            allied_players=ServerControlService._optional_int(session_data.get("AlliedPlayerCount")),
            axis_players=ServerControlService._optional_int(session_data.get("AxisPlayerCount")),
            time_remaining_seconds=ServerControlService._optional_int(session_data.get("RemainingMatchTime")),
            raw_time_remaining=None,
        )

    @staticmethod
    def _unwrap_session_payload(payload: dict[str, Any]) -> dict[str, Any]:
        if "session" in payload and isinstance(payload["session"], dict):
            return payload["session"]
        return payload

    @staticmethod
    def _optional_int(value: Any) -> Optional[int]:
        try:
            if value is None or value == "":
                return None
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _optional_str(value: Any) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        return text or None
