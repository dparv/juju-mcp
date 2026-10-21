# Copyright 2026 dparv
# See LICENSE file for licensing details.

"""Management of the MCP server workload on the controller machine.

This module has no ops dependency: it discovers the local controller, renders
the server configuration, manages TLS material and drives the systemd unit.
"""

from __future__ import annotations

import dataclasses
import datetime
import glob
import hashlib
import ipaddress
import json
import logging
import os
import shutil
import ssl
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

logger = logging.getLogger(__name__)

SERVICE_NAME = "juju-mcp"
CONFIG_DIR = Path("/etc/juju-mcp")
CONFIG_FILE = CONFIG_DIR / "config.json"
TLS_DIR = CONFIG_DIR / "tls"
TLS_CERT_FILE = TLS_DIR / "server.crt"
TLS_KEY_FILE = TLS_DIR / "server.key"
UNIT_FILE = Path(f"/etc/systemd/system/{SERVICE_NAME}.service")
# Digest of the config/unit/TLS files the running service was started with.
APPLIED_DIGEST_FILE = CONFIG_DIR / ".applied-digest"
# Private state of the server: JUJU_DATA for the Juju CLI (systemd StateDirectory).
STATE_DIR = Path("/var/lib/juju-mcp")
JUJU_DATA_DIR = STATE_DIR / "juju"
# The Juju CLI from the juju snap, executed directly (outside snap confinement)
# so that it can use the service's private JUJU_DATA under systemd hardening.
JUJU_SNAP = "juju"
JUJU_CLI = Path("/snap/juju/current/bin/juju")
# Marker recording that this charm installed the juju snap (so it may remove it).
SNAP_MARKER_FILE = CONFIG_DIR / ".installed-juju-snap"

# Juju 4 machine controllers run jujud from a snap and publish runtime info here.
JUJU4_RUNTIME_CONF = Path("/var/snap/jujud/current/runtime.conf")
# Juju 3 controller machine agents keep the API port in their agent.conf.
JUJU3_MACHINE_AGENT_GLOB = "/var/lib/juju/agents/machine-*/agent.conf"

SELF_SIGNED_DAYS = 365
SELF_SIGNED_RENEW_BEFORE = datetime.timedelta(days=30)

UNIT_TEMPLATE = """\
[Unit]
Description=Juju MCP server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=PYTHONPATH={pythonpath}
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=PYTHONUNBUFFERED=1
Environment=HOME={state_dir}
Environment=JUJU_DATA={juju_data}
StateDirectory=juju-mcp
StateDirectoryMode=0700
ExecStart={python} -m juju_mcp_server --config {config}
Restart=on-failure
RestartSec=5
# Hardening: the server only needs to read its config and talk to the network.
NoNewPrivileges=yes
CapabilityBoundingSet=
AmbientCapabilities=
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectClock=yes
ProtectHostname=yes
RestrictNamespaces=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
LockPersonality=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK
SystemCallArchitectures=native

[Install]
WantedBy=multi-user.target
"""


class DiscoveryError(Exception):
    """Raised when the controller API endpoint or CA cannot be determined."""


@dataclasses.dataclass(frozen=True)
class ControllerInfo:
    """How to reach the controller API."""

    endpoints: list[str]
    ca_cert: str
    uuid: str = ""
    version: str = ""


def cli_channel_for(version: str) -> str | None:
    """Snap channel of the Juju CLI matching a Juju version, e.g. ``3.6/stable``."""
    parts = version.split(".")
    if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
        return None
    return f"{parts[0]}.{parts[1]}/stable"


