#!/usr/bin/env python3
# Copyright 2026 dparv
# See LICENSE file for licensing details.

"""Subordinate charm running an MCP server next to a Juju controller."""

from __future__ import annotations

import dataclasses
import json
import logging
import secrets
import socket
import sys
import time

import ops

import workload
from juju_mcp_server import __version__
from juju_mcp_server.config import LOG_LEVELS

logger = logging.getLogger(__name__)

PEER_RELATION = "mcp-peers"
CONTROLLER_RELATION = "controller"
TOKEN_SECRET_ID_KEY = "token-secret-id"
TOKEN_ROTATED_KEY = "token-rotated-at"
TOKEN_SECRET_LABEL = "mcp-token"
MCP_PATH = "/mcp"
WILDCARD_ADDRESSES = ("", "0.0.0.0", "::")


class _BlockedError(Exception):
    """Reconciliation cannot proceed until an operator acts."""


class _WaitingError(Exception):
    """Reconciliation cannot proceed until something else happens."""


class JujuMcpCharm(ops.CharmBase):
    """Run and configure the Juju MCP server."""

    def __init__(self, framework: ops.Framework):
        super().__init__(framework)
        self._status: ops.StatusBase | None = None
        for event in (
            self.on.install,
            self.on.upgrade_charm,
            self.on.config_changed,
            self.on.start,
            self.on.update_status,
            self.on.leader_elected,
            self.on.secret_changed,
            self.on[PEER_RELATION].relation_created,
            self.on[PEER_RELATION].relation_changed,
            self.on[CONTROLLER_RELATION].relation_joined,
            self.on[CONTROLLER_RELATION].relation_changed,
        ):
            framework.observe(event, self._on_reconcile)
        framework.observe(self.on.remove, self._on_remove)
        framework.observe(self.on.collect_unit_status, self._on_collect_unit_status)
        framework.observe(self.on["get-connection-info"].action, self._on_get_connection_info)
        framework.observe(self.on["rotate-token"].action, self._on_rotate_token)

    # Event handlers.

    def _on_reconcile(self, _: ops.EventBase) -> None:
        self._reconcile()

    def _on_remove(self, _: ops.RemoveEvent) -> None:
        workload.remove()

    def _on_collect_unit_status(self, event: ops.CollectStatusEvent) -> None:
        if self._status is not None:
            event.add_status(self._status)

    def _on_get_connection_info(self, event: ops.ActionEvent) -> None:
        token = self._token()
        if token is None:
            event.fail("the bearer token is not available yet")
            return
        url = self._url()
        results: dict[str, str] = {"url": url, "token": token}
        if self._tls_enabled() and not self.config.get("tls-secret"):
            cert = workload.read_cert()
            if cert:
                results["ca-cert"] = cert
        results["client-config"] = json.dumps(
            {
                "mcpServers": {
                    "juju": {
                        "type": "http",
                        "url": url,
                        "headers": {"Authorization": f"Bearer {token}"},
                    }
                }
            },
            indent=2,
        )
        event.set_results(results)

    def _on_rotate_token(self, event: ops.ActionEvent) -> None:
        if not self.unit.is_leader():
            event.fail("rotate-token must be run on the leader unit")
            return
        peers = self.model.get_relation(PEER_RELATION)
        secret_id = peers.data[self.app].get(TOKEN_SECRET_ID_KEY) if peers else None
        if peers is None or not secret_id:
            event.fail("the bearer token has not been created yet")
            return
        token = _new_token()
        self.model.get_secret(id=secret_id).set_content({"token": token})
        # Poke the peers so non-leader units re-read the secret.
        peers.data[self.app][TOKEN_ROTATED_KEY] = str(time.time())
        self._reconcile(token=token)
        event.set_results({"result": "token rotated; reconfigure MCP clients"})

    # Reconciliation.

    def _reconcile(self, token: str | None = None) -> None:
        try:
            self._reconcile_workload(token)
        except _BlockedError as e:
            self._status = ops.BlockedStatus(str(e))
        except _WaitingError as e:
            self._status = ops.WaitingStatus(str(e))

    def _reconcile_workload(self, token: str | None) -> None:
        pythonpath = self._pythonpath()
        workload.install_service(sys.executable, pythonpath)
        self.unit.set_workload_version(__version__)

        log_level = str(self.config.get("log-level", "info")).lower()
        if log_level not in LOG_LEVELS:
            raise _BlockedError(f"invalid log-level {log_level!r}")
        port = int(self.config.get("port", 8765))
        if not 0 < port < 65536:
            raise _BlockedError(f"invalid port {port}")

        token = token or self._token()
        if token is None:
            raise _WaitingError("waiting for the leader to create the bearer token")
        username, password = self._credentials()
        controller = self._controller()
        self._ensure_juju_cli(controller)
        tls = self._configure_tls()

        workload.write_config(
            {
                "controller_endpoints": controller.endpoints,
                "controller_ca_cert": controller.ca_cert,
                "username": username,
                "password": password,
                "auth_token": token,
                "listen_host": self._bind_address(),
                "listen_port": port,
                "tls_cert_file": str(workload.TLS_CERT_FILE) if tls else None,
                "tls_key_file": str(workload.TLS_KEY_FILE) if tls else None,
                "allow_write": bool(self.config.get("allow-write")),
                "log_level": log_level,
                "controller_uuid": controller.uuid,
                "juju_cli": str(workload.JUJU_CLI),
                "juju_data": str(workload.JUJU_DATA_DIR),
            }
        )
        # Compare against what the running service was started with, not
        # against the start of this hook: files (e.g. TLS material) may have
        # changed earlier in the hook or in a hook that failed before restarting.
        digest = workload.config_digest()
        if digest != workload.applied_digest() or not workload.is_active():
            logger.info("configuration changed; restarting %s", workload.SERVICE_NAME)
            workload.restart()
            workload.mark_applied(digest)
        else:
            workload.ensure_running()
        self.unit.set_ports(port)

        if not workload.wait_healthy(self._probe_address(), port, tls):
            raise _BlockedError(
                f"MCP server is not responding; see journalctl -u {workload.SERVICE_NAME}"
            )
        mode = "read-write" if self.config.get("allow-write") else "read-only"
        self._status = ops.ActiveStatus(f"{mode} MCP on {self._url()}")

    def _pythonpath(self) -> list[str]:
        """Return the import path the service needs: charm source and venv."""
        charm_dir = str(self.charm_dir)
        paths = [str(self.charm_dir / "src")]
        paths += [p for p in sys.path if p.startswith(charm_dir) and p not in paths]
        return paths

    def _token(self) -> str | None:
        """Return the shared bearer token, creating it on the leader if needed."""
        peers = self.model.get_relation(PEER_RELATION)
        if peers is None:
            return None
        secret_id = peers.data[self.app].get(TOKEN_SECRET_ID_KEY)
        if not secret_id:
            if not self.unit.is_leader():
                return None
            token = _new_token()
            secret = self.app.add_secret(
                {"token": token},
                label=TOKEN_SECRET_LABEL,
                description="Bearer token for the Juju MCP server",
            )
            assert secret.id is not None
            peers.data[self.app][TOKEN_SECRET_ID_KEY] = secret.id
            return token
        try:
            # Units of the owning application read the latest revision directly.
            return self.model.get_secret(id=secret_id).peek_content()["token"]
        except (ops.SecretNotFoundError, ops.ModelError, KeyError) as e:
            logger.warning("cannot read bearer token secret: %s", e)
            return None

    def _credentials(self) -> tuple[str, str]:
        content = self._user_secret(
            "credentials-secret", required_keys=("username", "password"), required=True
        )
        assert content is not None
        return content["username"], content["password"]

    def _user_secret(
        self, option: str, required_keys: tuple[str, ...], required: bool
    ) -> dict[str, str] | None:
        secret_id = self.config.get(option)
        if not secret_id:
            if required:
                raise _BlockedError(f"set the {option} config option")
            return None
        if not isinstance(secret_id, str):
            raise _BlockedError(f"invalid {option}")
        try:
            content = self.model.get_secret(id=secret_id).get_content(refresh=True)
        except (ops.SecretNotFoundError, ops.ModelError) as e:
            logger.warning("cannot read %s: %s", option, e)
            raise _BlockedError(f"{option} not found or not granted to this application") from e
        missing = [k for k in required_keys if not content.get(k)]
        if missing:
            raise _BlockedError(f"{option} is missing keys: {', '.join(missing)}")
        return content

    def _controller(self) -> workload.ControllerInfo:
        override = [e.strip() for e in str(self.config.get("controller-endpoints", "")).split(",")]
        override = [e for e in override if e]
        try:
            info = workload.discover_controller(self.charm_dir.parent)
        except workload.DiscoveryError as e:
            raise _BlockedError(f"cannot discover the controller: {e}") from e
        if override:
            info = dataclasses.replace(info, endpoints=override)
        return info

    def _ensure_juju_cli(self, controller: workload.ControllerInfo) -> None:
        """Install the Juju CLI snap the server drives through Jubilant."""
        # Units in the controller model run the controller's agent version.
        version = controller.version or str(self.model.juju_version)
        channel = str(self.config.get("juju-channel") or "").strip()
        channel = channel or workload.cli_channel_for(version)
        if not channel:
            raise _BlockedError("cannot determine the controller version; set juju-channel")
        if workload.juju_snap_channel() != channel:
            self.unit.status = ops.MaintenanceStatus(f"installing juju snap ({channel})")
            try:
                workload.ensure_juju_snap(channel)
            except workload.SnapError as e:
                logger.error("cannot install juju snap %s: %s", channel, e)
                raise _BlockedError(
                    f"cannot install juju snap ({channel}); see juju debug-log"
                ) from e

    def _tls_enabled(self) -> bool:
        return not self.config.get("insecure-http")

    def _configure_tls(self) -> bool:
        """Install TLS material; return whether TLS is enabled."""
        if not self._tls_enabled():
            workload.remove_tls()
            return False
        content = self._user_secret(
            "tls-secret", required_keys=("certificate", "private-key"), required=False
        )
        if content is not None:
            workload.write_tls(content["certificate"], content["private-key"])
        else:
            workload.ensure_self_signed(self._certificate_sans())
        return True

    def _certificate_sans(self) -> list[str]:
        names = {"localhost", "127.0.0.1", "::1", socket.gethostname(), socket.getfqdn()}
        names |= set(self._ingress_addresses())
        if external := self._external_hostname():
            names.add(external)
        bind = self._bind_address()
        if bind not in WILDCARD_ADDRESSES:
            names.add(bind)
        return sorted(n for n in names if n)

    def _ingress_addresses(self) -> list[str]:
        # The peer endpoint is always bound in the principal's (machine's) space.
        binding = self.model.get_binding(PEER_RELATION)
        if binding is None:
            return []
        try:
            return [str(a) for a in binding.network.ingress_addresses]
        except (ops.ModelError, ValueError) as e:
            logger.debug("cannot read ingress addresses: %s", e)
            return []

    def _external_hostname(self) -> str:
        return str(self.config.get("external-hostname") or "").strip()

    def _bind_address(self) -> str:
        return str(self.config.get("bind-address") or "0.0.0.0").strip()

    def _probe_address(self) -> str:
        bind = self._bind_address()
        if bind in ("", "0.0.0.0"):
            return "127.0.0.1"
        if bind == "::":
            return "::1"
        return bind

    def _url(self) -> str:
        scheme = "https" if self._tls_enabled() else "http"
        host = self._external_hostname() or self._bind_address()
        if host in WILDCARD_ADDRESSES:
            addresses = self._ingress_addresses()
            host = addresses[0] if addresses else socket.getfqdn()
        if ":" in host:
            host = f"[{host}]"
        return f"{scheme}://{host}:{self.config.get('port', 8765)}{MCP_PATH}"


def _new_token() -> str:
    return secrets.token_urlsafe(32)


if __name__ == "__main__":  # pragma: nocover
    ops.main(JujuMcpCharm)
