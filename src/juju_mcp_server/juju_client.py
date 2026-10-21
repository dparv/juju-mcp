# Copyright 2026 dparv
# See LICENSE file for licensing details.

"""Juju access for the MCP tools, built on Jubilant (the Juju CLI wrapper).

The server keeps a private ``JUJU_DATA`` directory with a single controller
entry (``controllers.yaml``) and the MCP user's credentials
(``accounts.yaml``). Every tool call runs one or more ``juju`` CLI commands
through :class:`jubilant.Juju`, so behaviour, output formats and permission
checks are exactly those of the Juju CLI matching the controller version.

Jubilant is synchronous; callers run these methods in a worker thread.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import jubilant
import yaml

logger = logging.getLogger(__name__)

#: Name of the controller entry in the private JUJU_DATA.
CONTROLLER_NAME = "mcp"
#: Default per-command timeout; long-running commands pass their own.
COMMAND_TIMEOUT = 60.0
#: Extra time granted to the CLI process beyond a command's own --wait.
WAIT_SLACK = 30.0

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
# "unit-app-0: 2026-10-21 14:37:37 INFO juju.worker.uniter ran ..."
_LOG_RE = re.compile(
    r"^(?P<entity>\S+): (?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?) "
    r"(?P<level>[A-Z]+) (?P<module>\S+) (?P<message>.*)$"
)


class JujuClientError(Exception):
    """Raised for anticipated failures talking to Juju."""


class _Juju(jubilant.Juju):
    """A :class:`jubilant.Juju` that never lets a CLI command hang forever."""

    def __init__(self, *, timeout: float = COMMAND_TIMEOUT, **kwargs: Any):
        super().__init__(**kwargs)
        self.command_timeout = timeout

    def _cli(self, *args: str, timeout: float | None = None, **kwargs: Any) -> tuple[str, str]:
        try:
            return super()._cli(*args, timeout=timeout or self.command_timeout, **kwargs)
        except subprocess.TimeoutExpired as e:
            raise JujuClientError(
                f"`juju {args[0]}` did not finish within {e.timeout:.0f}s"
            ) from None


def cli_error_message(error: jubilant.CLIError) -> str:
    """Extract the useful part of a failed CLI command's stderr."""
    lines = [line.strip() for line in (error.stderr or "").splitlines() if line.strip()]
    errors = [line.removeprefix("ERROR ").strip() for line in lines if line.startswith("ERROR")]
    return "; ".join(errors or lines[-3:]) or f"juju exited with status {error.returncode}"


def parse_debug_log(text: str) -> list[dict[str, Any]]:
    """Parse ``juju debug-log --date`` output; continuation lines are appended."""
    records: list[dict[str, Any]] = []
    for line in text.splitlines():
        if m := _LOG_RE.match(line):
            records.append(
                {
                    "timestamp": m["ts"],
                    "entity": m["entity"],
                    "level": m["level"],
                    "module": m["module"],
                    "message": m["message"],
                }
            )
        elif records and line.strip():
            records[-1]["message"] += "\n" + line
    return records


def _model_summary(model: dict[str, Any]) -> dict[str, Any]:
    status = model.get("status") or {}
    return {
        "name": model.get("short-name") or model.get("name"),
        "qualified-name": model.get("name"),
        "uuid": model.get("model-uuid"),
        "type": model.get("model-type"),
        "owner": model.get("owner"),
        "cloud": model.get("cloud"),
        "region": model.get("region"),
        "agent-version": model.get("agent-version"),
        "life": model.get("life"),
        "status": status.get("current"),
        "status-message": status.get("message"),
        "access": model.get("access"),
        "is-controller": model.get("is-controller"),
    }


