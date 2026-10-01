# SPDX-License-Identifier: Apache-2.0
"""Shared build and invocation plumbing for the C++ pipelined-fetch harnesses.

The harnesses are C++ because they exercise the production planner and
fetch table directly. This module builds them once per session and runs
them, so the checks participate in the normal pytest suite. They link
neither libibverbs nor the Aerospike client, so they skip only when ``make``
or a C++ compiler is missing.

See ``docs/design/v1/distributed/l2_adapters/aerospike_rdma.md`` for the
data path they sit under.
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
_BUILD_TIMEOUT_SECONDS = 300
_RUN_TIMEOUT_SECONDS = 120


@pytest.fixture(scope="session")
def _logic_build_dir() -> Path:
    """Build the device-independent harnesses once and return their directory.

    Returns:
        Path to the directory holding the built executables.

    Raises:
        pytest.skip.Exception: If ``make`` or a C++ compiler is unavailable.
    """
    if shutil.which("make") is None:
        pytest.skip("make is not available")
    if shutil.which(os.environ.get("CXX", "g++")) is None:
        pytest.skip("no C++ compiler available")

    build = subprocess.run(
        ["make", "--silent", "logic"],
        cwd=_HARNESS_DIR,
        capture_output=True,
        text=True,
        timeout=_BUILD_TIMEOUT_SECONDS,
        check=False,
    )
    if build.returncode != 0:
        pytest.fail(
            "failed to build the device-independent harnesses:\n"
            f"stdout:\n{build.stdout}\nstderr:\n{build.stderr}"
        )
    return _HARNESS_DIR / "build"


@pytest.fixture
def logic_harness(_logic_build_dir: Path) -> Callable[[str], str]:
    """Return a callable that runs a named logic harness and yields its stdout.

    There is no device to skip on, so any non-zero exit is a failure.

    Returns:
        A function mapping a harness name to that harness's stdout.
    """

    def run(name: str) -> str:
        binary = _logic_build_dir / name
        if not binary.exists():
            pytest.fail(f"harness build reported success but {binary} is missing")

        result = subprocess.run(
            [str(binary)],
            capture_output=True,
            text=True,
            timeout=_RUN_TIMEOUT_SECONDS,
            check=False,
        )
        if result.returncode != 0:
            pytest.fail(
                f"{name} failed.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        return result.stdout

    return run


@pytest.fixture
def logic_harness_build_dir(_logic_build_dir: Path) -> Path:
    """Return the directory holding the built device-independent harnesses.

    Exposed for tests that run a harness in a way ``logic_harness`` does not
    cover -- passing a fixture file, or expecting a non-zero exit.

    Returns:
        Path to the build output directory.
    """
    return _logic_build_dir


@pytest.fixture
def logic_harness_with_fixture(
    _logic_build_dir: Path,
) -> Callable[[str, Path], str]:
    """Return a callable running a logic harness against a fixture file.

    Separate from ``logic_harness`` because these harnesses take an argument
    and print data rather than asserting and printing ``PASS``.

    Returns:
        A function mapping a harness name and a fixture path to its stdout.
    """

    def run(name: str, fixture: Path) -> str:
        binary = _logic_build_dir / name
        if not binary.exists():
            pytest.fail(f"harness build reported success but {binary} is missing")
        if not fixture.exists():
            pytest.fail(f"fixture {fixture} is missing")

        result = subprocess.run(
            [str(binary), str(fixture)],
            capture_output=True,
            text=True,
            timeout=_RUN_TIMEOUT_SECONDS,
            check=False,
        )
        if result.returncode != 0:
            pytest.fail(
                f"{name} failed on {fixture.name}.\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        return result.stdout

    return run
