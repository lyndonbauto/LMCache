# SPDX-License-Identifier: Apache-2.0
"""ROCm platform primitives built on PyTorch's CUDA-compatible surface."""

# Future
from __future__ import annotations

# Standard
from typing import TYPE_CHECKING

# First Party
from lmcache.v1.platform.cuda import CudaDeviceSpec
from lmcache.v1.platform.ipc_policy import get_ipc_policy

if TYPE_CHECKING:
    # First Party
    from lmcache.v1.platform.base.event_ipc import EventIPCBackend


class RocmDeviceSpec(CudaDeviceSpec):
    """ROCm device specification for the detection registry."""

    @property
    def backend_name(self) -> str:
        """Return the LMCache-specific ROCm backend selector."""
        return "rocm"

    @property
    def event_ipc_backend(self) -> "EventIPCBackend":
        """Return the ROCm event IPC backend.

        HIP interprocess event handles can be opened only once per process, so
        ROCm uses :class:`~lmcache.v1.platform.rocm.event_ipc.RocmEventIPCBackend`
        rather than the CUDA default. Isolated IPC keeps the CUDA selection.
        Like the CUDA spec, the choice is made on first read and cached.
        """
        backend = self._event_backend_cache
        if backend is not None:
            return backend
        if get_ipc_policy().isolated_ipc:
            return super().event_ipc_backend
        # First Party
        from lmcache.v1.platform.rocm.event_ipc import RocmEventIPCBackend

        backend = RocmEventIPCBackend(device_type=self.device_type)
        self._event_backend_cache = backend
        return backend

    def is_available(self) -> bool:
        """Check ROCm availability through PyTorch's ``torch.cuda`` API."""
        try:
            # Third Party
            import torch

            return (
                torch.cuda.is_available()
                and getattr(getattr(torch, "version", None), "hip", None) is not None
            )
        except Exception:
            return False
