# Copyright 2026 dparv
# See LICENSE file for licensing details.

import json
import os
import pathlib
import sys
import textwrap
from typing import Any

import pytest
import yaml
from mcp import Client
from starlette.testclient import TestClient

from juju_mcp_server.app import BearerAuthMiddleware, build_app, build_server
from juju_mcp_server.config import ConfigError, ServerConfig
from juju_mcp_server.juju_client import (
    JujuClient,
    JujuClientError,
    cli_error_message,
    parse_debug_log,
)
from juju_mcp_server.tools import _parse_constraints, summarize_status

BASE_CONFIG: dict[str, Any] = {
    "controller_endpoints": ["127.0.0.1:17070"],
    "controller_ca_cert": "CA",
    "username": "mcp",
    "password": "pw",
    "auth_token": "secret-token",
}


def _config(**overrides: Any) -> ServerConfig:
    return ServerConfig.from_dict({**BASE_CONFIG, **overrides})


class FakeJuju:
    """Stand-in for JujuClient that never runs the Juju CLI."""

    username = "mcp"

    def __init__(self, models: list[dict[str, Any]] | None = None):
        self.models = models or []

    def model_summaries(self) -> list[dict[str, Any]]:
        return self.models

    def debug_log(self, model: str, **kwargs: Any) -> list[dict[str, Any]]:
        raise JujuClientError(f"model {model!r} not found or not accessible")

    def controller_info(self) -> dict[str, Any]:
        raise RuntimeError("boom")


@pytest.fixture
def anyio_backend():
    return "asyncio"


# Config.


def test_config_round_trip(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({**BASE_CONFIG, "unknown": 1}))
    config = ServerConfig.load(path)
    assert config.username == "mcp"
    assert config.tls_enabled is False
    assert config.juju_cli == "/snap/juju/current/bin/juju"


@pytest.mark.parametrize(
    "overrides",
    [
        {"controller_endpoints": []},
        {"auth_token": ""},
        {"listen_port": 0},
        {"tls_cert_file": "/x"},
        {"log_level": "loud"},
    ],
)
def test_config_validation(overrides):
    with pytest.raises(ConfigError):
        _config(**overrides)


# Auth middleware.


async def _echo_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"inner"})


def test_auth_rejects_missing_and_wrong_token():
    client = TestClient(BearerAuthMiddleware(_echo_app, "tok"))
    response = client.get("/mcp")
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")
    assert client.get("/mcp", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/mcp", headers={"Authorization": "Basic dG9r"}).status_code == 401


def test_auth_accepts_token_and_health_is_open():
    client = TestClient(BearerAuthMiddleware(_echo_app, "tok"))
    response = client.get("/mcp", headers={"Authorization": "bearer tok"})
    assert response.status_code == 200 and response.text == "inner"
    health = client.get("/healthz")
    assert health.status_code == 200 and health.json()["status"] == "ok"


def test_mcp_endpoint_over_http():
    app = build_app(_config(), juju=FakeJuju())  # type: ignore[arg-type]
    headers = {
        "Authorization": "Bearer secret-token",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": "2025-06-18",
    }
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        },
    }
    with TestClient(app) as client:
        assert client.post("/mcp", json=request).status_code == 401
        response = client.post("/mcp", json=request, headers=headers)
    assert response.status_code == 200
    assert '"serverInfo"' in response.text


# Tools with a fake client.


async def _tool_names(allow_write: bool) -> set[str]:
    server = build_server(_config(allow_write=allow_write), juju=FakeJuju())  # type: ignore[arg-type]
    async with Client(server) as client:
        return {t.name for t in (await client.list_tools()).tools}


@pytest.mark.anyio
async def test_read_only_tools_by_default():
    names = await _tool_names(allow_write=False)
    assert {"list_models", "model_status", "debug_log"} <= names
    assert not names & {"run_action", "exec_command", "deploy", "remove_application"}


@pytest.mark.anyio
async def test_write_tools_when_allowed():
    names = await _tool_names(allow_write=True)
    assert {"run_action", "exec_command", "deploy", "remove_application"} <= names


@pytest.mark.anyio
async def test_list_models_tool():
    models = [{"name": "default", "qualified-name": "admin/default", "uuid": "u"}]
    server = build_server(_config(), juju=FakeJuju(models))  # type: ignore[arg-type]
    async with Client(server) as client:
        result = await client.call_tool("list_models", {})
    assert not result.is_error
    assert result.structured_content == {"models": models}


@pytest.mark.anyio
async def test_juju_errors_are_reported_to_the_model():
    server = build_server(_config(), juju=FakeJuju())  # type: ignore[arg-type]
    async with Client(server) as client:
        result = await client.call_tool("debug_log", {"model": "nope"})
    assert result.is_error
    assert "not found or not accessible" in result.content[0].text  # type: ignore[union-attr]


@pytest.mark.anyio
async def test_unexpected_errors_carry_their_message():
    server = build_server(_config(), juju=FakeJuju())  # type: ignore[arg-type]
    async with Client(server) as client:
        result = await client.call_tool("controller_info", {})
    assert result.is_error
    assert "unexpected error: RuntimeError: boom" in result.content[0].text  # type: ignore[union-attr]


