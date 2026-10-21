# Copyright 2026 dparv
# See LICENSE file for licensing details.
#
# Run against a bootstrapped machine controller, after `charmcraft pack`:
#   tox -e integration

import asyncio
import json
import logging
import pathlib
import secrets
import ssl
import tempfile

import httpx2
import jubilant
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

logger = logging.getLogger(__name__)

APP = "juju-mcp"
MCP_USER = "mcp-it"
MCP_PASSWORD = secrets.token_urlsafe(16)


@pytest.mark.juju_setup
def test_deploy(charm: pathlib.Path, controller_juju: jubilant.Juju):
    """Deploy as a subordinate of the controller and wait for credentials."""
    juju = controller_juju
    juju.deploy(charm, app=APP)
    juju.integrate(f"{APP}:controller", "controller")
    juju.wait(lambda status: jubilant.all_blocked(status, APP))
    message = juju.status().apps[APP].units[f"{APP}/0"].workload_status.message
    assert "credentials-secret" in message


@pytest.mark.juju_setup
def test_configure_credentials(controller_juju: jubilant.Juju):
    """Create a dedicated Juju user, hand it over via a secret, and go active."""
    juju = controller_juju
    juju.cli("add-user", MCP_USER, include_model=False)
    juju.cli(
        "change-user-password", MCP_USER, "--no-prompt", include_model=False, stdin=MCP_PASSWORD
    )
    juju.cli("grant", MCP_USER, "read", "controller", include_model=False)
    uri = juju.add_secret("mcp-credentials", {"username": MCP_USER, "password": MCP_PASSWORD})
    juju.grant_secret(uri, APP)
    juju.config(APP, {"credentials-secret": uri})
    juju.wait(lambda status: jubilant.all_active(status, APP))


def _connection_info(juju: jubilant.Juju) -> dict[str, str]:
    task = juju.run(f"{APP}/0", "get-connection-info")
    return task.results


def test_tools_over_mcp(controller_juju: jubilant.Juju):
    """Talk MCP to the deployed server and call read-only tools."""
    info = _connection_info(controller_juju)
    assert info["url"].startswith("https://")

    async def run() -> tuple[set[str], dict]:
        with tempfile.NamedTemporaryFile("w", suffix=".pem") as ca:
            ca.write(info["ca-cert"])
            ca.flush()
            context = ssl.create_default_context(cafile=ca.name)
            http = httpx2.AsyncClient(
                headers={"Authorization": f"Bearer {info['token']}"}, verify=context
            )
            async with http, Client(streamable_http_client(info["url"], http_client=http)) as c:
                tools = {t.name for t in (await c.list_tools()).tools}
                result = await c.call_tool("list_models", {})
                assert not result.is_error, result.content
                return tools, result.structured_content or {}

    tools, models = asyncio.run(run())
    assert "model_status" in tools
    assert "deploy" not in tools  # read-only by default
    assert any(m["name"] == "controller" for m in models["models"])


def test_rejects_bad_token(controller_juju: jubilant.Juju):
    info = _connection_info(controller_juju)
    response = httpx2.post(info["url"], json={}, verify=False)  # noqa: S501
    assert response.status_code == 401


def test_allow_write_exposes_mutating_tools(controller_juju: jubilant.Juju):
    juju = controller_juju
    juju.config(APP, {"allow-write": True})
    juju.wait(lambda status: jubilant.all_active(status, APP))
    status = juju.status().apps[APP].units[f"{APP}/0"].workload_status.message
    assert status.startswith("read-write")


def test_rotate_token(controller_juju: jubilant.Juju):
    before = _connection_info(controller_juju)["token"]
    controller_juju.run(f"{APP}/0", "rotate-token")
    after = _connection_info(controller_juju)["token"]
    assert before != after
    logger.info("token rotated: %s", json.dumps({"changed": before != after}))
