# Copyright 2026 dparv
# See LICENSE file for licensing details.

"""ASGI application: the MCP streamable HTTP app behind bearer-token auth."""

from __future__ import annotations

import hmac
import json
import logging
from collections.abc import MutableMapping
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from . import __version__
from .config import ServerConfig
from .juju_client import JujuClient
from .tools import register_tools

logger = logging.getLogger(__name__)

MCP_PATH = "/mcp"
HEALTH_PATH = "/healthz"

INSTRUCTIONS = """\
This server gives access to a Juju controller. Use list_models first to find
model names, then model_status to inspect applications, units, machines and
relations. debug_log returns recent agent logs for troubleshooting. Model
arguments accept a model name, '<owner>/<name>' or a model UUID.
"""

Scope = MutableMapping[str, Any]


class BearerAuthMiddleware:
    """Require ``Authorization: Bearer <token>`` on every HTTP request.

    ``/healthz`` is answered directly and without authentication so that the
    charm can probe liveness without knowing the token.
    """

    def __init__(self, app: Any, token: str):
        if not token:
            raise ValueError("token must not be empty")
        self._app = app
        self._token = token.encode()

    async def __call__(self, scope: Scope, receive: Any, send: Any) -> None:
        """Handle an ASGI request."""
        if scope["type"] != "http":
            # Lifespan events must reach the MCP app (it starts its session manager).
            await self._app(scope, receive, send)
            return
        if scope["path"] == HEALTH_PATH:
            await _respond(send, 200, {"status": "ok", "version": __version__})
            return
        if not self._authorized(scope):
            await _respond(
                send,
                401,
                {"error": "unauthorized"},
                extra_headers=[(b"www-authenticate", b'Bearer realm="juju-mcp"')],
            )
            return
        await self._app(scope, receive, send)

    def _authorized(self, scope: Scope) -> bool:
        for name, value in scope.get("headers") or []:
            if name.lower() == b"authorization":
                scheme, _, token = value.partition(b" ")
                if scheme.lower() == b"bearer":
                    return hmac.compare_digest(token.strip(), self._token)
        return False


async def _respond(
    send: Any,
    status: int,
    body: dict[str, Any],
    extra_headers: list[tuple[bytes, bytes]] | None = None,
) -> None:
    payload = json.dumps(body).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(payload)).encode()),
        *(extra_headers or []),
    ]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": payload})


def build_server(config: ServerConfig, juju: JujuClient | None = None) -> MCPServer:
    """Create the MCP server with all tools registered."""
    if juju is None:
        juju = client_from_config(config)
    server = MCPServer(
        name="juju",
        title="Juju",
        version=__version__,
        instructions=INSTRUCTIONS,
        log_level=config.log_level.upper(),  # type: ignore[arg-type]
    )
    tools = register_tools(server, juju, allow_write=config.allow_write)
    logger.info(
        "registered %d tools (%s): %s",
        len(tools),
        "read-write" if config.allow_write else "read-only",
        ", ".join(tools),
    )
    return server


def client_from_config(config: ServerConfig) -> JujuClient:
    """Create the Jubilant-based Juju client described by ``config``."""
    return JujuClient(
        endpoints=config.controller_endpoints,
        cacert=config.controller_ca_cert,
        username=config.username,
        password=config.password,
        controller_uuid=config.controller_uuid,
        cli_binary=config.juju_cli,
        juju_data=config.juju_data,
    )


def build_app(config: ServerConfig, juju: JujuClient | None = None) -> BearerAuthMiddleware:
    """Create the authenticated ASGI application."""
    server = build_server(config, juju)
    app = server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        stateless_http=True,
        host=config.listen_host,
        # Clients are authenticated with a bearer token, and the server is
        # reached by arbitrary controller addresses, so host/origin pinning
        # would only get in the way.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    return BearerAuthMiddleware(app, config.auth_token)
