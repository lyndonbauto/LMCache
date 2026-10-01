# SPDX-License-Identifier: Apache-2.0
"""Tests for how the Aerospike build profile links libyaml.

``libaerospike.so`` does not declare its libyaml dependency, so the extension
must link a shared libyaml itself. ``.deps`` usually holds only the ``-dev``
package, whose ``libyaml.so`` symlink dangles without the runtime package;
``ld`` then skips it and picks ``libyaml.a``, which cannot satisfy
``libaerospike.so`` and fails at import with an undefined symbol. These tests
lay out fake library trees and check the link inputs the profile chooses.
"""

# Standard
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
import ctypes.util
import os

# Third Party
import pytest

# First Party
from setup_extensions.storage_backend_profiles import aerospike

if TYPE_CHECKING:
    # Third Party
    from setuptools.extension import Extension

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="POSIX library layout and symlinks"
)

_DEPS_YAML_SUBDIR = Path(".deps", "libyaml-install", "usr", "lib", "x86_64-linux-gnu")
_RUNTIME_SONAME = "libyaml-0.so.2"


@dataclass(frozen=True)
class _LibraryTrees:
    """Throwaway stand-ins for the ``.deps`` and system library directories."""

    deps_lib: Path
    system_lib: Path


@pytest.fixture
def trees(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _LibraryTrees:
    """Point the profile at empty ``.deps`` and system library directories."""
    pytest.importorskip("torch.utils.cpp_extension")
    root = tmp_path / "repo"
    deps_lib = root / _DEPS_YAML_SUBDIR
    deps_lib.mkdir(parents=True)
    system_lib = tmp_path / "system"
    system_lib.mkdir()

    monkeypatch.setattr(aerospike, "ROOT_DIR", root)
    monkeypatch.setattr(aerospike, "SYSTEM_LIB_DIRS", (str(system_lib),))
    monkeypatch.setattr(ctypes.util, "find_library", lambda name: None)
    for var in (
        "AEROSPIKE_INCLUDE_DIR",
        "AEROSPIKE_LIBRARY_DIR",
        "BUILD_WITH_AEROSPIKE_RDMA",
        "BUILD_WITH_AEROSPIKE_EFA",
    ):
        monkeypatch.delenv(var, raising=False)
    return _LibraryTrees(deps_lib=deps_lib, system_lib=system_lib)


def _build() -> "Extension":
    """Run the profile's ``build`` and return its single extension."""
    [extension] = aerospike.AerospikeStorageBackend().build([])
    return extension


def _yaml_libraries(extension: "Extension") -> list[str]:
    """Return the libyaml entries of the extension's ``-l`` libraries."""
    return [lib for lib in extension.libraries if "yaml" in lib]


def test_a_real_shared_libyaml_in_deps_is_linked_from_deps(
    trees: _LibraryTrees,
) -> None:
    (trees.deps_lib / "libyaml.so").write_bytes(b"")
    (trees.system_lib / _RUNTIME_SONAME).write_bytes(b"")

    extension = _build()

    assert _yaml_libraries(extension) == ["yaml"]
    assert str(trees.deps_lib) in extension.library_dirs
    assert extension.extra_objects == []


def test_a_dangling_deps_symlink_links_the_system_runtime_by_soname(
    trees: _LibraryTrees,
) -> None:
    """The regression: the static archive must not be picked over the runtime."""
    (trees.deps_lib / "libyaml.so").symlink_to(trees.deps_lib / _RUNTIME_SONAME)
    (trees.deps_lib / "libyaml.a").write_bytes(b"")
    (trees.system_lib / _RUNTIME_SONAME).write_bytes(b"")

    extension = _build()

    assert _yaml_libraries(extension) == [f":{_RUNTIME_SONAME}"]
    assert str(trees.deps_lib) not in extension.library_dirs
    assert extension.extra_objects == []


def test_a_runtime_found_only_by_find_library_is_linked_by_name(
    trees: _LibraryTrees, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        ctypes.util,
        "find_library",
        lambda name: _RUNTIME_SONAME if name == "yaml" else None,
    )

    extension = _build()

    assert _yaml_libraries(extension) == [f":{_RUNTIME_SONAME}"]


def test_the_static_archive_is_linked_only_when_no_shared_libyaml_exists(
    trees: _LibraryTrees,
) -> None:
    static = trees.deps_lib / "libyaml.a"
    static.write_bytes(b"")

    extension = _build()

    assert _yaml_libraries(extension) == []
    assert extension.extra_objects == [str(static)]
