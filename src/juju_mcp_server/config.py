# Copyright 2026 dparv
# See LICENSE file for licensing details.

"""Configuration of the MCP server.

The charm renders this configuration to a root-only JSON file which the
server reads at startup.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

LOG_LEVELS = ("debug", "info", "warning", "error", "critical")
DEFAULT_JUJU_CLI = "/snap/juju/current/bin/juju"
DEFAULT_JUJU_DATA = "/var/lib/juju-mcp/juju"


class ConfigError(ValueError):
    """Raised when the server configuration is invalid."""


@dataclasses.dataclass(frozen=True)
class ServerConfig:
    """Runtime configuration of the MCP server."""

    controller_endpoints: list[str]
    controller_ca_cert: str
    username: str
    password: str
    auth_token: str
    listen_host: str = "0.0.0.0"
    listen_port: int = 8765
    tls_cert_file: str | None = None
    tls_key_file: str | None = None
    allow_write: bool = False
    log_level: str = "info"
    controller_uuid: str = ""
    juju_cli: str = DEFAULT_JUJU_CLI
    juju_data: str = DEFAULT_JUJU_DATA

    def __post_init__(self):
        if not self.controller_endpoints:
            raise ConfigError("controller_endpoints must not be empty")
        for name in ("controller_ca_cert", "username", "password", "auth_token"):
            if not getattr(self, name):
                raise ConfigError(f"{name} must be set")
        if not 0 < self.listen_port < 65536:
            raise ConfigError(f"invalid listen_port {self.listen_port}")
        if bool(self.tls_cert_file) != bool(self.tls_key_file):
            raise ConfigError("tls_cert_file and tls_key_file must be set together")
        if self.log_level not in LOG_LEVELS:
            raise ConfigError(f"invalid log_level {self.log_level!r}")

    @property
    def tls_enabled(self) -> bool:
        """Whether the server serves HTTPS."""
        return bool(self.tls_cert_file)

    def to_dict(self) -> dict[str, Any]:
        """Return the configuration as a JSON-serialisable dict."""
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ServerConfig:
        """Build a configuration from a dict, ignoring unknown keys."""
        known = {f.name for f in dataclasses.fields(cls)}
        try:
            return cls(**{k: v for k, v in data.items() if k in known})
        except TypeError as e:
            raise ConfigError(str(e)) from e

    @classmethod
    def load(cls, path: str | Path) -> ServerConfig:
        """Load the configuration from a JSON file."""
        try:
            data = json.loads(Path(path).read_text())
        except (OSError, json.JSONDecodeError) as e:
            raise ConfigError(f"cannot read config {path}: {e}") from e
        if not isinstance(data, dict):
            raise ConfigError(f"config {path} is not a JSON object")
        return cls.from_dict(data)
