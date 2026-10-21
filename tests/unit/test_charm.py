# Copyright 2026 dparv
# See LICENSE file for licensing details.
#
# To learn more about testing, see https://canonical.com/juju/docs/ops/latest/explanation/testing/

import json

import pytest
from ops import testing

import workload
from charm import PEER_RELATION, TOKEN_SECRET_ID_KEY, JujuMcpCharm

pytestmark = pytest.mark.usefixtures("fake_workload")

CREDENTIALS = {"username": "mcp", "password": "s3cret"}


def _peers(token_secret: testing.Secret | None = None) -> testing.PeerRelation:
    app_data = {TOKEN_SECRET_ID_KEY: token_secret.id} if token_secret else {}
    return testing.PeerRelation(PEER_RELATION, local_app_data=app_data)


def _token_secret(token: str = "tok") -> testing.Secret:
    return testing.Secret(tracked_content={"token": token}, owner="app")


def _credentials_secret(content: dict[str, str] | None = None) -> testing.Secret:
    return testing.Secret(tracked_content=content or CREDENTIALS, owner=None)


def _ready_state(**kwargs) -> testing.State:
    token = _token_secret()
    creds = _credentials_secret()
    config = {"credentials-secret": creds.id, **kwargs.pop("config", {})}
    return testing.State(
        leader=kwargs.pop("leader", True),
        relations={_peers(token), testing.SubordinateRelation("controller")},
        secrets={token, creds},
        config=config,
        **kwargs,
    )


def _written_config() -> dict:
    return json.loads(workload.CONFIG_FILE.read_text())


def test_leader_creates_token_secret():
    ctx = testing.Context(JujuMcpCharm, juju_version="3.6.14")
    state_out = ctx.run(ctx.on.config_changed(), testing.State(leader=True, relations={_peers()}))

    peers = state_out.get_relations(PEER_RELATION)[0]
    secret_id = peers.local_app_data[TOKEN_SECRET_ID_KEY]
    secret = state_out.get_secret(id=secret_id)
    assert secret.latest_content and len(secret.latest_content["token"]) >= 32
    # Without credentials the unit still blocks.
    assert state_out.unit_status == testing.BlockedStatus(
        "set the credentials-secret config option"
    )


def test_non_leader_waits_for_token():
    ctx = testing.Context(JujuMcpCharm, juju_version="3.6.14")
    state_out = ctx.run(ctx.on.config_changed(), testing.State(relations={_peers()}))
    assert isinstance(state_out.unit_status, testing.WaitingStatus)


def test_credentials_secret_not_granted():
    ctx = testing.Context(JujuMcpCharm, juju_version="3.6.14")
    token = _token_secret()
    state_in = testing.State(
        leader=True,
        relations={_peers(token)},
        secrets={token},
        config={"credentials-secret": "secret:d0erdgfmp25c762i8np0"},
    )
    state_out = ctx.run(ctx.on.config_changed(), state_in)
    assert state_out.unit_status == testing.BlockedStatus(
        "credentials-secret not found or not granted to this application"
    )


def test_credentials_secret_missing_keys():
    ctx = testing.Context(JujuMcpCharm, juju_version="3.6.14")
    token = _token_secret()
    creds = _credentials_secret({"username": "mcp"})
    state_in = testing.State(
        leader=True,
        relations={_peers(token)},
        secrets={token, creds},
        config={"credentials-secret": creds.id},
    )
    state_out = ctx.run(ctx.on.config_changed(), state_in)
    assert state_out.unit_status == testing.BlockedStatus(
        "credentials-secret is missing keys: password"
    )


def test_happy_path_renders_config_and_starts(fake_workload):
    ctx = testing.Context(JujuMcpCharm, juju_version="3.6.14")
    state_out = ctx.run(ctx.on.config_changed(), _ready_state())

    config = _written_config()
    assert config["username"] == "mcp"
    assert config["password"] == "s3cret"
    assert config["auth_token"] == "tok"
    assert config["controller_endpoints"] == ["127.0.0.1:17070", "10.0.0.10:17070"]
    assert config["controller_ca_cert"].startswith("-----BEGIN CERTIFICATE-----")
    assert config["allow_write"] is False
    assert config["listen_port"] == 8765
    assert config["juju_cli"] == str(workload.JUJU_CLI)
    assert config["juju_data"] == str(workload.JUJU_DATA_DIR)
    # The Juju CLI snap matches the controller (agent) version.
    assert fake_workload.snap_channel == "3.6/stable"
    assert config["tls_cert_file"] == str(workload.TLS_CERT_FILE)
    assert workload.TLS_CERT_FILE.exists() and workload.TLS_KEY_FILE.exists()
    assert oct(workload.CONFIG_FILE.stat().st_mode & 0o777) == "0o600"
    assert ("restart", "juju-mcp") in fake_workload.calls

    assert isinstance(state_out.unit_status, testing.ActiveStatus)
    assert state_out.unit_status.message == "read-only MCP on https://192.0.2.0:8765/mcp"
    assert state_out.opened_ports == {testing.TCPPort(8765)}
    assert state_out.workload_version


