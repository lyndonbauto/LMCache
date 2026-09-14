# SPDX-License-Identifier: Apache-2.0
"""Pytest wrapper for the RDMA byte-equivalence harness.

The harness itself is C++ (``csrc/rdma_equivalence_test.cpp``) because it has
to call libibverbs directly. This wrapper builds it and runs it so the check
participates in the normal test suite.

It skips, rather than fails, when the toolchain or an RDMA device is missing,
since neither is available on a typical CI box. To make it run locally:

    sudo modprobe rdma_rxe
    sudo rdma link add rxe0 type rxe netdev lo
    ibv_devinfo -d rxe0

See ``docs/design/v1/distributed/l2_adapters/aerospike_rdma.md`` for the full
setup.
"""

# Standard
from pathlib import Path
import os
import shutil
import subprocess

# Third Party
import pytest

_HARNESS_DIR = Path(__file__).resolve().parent
# The harness follows the automake convention: 77 means "skipped".
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


@pytest.fixture(scope="module")
def harness_binary() -> Path:
    """Build the RDMA equivalence harness and return its path.

    Returns:
        Path to the freshly built harness executable.

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
            "failed to build the RDMA equivalence harness:\n"
            f"stdout:\n{build.stdout}\nstderr:\n{build.stderr}"
        )

    binary = _HARNESS_DIR / "build" / "rdma_equivalence_test"
    if not binary.exists():
        pytest.fail(f"harness build reported success but {binary} is missing")
    return binary


def test_rdma_write_is_byte_identical_to_the_normal_path(
    harness_binary: Path,
) -> None:
    """A payload delivered by RDMA into L1 matches the non-RDMA payload.

    Runs the C++ harness, which drives the production ``RdmaContext`` and
    kv-sink codec against a mock Aerospike writer that performs real
    ``ibv_post_send`` RDMA writes and fences on its own send completion queue.

    Args:
        harness_binary: Path to the built harness, from the fixture.
    """
    command = [str(harness_binary)]
    device = os.environ.get("RDMA_DEVICE", "")
    if device:
        command.append(device)

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

    assert result.returncode == 0, f"RDMA byte-equivalence harness failed.\n{output}"
    assert "PASS" in result.stdout, output
