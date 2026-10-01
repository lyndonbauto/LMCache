# SPDX-License-Identifier: Apache-2.0
"""Aerospike L2 storage backend profile.

Builds the ``lmcache.lmcache_aerospike`` extension against a libaerospike
development install.  Enabled via ``BUILD_WITH_AEROSPIKE=1`` (or the legacy
``BUILD_AEROSPIKE=1``), or auto-detected through ``AEROSPIKE_INCLUDE_DIR``.

RDMA reception (the Aerospike server writing KV payloads straight into
LMCache's pinned L1 slab) is a second, independent opt-in on top of that:
``BUILD_WITH_AEROSPIKE_RDMA=1`` compiles the pipelined kv-sink fetch. It
needs the kv-sink fork of the Aerospike C client (``aerospike/as_sink.h``),
built by ``.deps/build_aerospike_client_kvsink.sh``, and links ``libibverbs``
and ``libefa`` because that client's verbs transport does. The legacy
``BUILD_WITH_AEROSPIKE_EFA=1`` is accepted as a synonym: RC and SRD are both
chosen at run time now.

It defaults to off.  A machine with no RDMA hardware and no ``rdma-core``
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

# Searched in order for the system libyaml runtime (``libyaml-0.so.N``).
SYSTEM_LIB_DIRS: tuple[str, ...] = (
    "/usr/lib/x86_64-linux-gnu",
    "/usr/lib64",
    "/usr/lib",
)


def is_rdma_requested() -> bool:
    """Return True when the RDMA reception path was explicitly requested.

    Returns:
        True if either ``BUILD_WITH_AEROSPIKE_RDMA`` or
        ``BUILD_WITH_AEROSPIKE_EFA`` is set to ``1``.
    """
    return (
        os.environ.get(RDMA_ENV_VAR, "0") == "1"
        or os.environ.get(EFA_ENV_VAR, "0") == "1"
    )


def _system_yaml_soname() -> str:
    """Return the soname of the system libyaml runtime library.

    ``ctypes.util.find_library`` misses a runtime-only install (no
    ``libyaml.so`` development symlink), which is exactly the case where the
    extension must link the runtime library by name.

    Returns:
        A name such as ``"libyaml-0.so.2"``, or ``""`` if none is installed.
    """
    for directory in SYSTEM_LIB_DIRS:
        for candidate in sorted(Path(directory).glob("libyaml-0.so.[0-9]")):
            return candidate.name
    return ""


def _require_kv_sink_client(include_dirs: str) -> None:
    """Refuse an RDMA build against a stock Aerospike C client.

    The pipelined fetch calls ``aerospike_sink_create`` and sets the sink
    fields of batch-read rows, which only the kv-sink fork of the client has.
    Checking the header up front turns a wall of compiler errors into one
    actionable message.

    Args:
        include_dirs: ``AEROSPIKE_INCLUDE_DIR``, ``;``-separated.

    Raises:
        RuntimeError: If no directory holds ``aerospike/as_sink.h``.
    """
    candidates = [Path(d) for d in include_dirs.split(";") if d]
    if any((d / "aerospike" / "as_sink.h").exists() for d in candidates):
        return
    raise RuntimeError(
        f"{RDMA_ENV_VAR}=1 needs the kv-sink Aerospike C client, but no "
        "aerospike/as_sink.h was found under AEROSPIKE_INCLUDE_DIR. Build it "
        "with .deps/build_aerospike_client_kvsink.sh and source the "
        "aerospike-client-c.env it writes."
    )


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
        # libaerospike.so does not declare its libyaml dependency, so this
        # extension must, as a shared library. .deps usually holds only the
        # -dev package, whose libyaml.so dangles unless the runtime package
        # was extracted beside it; ld then skips it and would pick libyaml.a,
        # which cannot satisfy libaerospike.so. Hence the soname fallback.
        yaml_shared = deps_yaml_lib / "libyaml.so"
        yaml_static = deps_yaml_lib / "libyaml.a"
        system_yaml = _system_yaml_soname() or ctypes.util.find_library("yaml")

        libraries = ["aerospike"]
        if yaml_shared.exists():
            library_dirs.append(str(deps_yaml_lib))
            libraries.append("yaml")
        elif system_yaml:
            libraries.append(f":{system_yaml}")
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
            # Only now do we take a hard dependency on the kv-sink client and
            # rdma-core. Everything above must keep working without them.
            _require_kv_sink_client(as_include)
            for source in (
                "sink_fetch_table.cpp",
                "connector_sink_fetch.cpp",
                "layer_pipeline.cpp",
                "slot_planner.cpp",
                "memory_layout_conversion.cpp",
                "aerospike_pipelined_pybind.cpp",
            ):
                sources.append(f"csrc/storage_backends/aerospike/{source}")
            # The client's as_sink verbs transport calls into both; libaerospike
            # does not declare them, so this extension must.
            libraries.extend(["ibverbs", "efa"])
            macros.append(("LMCACHE_AEROSPIKE_RDMA", "1"))
            rdma_include = os.environ.get("RDMA_CORE_INCLUDE_DIR", "")
            if rdma_include:
                include_dirs.extend(rdma_include.split(";"))
            rdma_lib = os.environ.get("RDMA_CORE_LIBRARY_DIR", "")
            if rdma_lib:
                library_dirs.extend(rdma_lib.split(";"))

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