def test_no_restart_when_unchanged(fake_workload, monkeypatch: pytest.MonkeyPatch):
    # Scenario uses a fresh charm dir per run; pin the import path like on a real unit.
    monkeypatch.setattr(JujuMcpCharm, "_pythonpath", lambda self: ["/charm/src", "/charm/venv"])
    ctx = testing.Context(JujuMcpCharm, juju_version="3.6.14")
    state = _ready_state()
    ctx.run(ctx.on.config_changed(), state)
    fake_workload.calls.clear()

    ctx.run(ctx.on.update_status(), state)
    assert ("restart", "juju-mcp") not in fake_workload.calls


def test_allow_write_and_insecure_http():
    ctx = testing.Context(JujuMcpCharm, juju_version="3.6.14")
    state_in = _ready_state(config={"allow-write": True, "insecure-http": True, "port": 9000})
    state_out = ctx.run(ctx.on.config_changed(), state_in)

    config = _written_config()
    assert config["allow_write"] is True
    assert config["tls_cert_file"] is None
    assert config["listen_port"] == 9000
    assert not workload.TLS_CERT_FILE.exists()
    assert state_out.unit_status.message.startswith("read-write MCP on http://")


def test_tls_secret_is_installed():
    ctx = testing.Context(JujuMcpCharm, juju_version="3.6.14")
    tls = testing.Secret(tracked_content={"certificate": "CERT", "private-key": "KEY"}, owner=None)
    state_in = _ready_state(config={"tls-secret": tls.id})
    state_in = testing.State(
        leader=True,
        relations=state_in.relations,
        secrets={*state_in.secrets, tls},
        config=state_in.config,
    )
    ctx.run(ctx.on.config_changed(), state_in)
    assert workload.TLS_CERT_FILE.read_text() == "CERT"
    assert workload.TLS_KEY_FILE.read_text() == "KEY"


def test_controller_endpoints_override():
    ctx = testing.Context(JujuMcpCharm, juju_version="3.6.14")
    state_in = _ready_state(config={"controller-endpoints": "10.1.1.1:17070, 10.1.1.2:17070"})
    ctx.run(ctx.on.config_changed(), state_in)
    assert _written_config()["controller_endpoints"] == ["10.1.1.1:17070", "10.1.1.2:17070"]


def test_invalid_log_level():
    ctx = testing.Context(JujuMcpCharm, juju_version="3.6.14")
    state_out = ctx.run(ctx.on.config_changed(), _ready_state(config={"log-level": "loud"}))
    assert state_out.unit_status == testing.BlockedStatus("invalid log-level 'loud'")


