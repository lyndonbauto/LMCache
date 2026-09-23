# SPDX-License-Identifier: Apache-2.0
"""Frozen contracts for layer-at-a-time KV cache loading.

This package holds the two interfaces that let the remote-fetch side and the
GPU-load side of layerwise loading be built and tested independently:

- :class:`~lmcache.v1.layerwise.contract.LayerArrivalSource` -- "layer N of
  this fetch has landed in host memory". Implemented by the transport.
- :class:`~lmcache.v1.layerwise.contract.LayerLoadSink` -- "copy layer N to the
  GPU now". Implemented by the multiprocess loader.

:class:`~lmcache.v1.layerwise.pump.LayerArrivalPump` is the only component that
depends on both. Everything else depends on one side and substitutes a fake for
the other, which is what makes the two halves separately testable: the
transport needs no GPU and the loader needs no RDMA fabric.
"""

# Local
from .contract import (
    LayerArrivalSource,
    LayerArrivalStatus,
    LayerArrivalTimeoutError,
    LayerFetchPlan,
    LayerLoadSink,
    LayerNotInPlanError,
    LayerUnservableError,
    LayerwiseContractError,
    SlotPlacement,
    StaleGenerationError,
)
from .fakes import (
    RecordingLayerLoadSink,
    ScriptedLayerArrivalSource,
    UnservableLayerArrivalSource,
)
from .planner import (
    MAX_SLOTS_PER_REQUEST,
    ByteRange,
    ChunkPlacement,
    FetchPlanner,
    KernelGroupGeometry,
    ModelLayout,
    PlanRequest,
    RecordKeyDigests,
    SlotDigestSource,
    plane_segment_bytes,
)
from .pump import LayerArrivalPump

__all__ = [
    "MAX_SLOTS_PER_REQUEST",
    "ByteRange",
    "ChunkPlacement",
    "FetchPlanner",
    "KernelGroupGeometry",
    "LayerArrivalPump",
    "LayerArrivalSource",
    "LayerArrivalStatus",
    "LayerArrivalTimeoutError",
    "LayerFetchPlan",
    "LayerLoadSink",
    "LayerNotInPlanError",
    "LayerUnservableError",
    "LayerwiseContractError",
    "ModelLayout",
    "PlanRequest",
    "RecordKeyDigests",
    "RecordingLayerLoadSink",
    "ScriptedLayerArrivalSource",
    "SlotDigestSource",
    "SlotPlacement",
    "StaleGenerationError",
    "UnservableLayerArrivalSource",
    "plane_segment_bytes",
]