class JujuClient:
    """Runs Juju CLI commands, via Jubilant, as the configured MCP user."""

    def __init__(
        self,
        *,
        endpoints: list[str],
        cacert: str,
        username: str,
        password: str,
        controller_uuid: str = "",
        cli_binary: str = "juju",
        juju_data: str | Path,
    ):
        self._endpoints = list(endpoints)
        self._cacert = cacert
        self._username = username
        self._password = password
        self._controller_uuid = controller_uuid
        self._cli_binary = cli_binary
        self._juju_data = Path(juju_data)

    @property
    def username(self) -> str:
        """The Juju user the client authenticates as."""
        return self._username

    # Setup.

    def setup(self) -> None:
        """Write the private JUJU_DATA and point the Juju CLI at it.

        ``controllers.yaml`` and ``accounts.yaml`` are rewritten on every
        start so credential or endpoint changes take effect; other files
        (cookies, models cache) are left to the CLI.
        """
        self._juju_data.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self._juju_data, 0o700)
        controller: dict[str, Any] = {
            "api-endpoints": self._endpoints,
            "ca-cert": self._cacert,
        }
        if self._controller_uuid:
            controller["uuid"] = self._controller_uuid
        _write_yaml(
            self._juju_data / "controllers.yaml",
            {"controllers": {CONTROLLER_NAME: controller}, "current-controller": CONTROLLER_NAME},
        )
        _write_yaml(
            self._juju_data / "accounts.yaml",
            {
                "controllers": {
                    CONTROLLER_NAME: {"user": self._username, "password": self._password}
                }
            },
        )
        # A stale models cache could point at removed or renamed models.
        (self._juju_data / "models.yaml").unlink(missing_ok=True)
        os.environ["JUJU_DATA"] = str(self._juju_data)

    def juju(self, model: str | None = None, timeout: float = COMMAND_TIMEOUT) -> jubilant.Juju:
        """Return a Jubilant client, optionally bound to a resolved model."""
        target = f"{CONTROLLER_NAME}:{model}" if model else None
        return _Juju(model=target, cli_binary=self._cli_binary, timeout=timeout)

    def _cli_json(self, juju: jubilant.Juju, *args: str, include_model: bool = True) -> Any:
        out = juju.cli(*args, "--format", "json", include_model=include_model)
        return json.loads(out) if out.strip() else {}

    # Controller and models.

    def controller_info(self) -> dict[str, Any]:
        """Return details of the controller and the connected account."""
        data = self._cli_json(self.juju(), "show-controller", CONTROLLER_NAME, include_model=False)
        info = data.get(CONTROLLER_NAME) or next(iter(data.values()), {})
        details = info.get("details") or {}
        account = info.get("account") or {}
        return {
            "controller-uuid": details.get("uuid"),
            "server-version": details.get("agent-version"),
            "cloud": details.get("cloud"),
            "region": details.get("region"),
            "api-endpoints": details.get("api-endpoints"),
            "user": account.get("user") or self._username,
            "access": account.get("access"),
        }

    def model_summaries(self) -> list[dict[str, Any]]:
        """Return summaries of the models the user can access."""
        data = self._cli_json(self.juju(), "models", "-c", CONTROLLER_NAME, include_model=False)
        summaries = [_model_summary(m) for m in data.get("models") or []]
        return sorted(summaries, key=lambda s: s["qualified-name"] or "")

    def resolve_model(self, model: str) -> str:
        """Resolve a model name, ``owner/name`` or UUID to ``owner/name``."""
        summaries = self.model_summaries()
        if _UUID_RE.match(model):
            matches = [s for s in summaries if s["uuid"] == model]
        elif "/" in model:
            matches = [s for s in summaries if s["qualified-name"] == model]
        else:
            matches = [s for s in summaries if s["name"] == model]
        if not matches:
            raise JujuClientError(f"model {model!r} not found or not accessible")
        if len(matches) > 1:
            names = ", ".join(s["qualified-name"] for s in matches)
            raise JujuClientError(f"model name {model!r} is ambiguous; use one of: {names}")
        return matches[0]["qualified-name"]

    def model(self, model: str, timeout: float = COMMAND_TIMEOUT) -> jubilant.Juju:
        """Return a Jubilant client bound to ``model`` (name, owner/name or UUID)."""
        return self.juju(self.resolve_model(model), timeout=timeout)

    # Read operations.

    def status(self, model: str, patterns: list[str] | None = None) -> dict[str, Any]:
        """Return ``juju status --format=json`` for a model, optionally filtered."""
        return self._cli_json(self.model(model), "status", *(patterns or []))

    def model_config(self, model: str) -> dict[str, Any]:
        """Return the model's configuration values."""
        return dict(self.model(model).model_config())

    def application_config(self, model: str, application: str) -> dict[str, Any]:
        """Return application settings with value, default, source and description."""
        data = self._cli_json(self.model(model), "config", application)
        return {
            "charm": data.get("charm"),
            "settings": data.get("settings") or {},
            "application-config": data.get("application-config") or {},
        }

    def actions(self, model: str, application: str) -> dict[str, Any]:
        """Return the charm's actions with their parameter schemas."""
        return self._cli_json(self.model(model), "actions", application, "--schema")

    def show_task(self, model: str, task_id: str, wait: float = 0) -> dict[str, Any]:
        """Return a task's status and results, optionally waiting for completion."""
        args = ["show-task", task_id]
        if wait:
            args += ["--wait", f"{wait:.0f}s"]
        juju = self.model(model, timeout=wait + WAIT_SLACK)
        try:
            return self._cli_json(juju, *args)
        except jubilant.CLIError as e:
            # Still pending after --wait: report the current state instead.
            if wait and "timed out" in (e.stderr or ""):
                return self._cli_json(self.model(model), "show-task", task_id)
            raise

    def debug_log(
        self,
        model: str,
        *,
        lines: int = 100,
        level: str = "",
        include: list[str] | None = None,
        exclude: list[str] | None = None,
        include_module: list[str] | None = None,
        exclude_module: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return the most recent debug-log lines (``juju debug-log --limit``)."""
        args = ["debug-log", "--limit", str(lines), "--date", "--utc"]
        if level:
            args += ["--level", level.upper()]
        for flag, values in (
            ("--include", include),
            ("--exclude", exclude),
            ("--include-module", include_module),
            ("--exclude-module", exclude_module),
        ):
            for value in values or []:
                args += [flag, value]
        return parse_debug_log(self.model(model).cli(*args))

    def secrets(self, model: str) -> list[dict[str, Any]]:
        """Return secret metadata for a model (never the content)."""
        return [_to_plain(s) for s in self.model(model).secrets()]

    # Write operations.

    def exec(
        self,
        model: str,
        command: str,
        *,
        units: list[str] | None = None,
        applications: list[str] | None = None,
        machines: list[str] | None = None,
        wait: float = 300,
    ) -> dict[str, Any]:
        """Run ``juju exec`` on the targets and return per-target results."""
        args = ["exec", "--format", "json", "--wait", f"{wait:.0f}s"]
        for flag, values in (
            ("--unit", units),
            ("--application", applications),
            ("--machine", machines),
        ):
            if values:
                args += [flag, ",".join(values)]
        args += ["--", command]
        juju = self.model(model, timeout=wait + WAIT_SLACK)
        try:
            out = juju.cli(*args)
        except jubilant.CLIError as e:
            # `juju exec` exits non-zero when a command fails but still
            # prints the per-target results.
            if "task failed" not in (e.stderr or "") or not (e.stdout or "").strip():
                raise
            out = e.stdout
        return json.loads(out) if out.strip() else {}


def _to_plain(value: Any) -> Any:
    """Convert Jubilant dataclasses (and datetimes) into JSON-compatible data."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            f.name.replace("_", "-"): _to_plain(getattr(value, f.name))
            for f in dataclasses.fields(value)
        }
    if isinstance(value, dict):
        return {str(k): _to_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_plain(v) for v in value]
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def task_to_dict(task: jubilant.Task) -> dict[str, Any]:
    """Convert a Jubilant task into a JSON-compatible dict."""
    return _to_plain(task)


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            yaml.safe_dump(data, f, default_flow_style=False)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