def test_unhealthy_server_blocks(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(workload, "wait_healthy", lambda *a, **kw: False)
    ctx = testing.Context(JujuMcpCharm, juju_version="3.6.14")
    state_out = ctx.run(ctx.on.config_changed(), _ready_state())
    assert isinstance(state_out.unit_status, testing.BlockedStatus)
    assert "not responding" in state_out.unit_status.message


def test_get_connection_info():
    ctx = testing.Context(JujuMcpCharm, juju_version="3.6.14")
    state = _ready_state()
    state = ctx.run(ctx.on.config_changed(), state)
    ctx.run(ctx.on.action("get-connection-info"), state)

    results = ctx.action_results
    assert results is not None
    assert results["token"] == "tok"
    assert results["url"].startswith("https://") and results["url"].endswith(":8765/mcp")
    assert results["ca-cert"].startswith("-----BEGIN CERTIFICATE-----")
    client = json.loads(results["client-config"])
    assert client["mcpServers"]["juju"]["headers"]["Authorization"] == "Bearer tok"


def test_rotate_token_on_leader():
    ctx = testing.Context(JujuMcpCharm, juju_version="3.6.14")
    state_out = ctx.run(ctx.on.action("rotate-token"), _ready_state())

    peers = state_out.get_relations(PEER_RELATION)[0]
    secret = state_out.get_secret(id=peers.local_app_data[TOKEN_SECRET_ID_KEY])
    assert secret.latest_content and secret.latest_content["token"] != "tok"
    assert _written_config()["auth_token"] == secret.latest_content["token"]


def test_rotate_token_requires_leader():
    ctx = testing.Context(JujuMcpCharm, juju_version="3.6.14")
    with pytest.raises(testing.ActionFailed, match="leader"):
        ctx.run(ctx.on.action("rotate-token"), _ready_state(leader=False))


def test_remove_cleans_up(fake_workload):
    ctx = testing.Context(JujuMcpCharm, juju_version="3.6.14")
    state = ctx.run(ctx.on.config_changed(), _ready_state())
    assert workload.CONFIG_FILE.exists()

    ctx.run(ctx.on.remove(), state)
    assert not workload.CONFIG_FILE.exists()
    assert not workload.UNIT_FILE.exists()
    assert not workload.TLS_CERT_FILE.exists()
    assert fake_workload.active is False


def test_external_hostname_in_url_and_certificate():
    from cryptography import x509

    ctx = testing.Context(JujuMcpCharm, juju_version="3.6.14")
    state = ctx.run(
        ctx.on.config_changed(), _ready_state(config={"external-hostname": "192.168.2.29"})
    )
    assert state.unit_status == testing.ActiveStatus(
        "read-only MCP on https://192.168.2.29:8765/mcp"
    )
    cert = x509.load_pem_x509_certificate(workload.TLS_CERT_FILE.read_bytes())
    sans = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert "192.168.2.29" in {str(ip) for ip in sans.get_values_for_type(x509.IPAddress)}

    ctx.run(ctx.on.action("get-connection-info"), state)
    assert ctx.action_results and ctx.action_results["url"] == "https://192.168.2.29:8765/mcp"


def test_tls_change_restarts_service(fake_workload, monkeypatch: pytest.MonkeyPatch):
    """Regression: a regenerated certificate must be picked up by the server."""
    monkeypatch.setattr(JujuMcpCharm, "_pythonpath", lambda self: ["/charm/src", "/charm/venv"])
    ctx = testing.Context(JujuMcpCharm, juju_version="3.6.14")
    ctx.run(ctx.on.config_changed(), _ready_state())
    fake_workload.calls.clear()

    # Only the certificate SANs change; config.json stays identical.
    ctx.run(ctx.on.config_changed(), _ready_state(config={"external-hostname": "192.168.2.29"}))
    assert ("restart", "juju-mcp") in fake_workload.calls


def test_restart_when_never_applied(fake_workload, monkeypatch: pytest.MonkeyPatch):
    """Units upgraded from a version without the digest file restart once."""
    monkeypatch.setattr(JujuMcpCharm, "_pythonpath", lambda self: ["/charm/src", "/charm/venv"])
    ctx = testing.Context(JujuMcpCharm, juju_version="3.6.14")
    state = _ready_state()
    ctx.run(ctx.on.config_changed(), state)
    workload.APPLIED_DIGEST_FILE.unlink()
    fake_workload.calls.clear()

    ctx.run(ctx.on.upgrade_charm(), state)
    assert ("restart", "juju-mcp") in fake_workload.calls
    fake_workload.calls.clear()
    ctx.run(ctx.on.update_status(), state)
    assert ("restart", "juju-mcp") not in fake_workload.calls


def test_juju_channel_override(fake_workload):
    ctx = testing.Context(JujuMcpCharm, juju_version="3.6.14")
    ctx.run(ctx.on.config_changed(), _ready_state(config={"juju-channel": "3/candidate"}))
    assert fake_workload.snap_channel == "3/candidate"


def test_juju4_controller_gets_juju4_cli(fake_workload):
    ctx = testing.Context(JujuMcpCharm, juju_version="4.0.1")
    ctx.run(ctx.on.config_changed(), _ready_state())
    assert fake_workload.snap_channel == "4.0/stable"


def test_snap_install_failure_blocks(fake_workload):
    fake_workload.snap_error = "error: cannot reach the snap store"
    ctx = testing.Context(JujuMcpCharm, juju_version="3.6.14")
    state_out = ctx.run(ctx.on.config_changed(), _ready_state())
    assert state_out.unit_status == testing.BlockedStatus(
        "cannot install juju snap (3.6/stable); see juju debug-log"
    )
