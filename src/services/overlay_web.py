import asyncio
import logging
import os
from typing import Any, Dict, Optional

from aiohttp import web

from services.kill_feed import KillFeedService

logger = logging.getLogger(__name__)

OVERLAY_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>HLL Match Overlay</title>
  <style>
    :root {
      --bg-strong: rgba(8, 12, 18, 0.92);
      --bg-panel: rgba(8, 12, 18, 0.78);
      --bg-soft: rgba(255, 255, 255, 0.06);
      --border: rgba(255, 255, 255, 0.14);
      --text: #f4efe5;
      --muted: rgba(244, 239, 229, 0.72);
      --allies: #6db4ff;
      --axis: #ff7b6d;
      --accent: #d8b36a;
      --shadow: 0 18px 48px rgba(0, 0, 0, 0.38);
    }

    * {
      box-sizing: border-box;
    }

    html, body {
      margin: 0;
      width: 100%;
      height: 100%;
      overflow: hidden;
      background: transparent;
      color: var(--text);
      font-family: "Segoe UI", Tahoma, Geneva, Verdana, sans-serif;
    }

    body {
      position: relative;
    }

    .overlay-shell {
      position: relative;
      width: 100%;
      height: 100%;
      padding: 20px 28px 24px;
    }

    .top-bar {
      display: grid;
      grid-template-columns: minmax(220px, 1fr) minmax(260px, auto) minmax(220px, 1fr);
      align-items: center;
      gap: 16px;
      width: min(1120px, calc(100vw - 56px));
      margin: 0 auto;
    }

    .team-panel,
    .match-panel,
    .ticker-panel {
      border: 1px solid var(--border);
      backdrop-filter: blur(12px);
      box-shadow: var(--shadow);
    }

    .team-panel {
      display: flex;
      align-items: center;
      justify-content: space-between;
      min-height: 78px;
      padding: 14px 18px;
      border-radius: 18px;
      background:
        linear-gradient(135deg, rgba(255, 255, 255, 0.06), transparent 55%),
        linear-gradient(180deg, var(--bg-strong), rgba(8, 12, 18, 0.72));
    }

    .team-panel.allies {
      border-color: rgba(109, 180, 255, 0.4);
    }

    .team-panel.axis {
      border-color: rgba(255, 123, 109, 0.4);
    }

    .team-meta {
      display: grid;
      gap: 4px;
    }

    .team-label {
      font-size: 12px;
      letter-spacing: 0.14em;
      text-transform: uppercase;
      color: var(--muted);
    }

    .team-panel.allies .team-name {
      color: var(--allies);
    }

    .team-panel.axis .team-name {
      color: var(--axis);
    }

    .team-name {
      font-size: 24px;
      font-weight: 700;
      line-height: 1;
    }

    .team-count {
      font-size: 34px;
      font-weight: 800;
      line-height: 1;
      letter-spacing: -0.04em;
    }

    .match-panel {
      min-height: 78px;
      padding: 14px 22px;
      border-radius: 22px;
      background:
        radial-gradient(circle at top, rgba(216, 179, 106, 0.18), transparent 58%),
        linear-gradient(180deg, var(--bg-strong), rgba(8, 12, 18, 0.8));
      text-align: center;
    }

    .server-name {
      font-size: 13px;
      letter-spacing: 0.16em;
      text-transform: uppercase;
      color: var(--muted);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .map-name {
      margin-top: 6px;
      font-size: 28px;
      font-weight: 800;
      line-height: 1.1;
      letter-spacing: -0.03em;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .bottom-center {
      position: absolute;
      left: 50%;
      bottom: 24px;
      transform: translateX(-50%);
      width: min(980px, calc(100vw - 56px));
    }

    .ticker-panel {
      min-height: 112px;
      padding: 12px 16px;
      border-radius: 18px;
      background:
        linear-gradient(135deg, rgba(216, 179, 106, 0.14), transparent 45%),
        linear-gradient(180deg, var(--bg-strong), var(--bg-panel));
      overflow: hidden;
    }

    .ticker-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 10px;
      font-size: 11px;
      letter-spacing: 0.16em;
      text-transform: uppercase;
      color: var(--muted);
    }

    .ticker-title {
      color: var(--accent);
      font-weight: 700;
    }

    .ticker-viewport {
      position: relative;
      height: 60px;
      overflow: hidden;
      mask-image: linear-gradient(to bottom, transparent 0%, black 14%, black 86%, transparent 100%);
    }

    .ticker-track {
      display: grid;
      gap: 8px;
      will-change: transform;
    }

    .ticker-entry {
      display: grid;
      grid-template-columns: auto minmax(0, 1fr) auto minmax(0, 1fr);
      align-items: center;
      gap: 10px;
      min-height: 26px;
      padding: 0 2px;
      font-size: 22px;
      font-weight: 700;
      line-height: 1;
    }

    .ticker-entry.team-kill .ticker-weapon {
      border-color: rgba(255, 123, 109, 0.34);
      color: var(--axis);
      background: rgba(255, 123, 109, 0.08);
    }

    .ticker-player {
      min-width: 0;
      overflow: hidden;
      white-space: nowrap;
      text-overflow: ellipsis;
      text-shadow: 0 2px 10px rgba(0, 0, 0, 0.4);
    }

    .ticker-player.victim {
      text-align: right;
    }

    .ticker-weapon {
      min-width: 104px;
      padding: 7px 12px;
      border-radius: 999px;
      border: 1px solid rgba(216, 179, 106, 0.34);
      background: rgba(216, 179, 106, 0.08);
      color: var(--accent);
      font-size: 11px;
      font-weight: 800;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      text-align: center;
    }

    .ticker-separator {
      font-size: 14px;
      color: var(--muted);
      letter-spacing: 0.14em;
      text-transform: uppercase;
    }

    .ticker-empty {
      display: flex;
      align-items: center;
      justify-content: center;
      height: 100%;
      color: var(--muted);
      font-size: 14px;
      letter-spacing: 0.12em;
      text-transform: uppercase;
    }
  </style>
</head>
<body>
  <div class="overlay-shell">
    <header class="top-bar">
      <section class="team-panel allies">
        <div class="team-meta">
          <span class="team-label">Allied Players</span>
          <span class="team-name">Allies</span>
        </div>
        <div class="team-count" id="alliesCount">0/50</div>
      </section>

      <section class="match-panel">
        <div class="server-name" id="serverName">Waiting for server</div>
        <div class="map-name" id="mapName">Overlay Loading</div>
      </section>

      <section class="team-panel axis">
        <div class="team-meta">
          <span class="team-label">Axis Players</span>
          <span class="team-name">Axis</span>
        </div>
        <div class="team-count" id="axisCount">0/50</div>
      </section>
    </header>

    <section class="bottom-center">
      <div class="ticker-panel">
        <div class="ticker-header">
          <span class="ticker-title">Live Kill Feed</span>
          <span id="tickerServer">Waiting for kills</span>
        </div>
        <div class="ticker-viewport">
          <div class="ticker-track" id="tickerTrack">
            <div class="ticker-empty">Waiting for kill events...</div>
          </div>
        </div>
      </div>
    </section>
  </div>

  <script>
    const params = new URLSearchParams(window.location.search);
    const server = params.get("server");
    const limit = params.get("limit") || "8";

    const alliesCountElement = document.getElementById("alliesCount");
    const axisCountElement = document.getElementById("axisCount");
    const serverNameElement = document.getElementById("serverName");
    const mapNameElement = document.getElementById("mapName");
    const tickerServerElement = document.getElementById("tickerServer");
    const tickerTrackElement = document.getElementById("tickerTrack");

    let animationFrameId = null;
    let lastAnimationTimestamp = 0;
    let currentOffset = 0;
    let scrollActive = false;

    function escapeHtml(value) {
      return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#39;");
    }

    function stopTickerAnimation() {
      scrollActive = false;
      if (animationFrameId !== null) {
        window.cancelAnimationFrame(animationFrameId);
        animationFrameId = null;
      }
      currentOffset = 0;
      lastAnimationTimestamp = 0;
      tickerTrackElement.style.transform = "translateY(0px)";
    }

    function animateTicker(timestamp) {
      if (!scrollActive) {
        return;
      }

      if (!lastAnimationTimestamp) {
        lastAnimationTimestamp = timestamp;
      }

      const elapsed = timestamp - lastAnimationTimestamp;
      lastAnimationTimestamp = timestamp;
      currentOffset += elapsed * 0.024;

      const firstEntry = tickerTrackElement.firstElementChild;
      if (firstEntry) {
        const entryHeight = firstEntry.getBoundingClientRect().height + 8;
        if (entryHeight > 0 && currentOffset >= entryHeight) {
          currentOffset -= entryHeight;
          tickerTrackElement.appendChild(firstEntry.cloneNode(true));
          tickerTrackElement.removeChild(firstEntry);
        }
      }

      tickerTrackElement.style.transform = `translateY(-${currentOffset}px)`;
      animationFrameId = window.requestAnimationFrame(animateTicker);
    }

    function startTickerAnimationIfNeeded() {
      const entries = tickerTrackElement.querySelectorAll(".ticker-entry");
      const firstEntry = tickerTrackElement.firstElementChild;
      const entryHeight = firstEntry ? firstEntry.getBoundingClientRect().height : 0;

      if (entries.length < 3 || entryHeight <= 0) {
        stopTickerAnimation();
        return;
      }

      scrollActive = true;
      if (animationFrameId === null) {
        animationFrameId = window.requestAnimationFrame(animateTicker);
      }
    }

    function buildTickerEntry(event) {
      const teamKill = event.action === "TEAM KILL";
      const killer = escapeHtml(event.killer_name);
      const victim = escapeHtml(event.victim_name);
      const weapon = escapeHtml(event.weapon || "UNKNOWN");

      return `
        <article class="ticker-entry ${teamKill ? "team-kill" : ""}">
          <span class="ticker-player killer">${killer}</span>
          <span class="ticker-separator">eliminated</span>
          <span class="ticker-weapon">${weapon}</span>
          <span class="ticker-player victim">${victim}</span>
        </article>
      `;
    }

    function renderTicker(events) {
      if (!Array.isArray(events) || events.length === 0) {
        stopTickerAnimation();
        tickerTrackElement.innerHTML = '<div class="ticker-empty">Waiting for kill events...</div>';
        tickerServerElement.textContent = "Waiting for kills";
        return;
      }

      const normalizedEvents = events.slice(0, Number(limit));
      const repeatedEntries = normalizedEvents.length >= 3
        ? normalizedEvents.concat(normalizedEvents)
        : normalizedEvents;

      stopTickerAnimation();
      tickerTrackElement.innerHTML = repeatedEntries.map(buildTickerEntry).join("");
      tickerServerElement.textContent = normalizedEvents[0].server_name || "Live server";
      startTickerAnimationIfNeeded();
    }

    function renderTopBar(state) {
      const teamCapacity = Number(state.team_capacity || 50);
      alliesCountElement.textContent = `${Number(state.allied_players || 0)}/${teamCapacity}`;
      axisCountElement.textContent = `${Number(state.axis_players || 0)}/${teamCapacity}`;
      serverNameElement.textContent = state.server_name || "Server unavailable";
      mapNameElement.textContent = state.current_map || "Map unavailable";
    }

    async function fetchJson(path, query) {
      const response = await fetch(`${path}?${query.toString()}`, { cache: "no-store" });
      if (!response.ok) {
        throw new Error(`${path} failed with status ${response.status}`);
      }
      return response.json();
    }

    async function refreshOverlay() {
      const query = new URLSearchParams({ limit });
      if (server) {
        query.set("server", server);
      }

      const [feedPayload, statePayload] = await Promise.all([
        fetchJson("/api/kill-feed", query),
        fetchJson("/api/overlay-state", query),
      ]);

      renderTicker(Array.isArray(feedPayload.events) ? feedPayload.events : []);
      renderTopBar(statePayload);
    }

    async function loop() {
      try {
        await refreshOverlay();
      } catch (error) {
        console.error(error);
      } finally {
        window.setTimeout(loop, 1000);
      }
    }

    loop();
  </script>
</body>
</html>
"""


class KillFeedWebServer:
    """Expose kill feed JSON, overlay state, and OBS overlay HTML over HTTP."""

    def __init__(
        self,
        kill_feed_service: KillFeedService,
        host: str,
        port: int,
        team_player_cap: int,
    ) -> None:
        self.kill_feed_service = kill_feed_service
        self.host = host
        self.port = port
        self.team_player_cap = team_player_cap
        self._app = web.Application()
        self._app.add_routes(
            [
                web.get("/", self.handle_index),
                web.get("/health", self.handle_health),
                web.get("/api/kill-feed", self.handle_kill_feed),
                web.get("/api/overlay-state", self.handle_overlay_state),
                web.get("/overlay", self.handle_overlay),
            ]
        )
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._started = False

    async def start(self) -> None:
        if self._started:
            return

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, host=self.host, port=self.port)
        await self._site.start()
        self._started = True
        logger.info("Kill feed web server listening on %s:%s", self.host, self.port)

    async def stop(self) -> None:
        if self._runner is None:
            return

        await self._runner.cleanup()
        self._runner = None
        self._site = None
        self._started = False

    async def handle_index(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "service": "hll-kill-feed",
                "overlay_url": "/overlay",
                "feed_url": "/api/kill-feed",
                "overlay_state_url": "/api/overlay-state",
            }
        )

    async def handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def handle_kill_feed(self, request: web.Request) -> web.Response:
        limit, server_index = self._parse_request_filters(request)
        events = await self.kill_feed_service.get_events(server_index=server_index, limit=limit)
        return web.json_response({"events": events})

    async def handle_overlay_state(self, request: web.Request) -> web.Response:
        _, server_index = self._parse_request_filters(request)
        resolved_server_index = server_index

        if resolved_server_index is None:
            servers = self.kill_feed_service.api_client.get_servers()
            resolved_server_index = servers[0][0] if servers else None

        payload = {
            "server_index": None if resolved_server_index is None else resolved_server_index + 1,
            "server_name": "Server unavailable",
            "current_map": "Map unavailable",
            "allied_players": 0,
            "axis_players": 0,
            "team_capacity": self.team_player_cap,
        }

        if resolved_server_index is None:
            return web.json_response(payload)

        server_name = self.kill_feed_service.api_client.get_server_name(resolved_server_index)
        payload["server_name"] = server_name

        client = self.kill_feed_service.http_client_factory(resolved_server_index)
        if client is None:
            return web.json_response(payload)

        try:
            gamestate_response = await asyncio.to_thread(client.get_gamestate)
        except Exception as exc:
            logger.warning("Overlay state gamestate lookup failed for server '%s': %s", server_name, exc)
            return web.json_response(payload)

        gamestate = gamestate_response.get("result") if isinstance(gamestate_response, dict) else None
        if not isinstance(gamestate, dict):
            return web.json_response(payload)

        current_map = gamestate.get("current_map", {}) if isinstance(gamestate.get("current_map"), dict) else {}
        payload["current_map"] = current_map.get("pretty_name") or current_map.get("id") or "Map unavailable"
        payload["allied_players"] = self._safe_int(gamestate.get("num_allied_players"))
        payload["axis_players"] = self._safe_int(gamestate.get("num_axis_players"))
        return web.json_response(payload)

    async def handle_overlay(self, request: web.Request) -> web.Response:
        return web.Response(text=OVERLAY_HTML, content_type="text/html")

    def _parse_request_filters(self, request: web.Request) -> tuple[Optional[int], Optional[int]]:
        limit_value = request.query.get("limit")
        server_value = request.query.get("server")

        limit: Optional[int] = None
        if limit_value:
            try:
                limit = max(1, int(limit_value))
            except ValueError as exc:
                raise web.HTTPBadRequest(text='{"error":"Invalid limit parameter"}', content_type="application/json") from exc

        server_index: Optional[int] = None
        if server_value:
            try:
                server_number = int(server_value)
            except ValueError as exc:
                raise web.HTTPBadRequest(text='{"error":"Invalid server parameter"}', content_type="application/json") from exc

            if server_number <= 0:
                raise web.HTTPBadRequest(
                    text='{"error":"Server parameter must be >= 1"}',
                    content_type="application/json",
                )
            server_index = server_number - 1

        return limit, server_index

    @staticmethod
    def _safe_int(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0


def build_kill_feed_web_server(kill_feed_service: KillFeedService) -> KillFeedWebServer:
    host = os.getenv("KILL_FEED_HOST", "0.0.0.0")
    port = int(os.getenv("KILL_FEED_PORT", os.getenv("PORT", "8080")))
    team_player_cap = int(os.getenv("OVERLAY_TEAM_PLAYER_CAP", "50"))
    return KillFeedWebServer(
        kill_feed_service=kill_feed_service,
        host=host,
        port=port,
        team_player_cap=team_player_cap,
    )
