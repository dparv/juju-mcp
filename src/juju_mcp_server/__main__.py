# Copyright 2026 dparv
# See LICENSE file for licensing details.

"""Entry point: ``python -m juju_mcp_server --config /etc/juju-mcp/config.json``."""

from __future__ import annotations

import argparse
import logging
import os
import sys

import uvicorn

from .app import build_app, client_from_config
from .config import ConfigError, ServerConfig

DEFAULT_CONFIG = "/etc/juju-mcp/config.json"


def main(argv: list[str] | None = None) -> int:
    """Run the MCP server until interrupted."""
    parser = argparse.ArgumentParser(prog="juju-mcp-server", description=__doc__)
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="path to the JSON config")
    args = parser.parse_args(argv)

    try:
        config = ServerConfig.load(args.config)
    except ConfigError as e:
        print(f"juju-mcp-server: {e}", file=sys.stderr)
        return 2

    logging.basicConfig(
        level=config.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Jubilant logs every CLI invocation at INFO; only show that when debugging.
    if config.log_level != "debug":
        logging.getLogger("jubilant").setLevel(logging.WARNING)

    if not os.access(config.juju_cli, os.X_OK):
        print(f"juju-mcp-server: juju CLI {config.juju_cli} not found", file=sys.stderr)
        return 2
    juju = client_from_config(config)
    juju.setup()

    uvicorn.run(
        build_app(config, juju),
        host=config.listen_host,
        port=config.listen_port,
        ssl_certfile=config.tls_cert_file,
        ssl_keyfile=config.tls_key_file,
        log_level=config.log_level,
        proxy_headers=False,
        server_header=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