# The real JujuClient (Jubilant) against a fake `juju` executable.

FAKE_JUJU = textwrap.dedent(
    """\
    import json, os, sys
    args = sys.argv[1:]
    with open(os.environ["FAKE_JUJU_LOG"], "a") as log:
        log.write(json.dumps({"args": args, "juju_data": os.environ.get("JUJU_DATA")}) + "\\n")
    cmd = args[0]
    if cmd == "models":
        print(json.dumps({"models": [
            {"name": "admin/web", "short-name": "web", "model-uuid":
             "11111111-2222-3333-4444-555555555555", "model-type": "iaas", "owner": "admin",
             "status": {"current": "available"}, "access": "read"},
            {"name": "bob/web", "short-name": "web", "model-uuid":
             "66666666-2222-3333-4444-555555555555", "model-type": "iaas", "owner": "bob",
             "status": {"current": "available"}, "access": "read"},
            {"name": "admin/db", "short-name": "db", "model-uuid":
             "77777777-2222-3333-4444-555555555555", "model-type": "caas", "owner": "admin",
             "status": {"current": "available"}, "access": "write"},
        ]}))
    elif cmd == "show-controller":
        print(json.dumps({"mcp": {"details": {"uuid": "c-uuid", "agent-version": "3.6.14",
              "cloud": "lxd"}, "account": {"user": "mcp", "access": "login"}}}))
    elif cmd == "status":
        print(json.dumps({"model": {"name": "db", "type": "caas"}, "applications": {
            "pg": {"charm-name": "postgresql-k8s", "charm-rev": 3,
                   "application-status": {"current": "active"}, "units": {}}}}))
    elif cmd == "debug-log":
        print("unit-pg-0: 2026-10-21 10:00:00 ERROR juju.worker.uniter hook failed")
        print("  traceback line")
    elif cmd == "run":
        print(json.dumps({"pg/0": {"id": "7", "status": "failed", "message": "boom",
              "results": {"return-code": 1, "stderr": "bad"}}}))
        print("ERROR the following task failed:", file=sys.stderr)
        sys.exit(1)
    elif cmd == "config":
        print(json.dumps({"application": "pg", "charm": "postgresql-k8s",
              "settings": {"x": {"value": 1, "source": "default"}}}))
    elif cmd == "secrets":
        print(json.dumps({}))
    else:
        print("ERROR unsupported command " + cmd, file=sys.stderr)
        sys.exit(2)
    """
)


