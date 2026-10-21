# Copyright 2026 dparv
# See LICENSE file for licensing details.

"""MCP tools exposing Juju operations, implemented with Jubilant.

Read-only tools are always registered. Mutating tools are only registered
when the server runs with ``allow_write`` enabled. In both cases Juju's own
access control for the configured user is the final authority.

Jubilant is synchronous (it runs the ``juju`` CLI), so every tool runs its
Juju work in a worker thread to keep the server responsive.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import shlex
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, ParamSpec, TypeVar

import jubilant
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from .juju_client import WAIT_SLACK, JujuClient, JujuClientError, cli_error_message, task_to_dict

logger = logging.getLogger(__name__)

P = ParamSpec("P")
R = TypeVar("R")

ModelArg = Annotated[
    str,
    Field(description="Model name, '<owner>/<name>' or model UUID (see list_models)."),
]
ApplicationArg = Annotated[str, Field(description="Application name.")]
TimeoutArg = Annotated[float, Field(ge=1, le=3600, description="Seconds to wait for completion.")]
ConfigArg = dict[str, str | int | float | bool]

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
ADDITIVE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)


def juju_errors(fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """Report Juju CLI failures back to the model as tool errors."""

    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await fn(*args, **kwargs)
        except ToolError:
            raise
        except jubilant.CLIError as e:
            raise ToolError(cli_error_message(e)) from e
        except (JujuClientError, TimeoutError, ValueError) as e:
            raise ToolError(str(e)) from e
        except FileNotFoundError as e:
            raise ToolError(f"juju CLI not available: {e}") from e
        except Exception as e:
            # Clients are authenticated operators; a real message is more
            # useful to them than the SDK's generic "Error executing tool".
            logger.exception("tool %s failed", fn.__name__)
            raise ToolError(f"unexpected error: {type(e).__name__}: {e}") from e

    return wrapper


async def _run(fn: Callable[..., R], *args: Any, **kwargs: Any) -> R:
    return await asyncio.to_thread(fn, *args, **kwargs)


# Status summary.


def summarize_status(status: dict[str, Any]) -> dict[str, Any]:
    """Reduce ``juju status --format=json`` to the most useful fields."""
    model = status.get("model") or {}
    apps: dict[str, Any] = {}
    relations: dict[tuple[str, ...], dict[str, Any]] = {}
    for name, app in (status.get("applications") or {}).items():
        units = {u: _summarize_unit(unit) for u, unit in (app.get("units") or {}).items()}
        apps[name] = {
            "charm": app.get("charm-name") or app.get("charm"),
            "channel": app.get("charm-channel"),
            "revision": app.get("charm-rev"),
            "base": _base(app.get("base")),
            "status": _status(app.get("application-status")),
            "workload-version": app.get("version") or None,
            "exposed": app.get("exposed"),
            "life": app.get("life") or None,
            "scale": app.get("scale") or len(units),
            "subordinate-to": app.get("subordinate-to") or [],
            "units": units,
        }
        for endpoint, related in (app.get("relations") or {}).items():
            for rel in related or []:
                other = rel.get("related-application")
                key = tuple(sorted((name, other or "")) + [rel.get("interface") or ""])
                relations.setdefault(
                    key,
                    {
                        "interface": rel.get("interface"),
                        "scope": rel.get("scope"),
                        "endpoints": [],
                    },
                )["endpoints"].append(f"{name}:{endpoint}")
    machines = {
        mid: _summarize_machine(machine) for mid, machine in (status.get("machines") or {}).items()
    }
    return {
        "model": {
            "name": model.get("name"),
            "type": model.get("type"),
            "cloud": model.get("cloud"),
            "region": model.get("region"),
            "version": model.get("version"),
            "upgrade-available": model.get("upgrade-available") or None,
            "status": _status(model.get("model-status")),
        },
        "applications": apps,
        "machines": machines,
        "relations": [
            {**rel, "endpoints": sorted(set(rel["endpoints"]))} for rel in relations.values()
        ],
        "remote-applications": sorted((status.get("application-endpoints") or {}).keys()),
        "offers": sorted((status.get("offers") or {}).keys()),
    }


def _status(info: dict[str, Any] | None) -> dict[str, Any] | None:
    if not info:
        return None
    return {
        "current": info.get("current"),
        "message": info.get("message") or None,
        "since": info.get("since"),
    }


def _base(base: dict[str, Any] | None) -> str | None:
    if not base or not base.get("name"):
        return None
    return f"{base['name']}@{base.get('channel', '')}"


def _summarize_unit(unit: dict[str, Any]) -> dict[str, Any]:
    summary = {
        "workload-status": _status(unit.get("workload-status")),
        "agent-status": _status(unit.get("juju-status")),
        "leader": unit.get("leader") or False,
        "machine": unit.get("machine") or None,
        "address": unit.get("public-address") or unit.get("address") or None,
        "open-ports": unit.get("open-ports") or [],
    }
    subordinates = unit.get("subordinates") or {}
    if subordinates:
        summary["subordinates"] = {n: _summarize_unit(s) for n, s in subordinates.items()}
    return summary


def _summarize_machine(machine: dict[str, Any]) -> dict[str, Any]:
    summary = {
        "agent-status": _status(machine.get("juju-status")),
        "instance-status": _status(machine.get("machine-status")),
        "instance-id": machine.get("instance-id"),
        "hostname": machine.get("hostname"),
        "dns-name": machine.get("dns-name"),
        "ip-addresses": machine.get("ip-addresses") or [],
        "base": _base(machine.get("base")),
        "hardware": machine.get("hardware"),
    }
    containers = machine.get("containers") or {}
    if containers:
        summary["containers"] = {c: _summarize_machine(m) for c, m in containers.items()}
    return summary


# Registration.


def register_tools(server: MCPServer, juju: JujuClient, allow_write: bool) -> list[str]:
    """Register all tools on ``server``; return the names of registered tools."""
    names = _register_read_tools(server, juju)
    if allow_write:
        names += _register_write_tools(server, juju)
    return names


def _register_read_tools(server: MCPServer, juju: JujuClient) -> list[str]:  # noqa: C901
    @server.tool(annotations=READ_ONLY)
    @juju_errors
    async def controller_info() -> dict[str, Any]:
        """Show the controller this server is connected to and the user it acts as."""
        return await _run(juju.controller_info)

    @server.tool(annotations=READ_ONLY)
    @juju_errors
    async def list_models() -> dict[str, Any]:
        """List the models the configured Juju user can access."""
        return {"models": await _run(juju.model_summaries)}

    @server.tool(annotations=READ_ONLY)
    @juju_errors
    async def model_status(
        model: ModelArg,
        patterns: Annotated[
            list[str] | None,
            Field(description="Optional status filters, as for `juju status <pattern>...`."),
        ] = None,
        raw: Annotated[
            bool,
            Field(description="Return the complete `juju status --format=json` output."),
        ] = False,
    ) -> dict[str, Any]:
        """Show the status of a model: applications, units, machines and relations."""
        status = await _run(juju.status, model, patterns)
        return status if raw else summarize_status(status)

    @server.tool(annotations=READ_ONLY)
    @juju_errors
    async def get_model_config(model: ModelArg) -> dict[str, Any]:
        """Show the configuration of a model."""
        return await _run(juju.model_config, model)

    @server.tool(annotations=READ_ONLY)
    @juju_errors
    async def get_application_config(
        model: ModelArg, application: ApplicationArg
    ) -> dict[str, Any]:
        """Show an application's charm settings (value, default, source, description)."""
        return await _run(juju.application_config, model, application)

    @server.tool(annotations=READ_ONLY)
    @juju_errors
    async def list_actions(model: ModelArg, application: ApplicationArg) -> dict[str, Any]:
        """List the actions an application's charm defines, with parameter schemas."""
        return {"actions": await _run(juju.actions, model, application)}

    @server.tool(annotations=READ_ONLY)
    @juju_errors
    async def show_action(
        model: ModelArg,
        action_id: Annotated[str, Field(description="Action/task ID.")],
        wait: Annotated[
            float, Field(ge=0, le=3600, description="Seconds to wait for completion.")
        ] = 0,
    ) -> dict[str, Any]:
        """Show the status and output of an action or exec task by ID."""
        return await _run(juju.show_task, model, action_id, wait)

    @server.tool(annotations=READ_ONLY)
    @juju_errors
    async def debug_log(
        model: ModelArg,
        lines: Annotated[int, Field(ge=1, le=5000, description="Number of lines.")] = 100,
        level: Annotated[
            str, Field(description="Minimum level: TRACE, DEBUG, INFO, WARNING or ERROR.")
        ] = "",
        include: Annotated[
            list[str] | None,
            Field(description="Only these entities: units (app/0), applications or machines."),
        ] = None,
        exclude: Annotated[list[str] | None, Field(description="Exclude these entities.")] = None,
        include_module: Annotated[
            list[str] | None, Field(description="Only these logging modules.")
        ] = None,
        exclude_module: Annotated[
            list[str] | None, Field(description="Exclude these logging modules.")
        ] = None,
    ) -> dict[str, Any]:
        """Return the most recent debug-log lines for a model (like `juju debug-log`)."""
        records = await _run(
            functools.partial(
                juju.debug_log,
                model,
                lines=lines,
                level=level,
                include=include,
                exclude=exclude,
                include_module=include_module,
                exclude_module=exclude_module,
            )
        )
        return {"lines": records}

    @server.tool(annotations=READ_ONLY)
    @juju_errors
    async def list_secrets(model: ModelArg) -> dict[str, Any]:
        """List secrets in a model (metadata only, secret content is never returned)."""
        return {"secrets": await _run(juju.secrets, model)}

    return [
        "controller_info",
        "list_models",
        "model_status",
        "get_model_config",
        "get_application_config",
        "list_actions",
        "show_action",
        "debug_log",
        "list_secrets",
    ]


