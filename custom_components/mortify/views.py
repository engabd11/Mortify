"""HTTP views for Mortify."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .game_manager import MortifyGameManager

_LOGGER = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
TEMPLATES_DIR = Path(__file__).parent / "templates"


class MortifyAdminView(HomeAssistantView):
    """Serve the admin/host UI."""

    url = "/mortify/admin"
    name = "mortify:admin"
    requires_auth = False

    def __init__(self, hass: HomeAssistant, manager: MortifyGameManager) -> None:
        self.hass = hass
        self.manager = manager

    async def get(self, request: web.Request) -> web.Response:
        html_file = TEMPLATES_DIR / "admin.html"
        if html_file.exists():
            content = html_file.read_text(encoding="utf-8")
            return web.Response(content_type="text/html", text=content)
        return web.Response(status=404, text="Admin UI not found")


class MortifyPlayerView(HomeAssistantView):
    """Serve the player UI (QR code destination)."""

    url = "/mortify/play"
    name = "mortify:player"
    requires_auth = False

    def __init__(self, hass: HomeAssistant, manager: MortifyGameManager) -> None:
        self.hass = hass
        self.manager = manager

    async def get(self, request: web.Request) -> web.Response:
        html_file = TEMPLATES_DIR / "player.html"
        if html_file.exists():
            content = html_file.read_text(encoding="utf-8")
            return web.Response(content_type="text/html", text=content)
        return web.Response(status=404, text="Player UI not found")


class MortifyStaticView(HomeAssistantView):
    """Serve static assets (CSS, JS)."""

    url = "/mortify/static/{filename:.+}"
    name = "mortify:static"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request) -> web.Response:
        filename = request.match_info["filename"]
        file_path = STATIC_DIR / filename

        if not file_path.exists():
            return web.Response(status=404)

        # Security: ensure we stay within static dir
        try:
            file_path.resolve().relative_to(STATIC_DIR.resolve())
        except ValueError:
            return web.Response(status=403)

        content_type = "text/plain"
        if filename.endswith(".css"):
            content_type = "text/css"
        elif filename.endswith(".js"):
            content_type = "application/javascript"
        elif filename.endswith(".mp3"):
            content_type = "audio/mpeg"
        elif filename.endswith(".svg"):
            content_type = "image/svg+xml"

        return web.Response(
            content_type=content_type,
            body=file_path.read_bytes(),
        )


class MortifyAPIView(HomeAssistantView):
    """REST API for state queries."""

    url = "/mortify/api/{action}"
    name = "mortify:api"
    requires_auth = False

    def __init__(self, hass: HomeAssistant, manager: MortifyGameManager) -> None:
        self.hass = hass
        self.manager = manager

    async def get(self, request: web.Request) -> web.Response:
        action = request.match_info["action"]

        if action == "state":
            return web.json_response(self.manager._build_full_state())

        elif action == "speakers":
            speakers = await self.manager.async_get_available_speakers()
            return web.json_response({"speakers": speakers})

        elif action == "entities":
            entities = await self.manager.async_get_available_entities()
            return web.json_response({"entities": entities})

        elif action == "player":
            player_id = request.query.get("id")
            if not player_id:
                return web.json_response({"error": "Missing player_id"}, status=400)
            state = self.manager.get_player_private_state(player_id)
            if not state:
                return web.json_response({"error": "Player not found"}, status=404)
            return web.json_response(state)

        elif action == "qr":
            # Return the QR code data URL for the player join link
            ha_url = self.manager.ha_url.rstrip("/")
            player_url = f"{ha_url}/mortify/play?game={self.manager.game_id}"
            return web.json_response({"url": player_url, "game_id": self.manager.game_id})

        return web.json_response({"error": "Unknown action"}, status=404)

    async def post(self, request: web.Request) -> web.Response:
        action = request.match_info["action"]
        try:
            body = await request.json()
        except Exception:
            body = {}

        if action == "start":
            result = await self.manager.async_start_game(
                body.get("speaker_entity_id", ""),
                body.get("entity_ids", []),
                body.get("player_names"),
            )
            return web.json_response(result)

        elif action == "next_act":
            result = await self.manager.async_next_act()
            return web.json_response(result)

        elif action == "reset":
            await self.manager.async_reset_game()
            return web.json_response({"success": True})

        elif action == "join":
            result = await self.manager.async_player_join(
                body.get("name", "Anonymous"),
                str(id(request)),
            )
            return web.json_response(result)

        elif action == "interrogate":
            result = await self.manager.async_interrogate_suspect(
                body.get("player_id", ""),
                body.get("suspect_role_id", ""),
                body.get("question", ""),
            )
            return web.json_response(result)

        elif action == "clue":
            result = await self.manager.async_discover_clue(
                body.get("player_id", ""),
                body.get("entity_id", ""),
            )
            return web.json_response(result)

        elif action == "accuse":
            result = await self.manager.async_submit_accusation(
                body.get("player_id", ""),
                body.get("accused_role_id", ""),
            )
            return web.json_response(result)

        return web.json_response({"error": "Unknown action"}, status=404)