@pytest.fixture
def fake_cli(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> JujuClient:
    script = tmp_path / "juju"
    script.write_text(f"#!{sys.executable}\n" + FAKE_JUJU)
    script.chmod(0o755)
    monkeypatch.setenv("FAKE_JUJU_LOG", str(tmp_path / "calls.log"))
    monkeypatch.delenv("JUJU_DATA", raising=False)
    client = JujuClient(
        endpoints=["127.0.0.1:17070", "10.0.0.1:17070"],
        cacert="CA-PEM",
        username="mcp",
        password="pw",
        controller_uuid="c-uuid",
        cli_binary=str(script),
        juju_data=tmp_path / "juju-data",
    )
    client.setup()
    return client


def _calls(tmp_path: pathlib.Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (tmp_path / "calls.log").read_text().splitlines()]


def test_setup_writes_private_juju_data(fake_cli: JujuClient, tmp_path: pathlib.Path):
    data = tmp_path / "juju-data"
    assert os.environ["JUJU_DATA"] == str(data)
    assert oct(data.stat().st_mode & 0o777) == "0o700"
    controllers = yaml.safe_load((data / "controllers.yaml").read_text())
    assert controllers["current-controller"] == "mcp"
    assert controllers["controllers"]["mcp"] == {
        "api-endpoints": ["127.0.0.1:17070", "10.0.0.1:17070"],
        "ca-cert": "CA-PEM",
        "uuid": "c-uuid",
    }
    accounts = yaml.safe_load((data / "accounts.yaml").read_text())
    assert accounts["controllers"]["mcp"] == {"user": "mcp", "password": "pw"}
    assert oct((data / "accounts.yaml").stat().st_mode & 0o777) == "0o600"


def test_model_resolution(fake_cli: JujuClient):
    assert fake_cli.resolve_model("db") == "admin/db"
    assert fake_cli.resolve_model("bob/web") == "bob/web"
    assert fake_cli.resolve_model("77777777-2222-3333-4444-555555555555") == "admin/db"
    with pytest.raises(JujuClientError, match="ambiguous"):
        fake_cli.resolve_model("web")
    with pytest.raises(JujuClientError, match="not found"):
        fake_cli.resolve_model("nope")


def test_status_uses_qualified_model(fake_cli: JujuClient, tmp_path: pathlib.Path):
    status = fake_cli.status("db", ["pg"])
    assert status["applications"]["pg"]["charm-rev"] == 3
    call = _calls(tmp_path)[-1]
    assert call["args"] == ["status", "--model", "mcp:admin/db", "pg", "--format", "json"]
    assert call["juju_data"] == str(tmp_path / "juju-data")


def test_debug_log_parsing_and_flags(fake_cli: JujuClient, tmp_path: pathlib.Path):
    records = fake_cli.debug_log("db", lines=5, level="error", include=["pg/0"])
    assert records == [
        {
            "timestamp": "2026-10-21 10:00:00",
            "entity": "unit-pg-0",
            "level": "ERROR",
            "module": "juju.worker.uniter",
            "message": "hook failed\n  traceback line",
        }
    ]
    args = _calls(tmp_path)[-1]["args"]
    assert args[:3] == ["debug-log", "--model", "mcp:admin/db"]
    assert ["--limit", "5"] == args[3:5] and "--include" in args and "ERROR" in args


@pytest.mark.anyio
async def test_failed_action_is_a_result_not_an_error(fake_cli: JujuClient):
    server = build_server(_config(allow_write=True), juju=fake_cli)
    async with Client(server) as client:
        result = await client.call_tool(
            "run_action", {"model": "db", "unit": "pg/0", "action": "backup"}
        )
    assert not result.is_error, result.content
    assert result.structured_content is not None
    assert result.structured_content["status"] == "failed"
    assert result.structured_content["return-code"] == 1


@pytest.mark.anyio
async def test_cli_errors_become_tool_errors(fake_cli: JujuClient):
    server = build_server(_config(allow_write=True), juju=fake_cli)
    async with Client(server) as client:
        result = await client.call_tool(
            "integrate", {"model": "db", "endpoint_a": "a", "endpoint_b": "b"}
        )
    assert result.is_error
    assert "unsupported command integrate" in result.content[0].text  # type: ignore[union-attr]


@pytest.mark.anyio
async def test_controller_info_and_app_config(fake_cli: JujuClient):
    server = build_server(_config(), juju=fake_cli)
    async with Client(server) as client:
        info = await client.call_tool("controller_info", {})
        config = await client.call_tool(
            "get_application_config", {"model": "db", "application": "pg"}
        )
    assert info.structured_content == {
        "controller-uuid": "c-uuid",
        "server-version": "3.6.14",
        "cloud": "lxd",
        "region": None,
        "api-endpoints": None,
        "user": "mcp",
        "access": "login",
    }
    assert config.structured_content is not None
    assert config.structured_content["settings"]["x"]["source"] == "default"


# Helpers.


def test_parse_debug_log_ignores_noise():
    assert parse_debug_log("\nrandom preamble\n") == []


def test_cli_error_message():
    class Err:
        returncode = 1
        stderr = "some info\nERROR permission denied\n"

    assert cli_error_message(Err()) == "permission denied"  # type: ignore[arg-type]


def test_parse_constraints():
    assert _parse_constraints("cores=2 mem=4G") == {"cores": "2", "mem": "4G"}
    assert _parse_constraints(None) is None


def test_summarize_status():
    status = {
        "model": {
            "name": "m",
            "type": "iaas",
            "cloud": "lxd",
            "version": "3.6.14",
            "model-status": {"current": "available"},
        },
        "applications": {
            "app": {
                "charm-name": "app",
                "charm-channel": "stable",
                "charm-rev": 3,
                "base": {"name": "ubuntu", "channel": "24.04"},
                "application-status": {"current": "active", "message": "ready"},
                "relations": {
                    "juju-info": [
                        {
                            "related-application": "sub",
                            "interface": "juju-info",
                            "scope": "container",
                        }
                    ]
                },
                "units": {
                    "app/0": {
                        "workload-status": {"current": "active", "message": "ready"},
                        "juju-status": {"current": "idle"},
                        "leader": True,
                        "machine": "0",
                        "public-address": "10.0.0.5",
                        "subordinates": {
                            "sub/0": {"workload-status": {"current": "blocked", "message": "x"}}
                        },
                    }
                },
            },
            "sub": {
                "charm-name": "sub",
                "subordinate-to": ["app"],
                "relations": {
                    "juju-info": [
                        {
                            "related-application": "app",
                            "interface": "juju-info",
                            "scope": "container",
                        }
                    ]
                },
            },
        },
        "machines": {
            "0": {
                "juju-status": {"current": "started"},
                "dns-name": "10.0.0.5",
                "base": {"name": "ubuntu", "channel": "24.04"},
            }
        },
    }
    summary = summarize_status(status)
    assert summary["model"]["cloud"] == "lxd"
    app = summary["applications"]["app"]
    assert app["base"] == "ubuntu@24.04"
    assert app["status"]["current"] == "active"
    assert app["scale"] == 1
    unit = app["units"]["app/0"]
    assert unit["leader"] is True
    assert unit["agent-status"]["current"] == "idle"
    assert unit["subordinates"]["sub/0"]["workload-status"]["current"] == "blocked"
    assert summary["relations"] == [
        {
            "interface": "juju-info",
            "scope": "container",
            "endpoints": ["app:juju-info", "sub:juju-info"],
        }
    ]
    assert summary["machines"]["0"]["agent-status"]["current"] == "started"
