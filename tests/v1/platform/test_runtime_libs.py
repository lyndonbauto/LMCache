# SPDX-License-Identifier: Apache-2.0
"""Tests for binding to the GPU runtime PyTorch already loaded."""

# Standard
import ctypes

# Third Party
import pytest
import torch

# First Party
from lmcache.v1.platform import torch_ops
from lmcache.v1.platform.runtime_libs import load_runtime_library, mapped_library_path

_HIP_PATH = "/site-packages/_rocm_sdk_core/lib/libamdhip64.so.7"
_MAPS = [
    "7f00-7f10 r-xp 00000000 08:01 1 /usr/lib/libc.so.6\n",
    "7f20-7f30 r-xp 00000000 08:01 2 /opt/rocm/lib/libamdhip64_static_helper.so\n",
    f"7f40-7f50 r-xp 00000000 08:01 3 {_HIP_PATH}\n",
    "7f60-7f70 rw-p 00000000 00:00 0\n",
]


def test_mapped_library_path_finds_the_versioned_copy() -> None:
    path = mapped_library_path("libamdhip64", lambda: _MAPS)
    assert path == _HIP_PATH


def test_mapped_library_path_needs_an_exact_stem() -> None:
    assert mapped_library_path("libamdhip64_static", lambda: _MAPS) is None
    assert mapped_library_path("libcudart", lambda: _MAPS) is None
    assert mapped_library_path("libamdhip64", lambda: []) is None


def test_load_prefers_the_mapped_copy_over_fallbacks() -> None:
    loaded: list[str] = []

    def load(name: str) -> ctypes.CDLL:
        loaded.append(name)
        return object()  # type: ignore[return-value]

    load_runtime_library("libamdhip64", ["libamdhip64.so"], lambda: _MAPS, load)
    assert loaded == [_HIP_PATH]


def test_load_falls_back_in_order_and_returns_none_when_nothing_loads() -> None:
    attempts: list[str] = []

    def load(name: str) -> ctypes.CDLL:
        attempts.append(name)
        if name == "second.so":
            return object()  # type: ignore[return-value]
        raise OSError(name)

    assert (
        load_runtime_library("libnothing", ["first.so", "second.so"], lambda: [], load)
        is not None
    )
    assert attempts == ["first.so", "second.so"]
    assert load_runtime_library("libnothing", ["first.so"], lambda: [], load) is None


class _FakeFunction:
    """Stand-in for a ctypes function: records calls, returns a status."""

    def __init__(self, status: int = 0) -> None:
        self.status = status
        self.calls: list[tuple[int, ...]] = []

    def __call__(self, *args: object) -> int:
        self.calls.append(tuple(getattr(arg, "value", arg) for arg in args))
        return self.status


class _FakeLibrary:
    def __init__(self, **symbols: _FakeFunction) -> None:
        for name, function in symbols.items():
            setattr(self, name, function)


def test_rocm_builds_bind_hip_memcpy_from_libamdhip64() -> None:
    requested: list[str] = []
    library = _FakeLibrary(hipMemcpy=_FakeFunction())

    def load(stem: str, fallbacks: list[str]) -> _FakeLibrary:
        requested.append(stem)
        return library

    memcpy = torch_ops._load_gpu_memcpy("7.15", None, load)  # type: ignore[arg-type]
    assert requested == ["libamdhip64"]
    assert memcpy is not None and memcpy.name == "hipMemcpy"
    memcpy(0x2000, 0x1000, 64, torch_ops._MEMCPY_DEFAULT)
    assert library.hipMemcpy.calls == [(0x2000, 0x1000, 64, 4)]


def test_cuda_builds_bind_cuda_memcpy_from_libcudart() -> None:
    requested: list[str] = []

    def load(stem: str, fallbacks: list[str]) -> _FakeLibrary:
        requested.append(stem)
        return _FakeLibrary(cudaMemcpy=_FakeFunction())

    memcpy = torch_ops._load_gpu_memcpy(None, "13.0", load)  # type: ignore[arg-type]
    assert requested == ["libcudart"]
    assert memcpy is not None and memcpy.name == "cudaMemcpy"


def test_cpu_builds_and_unusable_runtimes_use_the_cpu_fallback() -> None:
    assert torch_ops._load_gpu_memcpy(None, None, lambda *_: None) is None
    assert torch_ops._load_gpu_memcpy("7.15", None, lambda *_: None) is None
    # A HIP library without hipMemcpy (for example a CUDA one) is not used.
    wrong = _FakeLibrary(cudaMemcpy=_FakeFunction())
    assert torch_ops._load_gpu_memcpy("7.15", None, lambda *_: wrong) is None


def test_memcpy_errors_raise() -> None:
    memcpy = torch_ops._GpuMemcpy("hipMemcpy", _FakeFunction(status=1))  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="hipMemcpy failed with error code 1"):
        memcpy(1, 2, 3, torch_ops._MEMCPY_DEFAULT)


@pytest.mark.skipif(not torch.cuda.is_available(), reason="requires a GPU")
def test_gpu_memcpy_copies_device_memory_with_the_loaded_runtime() -> None:
    memcpy = torch_ops._get_gpu_memcpy()
    assert memcpy is not None
    assert memcpy.name == ("hipMemcpy" if torch.version.hip else "cudaMemcpy")
    src = torch.arange(1 << 16, dtype=torch.int32, device="cuda")
    dst = torch.zeros_like(src)
    memcpy(dst.data_ptr(), src.data_ptr(), src.numel() * 4, torch_ops._MEMCPY_DEFAULT)
    assert torch.equal(dst, src)
