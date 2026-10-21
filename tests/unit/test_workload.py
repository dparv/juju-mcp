# Copyright 2026 dparv
# See LICENSE file for licensing details.

import datetime
import pathlib

import pytest
from cryptography import x509

import workload

pytestmark = pytest.mark.usefixtures("fake_workload")

# The fake_workload fixture replaces this; keep the real one for its own test.
_real_juju_snap_channel = workload.juju_snap_channel


def test_discover_from_unit_agent_conf(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(workload, "JUJU4_RUNTIME_CONF", tmp_path / "missing")
    unit_dir = tmp_path / "unit-juju-mcp-0"
    unit_dir.mkdir()
    (unit_dir / "agent.conf").write_text(
        "cacert: UNIT-CA\napiaddresses:\n- 10.0.0.1:17070\n- 10.0.0.2:17070\n"
        "controller: controller-94bff2ee-3a9a-40de-8032-42b3936d1315\n"
        "upgradedToVersion: 3.6.14\n"
    )
    info = workload.discover_controller(unit_dir)
    assert info.ca_cert == "UNIT-CA"
    assert info.uuid == "94bff2ee-3a9a-40de-8032-42b3936d1315"
    assert info.version == "3.6.14"
    assert info.endpoints == ["127.0.0.1:17070", "10.0.0.1:17070", "10.0.0.2:17070"]


def test_discover_juju3_machine_agent_port(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(workload, "JUJU4_RUNTIME_CONF", tmp_path / "missing")
    machine = tmp_path / "agents" / "machine-0"
    machine.mkdir(parents=True)
    (machine / "agent.conf").write_text("cacert: MACHINE-CA\napiport: 17777\n")
    info = workload.discover_controller(tmp_path / "nonexistent-unit")
    assert info.ca_cert == "MACHINE-CA"
    assert info.endpoints == ["127.0.0.1:17777"]


def test_discover_juju4_runtime_conf(tmp_path: pathlib.Path):
    info = workload.discover_controller(tmp_path / "nonexistent-unit")
    assert info.endpoints == ["127.0.0.1:17070", "10.0.0.10:17070"]
    assert "BEGIN CERTIFICATE" in info.ca_cert


def test_discover_fails_without_sources(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(workload, "JUJU4_RUNTIME_CONF", tmp_path / "missing")
    with pytest.raises(workload.DiscoveryError):
        workload.discover_controller(tmp_path)


def test_self_signed_is_reused_until_sans_change():
    assert workload.ensure_self_signed(["localhost", "10.0.0.1"]) is True
    first = workload.TLS_CERT_FILE.read_text()
    assert workload.ensure_self_signed(["10.0.0.1", "localhost"]) is False
    assert workload.TLS_CERT_FILE.read_text() == first

    assert workload.ensure_self_signed(["localhost", "10.0.0.2"]) is True
    cert = x509.load_pem_x509_certificate(workload.TLS_CERT_FILE.read_bytes())
    sans = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert {str(ip) for ip in sans.get_values_for_type(x509.IPAddress)} == {"10.0.0.2"}
    assert cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    assert oct(workload.TLS_KEY_FILE.stat().st_mode & 0o777) == "0o600"


def test_self_signed_renewed_near_expiry(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(workload, "SELF_SIGNED_DAYS", 10)
    workload.ensure_self_signed(["localhost"])
    cert = x509.load_pem_x509_certificate(workload.TLS_CERT_FILE.read_bytes())
    remaining = cert.not_valid_after_utc - datetime.datetime.now(datetime.timezone.utc)
    assert remaining < workload.SELF_SIGNED_RENEW_BEFORE
    assert workload.ensure_self_signed(["localhost"]) is True


def test_render_unit():
    unit = workload.render_unit("/usr/bin/python3", ["/charm/src", "/charm/venv"])
    assert "ExecStart=/usr/bin/python3 -m juju_mcp_server --config" in unit
    assert "Environment=PYTHONPATH=/charm/src:/charm/venv" in unit
    assert "ProtectSystem=strict" in unit
    assert "StateDirectory=juju-mcp" in unit
    assert f"Environment=JUJU_DATA={workload.JUJU_DATA_DIR}" in unit


def test_install_service_reloads_only_on_change(fake_workload):
    assert workload.install_service("/usr/bin/python3", ["/a"]) is True
    assert ("daemon-reload",) in fake_workload.calls
    fake_workload.calls.clear()
    assert workload.install_service("/usr/bin/python3", ["/a"]) is False
    assert fake_workload.calls == []


@pytest.mark.parametrize(
    ("version", "channel"),
    [("3.6.14", "3.6/stable"), ("4.0.1", "4.0/stable"), ("3.6", "3.6/stable"), ("", None)],
)
def test_cli_channel_for(version, channel):
    assert workload.cli_channel_for(version) == channel


def test_snap_channel_parsing(monkeypatch: pytest.MonkeyPatch):
    class Result:
        returncode = 0
        stdout = (
            "Name  Version  Rev    Tracking     Publisher    Notes\n"
            "juju  3.6.29   36360  3.6/stable   canonical**  -\n"
        )

    monkeypatch.setattr(workload.subprocess, "run", lambda *a, **kw: Result())
    assert _real_juju_snap_channel() == "3.6/stable"