def _read_yaml(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as e:
        logger.debug("cannot read %s: %s", path, e)
        return {}
    return data if isinstance(data, dict) else {}


def _port_of(address: str) -> int | None:
    host, sep, port = address.rpartition(":")
    if not sep or not host:
        return None
    try:
        return int(port)
    except ValueError:
        return None


def discover_controller(unit_agent_dir: Path) -> ControllerInfo:
    """Discover the controller API endpoints and CA certificate.

    The unit's own agent.conf is the primary source and works for any Juju
    version. Controller-specific runtime files are used as fallbacks. The
    loopback endpoint is listed first so the co-located controller is used
    when the principal is a controller unit; Juju's published addresses are
    kept as fallbacks for HA failover.
    """
    ca_cert = ""
    addresses: list[str] = []
    port: int | None = None

    unit_conf = _read_yaml(unit_agent_dir / "agent.conf")
    ca_cert = unit_conf.get("cacert") or ""
    uuid = str(unit_conf.get("controller") or "").removeprefix("controller-")
    version = str(unit_conf.get("upgradedToVersion") or "")
    addresses += [a for a in unit_conf.get("apiaddresses") or [] if isinstance(a, str)]

    runtime_conf = _read_yaml(JUJU4_RUNTIME_CONF)
    ca_cert = ca_cert or runtime_conf.get("ca-cert") or ""
    addresses += [a for a in runtime_conf.get("api-addresses") or [] if isinstance(a, str)]

    for path in sorted(glob.glob(JUJU3_MACHINE_AGENT_GLOB)):
        machine_conf = _read_yaml(Path(path))
        ca_cert = ca_cert or machine_conf.get("cacert") or ""
        port = port or machine_conf.get("apiport")

    port = port or next((p for p in map(_port_of, addresses) if p), None)
    if not ca_cert:
        raise DiscoveryError("controller CA certificate not found in agent configuration")
    if not port:
        raise DiscoveryError("controller API port not found in agent configuration")

    endpoints = [f"127.0.0.1:{port}"]
    for address in addresses:
        if address not in endpoints:
            endpoints.append(address)
    return ControllerInfo(endpoints=endpoints, ca_cert=ca_cert, uuid=uuid, version=version)


# Rendering and writing files.


def _atomic_write(path: Path, content: str, mode: int = 0o600) -> bool:
    """Write ``content`` to ``path`` if it differs; return whether it changed."""
    try:
        if path.read_text() == content:
            os.chmod(path, mode)
            return False
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return True


def render_unit(python: str, pythonpath: list[str]) -> str:
    """Render the systemd unit for the given interpreter and module path."""
    return UNIT_TEMPLATE.format(
        python=python,
        pythonpath=":".join(pythonpath),
        config=CONFIG_FILE,
        state_dir=STATE_DIR,
        juju_data=JUJU_DATA_DIR,
    )


def install_service(python: str, pythonpath: list[str]) -> bool:
    """Install or update the systemd unit; return whether it changed."""
    changed = _atomic_write(UNIT_FILE, render_unit(python, pythonpath), mode=0o644)
    if changed:
        _systemctl("daemon-reload")
    return changed


def write_config(config: dict) -> bool:
    """Write the server configuration; return whether it changed."""
    return _atomic_write(CONFIG_FILE, json.dumps(config, indent=2, sort_keys=True) + "\n")


def config_digest() -> str:
    """Return a digest of the rendered config, unit and TLS files."""
    h = hashlib.sha256()
    for path in (CONFIG_FILE, UNIT_FILE, TLS_CERT_FILE, TLS_KEY_FILE):
        try:
            h.update(path.read_bytes())
        except FileNotFoundError:
            h.update(b"-")
    return h.hexdigest()


def applied_digest() -> str | None:
    """Return the digest recorded when the service was last (re)started."""
    try:
        return APPLIED_DIGEST_FILE.read_text().strip()
    except FileNotFoundError:
        return None


def mark_applied(digest: str) -> None:
    """Record that the service is running with files matching ``digest``."""
    _atomic_write(APPLIED_DIGEST_FILE, digest + "\n")


# Juju CLI.


class SnapError(Exception):
    """Raised when the juju snap cannot be installed or refreshed."""


def juju_snap_channel() -> str | None:
    """Return the channel the installed juju snap tracks, or None if absent."""
    result = subprocess.run(
        ["snap", "list", JUJU_SNAP, "--unicode=never"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    lines = result.stdout.strip().splitlines()
    # Name  Version  Rev  Tracking  Publisher  Notes
    fields = lines[1].split() if len(lines) > 1 else []
    return fields[3] if len(fields) > 3 else ""


def ensure_juju_snap(channel: str) -> bool:
    """Install or refresh the juju snap to ``channel``; return whether it changed."""
    current = juju_snap_channel()
    if current == channel:
        return False
    if current is None:
        args = ["snap", "install", JUJU_SNAP, f"--channel={channel}"]
    else:
        args = ["snap", "refresh", JUJU_SNAP, f"--channel={channel}"]
    logger.info("running %s", " ".join(args))
    result = subprocess.run(args, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise SnapError((result.stderr or result.stdout).strip() or f"{args[1]} failed")
    if current is None:
        _atomic_write(SNAP_MARKER_FILE, channel + "\n")
    return True


def remove_juju_snap() -> None:
    """Remove the juju snap, but only if this charm installed it."""
    if SNAP_MARKER_FILE.exists():
        subprocess.run(["snap", "remove", JUJU_SNAP], check=False, capture_output=True)
        SNAP_MARKER_FILE.unlink(missing_ok=True)


# TLS.


def write_tls(cert_pem: str, key_pem: str) -> bool:
    """Install the given certificate and key; return whether they changed."""
    changed = _atomic_write(TLS_CERT_FILE, cert_pem, mode=0o644)
    changed |= _atomic_write(TLS_KEY_FILE, key_pem, mode=0o600)
    return changed


def remove_tls() -> None:
    """Remove installed TLS material."""
    TLS_CERT_FILE.unlink(missing_ok=True)
    TLS_KEY_FILE.unlink(missing_ok=True)


def _sans(cert: x509.Certificate) -> set[str]:
    try:
        ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return set()
    names = {str(n) for n in ext.get_values_for_type(x509.DNSName)}
    names |= {str(n) for n in ext.get_values_for_type(x509.IPAddress)}
    return names


def ensure_self_signed(sans: list[str]) -> bool:
    """Ensure a valid self-signed certificate covering ``sans`` is installed.

    The certificate is reused while it covers the same names and is not close
    to expiry. Returns whether a new certificate was written.
    """
    wanted = set(sans)
    try:
        current = x509.load_pem_x509_certificate(TLS_CERT_FILE.read_bytes())
        TLS_KEY_FILE.stat()
    except (FileNotFoundError, ValueError):
        current = None
    if current is not None:
        expires = current.not_valid_after_utc
        now = datetime.datetime.now(datetime.timezone.utc)
        if _sans(current) == wanted and expires - now > SELF_SIGNED_RENEW_BEFORE:
            return False
    cert_pem, key_pem = generate_self_signed(sans)
    return write_tls(cert_pem, key_pem)


def generate_self_signed(sans: list[str]) -> tuple[str, str]:
    """Generate a self-signed certificate usable as its own trust anchor."""
    key = ec.generate_private_key(ec.SECP256R1())
    alt_names: list[x509.GeneralName] = []
    for name in sans:
        try:
            alt_names.append(x509.IPAddress(ipaddress.ip_address(name)))
        except ValueError:
            alt_names.append(x509.DNSName(name))
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "juju-mcp")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=SELF_SIGNED_DAYS))
        .add_extension(x509.SubjectAlternativeName(alt_names), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return cert_pem, key_pem


def read_cert() -> str | None:
    """Return the installed certificate PEM, if any."""
    try:
        return TLS_CERT_FILE.read_text()
    except FileNotFoundError:
        return None


# Service control.


def _systemctl(*args: str) -> None:
    subprocess.run(["systemctl", *args], check=True, capture_output=True, text=True)


def restart() -> None:
    """Enable and (re)start the service."""
    _systemctl("enable", SERVICE_NAME)
    _systemctl("restart", SERVICE_NAME)


def ensure_running() -> None:
    """Enable and start the service if it is not running."""
    _systemctl("enable", "--now", SERVICE_NAME)


def is_active() -> bool:
    """Return whether the service is active."""
    result = subprocess.run(
        ["systemctl", "is-active", "--quiet", SERVICE_NAME], check=False, capture_output=True
    )
    return result.returncode == 0


def stop() -> None:
    """Stop and disable the service."""
    subprocess.run(
        ["systemctl", "disable", "--now", SERVICE_NAME], check=False, capture_output=True
    )


def remove() -> None:
    """Stop the service and remove all files the charm installed."""
    stop()
    UNIT_FILE.unlink(missing_ok=True)
    subprocess.run(["systemctl", "daemon-reload"], check=False, capture_output=True)
    remove_tls()
    CONFIG_FILE.unlink(missing_ok=True)
    APPLIED_DIGEST_FILE.unlink(missing_ok=True)
    shutil.rmtree(STATE_DIR, ignore_errors=True)
    remove_juju_snap()


def wait_healthy(host: str, port: int, tls: bool, timeout: float = 20.0) -> bool:
    """Poll the server's /healthz endpoint until it answers or ``timeout`` passes."""
    scheme = "https" if tls else "http"
    if ":" in host:
        host = f"[{host}]"
    url = f"{scheme}://{host}:{port}/healthz"
    context = None
    if tls:
        # Only liveness is checked here; the certificate may be self-signed.
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    deadline = time.monotonic() + timeout
    while True:
        try:
            with urllib.request.urlopen(url, timeout=3, context=context) as resp:  # noqa: S310
                if resp.status == 200:
                    return True
        except (OSError, urllib.error.URLError) as e:
            logger.debug("health check %s failed: %s", url, e)
        if time.monotonic() >= deadline:
            return False
        time.sleep(1)
