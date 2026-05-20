"""HTTP views and player WebSocket for Mortify.

Same architecture as Quizify:

1. Static assets served with cache_headers=False.
2. ``/mortify/play`` serves the player HTML from disk; the join code is
   read from ``?code=`` query string.
3. ``/api/mortify/qr`` renders a QR PNG for the join URL.
4. ``/api/mortify/player_ws`` is a *raw* aiohttp WebSocket route
   registered via ``hass.http.app.router.add_get`` — NOT a
   ``HomeAssistantView``. The HA view middleware interferes with
   unauthenticated WebSocket upgrades, which is the bug the original
   Mortify hit (and which Quizify works around the same way).
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import time
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web
from homeassistant.components.http import HomeAssistantView, StaticPathConfig
from homeassistant.core import HomeAssistant

from .const import (
    JOIN_URL_PREFIX,
    MAX_PLAYER_NAME_LENGTH,
    MAX_SESSION_ID_LENGTH,
    MAX_TOKEN_LENGTH,
    PLAY_URL,
    PLAYER_WS_JOIN_TIMEOUT,
    PLAYER_WS_STRICT_ORIGIN,
    PLAYER_WS_URL,
    QR_RATE_LIMIT_REQUESTS,
    QR_RATE_LIMIT_WINDOW,
    STATIC_URL,
)
from .game import InvalidStateError, SessionFullError
from .manager import get_manager

_LOGGER = logging.getLogger(__name__)

FRONTEND_DIST = Path(__file__).parent / "frontend" / "dist"

_NO_CACHE_HEADERS = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
    # Same defensive headers as Quizify for the unauthenticated player page.
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "SAMEORIGIN",
}

_html_cache: dict[str, str] = {}


def _read_file(path: Path) -> str:
    return path.read_text(encoding="utf-8")


async def _get_html(hass: HomeAssistant, path: Path) -> str | None:
    key = str(path)
    if key in _html_cache:
        return _html_cache[key]
    if not path.exists():
        return None
    content = await hass.async_add_executor_job(_read_file, path)
    _html_cache[key] = content
    return content


def _html_response(text: str) -> web.Response:
    return web.Response(
        text=text, content_type="text/html",
        charset="utf-8", headers=_NO_CACHE_HEADERS,
    )


async def async_register_views(hass: HomeAssistant) -> None:
    """Register views, raw WS route, and static paths."""
    hass.http.register_view(MortifyQRView(hass))
    hass.http.register_view(MortifyPlayView(hass))
    hass.http.register_view(MortifyJoinRedirectView)

    ws_handler = MortifyPlayerWSHandler(hass)
    hass.http.app.router.add_get(PLAYER_WS_URL, ws_handler.handle)

    if FRONTEND_DIST.exists():
        await hass.http.async_register_static_paths([
            StaticPathConfig(STATIC_URL, str(FRONTEND_DIST), cache_headers=False),
        ])
    else:
        _LOGGER.warning(
            "Mortify frontend missing at %s — the panel won't load",
            FRONTEND_DIST,
        )


# ---------------------------------------------------------------------------
# QR
# ---------------------------------------------------------------------------

class MortifyQRView(HomeAssistantView):
    url = "/api/mortify/qr"
    name = "api:mortify:qr"
    requires_auth = False
    MAX_LEN = 512

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._rate_buckets: dict[str, list[float]] = {}
        self._last_sweep: float = 0.0

    def _check_rate_limit(self, ip: str) -> bool:
        now = time.time()
        cutoff = now - QR_RATE_LIMIT_WINDOW
        if now - self._last_sweep > 300:
            self._rate_buckets = {
                k: [t for t in v if t > cutoff]
                for k, v in self._rate_buckets.items()
                if any(t > cutoff for t in v)
            }
            self._last_sweep = now
        times = [t for t in self._rate_buckets.get(ip, []) if t > cutoff]
        if len(times) >= QR_RATE_LIMIT_REQUESTS:
            self._rate_buckets[ip] = times
            return False
        times.append(now)
        self._rate_buckets[ip] = times
        return True

    async def get(self, request: web.Request) -> web.Response:
        client_ip = request.remote or "unknown"
        if not self._check_rate_limit(client_ip):
            return web.Response(status=429, text="Too many QR requests")
        data = request.query.get("data", "")
        if not data:
            return web.Response(status=400, text="missing 'data' parameter")
        if len(data) > self.MAX_LEN:
            return web.Response(status=400, text="'data' too long")

        def _make() -> bytes:
            import qrcode  # noqa: PLC0415
            qr = qrcode.QRCode(
                version=None,
                error_correction=qrcode.constants.ERROR_CORRECT_M,
                box_size=12, border=2,
            )
            qr.add_data(data)
            qr.make(fit=True)
            # Dark gold tone to fit the Mortify aesthetic; pure white background.
            img = qr.make_image(fill_color="#0d0b0e", back_color="#ffffff")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()

        png = await self.hass.async_add_executor_job(_make)
        return web.Response(
            body=png, content_type="image/png",
            headers={"Cache-Control": "public, max-age=300"},
        )


# ---------------------------------------------------------------------------
# Player page
# ---------------------------------------------------------------------------

class MortifyPlayView(HomeAssistantView):
    """Serve the player HTML. The join code comes from ``?code=`` in JS."""

    url = PLAY_URL
    name = "mortify:play"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request) -> web.Response:  # noqa: ARG002
        html_path = FRONTEND_DIST / "player.html"
        content = await _get_html(self.hass, html_path)
        if content is None:
            return web.Response(text="Player page not found", status=404)
        return _html_response(content)


class MortifyJoinRedirectView(HomeAssistantView):
    """Redirect old ``/mortify/join/CODE`` URLs to ``/mortify/play?code=CODE``."""

    url = f"{JOIN_URL_PREFIX}/{{join_code}}"
    name = "mortify:join_redirect"
    requires_auth = False

    async def get(self, request: web.Request, join_code: str = "") -> web.Response:  # noqa: ARG002
        sanitized = "".join(c for c in join_code.upper() if c.isalnum())[:6]
        target = f"{PLAY_URL}?code={sanitized}" if sanitized else PLAY_URL
        raise web.HTTPFound(location=target)


# ---------------------------------------------------------------------------
# Player WebSocket — raw aiohttp handler, NOT HomeAssistantView
# ---------------------------------------------------------------------------

class MortifyPlayerWSHandler:
    """Raw aiohttp WebSocket handler for guest players.

    The same workaround Quizify uses: registering as a
    HomeAssistantView triggers HA middleware that interferes with the
    unauthenticated WebSocket upgrade and returns 500. Registering
    directly on the underlying aiohttp router bypasses it.
    """

    MAX_MSG_SIZE = 16 * 1024
    RATE_LIMIT_BURST = 20
    RATE_LIMIT_WINDOW = 5.0
    RATE_LIMIT_CONNECTIONS = 10
    RATE_LIMIT_CONN_WINDOW = 60

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._conn_buckets: dict[str, list[float]] = {}
        self._last_sweep: float = 0.0

    def _check_connection_rate_limit(self, ip: str) -> bool:
        now = time.time()
        cutoff = now - self.RATE_LIMIT_CONN_WINDOW
        if now - self._last_sweep > 300:
            self._conn_buckets = {
                k: [t for t in v if t > cutoff]
                for k, v in self._conn_buckets.items()
                if any(t > cutoff for t in v)
            }
            self._last_sweep = now
        times = [t for t in self._conn_buckets.get(ip, []) if t > cutoff]
        self._conn_buckets[ip] = times
        if len(times) >= self.RATE_LIMIT_CONNECTIONS:
            return False
        times.append(now)
        return True

    def _check_origin(self, request: web.Request) -> bool:
        """Origin policy for the player WS. Same as Quizify."""
        if not PLAYER_WS_STRICT_ORIGIN:
            return True
        origin = request.headers.get("Origin")
        if not origin:
            return True  # Native apps, QR launchers, curl.
        host = request.headers.get("Host", "")
        if not host:
            return False
        try:
            from urllib.parse import urlparse  # noqa: PLC0415
            parsed = urlparse(origin)
            origin_authority = parsed.netloc.lower()
        except Exception:  # noqa: BLE001
            return False
        return origin_authority == host.lower()

    async def handle(self, request: web.Request) -> web.StreamResponse:
        client_ip = request.remote or "unknown"
        if not self._check_origin(request):
            _LOGGER.warning(
                "Rejecting Mortify player WS from %r (ip=%s)",
                request.headers.get("Origin"), client_ip,
            )
            return web.Response(status=403, text="Origin not allowed")
        if not self._check_connection_rate_limit(client_ip):
            return web.Response(status=429, text="Too many connections")

        ws = web.WebSocketResponse(max_msg_size=self.MAX_MSG_SIZE, heartbeat=30)
        await ws.prepare(request)

        manager = get_manager(self.hass)
        if manager is None:
            await _ws_send(ws, {
                "event": "error", "code": "not_ready",
                "message": "Mortify is not initialised",
            })
            await ws.close()
            return ws

        bound: dict[str, Any] = {
            "session_id": None,
            "player_id": None,
            "unsubscribe": None,
        }
        msg_times: list[float] = []

        async def forward(event: dict[str, Any]) -> None:
            session = (
                manager.get_session(bound["session_id"])
                if bound["session_id"] else None
            )
            if session is None:
                return
            pid = bound["player_id"]
            payload = {
                **event,
                "game": session.to_dict(include_secrets=False),
                "you": (
                    session.players[pid].to_private_dict()
                    if pid and pid in session.players else None
                ),
            }
            await _ws_send(ws, payload)

        # Idle-join watchdog (matches Quizify).
        async def _idle_join_watchdog() -> None:
            try:
                await asyncio.sleep(PLAYER_WS_JOIN_TIMEOUT)
            except asyncio.CancelledError:
                return
            if bound["session_id"] is None and not ws.closed:
                _LOGGER.debug(
                    "Closing idle Mortify player WS (ip=%s)", client_ip,
                )
                try:
                    await _ws_send(ws, {
                        "event": "error", "code": "idle_timeout",
                        "message": "No join received",
                    })
                except Exception:  # noqa: BLE001
                    pass
                await ws.close()

        idle_task = asyncio.create_task(_idle_join_watchdog())

        try:
            async for msg in ws:
                if msg.type == WSMsgType.ERROR:
                    _LOGGER.debug("Mortify WS error: %s", ws.exception())
                    break
                if msg.type != WSMsgType.TEXT:
                    continue

                now = asyncio.get_running_loop().time()
                msg_times.append(now)
                msg_times[:] = [
                    t for t in msg_times
                    if now - t <= self.RATE_LIMIT_WINDOW
                ]
                if len(msg_times) > self.RATE_LIMIT_BURST:
                    await _ws_send(ws, {
                        "event": "error", "code": "rate_limited",
                        "message": "Slow down",
                    })
                    continue

                try:
                    data = json.loads(msg.data)
                except (TypeError, ValueError):
                    await _ws_send(ws, {
                        "event": "error", "code": "bad_json",
                        "message": "Malformed JSON",
                    })
                    continue
                if not isinstance(data, dict):
                    continue

                msg_type = data.get("type")
                try:
                    if msg_type == "ping":
                        await _ws_send(ws, {"event": "pong"})
                    elif msg_type == "join":
                        await _handle_join(manager, ws, data, bound, forward)
                    elif msg_type == "resume":
                        await _handle_resume(manager, ws, data, bound, forward)
                    elif msg_type == "discover_clue":
                        await _handle_discover_clue(manager, ws, data, bound)
                    elif msg_type == "interrogate":
                        await _handle_interrogate(manager, ws, data, bound)
                    elif msg_type == "accuse":
                        await _handle_accuse(manager, ws, data, bound)
                    elif msg_type == "leave":
                        break
                    else:
                        await _ws_send(ws, {
                            "event": "error", "code": "unknown_type",
                            "message": f"Unknown type {msg_type!r}",
                        })
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("Mortify player WS handler crashed")
                    await _ws_send(ws, {
                        "event": "error", "code": "internal",
                        "message": "Internal error",
                    })
        finally:
            idle_task.cancel()
            try:
                await idle_task
            except (asyncio.CancelledError, Exception):
                pass
            unsubscribe = bound.get("unsubscribe")
            if unsubscribe:
                try:
                    unsubscribe()
                except Exception:  # noqa: BLE001
                    _LOGGER.debug("Unsubscribe raised", exc_info=True)
            # Remove the player from the session — they've disconnected.
            sid = bound.get("session_id")
            pid = bound.get("player_id")
            if sid and pid:
                session = manager.get_session(sid)
                if session is not None:
                    # Quiet removal so the broadcast goes out cleanly.
                    session.remove_player(pid)
            if not ws.closed:
                await ws.close()
        return ws


# ---------------------------------------------------------------------------
# Protocol handlers
# ---------------------------------------------------------------------------

async def _handle_join(manager, ws, data, bound, forward):
    if bound["session_id"] is not None:
        await _ws_send(ws, {
            "event": "error", "code": "already_joined",
            "message": "Already joined",
        })
        return
    join_code = str(data.get("join_code", "")).strip().upper()
    name = str(data.get("name", "")).strip()
    if not join_code or not name:
        await _ws_send(ws, {
            "event": "error", "code": "bad_request",
            "message": "join_code and name are required",
        })
        return
    if len(join_code) > 16 or len(name) > 200:
        await _ws_send(ws, {
            "event": "error", "code": "bad_request",
            "message": "Input too long",
        })
        return
    if len(name) > MAX_PLAYER_NAME_LENGTH:
        name = name[:MAX_PLAYER_NAME_LENGTH]
    session = manager.get_by_join_code(join_code)
    if session is None:
        await _ws_send(ws, {
            "event": "error", "code": "not_found",
            "message": "No game with that code",
        })
        return
    try:
        player = session.add_player(name)
    except SessionFullError as err:
        await _ws_send(ws, {
            "event": "error", "code": "session_full",
            "message": str(err),
        })
        return
    token = manager.issue_player_token(session.session_id, player.player_id)
    bound["session_id"] = session.session_id
    bound["player_id"] = player.player_id
    bound["unsubscribe"] = session.subscribe(forward)
    await _ws_send(ws, {
        "event": "joined",
        "player_id": player.player_id,
        "player_token": token,
        "session_id": session.session_id,
        "name": player.name,
        "game": session.to_dict(include_secrets=False),
        "you": player.to_private_dict(),
    })


async def _handle_resume(manager, ws, data, bound, forward):
    if bound["session_id"] is not None:
        await _ws_send(ws, {
            "event": "error", "code": "already_joined",
            "message": "Already joined",
        })
        return
    session_id = str(data.get("session_id", ""))
    token = str(data.get("player_token", ""))
    if not session_id or not token:
        await _ws_send(ws, {
            "event": "error", "code": "bad_request",
            "message": "session_id and player_token required",
        })
        return
    if (
        len(session_id) > MAX_SESSION_ID_LENGTH
        or len(token) > MAX_TOKEN_LENGTH
    ):
        await _ws_send(ws, {
            "event": "error", "code": "bad_request",
            "message": "Token or session_id too long",
        })
        return
    verified = manager.verify_player_token(token, session_id)
    if not verified:
        await _ws_send(ws, {
            "event": "error", "code": "invalid_token",
            "message": "Session expired",
        })
        return
    session = manager.get_session(session_id)
    if session is None or verified not in session.players:
        await _ws_send(ws, {
            "event": "error", "code": "not_found",
            "message": "Session no longer exists",
        })
        return
    player = session.players[verified]
    bound["session_id"] = session_id
    bound["player_id"] = verified
    bound["unsubscribe"] = session.subscribe(forward)
    await _ws_send(ws, {
        "event": "resumed",
        "player_id": player.player_id,
        "player_token": token,
        "session_id": session.session_id,
        "name": player.name,
        "game": session.to_dict(include_secrets=False),
        "you": player.to_private_dict(),
    })


async def _handle_discover_clue(manager, ws, data, bound):
    if bound["session_id"] is None:
        await _ws_send(ws, {
            "event": "error", "code": "not_joined",
            "message": "Join first",
        })
        return
    entity_id = data.get("entity_id")
    if not isinstance(entity_id, str) or not entity_id:
        await _ws_send(ws, {
            "event": "error", "code": "bad_request",
            "message": "entity_id required",
        })
        return
    session = manager.get_session(bound["session_id"])
    if session is None:
        await _ws_send(ws, {
            "event": "error", "code": "not_found",
            "message": "Session ended",
        })
        return
    try:
        payload = await session.discover_clue(bound["player_id"], entity_id)
    except InvalidStateError as err:
        await _ws_send(ws, {
            "event": "error", "code": "invalid_state",
            "message": str(err),
        })
        return
    # Private response — only the discovering player gets the clue text.
    await _ws_send(ws, {"event": "clue_result", **payload})


async def _handle_interrogate(manager, ws, data, bound):
    if bound["session_id"] is None:
        await _ws_send(ws, {
            "event": "error", "code": "not_joined",
            "message": "Join first",
        })
        return
    suspect_role_id = data.get("suspect_role_id")
    question = data.get("question")
    if (
        not isinstance(suspect_role_id, str)
        or not isinstance(question, str)
        or not suspect_role_id
        or not question.strip()
    ):
        await _ws_send(ws, {
            "event": "error", "code": "bad_request",
            "message": "suspect_role_id and question required",
        })
        return
    session = manager.get_session(bound["session_id"])
    if session is None:
        await _ws_send(ws, {
            "event": "error", "code": "not_found",
            "message": "Session ended",
        })
        return
    try:
        result = await session.submit_interrogation(
            bound["player_id"], suspect_role_id, question,
        )
    except InvalidStateError as err:
        await _ws_send(ws, {
            "event": "error", "code": "invalid_state",
            "message": str(err),
        })
        return
    await _ws_send(ws, {"event": "interrogation_result", **result})


async def _handle_accuse(manager, ws, data, bound):
    if bound["session_id"] is None:
        await _ws_send(ws, {
            "event": "error", "code": "not_joined",
            "message": "Join first",
        })
        return
    accused = data.get("accused_role_id")
    if not isinstance(accused, str) or not accused:
        await _ws_send(ws, {
            "event": "error", "code": "bad_request",
            "message": "accused_role_id required",
        })
        return
    session = manager.get_session(bound["session_id"])
    if session is None:
        await _ws_send(ws, {
            "event": "error", "code": "not_found",
            "message": "Session ended",
        })
        return
    try:
        result = await session.submit_accusation(bound["player_id"], accused)
    except InvalidStateError as err:
        await _ws_send(ws, {
            "event": "error", "code": "invalid_state",
            "message": str(err),
        })
        return
    await _ws_send(ws, {"event": "accuse_result", **result})


async def _ws_send(ws: web.WebSocketResponse, payload: dict[str, Any]) -> bool:
    if ws.closed:
        return False
    try:
        await ws.send_json(payload)
        return True
    except ConnectionResetError:
        return False
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Mortify WS send failed", exc_info=True)
        return False
