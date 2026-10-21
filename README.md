# juju-mcp

A subordinate Juju charm that runs a
[Model Context Protocol](https://modelcontextprotocol.io) (MCP) server on Juju
controller machines. AI assistants and other MCP clients, such as Claude Code,
opencode, VS Code or your own scripts, can then **inspect, troubleshoot and
optionally operate** every model the controller manages, using natural
language.

```text
"Why is forgejo in error in the portfolio model?"
  -> model_status(portfolio) -> debug_log(portfolio, include=[forgejo]) -> answer
```

The charm attaches to the
[`juju-controller`](https://charmhub.io/juju-controller) application in the
`controller` model. It finds the local controller API on its own and drives
the Juju CLI through [Jubilant](https://github.com/canonical/jubilant), logged
in as a **dedicated Juju user that you choose**. It serves MCP over HTTPS, and
clients authenticate with a bearer token. Tools are **read-only by default**.

---

## Contents

- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Deployment in detail](#deployment-in-detail)
- [Networking](#networking)
- [TLS](#tls)
- [Connecting MCP clients](#connecting-mcp-clients)
- [Tools reference](#tools-reference)
- [Configuration reference](#configuration-reference)
- [Actions reference](#actions-reference)
- [Security model](#security-model)
- [Operations](#operations)
- [Troubleshooting](#troubleshooting)
- [Limitations](#limitations)
- [Development](#development)

---

## How it works

```text
 MCP client (Claude Code, opencode, VS Code, Python, ...)
        |  HTTPS, streamable HTTP transport
        |  Authorization: Bearer <token>
        v
+---------------------------- controller machine ----------------------------+
|  juju-mcp (subordinate)                                                    |
|    systemd: juju-mcp.service  ->  https://0.0.0.0:8765/mcp                 |
|        |  Jubilant -> juju CLI (juju snap), private JUJU_DATA,            |
|        |  logged in as the user from `credentials-secret`                  |
|        v                                                                   |
|  controller (juju-controller charm) / jujud  :17070                        |
+----------------------------------------------------------------------------+
                         |
                         v
              all models on the controller
```

* **Subordinate placement:** the charm relates to the controller application
  over the implicit `juju-info` interface, so one MCP server runs on every
  controller machine, HA controllers included.
* **Controller discovery:** the API port and CA certificate come from the
  unit's own agent configuration, with Juju 3.6 (`/var/lib/juju`) and Juju 4
  (snap `jujud`) fallbacks. The server connects to `127.0.0.1` first and uses
  the other controller addresses for failover.
* **Juju access through Jubilant:** each tool runs one or more `juju` CLI
  commands (`status`, `debug-log`, `run`, `exec`, `deploy`, ...) through
  [Jubilant](https://documentation.ubuntu.com/jubilant/). The charm installs
  the `juju` snap on a channel matching the controller version (for example
  `3.6/stable`, or `4.0/stable` on Juju 4). The server writes a private
  `JUJU_DATA` (`/var/lib/juju-mcp/juju`) with one controller entry, `mcp`,
  and the account from `credentials-secret`. Outputs, filters and permission
  checks are exactly those of `juju` itself.
* **Client authentication:** a random bearer token is stored in an
  application-owned Juju secret and shared by all units.
* **Transport:** MCP streamable HTTP in stateless mode at `/mcp`, with HTTPS by
  default (self-signed or your own certificate). `GET /healthz` is the only
  unauthenticated path.
* **Workload:** a Python server (`mcp` SDK, `jubilant`, `uvicorn`) that ships
  inside the charm's virtual environment and runs as a hardened systemd
  service. The server runs the Juju CLI binary from the snap directly
  (`/snap/juju/current/bin/juju`), so it can use its private `JUJU_DATA`
  under the service's sandbox.
* **Stateless:** every CLI command logs in afresh. The server keeps no
  long-lived connections, and Jubilant's blocking calls run in worker threads
  so the server stays responsive. Each command has a timeout, so a stuck
  command can't hang a tool.

## Requirements

* A Juju **machine** controller (LXD, MAAS, OpenStack, AWS, manual, ...),
  version 3.6 or later. Kubernetes controllers don't support subordinate
  charms.
* Controller machines running Ubuntu 22.04, 24.04 or 26.04 (amd64 or arm64).
  A subordinate must match its principal's base, so deploy the `.charm` built
  for your controller machines' base.
* The controller machines must be able to install the `juju` snap, from the
  Snap Store or a Snap Store Proxy in air-gapped environments.
* Network access from your MCP clients to the controller machines on the MCP
  port (8765 by default).
* Juju **superuser** access to set the charm up: you need it to create users,
  grant access and deploy into the `controller` model.

## Quick start

```bash
juju switch <controller>:controller

# 1. A dedicated Juju user for the MCP server.
juju add-user mcp
juju change-user-password mcp                 # pick a strong password
juju grant mcp read <model>                   # repeat for each model it may see

# 2. Deploy as a subordinate of the controller.
juju deploy ./juju-mcp_ubuntu@24.04-amd64.charm juju-mcp
juju integrate juju-mcp:controller controller

# 3. Give it the credentials.
uri=$(juju add-secret mcp-credentials username=mcp password='<password>')
juju grant-secret mcp-credentials juju-mcp
juju config juju-mcp credentials-secret="$uri"

# 4. Wait for: active  read-only MCP on https://<address>:8765/mcp
juju status juju-mcp

# 5. Get the URL, token and CA certificate for your client.
juju run juju-mcp/leader get-connection-info
```

Then [connect a client](#connecting-mcp-clients).

## Deployment in detail

### 1. Create a Juju user for the server

The MCP server can do exactly what this user can do, and nothing more. Create
a dedicated user instead of reusing `admin`:

```bash
juju add-user mcp
juju change-user-password mcp
```

To set the password non-interactively (for example from automation):

```bash
echo "$PASSWORD" | juju change-user-password mcp --no-prompt
```

Grant model access according to the tools you plan to use:

| Juju model access | Tools that work |
| --- | --- |
| `read` | All read-only tools: status, config, actions list, action results, debug log, secret metadata |
| `write` | Additionally: `run_action`, `set_application_config`, `reset_application_config`, `deploy`, `add_units`, `remove_units`, `integrate`, `remove_relation`, `refresh_application`, `remove_application` |
| `admin` | Additionally: `exec_command` (`juju exec`, which runs as root on the targets) |

```bash
juju grant mcp read  prod-db
juju grant mcp write staging
juju grant mcp admin sandbox
```

Models the user can't access don't appear in `list_models` and can't be
targeted. To reduce access later, run `juju revoke mcp write staging`. The
change takes effect on the next tool call, with no restart needed.

> The write tools are only *registered* when `allow-write=true` (see below).
> With the default `allow-write=false`, even an `admin` user is limited to
> read-only tools.

### 2. Deploy and integrate

```bash
juju switch <controller>:controller
juju deploy ./juju-mcp_ubuntu@24.04-amd64.charm juju-mcp   # or: juju deploy juju-mcp
juju integrate juju-mcp:controller controller
```

Pick the `.charm` that matches the controller machines' base and architecture,
for example `juju-mcp_ubuntu@22.04-amd64.charm` for 22.04 controllers.
Charmcraft builds one file per platform. To check the base of your controller
machines:

```bash
juju machines -m controller --format=json | jq -r '.machines[].base | "\(.name)@\(.channel)"'
```

With an HA controller (`juju enable-ha`), every controller machine gets a
`juju-mcp` unit automatically. All units share the same bearer token.

Until credentials are configured, the unit shows
`blocked: set the credentials-secret config option`.

### 3. Provide the credentials

```bash
uri=$(juju add-secret mcp-credentials username=mcp password='<password>')
echo "$uri"                                   # e.g. secret:d3abc...
juju grant-secret mcp-credentials juju-mcp
juju config juju-mcp credentials-secret="$uri"
```

The secret must contain `username` and `password` keys.

### 4. Verify

```bash
juju status juju-mcp
# juju-mcp/0*  active  idle  ...  read-only MCP on https://10.0.0.5:8765/mcp
```

The status message shows the mode (`read-only` or `read-write`) and the URL
clients should use.

### 5. Get the connection details

```bash
juju run juju-mcp/leader get-connection-info
```

| Result key | Content |
| --- | --- |
| `url` | MCP endpoint, e.g. `https://10.0.0.5:8765/mcp` |
| `token` | Bearer token for the `Authorization` header |
| `ca-cert` | The self-signed certificate to trust. Only returned when `tls-secret` is unset. |
| `client-config` | Ready-made JSON in the common `mcpServers` format |

To save the CA certificate to a file, use JSON output. The default YAML
output can introduce indentation when copied by hand.

```bash
juju run juju-mcp/leader get-connection-info --format=json \
  | jq -r '.[].results["ca-cert"]' > ~/juju-mcp-ca.pem
```

## Networking

### Opening the port

The charm opens its port (`8765/tcp` by default) on the controller machines.
On clouds where Juju manages security groups (OpenStack, AWS, GCE, Azure),
expose the application to open it, ideally only to your client networks:

```bash
juju expose juju-mcp --to-cidrs 192.168.0.0/16      # or plain: juju expose juju-mcp
```

On MAAS, LXD and manual clouds, check any host or network firewall between
the clients and the controller.

### Floating IPs, NAT and DNS names: `external-hostname`

The advertised URL uses the unit's ingress address. If clients reach the
controller through a different address, such as an OpenStack floating IP, a
NAT address or a DNS name, set `external-hostname`:

```bash
juju config juju-mcp external-hostname=192.168.2.29        # or mcp.example.com
```

That address is then used in `get-connection-info` and in the status message,
and it's added to the self-signed certificate. The certificate is regenerated
and the server restarted automatically. Re-save the CA certificate on your
clients afterwards.

> **Tip:** the address your `juju` CLI uses is a good candidate. Check
> `juju show-controller --format=json | jq '.[].details["api-endpoints"]'`.

### Restricting the listen address: `bind-address`

By default the server listens on all addresses (`0.0.0.0`). To listen on a
single interface:

```bash
juju config juju-mcp bind-address=10.0.0.5
```

### SSH tunnel (no open port)

For maximum isolation, bind to loopback and reach the server through an SSH
tunnel:

```bash
juju config juju-mcp bind-address=127.0.0.1
juju ssh -m controller 0 -N -L 8765:127.0.0.1:8765      # keep this running
# then use https://127.0.0.1:8765/mcp, which the self-signed cert covers
```

## TLS

### Default: self-signed certificate

When `tls-secret` is unset, the charm generates a self-signed ECDSA
certificate:

* It's valid for one year and renewed automatically 30 days before expiry.
* Its SANs cover `localhost`, `127.0.0.1`, `::1`, the machine hostname, the
  unit's ingress addresses, `bind-address` (if specific) and
  `external-hostname`.
* It's regenerated whenever that list changes, for example when you set
  `external-hostname`.

Clients must trust this certificate: get it from `get-connection-info`
(`ca-cert`), save it to a file, and configure your client to trust it (see
[Connecting MCP clients](#connecting-mcp-clients)). Re-save it whenever it's
regenerated.

### Your own certificate: `tls-secret`

Use a certificate from your own CA or a public CA:

```bash
uri=$(juju add-secret mcp-tls certificate="$(cat server.crt)" private-key="$(cat server.key)")
juju grant-secret mcp-tls juju-mcp
juju config juju-mcp tls-secret="$uri"
```

`certificate` may contain the full chain (leaf first). The certificate's SANs
must cover the address clients use. Updating the secret
(`juju update-secret mcp-tls ...`) rolls the new certificate out
automatically.

### Behind a TLS-terminating proxy: `insecure-http`

```bash
juju config juju-mcp insecure-http=true bind-address=127.0.0.1
```

This serves plain HTTP. Only use it when a reverse proxy on the same host
terminates TLS, or for a loopback-only setup. The bearer token would otherwise
cross the network in clear text.

## Connecting MCP clients

All clients need three things from
`juju run juju-mcp/leader get-connection-info`:

1. **URL**, e.g. `https://192.168.2.29:8765/mcp`
2. **Header** `Authorization: Bearer <token>`
3. **Trust** for the CA certificate (only with the default self-signed
   certificate), saved for example to `~/juju-mcp-ca.pem`

The transport is MCP **streamable HTTP** (sometimes called `http` or `remote`
in client configs).

### Trusting the self-signed certificate

Skip this if you use your own certificate through `tls-secret`.

```bash
juju run juju-mcp/leader get-connection-info --format=json \
  | jq -r '.[].results["ca-cert"]' > ~/juju-mcp-ca.pem
```

| Client type | How to trust `~/juju-mcp-ca.pem` |
| --- | --- |
| Node-based (Claude Code, opencode, mcp-remote, Inspector) | `export NODE_EXTRA_CA_CERTS=~/juju-mcp-ca.pem` **before** starting the client |
| Python (`ssl`, `httpx`) | `ssl.create_default_context(cafile=...)`, or `export SSL_CERT_FILE=...` |
| curl | `--cacert ~/juju-mcp-ca.pem` |
| Anything else / system-wide | `sudo cp ~/juju-mcp-ca.pem /usr/local/share/ca-certificates/juju-mcp.crt && sudo update-ca-certificates` |

The certificate is regenerated when its address list changes (for example
after setting `external-hostname`) and renewed yearly. Fetch it again
afterwards.

### Python (included example)

[`examples/client.py`](examples/client.py) is a self-contained script (PEP 723
inline dependencies) that fetches the connection details through the juju CLI
for you:

```bash
# Demo: controller info, all models, per-application status
uv run examples/client.py -m <controller>:controller

# List tools
uv run examples/client.py -m <controller>:controller tools

# Call any tool with JSON arguments
uv run examples/client.py -m <controller>:controller call list_models
uv run examples/client.py -m <controller>:controller call model_status '{"model": "prod-db"}'
uv run examples/client.py -m <controller>:controller call debug_log \
    '{"model": "prod-db", "lines": 50, "level": "WARNING", "include": ["postgresql/0"]}'

# Override the URL (floating IP, SSH tunnel, ...)
uv run examples/client.py -m <controller>:controller --url https://127.0.0.1:8765/mcp tools

# Without the juju CLI
export JUJU_MCP_URL=https://192.168.2.29:8765/mcp
export JUJU_MCP_TOKEN=<token>
export JUJU_MCP_CA_CERT=~/juju-mcp-ca.pem
uv run examples/client.py tools
```

The core of a client in your own code:

```python
import ssl, httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

ctx = ssl.create_default_context(cafile="juju-mcp-ca.pem")
http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, verify=ctx)
async with http, Client(streamable_http_client(url, http_client=http)) as client:
    result = await client.call_tool("model_status", {"model": "prod-db"})
    print(result.structured_content)
```

### Claude Code

Node-based clients read extra CA certificates from `NODE_EXTRA_CA_CERTS`
**at startup**. Export it in the shell that launches `claude`, for example in
`~/.bashrc`:

```bash
export NODE_EXTRA_CA_CERTS=~/juju-mcp-ca.pem
claude mcp add --transport http --scope user juju https://192.168.2.29:8765/mcp \
  --header "Authorization: Bearer <token>"
claude          # then /mcp should list "juju" as connected
```

### opencode

In `opencode.json` (project) or `~/.config/opencode/opencode.json` (global),
starting opencode with `NODE_EXTRA_CA_CERTS` exported as above:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "juju": {
      "type": "remote",
      "url": "https://192.168.2.29:8765/mcp",
      "headers": { "Authorization": "Bearer <token>" }
    }
  }
}
```

### VS Code (GitHub Copilot agent mode)

In `.vscode/mcp.json`. For the self-signed certificate, add it to the
system trust store (see the table above) and restart VS Code.

```json
{
  "servers": {
    "juju": {
      "type": "http",
      "url": "https://192.168.2.29:8765/mcp",
      "headers": { "Authorization": "Bearer <token>" }
    }
  }
}
```

### Claude Desktop and stdio-only clients

Clients that only speak stdio can use the
[`mcp-remote`](https://www.npmjs.com/package/mcp-remote) bridge:

```json
{
  "mcpServers": {
    "juju": {
      "command": "npx",
      "args": ["mcp-remote", "https://192.168.2.29:8765/mcp",
               "--header", "Authorization:${JUJU_MCP_AUTH}"],
      "env": {
        "JUJU_MCP_AUTH": "Bearer <token>",
        "NODE_EXTRA_CA_CERTS": "/home/me/juju-mcp-ca.pem"
      }
    }
  }
}
```

### MCP Inspector (interactive testing)

```bash
NODE_EXTRA_CA_CERTS=~/juju-mcp-ca.pem npx @modelcontextprotocol/inspector
```

Choose **Streamable HTTP**, enter the URL, and add the
`Authorization: Bearer <token>` header.

### Raw HTTP (curl)

The server is stateless, so single JSON-RPC requests work without an
`initialize` handshake. Responses arrive as server-sent events.

```bash
URL=https://192.168.2.29:8765
H=(-H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json"
   -H "Accept: application/json, text/event-stream" -H "MCP-Protocol-Version: 2025-06-18")

curl --cacert ~/juju-mcp-ca.pem $URL/healthz                       # no auth needed
curl --cacert ~/juju-mcp-ca.pem $URL/mcp "${H[@]}" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
curl --cacert ~/juju-mcp-ca.pem $URL/mcp "${H[@]}" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"list_models","arguments":{}}}'
```

## Tools reference

Every tool that takes a `model` argument accepts a model **name**
(`prod-db`), a **qualified name** (`admin/prod-db`) or a **UUID**. Use the
qualified name if two owners have models with the same name. Results are
structured JSON. Errors such as unknown models or permission denied come back
as tool errors with the Juju message.

### Read-only tools (always available)

| Tool | Arguments | Description |
| --- | --- | --- |
| `controller_info` | none | Controller UUID, Juju version, cloud, API endpoints, connected user and controller access level (`juju show-controller`) |
| `list_models` | none | Accessible models with name, qualified name, UUID, type, owner, cloud, region, status, life, access level and agent version (`juju models`) |
| `model_status` | `model`, `patterns?`, `raw?` | Summary of applications (charm, channel, revision, status, units, subordinates), machines and relations. `patterns` filter like `juju status <pattern>`. `raw=true` returns the complete FullStatus |
| `get_model_config` | `model` | Model configuration values |
| `get_application_config` | `model`, `application` | `charm`, `settings` (value, default, source, type and description per option) and `application-config` (`juju config <app>`) |
| `list_actions` | `model`, `application` | Actions the charm defines, with parameter JSON schemas |
| `show_action` | `model`, `action_id`, `wait?` | Status, output, timestamps and message of an action or exec task. `wait` blocks up to N seconds for completion |
| `debug_log` | `model`, `lines?`=100, `level?`, `include?`, `exclude?`, `include_module?`, `exclude_module?` | Most recent debug-log lines (`juju debug-log --limit N`), parsed into timestamp, entity, level, module and message. Entities are passed to `--include`/`--exclude`: units (`app/0`), applications (`app`) or machines (`0`) |
| `list_secrets` | `model` | Secret metadata (URI, label, owner, revisions). **Secret values are never returned** |

### Write tools (only when `allow-write=true`)

| Tool | Arguments | Min. Juju access | Description |
| --- | --- | --- | --- |
| `run_action` | `model`, `unit`, `action`, `params?`, `wait?`=300 | write | Run a charm action and wait for its result |
| `exec_command` | `model`, `command`, `units?`, `applications?`, `machines?`, `wait?`=300 | **admin** | `juju exec`: run a shell command **as root** on the targets |
| `set_application_config` | `model`, `application`, `config` | write | Set charm options, e.g. `{"log-level": "debug"}` |
| `reset_application_config` | `model`, `application`, `keys` | write | Reset options to their defaults |
| `deploy` | `model`, `charm`, `application?`, `channel?`, `revision?`, `base?`, `num_units?`=1, `config?`, `constraints?`, `to?`, `trust?` | write | Deploy a charm **from Charmhub** (local paths are refused) |
| `refresh_application` | `model`, `application`, `channel?`, `revision?` | write | Upgrade or switch a charm's channel or revision |
| `add_units` | `model`, `application`, `count?`=1, `to?` | write | Scale out |
| `remove_units` | `model`, `units`, `destroy_storage?`, `force?` | write | Scale in |
| `integrate` | `model`, `endpoint_a`, `endpoint_b` | write | Relate two applications (`app` or `app:endpoint`) |
| `remove_relation` | `model`, `endpoint_a`, `endpoint_b`, `force?` | write | Remove a relation |
| `remove_application` | `model`, `application`, `destroy_storage?`, `force?` | write | Remove an application and its units |

Tools carry MCP annotations (`readOnlyHint`, `destructiveHint`), so clients
that support them can ask for confirmation before destructive calls.

### Example prompts

* "List my models and tell me which ones have applications in error or
  blocked state."
* "Show the last 200 warning-or-worse log lines for postgresql/0 in prod-db
  and explain the failure."
* "What actions does the forgejo-runner charm offer, and what parameters does
  register-runner take?"
* "Compare the config of grafana in staging and prod." (read-only)
* "Run the `get-admin-password` action on grafana/0 in staging." (needs
  `allow-write`)

## Configuration reference

| Option | Type | Default | Description |
| --- | --- | --- | --- |
| `credentials-secret` | secret | - | **Required.** URI of a user secret with `username` and `password` keys. The server acts as this Juju user. |
| `allow-write` | boolean | `false` | Register the mutating tools. When `false`, only read-only tools exist. |
| `port` | int | `8765` | TCP listen port. Opened on the machine automatically. |
| `bind-address` | string | `0.0.0.0` | Listen address: `0.0.0.0`/`::` for all, or a specific IP (e.g. `127.0.0.1`). |
| `external-hostname` | string | `""` | Address clients use (floating IP, NAT, DNS). Used in the advertised URL and added to the self-signed certificate. |
| `tls-secret` | secret | - | URI of a user secret with `certificate` and `private-key` (PEM). If unset, a self-signed certificate is generated. |
| `insecure-http` | boolean | `false` | Serve plain HTTP. Only behind a TLS-terminating proxy or on loopback. |
| `controller-endpoints` | string | `""` | Comma-separated `host:port` list overriding the discovered controller API endpoints. |
| `juju-channel` | string | `""` | Snap channel of the Juju CLI that the server drives. Defaults to the controller's `<major>.<minor>/stable`. |
| `log-level` | string | `info` | Server log level: `debug`, `info`, `warning`, `error`, `critical`. At `debug`, every Juju CLI command is logged. |

Every change is applied in place. The server restarts only if its effective
configuration, unit file or certificate changed.

## Actions reference

| Action | Runs on | Description |
| --- | --- | --- |
| `get-connection-info` | any unit | Returns `url`, `token`, `ca-cert` (self-signed only) and `client-config` |
| `rotate-token` | **leader** | Generates a new bearer token. All units switch to it, and existing clients must be reconfigured |

```bash
juju run juju-mcp/leader get-connection-info
juju run juju-mcp/leader rotate-token
```

## Security model

* **Two credentials, two purposes.** The bearer token protects the MCP
  endpoint. The Juju user determines what the server can do in Juju. A leaked
  token gives exactly the access of that Juju user, through the tools
  enabled by `allow-write`.
* **Least privilege.** Use a dedicated user and grant `read` unless you need
  more. Grant `admin` only where `exec_command` is truly needed: it gives root
  on every machine in the model.
* **Read-only by default.** Write tools aren't even advertised to clients
  unless `allow-write=true`.
* **Secret hygiene.**
  * Credentials, token and TLS key live in Juju secrets.
  * On disk they're only in `/etc/juju-mcp/` and the server's private
    `JUJU_DATA`, `/var/lib/juju-mcp/juju`. Both are root-only, with mode
    `0700` for directories and `0600` for files.
  * `list_secrets` never returns secret values.
  * `get-connection-info` output is visible only to people who can run actions
    in the controller model.
* **Transport.** HTTPS by default. The token is compared in constant time.
  Unauthenticated requests get `401` and never reach the MCP layer.
* **Process hardening.** The systemd unit uses `ProtectSystem=strict`,
  `ProtectHome`, `PrivateTmp`, `NoNewPrivileges`, an empty capability set,
  restricted address families and more.
* **Audit.** Every Juju operation is performed as the configured user, so it
  shows up under that user in Juju's audit log. HTTP access is logged in the
  service journal.
* **Prompt injection.** Status messages, logs and action output are data
  produced by workloads. An AI client may treat them as instructions. Keep
  `allow-write=false` unless you supervise the client's actions, and prefer
  clients that ask for confirmation on destructive tools.

## Operations

### Rotate the bearer token

```bash
juju run juju-mcp/leader rotate-token
juju run juju-mcp/leader get-connection-info      # distribute the new token
```

### Rotate the Juju password

```bash
juju change-user-password mcp
juju update-secret mcp-credentials username=mcp password='<new-password>'
```

The charm picks up the new secret revision and restarts the server.

### Enable or disable write tools

```bash
juju config juju-mcp allow-write=true     # status: read-write MCP on ...
juju config juju-mcp allow-write=false
```

MCP clients refresh their tool list on reconnect.

### Upgrade the charm

```bash
juju refresh juju-mcp --path ./juju-mcp_ubuntu@24.04-amd64.charm   # or --channel ...
```

### Logs

```bash
juju ssh -m controller juju-mcp/0 -- sudo journalctl -u juju-mcp -f
juju config juju-mcp log-level=debug      # temporarily, for more detail
juju debug-log -m controller --include juju-mcp/0     # charm (hook) logs
```

### Remove

```bash
juju remove-application juju-mcp
```

This stops and removes the service, unit file, configuration, TLS material
and `/var/lib/juju-mcp`. It also removes the `juju` snap, but only if this
charm installed it. The `mcp` Juju user and the secrets you created stay. Remove them
with `juju remove-user mcp` and `juju remove-secret ...`.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `blocked: set the credentials-secret config option` | Configure credentials ([step 3](#3-provide-the-credentials)). |
| `blocked: credentials-secret not found or not granted to this application` | Run `juju grant-secret <secret> juju-mcp`, and check that the URI in config is correct. |
| `blocked: credentials-secret is missing keys: ...` | The secret needs both `username` and `password`. |
| `waiting: waiting for the leader to create the bearer token` | Transient on non-leader units. Should resolve after the leader's next hook. |
| `blocked: cannot discover the controller: ...` | The principal isn't a controller, or the agent config is unreadable. Set `controller-endpoints` explicitly. |
| `blocked: MCP server is not responding` | Check `journalctl -u juju-mcp` on the machine, for example for a port already in use or an invalid `tls-secret` key pair. |
| Client: *connection refused*, *no route to host* or timeout | The advertised address isn't reachable from the client. Use a reachable address and set [`external-hostname`](#floating-ips-nat-and-dns-names-external-hostname). Check security groups (`juju expose juju-mcp`). Test with `curl --cacert ca.pem https://<addr>:8765/healthz`. |
| Client: *self-signed certificate*, *unable to verify*, *IP address mismatch* | (1) Re-save the current `ca-cert` with the `--format=json` command above, because it changes when the SANs change. (2) For Node clients, export `NODE_EXTRA_CA_CERTS` *before* starting the client. (3) The address you connect to must be in the certificate: set `external-hostname`. Verify with `openssl verify -CAfile ca.pem -verify_ip <ip> <(echo \| openssl s_client -connect <ip>:8765 2>/dev/null \| openssl x509)`. |
| Client: `401 Unauthorized` | Missing or old token. Fetch it again with `get-connection-info`, especially after `rotate-token`. |
| Tool error: `model 'x' not found or not accessible` | The Juju user lacks access: `juju grant mcp read x`. |
| Tool error: `permission denied` | The operation needs more access (see the [table](#1-create-a-juju-user-for-the-server)). |
| `blocked: cannot install juju snap (<channel>)` | The machine can't reach the Snap Store, or the channel doesn't exist. Configure a Snap Store Proxy, or pick an existing channel with `juju config juju-mcp juju-channel=3.6/stable`. The details are in `juju debug-log --include juju-mcp/0`. |
| `blocked: cannot determine the controller version; set juju-channel` | Set `juju-channel` explicitly. |
| Tool error: `` `juju <cmd>` did not finish within Ns `` | The CLI command hit its timeout, which usually means the controller or the model's cloud is slow or unreachable. Check the same command with your own `juju` CLI. |
| Tool error: `juju CLI not available` | The snap was removed or is mid-refresh. Run `juju resolve`, or trigger a hook (for example `juju config juju-mcp log-level=info`) to reinstall it. |

## Limitations

* Machine controllers only. Kubernetes controllers can't host subordinates.
* Every tool call starts one or more `juju` processes and logs in
  (typically 0.3–1 s per call). That's fine for interactive use, but not
  meant for high-frequency polling.
* Juju 4 controllers get a Juju 4 CLI automatically, through `juju-channel`
  detection. Jubilant supports both Juju 3 and 4, but the tools haven't been
  tested in depth against Juju 4.
* `debug_log` returns a snapshot (no tailing). `exec_command` and
  `run_action` block until completion or `wait` seconds, then return the
  current state. Use `show_action` to poll further.
* Charms can only be deployed from Charmhub. Bundles, local charms and
  resources upload aren't supported through MCP.
* The bearer token is a single shared secret. There's no per-client identity
  or OAuth.

## Development

```text
src/
  charm.py                  # ops charm: reconcile, secrets, actions, status
  workload.py               # systemd unit, controller discovery, TLS, config files
  juju_mcp_server/          # the MCP server (no ops dependency)
    __main__.py             #   entry point: python -m juju_mcp_server --config ...
    app.py                  #   MCP app + bearer-token middleware + /healthz
    tools.py                #   tool definitions
    juju_client.py          #   Jubilant wrapper: JUJU_DATA, model resolution, CLI calls
    config.py               #   server config schema
examples/client.py          # example MCP client
tests/unit/                 # charm (ops.testing), workload and server tests
tests/integration/          # Jubilant tests against a real controller
```

```bash
uvx --with tox-uv tox -e format,lint,unit       # format, ruff/codespell/pyright, unit tests
charmcraft pack                                  # every platform buildable on this host
charmcraft pack --platform ubuntu@26.04:amd64    # or a single platform
uvx --with tox-uv tox -e integration             # needs a bootstrapped machine controller
```

Supported platforms are `ubuntu@{22.04,24.04,26.04}:{amd64,arm64}`. Each
`.charm` bundles compiled Python wheels for its base's Python (3.10, 3.12 or
3.14), so charms build natively. Build the arm64 charms on an arm64 host, or
with `charmcraft remote-build` on Launchpad.

Run the server locally against any controller for development:

```bash
cat > /tmp/mcp.json <<EOF
{"controller_endpoints": ["<controller-ip>:17070"], "controller_ca_cert": "<PEM>",
 "controller_uuid": "<uuid>", "username": "mcp", "password": "<pw>",
 "auth_token": "dev", "listen_host": "127.0.0.1", "listen_port": 8765,
 "juju_cli": "/snap/juju/current/bin/juju", "juju_data": "/tmp/juju-mcp-data",
 "log_level": "debug"}
EOF
PYTHONPATH=src .venv/bin/python -m juju_mcp_server --config /tmp/mcp.json   # plain HTTP
```

## License

Apache 2.0. See [LICENSE](LICENSE).
