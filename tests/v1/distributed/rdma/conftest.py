# SPDX-License-Identifier: Apache-2.0
"""Shared build and invocation plumbing for the C++ RDMA harnesses.

The harnesses are C++ because they call libibverbs directly. This module
builds them once per session and runs them, so the checks participate in the
normal pytest suite.

Everything skips, rather than fails, when the toolchain or an RDMA device is
missing, since neither is available on a typical CI box. To make them run:

    sudo modprobe rdma_rxe
    sudo rdma link add rxe0 type rxe netdev lo
    ibv_devinfo -d rxe0
    export RDMA_DEVICE=rxe0 RDMA_GID_INDEX=1

``RDMA_GID_INDEX=1`` is needed on ``lo``: its MAC is all zeros, so GID index 0
is an ``fe80::`` link-local address with no route and the queue pair fails to
reach RTR with ``ENETUNREACH``. Index 1 is the IPv4-mapped entry. EFA wants
index 0 instead.

See ``docs/design/v1/distributed/l2_adapters/aerospike_rdma.md`` for the full
setup.
"""

# Standard
from collections.abc import Callable
from pathlib import Path
import os
import shutil
import subprocess

# Third Party
import pytest

_HARNESS_DIR = Path(__file__).resolve().parent
# The harnesses follow the automake convention: 77 means "skipped".
_SKIP_EXIT_CODE = 77
_BUILD_TIMEOUT_SECONDS = 300
_RUN_TIMEOUT_SECONDS = 120


def _verbs_header_available() -> bool:
    """Report whether ``infiniband/verbs.h`` can be found for compilation.

    Honors ``RDMA_CORE_INCLUDE_DIR`` for an out-of-tree rdma-core, matching
    the build profile.

    Returns:
        True when the header exists in a location the harness will search.
    """
    override = os.environ.get("RDMA_CORE_INCLUDE_DIR", "")
    candidates = [Path(override)] if override else []
    candidates += [Path("/usr/include"), Path("/usr/local/include")]
    return any((base / "infiniband" / "verbs.h").exists() for base in candidates)


@pytest.fixture(scope="session")
def _harness_build_dir() -> Path:
    """Build every RDMA harness once and return the output directory.

    Returns:
        Path to the directory holding the built executables.

    Raises:
        pytest.skip.Exception: If ``make``, a C++ compiler, or the libibverbs
            development headers are unavailable.
    """
    if shutil.which("make") is None:
        pytest.skip("make is not available")
    if shutil.which(os.environ.get("CXX", "g++")) is None:
        pytest.skip("no C++ compiler available")
    if not _verbs_header_available():
        pytest.skip(
            "libibverbs development headers not found; install rdma-core / "
            "libibverbs-dev, or set RDMA_CORE_INCLUDE_DIR"
        )

    build = subprocess.run(
        ["make", "--silent"],
        cwd=_HARNESS_DIR,
        capture_output=True,
        text=True,
        timeout=_BUILD_TIMEOUT_SECONDS,
        check=False,
    )
    if build.returncode != 0:
        pytest.fail(
            "failed to build the RDMA harnesses:\n"
            f"stdout:\n{build.stdout}\nstderr:\n{build.stderr}"
        )
    return _HARNESS_DIR / "build"


@pytest.fixture(scope="session")
def shard_plan_harness() -> str:
    """Build and run the device-independent shard-plan harness, returning stdout.

    Kept separate from ``rdma_harness`` because the shard plan is pure
    arithmetic: it links neither libibverbs nor the Aerospike client, so it
    must still run on a box with no RDMA toolchain, where the session build
    for the other harnesses skips.

    Returns:
        The harness's stdout.

    Raises:
        pytest.skip.Exception: If ``make`` or a C++ compiler is unavailable.
    """
    if shutil.which("make") is None:
        pytest.skip("make is not available")
    if shutil.which(os.environ.get("CXX", "g++")) is None:
        pytest.skip("no C++ compiler available")

    result = subprocess.run(
        ["make", "--silent", "shard"],
        cwd=_HARNESS_DIR,
        capture_output=True,
        text=True,
        timeout=_BUILD_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(
            "shard_plan_test failed:\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result.stdout


@pytest.fixture
def rdma_harness(_harness_build_dir: Path) -> Callable[[str], str]:
    """Return a callable that runs a named harness and yields its stdout.

    The callable takes the harness executable's base name, runs it against
    ``RDMA_DEVICE`` and ``RDMA_GID_INDEX``, skips the test when no RDMA device
    is present, and fails it with the harness output on any other non-zero
    exit.

    Returns:
        A function mapping a harness name to that harness's stdout.
    """

    def run(name: str) -> str:
        binary = _harness_build_dir / name
        if not binary.exists():
            pytest.fail(f"harness build reported success but {binary} is missing")

        command = [str(binary)]
        device = os.environ.get("RDMA_DEVICE", "")
        if device:
            command.append(device)
            # Positional, so this can only be passed alongside a device name.
            gid_index = os.environ.get("RDMA_GID_INDEX", "")
            if gid_index:
                command.append(gid_index)

        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=_RUN_TIMEOUT_SECONDS,
            check=False,
        )
        output = f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        if result.returncode == _SKIP_EXIT_CODE:
            pytest.skip(f"no RDMA device available.\n{output}")
        if result.returncode != 0:
            pytest.fail(f"{name} failed.\n{output}")
        return result.stdout

    return run
