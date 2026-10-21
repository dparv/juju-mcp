# Copyright 2026 dparv
# See LICENSE file for licensing details.
#
# The integration tests use the Jubilant library. They need a bootstrapped
# machine (e.g. LXD) controller, and deploy the charm into its controller
# model as a subordinate of the `controller` application.

import os
import pathlib
from collections.abc import Iterator

import jubilant
import pytest


@pytest.fixture(scope="session")
def charm():
    """Return the absolute path of the charm under test."""
    charm = os.environ.get("CHARM_PATH")
    if not charm:
        charm_dir = pathlib.Path()  # Assume the current working directory is the charm root.
        charms = list(charm_dir.glob("*.charm"))
        assert charms, f"No charms were found in {charm_dir.absolute()}"
        assert len(charms) == 1, f"Found more than one charm {charms}"
        charm = charms[0]
    path = pathlib.Path(charm).resolve()
    assert path.is_file(), f"{path} is not a file"
    return path


@pytest.fixture(scope="module")
def controller_juju() -> Iterator[jubilant.Juju]:
    """Return a Juju client bound to the controller model."""
    juju = jubilant.Juju(model="controller")
    juju.wait_timeout = 15 * 60
    yield juju
    if os.environ.get("KEEP_MODELS"):
        return
    juju.remove_application("juju-mcp", force=True)
