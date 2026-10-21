#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=2.3,<3"]
# ///
# Copyright 2026 dparv
# See LICENSE file for licensing details.

r"""Example MCP client for the juju-mcp charm.

Connection details (URL, bearer token, CA certificate) are either fetched
from Juju by running the charm's `get-connection-info` action, or passed in
through environment variables.

Examples:
    # Fetch connection info from the deployed charm and run a short demo.
    uv run examples/client.py -m juju36:controller

    # List the tools the server exposes.
    uv run examples/client.py -m juju36:controller tools

    # Call any tool with JSON arguments.
    uv run examples/client.py -m juju36:controller call list_models
    uv run examples/client.py -m juju36:controller call model_status '{"model": "default"}'
    uv run examples/client.py -m juju36:controller call debug_log \
        '{"model": "default", "lines": 20, "level": "WARNING"}'

    # Reach the server through another address (e.g. an OpenStack floating IP).
    uv run examples/client.py -m juju36:controller --url https://192.168.2.29:8765/mcp

    # Without the juju CLI: provide the details yourself.
    export JUJU_MCP_URL=https://10.0.0.1:8765/mcp
    export JUJU_MCP_TOKEN=...
    export JUJU_MCP_CA_CERT=/path/to/ca.pem   # omit if the cert is publicly trusted
    uv run examples/client.py tools
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import json
import os
import ssl
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client


@dataclasses.dataclass(frozen=True)
class ConnectionInfo:
    """Everything needed to reach the MCP server."""

    url: str
    token: str
    ca_cert: str | None = None


def info_from_juju(model: str, app: str) -> ConnectionInfo:
    """Run the charm's get-connection-info action and parse its results."""
    cmd = ["juju", "run", "-m", model, f"{app}/leader", "get-connection-info", "--format=json"]
    try:
        out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
    except subprocess.CalledProcessError as e:
        sys.exit(f"`{' '.join(cmd)}` failed:\n{e.stderr}")
    # Output is keyed by unit name: {"juju-mcp/0": {"results": {...}, ...}}
    task = next(iter(json.loads(out).values()))
    results = task.get("results") or {}
    if "url" not in results or "token" not in results:
        sys.exit(f"unexpected action output: {task}")
    return ConnectionInfo(results["url"], results["token"], results.get("ca-cert"))


def info_from_env() -> ConnectionInfo:
    """Read connection details from JUJU_MCP_* environment variables."""
    url, token = os.environ.get("JUJU_MCP_URL"), os.environ.get("JUJU_MCP_TOKEN")
    if not url or not token:
        sys.exit("set JUJU_MCP_URL and JUJU_MCP_TOKEN, or pass -m <controller:model>")
    ca_path = os.environ.get("JUJU_MCP_CA_CERT")
    return ConnectionInfo(url, token, Path(ca_path).read_text() if ca_path else None)


@contextlib.asynccontextmanager
async def connect(info: ConnectionInfo, insecure: bool = False) -> AsyncIterator[Client]:
    """Open an MCP session over streamable HTTP with bearer-token auth."""
    if insecure:
        verify: ssl.SSLContext | bool = False
    elif info.ca_cert:
        # The charm's default certificate is self-signed: trust exactly that
        # certificate. Pinning it makes the hostname check redundant, and
        # skipping it lets clients use addresses not in the certificate
        # (floating IPs, NAT, SSH tunnels).
        context = ssl.create_default_context(cadata=info.ca_cert)
        context.check_hostname = False
        verify = context
    else:
        verify = True
    http = httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {info.token}"},
        verify=verify,
        # Some tools (run_action, exec_command) can take minutes.
        timeout=httpx2.Timeout(30, read=600),
    )
    async with http, Client(streamable_http_client(info.url, http_client=http)) as client:
        yield client


async def call(client: Client, name: str, arguments: dict[str, Any] | None = None) -> Any:
    """Call a tool and return its structured result, raising on tool errors."""
    result = await client.call_tool(name, arguments or {})
    text = "\n".join(getattr(c, "text", "") for c in result.content)
    if result.is_error:
        raise RuntimeError(text or f"{name} failed")
    if result.structured_content is not None:
        return result.structured_content
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


async def cmd_tools(client: Client) -> None:
    """Print the tools the server exposes."""
    for tool in (await client.list_tools()).tools:
        annotations = tool.annotations
        kind = "read-only" if annotations and annotations.read_only_hint else "write"
        summary = (tool.description or "").strip().splitlines()[0]
        print(f"{tool.name:<26} [{kind:<9}] {summary}")


async def cmd_call(client: Client, name: str, raw_args: str | None) -> None:
    """Call one tool and pretty-print the result."""
    arguments = json.loads(raw_args) if raw_args else {}
    print(json.dumps(await call(client, name, arguments), indent=2, default=str))


async def cmd_demo(client: Client) -> None:
    """Show controller info, models and a one-line status per application."""
    try:
        info = await call(client, "controller_info")
        print(f"controller {info['controller-uuid']} (Juju {info['server-version']})")
        print(f"connected as {info['user']}\n")
    except RuntimeError as e:
        print(f"controller_info failed: {e}\n")

    models = (await call(client, "list_models"))["models"]
    for model in models:
        counts = ", ".join(f"{v} {k}" for k, v in (model.get("counts") or {}).items())
        print(f"model {model['qualified-name']} [{model['status']}] {counts}")
        try:
            status = await call(client, "model_status", {"model": model["uuid"]})
        except RuntimeError as e:
            print(f"  cannot get status: {e}")
            continue
        for name, app in status["applications"].items():
            st = app["status"] or {}
            message = f" - {st['message']}" if st.get("message") else ""
            charm = (
                app["charm"] if app["revision"] is None else f"{app['charm']}@{app['revision']}"
            )
            print(f"  {name:<24} {charm:<32} {st.get('current')}{message}")
        print()


async def main() -> None:
    """Parse arguments and run the chosen command."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "-m",
        "--juju-model",
        help="model the charm is deployed in, e.g. 'juju36:controller'; "
        "if omitted, JUJU_MCP_URL/JUJU_MCP_TOKEN/JUJU_MCP_CA_CERT are used",
    )
    parser.add_argument("--app", default="juju-mcp", help="application name of the charm")
    parser.add_argument(
        "--url", help="override the server URL, e.g. to use a floating IP or an SSH tunnel"
    )
    parser.add_argument(
        "--insecure", action="store_true", help="skip TLS verification (testing only)"
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("demo", help="controller info, models and status (default)")
    sub.add_parser("tools", help="list available tools")
    call_parser = sub.add_parser("call", help="call a tool")
    call_parser.add_argument("tool", help="tool name, see `tools`")
    call_parser.add_argument("arguments", nargs="?", help="tool arguments as a JSON object")
    args = parser.parse_args()

    info = info_from_juju(args.juju_model, args.app) if args.juju_model else info_from_env()
    if args.url:
        info = dataclasses.replace(info, url=args.url)
    print(f"# connecting to {info.url}", file=sys.stderr)
    error: str | None = None
    async with connect(info, insecure=args.insecure) as client:
        # Handle tool errors inside the session; letting them escape would
        # surface as an anyio ExceptionGroup traceback.
        try:
            if args.command == "tools":
                await cmd_tools(client)
            elif args.command == "call":
                await cmd_call(client, args.tool, args.arguments)
            else:
                await cmd_demo(client)
        except (RuntimeError, json.JSONDecodeError) as e:
            error = str(e)
    if error:
        sys.exit(f"error: {error}")


if __name__ == "__main__":
    asyncio.run(main())
