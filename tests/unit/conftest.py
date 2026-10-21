# Copyright 2026 dparv
# See LICENSE file for licensing details.

import pathlib

import pytest

import workload

CA_CERT = "-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n"


class FakeSystemd:
    """Record systemctl calls instead of running them."""

    def __init__(self):
        self.calls: list[tuple[str, ...]] = []
        self.active = False
        self.snap_channel: str | None = None
        self.snap_error: str | None = None

    def ensure_juju_snap(self, channel: str) -> bool:
        if self.snap_error:
            raise workload.SnapError(self.snap_error)
        changed = self.snap_channel != channel
        self.snap_channel = channel
        return changed

    def systemctl(self, *args: str) -> None:
        self.calls.append(args)
        if args[0] in ("restart",) or args[:2] == ("enable", "--now"):
            self.active = True

    def stop(self) -> None:
        self.calls.append(("disable", "--now"))
        self.active = False


@pytest.fixture
def fake_workload(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> FakeSystemd:
    """Point all workload paths into tmp_path and fake systemd and health checks."""
    etc = tmp_path / "etc" / "juju-mcp"
    monkeypatch.setattr(workload, "CONFIG_DIR", etc)
    monkeypatch.setattr(workload, "CONFIG_FILE", etc / "config.json")
    monkeypatch.setattr(workload, "TLS_DIR", etc / "tls")
    monkeypatch.setattr(workload, "TLS_CERT_FILE", etc / "tls" / "server.crt")
    monkeypatch.setattr(workload, "TLS_KEY_FILE", etc / "tls" / "server.key")
    monkeypatch.setattr(workload, "UNIT_FILE", tmp_path / "systemd" / "juju-mcp.service")
    monkeypatch.setattr(workload, "APPLIED_DIGEST_FILE", etc / ".applied-digest")
    monkeypatch.setattr(workload, "SNAP_MARKER_FILE", etc / ".installed-juju-snap")
    monkeypatch.setattr(workload, "STATE_DIR", tmp_path / "var" / "juju-mcp")
    monkeypatch.setattr(workload, "JUJU_DATA_DIR", tmp_path / "var" / "juju-mcp" / "juju")

    runtime_conf = tmp_path / "jujud" / "runtime.conf"
    runtime_conf.parent.mkdir()
    runtime_conf.write_text(
        "api-addresses:\n- 10.0.0.10:17070\nca-cert: |\n  "
        + CA_CERT.replace("\n", "\n  ").rstrip()
        + "\n"
    )
    monkeypatch.setattr(workload, "JUJU4_RUNTIME_CONF", runtime_conf)
    monkeypatch.setattr(
        workload, "JUJU3_MACHINE_AGENT_GLOB", str(tmp_path / "agents" / "machine-*" / "agent.conf")
    )

    systemd = FakeSystemd()
    monkeypatch.setattr(workload, "_systemctl", systemd.systemctl)
    monkeypatch.setattr(workload, "is_active", lambda: systemd.active)
    monkeypatch.setattr(workload, "stop", systemd.stop)
    monkeypatch.setattr(workload, "wait_healthy", lambda *a, **kw: True)
    monkeypatch.setattr(workload, "juju_snap_channel", lambda: systemd.snap_channel)
    monkeypatch.setattr(workload, "ensure_juju_snap", systemd.ensure_juju_snap)
    monkeypatch.setattr(workload.subprocess, "run", lambda *a, **kw: None)
    return systemd
