# SPDX-License-Identifier: Apache-2.0
"""Aerospike L2 storage backend profile.

Builds the ``lmcache.lmcache_aerospike`` extension against a libaerospike
development install.  Enabled via ``BUILD_WITH_AEROSPIKE=1`` (or the legacy
``BUILD_AEROSPIKE=1``), or auto-detected through ``AEROSPIKE_INCLUDE_DIR``.

RDMA reception (the Aerospike server writing KV payloads straight into
LMCache's pinned L1 slab) is a second, independent opt-in on top of that:

* ``BUILD_WITH_AEROSPIKE_RDMA=1`` links ``libibverbs`` and compiles the
  Reliable Connected path, which is portable and works on Soft-RoCE.
* ``BUILD_WITH_AEROSPIKE_EFA=1`` additionally compiles the EFA/SRD path,
  which needs ``libefa`` and only exists on AWS EFA hardware.

Both default to off.  A machine with no RDMA hardware and no ``rdma-core``
development headers builds exactly as it did before.
"""

# Standard
from pathlib import Path
from typing import TYPE_CHECKING
import os

if TYPE_CHECKING:
    # Third Party
    from setuptools.extension import Extension

# First Party
from setup_extensions.storage_backend_profiles import StorageBackendProfile

# Repo root: setup_extensions/storage_backend_profiles/aerospike.py -> parents[2]
ROOT_DIR = Path(__file__).resolve().parents[2]

# Opt-in gates for the RDMA reception path. Strictly default-off: a normal
# build on a machine with no RDMA hardware must be unaffected.
RDMA_ENV_VAR = "BUILD_WITH_AEROSPIKE_RDMA"
EFA_ENV_VAR = "BUILD_WITH_AEROSPIKE_EFA"


def is_rdma_requested() -> bool:
    """Return True when the RDMA reception path was explicitly requested.

    Enabling EFA/SRD implies RDMA, since the SRD path is a variant of the
    same verbs foundation.

    Returns:
        True if either ``BUILD_WITH_AEROSPIKE_RDMA`` or
        ``BUILD_WITH_AEROSPIKE_EFA`` is set to ``1``.
    """
    return (
        os.environ.get(RDMA_ENV_VAR, "0") == "1"
        or os.environ.get(EFA_ENV_VAR, "0") == "1"
    )


def is_efa_requested() -> bool:
    """Return True when the EFA/SRD queue-pair path was requested.

    Returns:
        True if ``BUILD_WITH_AEROSPIKE_EFA`` is set to ``1``.
    """
    return os.environ.get(EFA_ENV_VAR, "0") == "1"


class AerospikeStorageBackend(StorageBackendProfile):
    """Optional native Aerospike L2 storage backend."""

    name = "aerospike"
    env_var = "BUILD_WITH_AEROSPIKE"

    def detect(self) -> bool:
        """Detect Aerospike via the legacy ``BUILD_AEROSPIKE`` flag or by the
        presence of ``AEROSPIKE_INCLUDE_DIR``."""
        as_env = os.environ.get("BUILD_AEROSPIKE")
        if as_env is not None:
            return as_env == "1"
        return os.environ.get("AEROSPIKE_INCLUDE_DIR", "") != ""

    def build(self, extra_cxx_flags: list[str]) -> list["Extension"]:
        """Build the Aerospike CppExtension."""
        # Standard
        import ctypes.util

        # Third Party
        from torch.utils import cpp_extension

        as_include = os.environ.get("AEROSPIKE_INCLUDE_DIR", "")
        as_lib = os.environ.get("AEROSPIKE_LIBRARY_DIR", "")
        deps_yaml_lib = (
            ROOT_DIR / ".deps" / "libyaml-install" / "usr" / "lib" / "x86_64-linux-gnu"
        )
        include_dirs = [
            "csrc/storage_backends",
            "csrc/storage_backends/aerospike",
        ]
        if as_include:
            include_dirs.extend(as_include.split(";"))
        library_dirs: list[str] = []
        if as_lib:
            library_dirs.extend(as_lib.split(";"))
        extra_objects: list[str] = []
        yaml_shared = deps_yaml_lib / "libyaml.so"
        yaml_static = deps_yaml_lib / "libyaml.a"
        if yaml_shared.exists() or yaml_static.exists():
            library_dirs.append(str(deps_yaml_lib))

        libraries = ["aerospike"]
        if yaml_shared.exists() or ctypes.util.find_library("yaml"):
            libraries.append("yaml")
        elif yaml_static.exists():
            extra_objects.append(str(yaml_static))
        libraries.extend(["ssl", "crypto", "pthread", "z", "rt"])
        if os.environ.get("AEROSPIKE_EVENT_LIB", "libuv") == "libuv":
            libraries.append("uv")

        sources = [
            "csrc/storage_backends/aerospike/pybind.cpp",
            "csrc/storage_backends/aerospike/connector.cpp",
            # The connector shards every write through it, RDMA or not.
            "csrc/storage_backends/aerospike/shard_plan.cpp",
        ]
        macros: list[tuple[str, str]] = []

        if is_rdma_requested():
            # Only now do we take a hard dependency on rdma-core. Everything
            # above must keep working on a host without libibverbs.
            sources.append("csrc/storage_backends/aerospike/rdma_context.cpp")
            sources.append("csrc/storage_backends/aerospike/notification_depth.cpp")
            sources.append("csrc/storage_backends/aerospike/kv_sink_client.cpp")
            sources.append("csrc/storage_backends/aerospike/kv_sink_fanout.cpp")
            # No verbs dependency of its own, but built here so a break in the
            # pipelining model fails the RDMA build rather than only the test
            # harness. Not yet exposed through pybind.
            sources.append("csrc/storage_backends/aerospike/layer_pipeline.cpp")
            sources.append("csrc/storage_backends/aerospike/slot_planner.cpp")
            sources.append(
                "csrc/storage_backends/aerospike/pipelined_fetch_session.cpp"
            )
            sources.append(
                "csrc/storage_backends/aerospike/connector_pipelined_rdma.cpp"
            )
            sources.append("csrc/storage_backends/aerospike/pipelined_fetch_issue.cpp")
            sources.append(
                "csrc/storage_backends/aerospike/memory_layout_conversion.cpp"
            )
            sources.append(
                "csrc/storage_backends/aerospike/aerospike_pipelined_pybind.cpp"
            )
            libraries.append("ibverbs")
            macros.append(("LMCACHE_AEROSPIKE_RDMA", "1"))
            rdma_include = os.environ.get("RDMA_CORE_INCLUDE_DIR", "")
            if rdma_include:
                include_dirs.extend(rdma_include.split(";"))
            rdma_lib = os.environ.get("RDMA_CORE_LIBRARY_DIR", "")
            if rdma_lib:
                library_dirs.extend(rdma_lib.split(";"))
            if is_efa_requested():
                libraries.append("efa")
                macros.append(("LMCACHE_AEROSPIKE_EFA", "1"))

        runtime_library_dirs = list(library_dirs)

        return [
            cpp_extension.CppExtension(
                "lmcache.lmcache_aerospike",
                sources=sources,
                include_dirs=include_dirs,
                library_dirs=library_dirs,
                libraries=libraries,
                define_macros=macros,
                extra_objects=extra_objects,
                runtime_library_dirs=runtime_library_dirs,
                extra_compile_args={
                    "cxx": extra_cxx_flags + ["-O3", "-std=c++17"],
                },
                extra_link_args=["-Wl,--no-as-needed"],
            ),
        ]
