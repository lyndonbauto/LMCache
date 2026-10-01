# SPDX-License-Identifier: Apache-2.0
"""Conformance harness for :class:`AerospikeLayerArrivalSource`.

Runs the real native ``SinkFetchTable`` behind the source, with no fabric,
device, or cluster. ``fabric_free_session.FabricFreeConnector`` is a
test-only pybind module built from
``tests/v1/distributed/rdma/csrc/fabric_free_session_pybind.cpp``. It plays
the native client's part for the source, and the :class:`ArrivalDriver`'s
part for the suite: a landed slot is a sink batch row that returned OK, a
declined slot is one that failed.
"""

# Standard
from collections.abc import Sequence
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import Protocol
import importlib.util
import shutil
import subprocess
import sys

# Third Party
import pytest

# First Party
from lmcache.v1.distributed.l2_adapters.layerwise_source import (
    AerospikeLayerArrivalSource,
    NativePlanIssuer,
    PipelinedFetchConnector,
    PlannedFetchConnector,
)
from lmcache.v1.layerwise.fakes import ArrivalDriver

# Local
from .conftest import TEST_NODE_NAMES, SourceHarness

_RDMA_TEST_DIR = Path(__file__).resolve().parents[1] / "distributed" / "rdma"

#: Large enough for every conformance plan; small enough to catch a slot
#: offset that escapes its window.
WINDOW_BYTES = 1 << 16

#: Well above any conformance plan, so only tests that mean to hit the cap do.
_MAX_SLOTS = 4096


@cache
def _load_module() -> ModuleType:
    """Build ``fabric_free_session`` once per test session and import it.

    Returns:
        The imported extension module.

    Raises:
        pytest.skip.Exception: If ``make``, a C++ compiler, or pybind11 is
            unavailable, or the build fails.
    """
    if shutil.which("make") is None:
        pytest.skip("make is not available")
    if shutil.which("g++") is None and shutil.which("c++") is None:
        pytest.skip("no C++ compiler available")
    if importlib.util.find_spec("pybind11") is None:
        pytest.skip("pybind11 is not installed")
    build = subprocess.run(
        ["make", "--silent", "pyharness", f"PYTHON={sys.executable}"],
        cwd=_RDMA_TEST_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    if build.returncode != 0:
        pytest.skip(f"fabric_free_session did not build:\n{build.stderr}")
    built = sorted((_RDMA_TEST_DIR / "build").glob("fabric_free_session*.so"))
    if not built:
        pytest.skip("fabric_free_session built but no module was found")
    spec = importlib.util.spec_from_file_location("fabric_free_session", built[0])
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {built[0]}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FabricFreeClient(
    PipelinedFetchConnector, PlannedFetchConnector, ArrivalDriver, Protocol
):
    """A native client that also lands and declines its own slots.

    It has the batch-completion surface ``NativeConnectorL2Adapter`` polls,
    so a real adapter and storage manager can wrap it. It runs no batch
    operations, so nothing ever completes.
    """

    def event_fd(self) -> int:
        """Return an eventfd that never fires."""
        ...

    def drain_completions(self) -> list[tuple[int, bool, str, list[bool] | None]]:
        """Return no completions."""
        ...

    def close(self) -> None:
        """Close the eventfd."""
        ...

    def pipelined_fetch_node_name(self) -> str:
        """Return the first node, which the placer is built with.

        Raises:
            RuntimeError: If the cluster has no nodes.
        """
        ...


def fabric_free_connector(
    window_count: int = 1, node_names: Sequence[str] = TEST_NODE_NAMES
) -> FabricFreeClient:
    """Build a fabric-free native client over ``window_count`` windows.

    Window ``w`` covers offsets ``[w * WINDOW_BYTES, (w + 1) * WINDOW_BYTES)``
    and runs one fetch of at most ``_MAX_SLOTS`` slots.

    Args:
        window_count: How many fetches may run at once.
        node_names: The cluster's nodes. Plans name nodes by index into
            this list.

    Returns:
        The connector. It is both the native client a source runs over and
        the driver that lands and declines slots.

    Raises:
        pytest.skip.Exception: If the test module cannot be built here.
    """
    connector: FabricFreeClient = _load_module().FabricFreeConnector(
        list(node_names), WINDOW_BYTES, _MAX_SLOTS, window_count=window_count
    )
    return connector


def aerospike_harness() -> SourceHarness:
    """Build an Aerospike source over a fresh fabric-free native client.

    Returns:
        The source, issuing through :class:`NativePlanIssuer`, and the
        connector as its driver.

    Raises:
        pytest.skip.Exception: If the test module cannot be built here.
    """
    connector = fabric_free_connector()
    source = AerospikeLayerArrivalSource(connector, NativePlanIssuer(connector))
    return SourceHarness(source, connector)
