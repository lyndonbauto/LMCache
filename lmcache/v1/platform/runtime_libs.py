# SPDX-License-Identifier: Apache-2.0
"""Bind to the accelerator runtime library that PyTorch already loaded.

A host can carry several copies of a GPU runtime. On the MI300X image LMCache
was validated on, ``ctypes.util.find_library("amdhip64")`` returns a system
``libamdhip64.so.5`` while PyTorch runs on the pip ROCm SDK's
``libamdhip64.so.7``. Loading the first gives the process a second HIP runtime
that cannot see PyTorch's allocations or streams. Calls through it fail or,
worse, act on a different context.

:func:`load_runtime_library` therefore prefers the copy that is already mapped
into the process, and only falls back to loading by name when none is.
"""

# Future
from __future__ import annotations

# Standard
from collections.abc import Callable, Sequence
import ctypes
import os

_PROC_MAPS = "/proc/self/maps"


def _read_proc_maps() -> list[str]:
    """Return this process's memory-map lines, or an empty list if unavailable."""
    try:
        with open(_PROC_MAPS, encoding="utf-8", errors="replace") as maps:
            return maps.readlines()
    except OSError:
        return []


def mapped_library_path(
    stem: str,
    read_maps: Callable[[], list[str]] = _read_proc_maps,
) -> str | None:
    """Return the path of a loaded shared library whose file name starts with ``stem``.

    Args:
        stem: Library name without the version suffix, for example
            ``"libamdhip64"`` or ``"libcudart"``. Matches ``stem.so`` and
            ``stem.so.<version>`` only, so ``"libcudart"`` does not match
            ``libcudart_static``.
        read_maps: Returns the process's memory-map lines. Injectable for
            tests; defaults to reading ``/proc/self/maps``.

    Returns:
        The first matching path in map order, or ``None`` when no such
        library is mapped (including on platforms without ``/proc``).
    """
    prefix = f"{stem}.so"
    for line in read_maps():
        fields = line.split()
        if len(fields) < 6:
            continue
        path = fields[-1]
        name = os.path.basename(path)
        if name == prefix or name.startswith(prefix + "."):
            return path
    return None


def load_runtime_library(
    stem: str,
    fallback_names: Sequence[str],
    read_maps: Callable[[], list[str]] = _read_proc_maps,
    load: Callable[[str], ctypes.CDLL] = ctypes.CDLL,
) -> ctypes.CDLL | None:
    """Load the runtime library PyTorch uses, or the first loadable fallback.

    Args:
        stem: Library name without the version suffix (see
            :func:`mapped_library_path`).
        fallback_names: Names or paths to try in order when no copy of the
            library is mapped yet.
        read_maps: Memory-map reader, injectable for tests.
        load: Library loader, injectable for tests.

    Returns:
        The loaded library, or ``None`` when neither the mapped copy nor any
        fallback can be loaded.
    """
    candidates: list[str] = []
    mapped = mapped_library_path(stem, read_maps)
    if mapped is not None:
        candidates.append(mapped)
    candidates.extend(fallback_names)
    for candidate in candidates:
        try:
            return load(candidate)
        except OSError:
            continue
    return None