def _register_write_tools(server: MCPServer, juju: JujuClient) -> list[str]:  # noqa: C901
    @server.tool(annotations=ADDITIVE)
    @juju_errors
    async def run_action(
        model: ModelArg,
        unit: Annotated[str, Field(description="Unit name, e.g. 'mysql/0' or 'mysql/leader'.")],
        action: Annotated[str, Field(description="Action name (see list_actions).")],
        params: Annotated[dict[str, Any] | None, Field(description="Action parameters.")] = None,
        wait: TimeoutArg = 300,
    ) -> dict[str, Any]:
        """Run a charm action on a unit and wait for its result."""

        def run() -> dict[str, Any]:
            client = juju.model(model, timeout=wait + WAIT_SLACK)
            try:
                return task_to_dict(client.run(unit, action, params, wait=wait))
            except jubilant.TaskError as e:
                # A failed action is a result, not a tool failure.
                return task_to_dict(e.task)

        return await _run(run)

    @server.tool(annotations=DESTRUCTIVE)
    @juju_errors
    async def exec_command(
        model: ModelArg,
        command: Annotated[str, Field(description="Shell command to run (as root).")],
        units: Annotated[
            list[str] | None, Field(description="Units to run on, e.g. ['mysql/0'].")
        ] = None,
        applications: Annotated[
            list[str] | None, Field(description="Run on all units of these applications.")
        ] = None,
        machines: Annotated[
            list[str] | None, Field(description="Machines to run on, e.g. ['0'].")
        ] = None,
        wait: TimeoutArg = 300,
    ) -> dict[str, Any]:
        """Run a shell command on units, applications or machines (like `juju exec`)."""
        if not (units or applications or machines):
            raise ToolError("specify at least one of units, applications or machines")
        results = await _run(
            functools.partial(
                juju.exec,
                model,
                command,
                units=units,
                applications=applications,
                machines=machines,
                wait=wait,
            )
        )
        return {"results": results}

    @server.tool(annotations=ADDITIVE)
    @juju_errors
    async def set_application_config(
        model: ModelArg,
        application: ApplicationArg,
        config: Annotated[
            ConfigArg, Field(description="Config options to set, e.g. {'log-level': 'debug'}.")
        ],
    ) -> dict[str, Any]:
        """Set charm configuration options on an application."""
        await _run(lambda: juju.model(model).config(application, config))
        return {"application": application, "set": sorted(config)}

    @server.tool(annotations=ADDITIVE)
    @juju_errors
    async def reset_application_config(
        model: ModelArg,
        application: ApplicationArg,
        keys: Annotated[list[str], Field(description="Config options to reset to defaults.")],
    ) -> dict[str, Any]:
        """Reset charm configuration options of an application to their defaults."""
        await _run(lambda: juju.model(model).config(application, reset=keys))
        return {"application": application, "reset": sorted(keys)}

    @server.tool(annotations=ADDITIVE)
    @juju_errors
    async def deploy(
        model: ModelArg,
        charm: Annotated[str, Field(description="Charmhub charm name, e.g. 'postgresql'.")],
        application: Annotated[
            str | None, Field(description="Application name (defaults to charm name).")
        ] = None,
        channel: Annotated[str | None, Field(description="Channel, e.g. '14/stable'.")] = None,
        revision: Annotated[int | None, Field(description="Charm revision.")] = None,
        base: Annotated[str | None, Field(description="Base, e.g. 'ubuntu@24.04'.")] = None,
        num_units: Annotated[int, Field(ge=0, le=100)] = 1,
        config: ConfigArg | None = None,
        constraints: Annotated[
            str | None, Field(description="Constraints, e.g. 'cores=2 mem=4G'.")
        ] = None,
        to: Annotated[str | None, Field(description="Placement directive.")] = None,
        trust: Annotated[bool, Field(description="Grant access to cloud credentials.")] = False,
    ) -> dict[str, Any]:
        """Deploy a charm from Charmhub into a model."""
        if charm.startswith(("local:", "/", ".", "~")):
            raise ToolError("only Charmhub charms can be deployed through MCP")
        await _run(
            lambda: juju.model(model, timeout=300).deploy(
                charm,
                application,
                channel=channel,
                revision=revision,
                base=base,
                num_units=num_units,
                config=config,
                constraints=_parse_constraints(constraints),
                to=to,
                trust=trust,
            )
        )
        return {"deployed": application or charm, "model": model}

    @server.tool(annotations=ADDITIVE)
    @juju_errors
    async def add_units(
        model: ModelArg,
        application: ApplicationArg,
        count: Annotated[int, Field(ge=1, le=100)] = 1,
        to: Annotated[str | None, Field(description="Placement directive.")] = None,
    ) -> dict[str, Any]:
        """Add units to an application."""
        await _run(lambda: juju.model(model).add_unit(application, num_units=count, to=to))
        return {"application": application, "added": count}

    @server.tool(annotations=DESTRUCTIVE)
    @juju_errors
    async def remove_units(
        model: ModelArg,
        units: Annotated[list[str], Field(min_length=1, description="Units to remove.")],
        destroy_storage: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        """Remove units from their applications."""
        await _run(
            lambda: juju.model(model).remove_unit(
                *units, destroy_storage=destroy_storage, force=force
            )
        )
        return {"removing": units}

    @server.tool(annotations=ADDITIVE)
    @juju_errors
    async def integrate(
        model: ModelArg,
        endpoint_a: Annotated[str, Field(description="'app' or 'app:endpoint'.")],
        endpoint_b: Annotated[str, Field(description="'app' or 'app:endpoint'.")],
    ) -> dict[str, Any]:
        """Integrate (relate) two applications."""
        await _run(lambda: juju.model(model).integrate(endpoint_a, endpoint_b))
        return {"integrated": [endpoint_a, endpoint_b]}

    @server.tool(annotations=DESTRUCTIVE)
    @juju_errors
    async def remove_relation(
        model: ModelArg,
        endpoint_a: Annotated[str, Field(description="'app' or 'app:endpoint'.")],
        endpoint_b: Annotated[str, Field(description="'app' or 'app:endpoint'.")],
        force: bool = False,
    ) -> dict[str, Any]:
        """Remove the relation between two applications."""
        await _run(lambda: juju.model(model).remove_relation(endpoint_a, endpoint_b, force=force))
        return {"removed": [endpoint_a, endpoint_b]}

    @server.tool(annotations=ADDITIVE)
    @juju_errors
    async def refresh_application(
        model: ModelArg,
        application: ApplicationArg,
        channel: Annotated[str | None, Field(description="Switch to this channel.")] = None,
        revision: Annotated[int | None, Field(description="Refresh to this revision.")] = None,
    ) -> dict[str, Any]:
        """Refresh (upgrade) an application's charm."""
        await _run(
            lambda: juju.model(model, timeout=300).refresh(
                application, channel=channel, revision=revision
            )
        )
        return {"refreshed": application, "channel": channel, "revision": revision}

    @server.tool(annotations=DESTRUCTIVE)
    @juju_errors
    async def remove_application(
        model: ModelArg,
        application: ApplicationArg,
        destroy_storage: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        """Remove an application and all of its units from a model."""
        await _run(
            lambda: juju.model(model).remove_application(
                application, destroy_storage=destroy_storage, force=force
            )
        )
        return {"removing": application}

    return [
        "run_action",
        "exec_command",
        "set_application_config",
        "reset_application_config",
        "deploy",
        "add_units",
        "remove_units",
        "integrate",
        "remove_relation",
        "refresh_application",
        "remove_application",
    ]


def _parse_constraints(constraints: str | None) -> dict[str, str] | None:
    """Parse 'cores=2 mem=4G' into {'cores': '2', 'mem': '4G'}."""
    if not constraints:
        return None
    parsed: dict[str, str] = {}
    for item in shlex.split(constraints):
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise ToolError(f"invalid constraint {item!r}; expected key=value")
        parsed[key] = value
    return parsed
