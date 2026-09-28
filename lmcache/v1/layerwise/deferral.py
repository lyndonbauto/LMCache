# SPDX-License-Identifier: Apache-2.0
"""Deciding, at lookup, which requests take the pipelined retrieve.

A request takes the pipelined path only if the lookup defers its L2 hits: the
prefetch controller then reports them as found without loading them, and the
retrieve fetches them layer by layer. This module holds what that decision
needs -- the daemon's settings, what a registered model contributes, and the
:class:`~lmcache.v1.distributed.api.L2Deferral` itself. See
``docs/design/v1/layerwise/c9-wiring.md``, "Lookup: deciding to defer".

Kept out of the package ``__init__`` for the same import-cycle reason as
:mod:`~lmcache.v1.layerwise.request_fetch`.
"""

# Standard
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

# First Party
from lmcache.logging import init_logger
from lmcache.v1.distributed.api import ObjectKey
from lmcache.v1.layerwise.pump import DEFAULT_LAYER_TIMEOUT_SECONDS
from lmcache.v1.layerwise.request_fetch import ChunkPlacer, FetchModel

logger = init_logger(__name__)


class SharedKeyPolicy(Enum):
    """What a retrieve does with a deferred key another request is fetching."""

    #: Fail the retrieve; vLLM recomputes the request.
    RECOMPUTE = "recompute"
    #: Wait a bounded time for the other fetch, then reuse or fetch the key.
    WAIT = "wait"


@dataclass(frozen=True)
class PipelinedFetchConfig:
    """The daemon's settings for the pipelined retrieve.

    Attributes:
        enabled: Whether lookups may defer L2 hits to a pipelined retrieve.
        max_chunks: Most chunks one request may defer. Registration checks
            that one RDMA window holds this many chunks of the model.
        shared_keys: See :class:`SharedKeyPolicy`.
        shared_wait_seconds: How long :attr:`SharedKeyPolicy.WAIT` waits.

    Raises:
        ValueError: If ``max_chunks`` is not positive, or
            ``shared_wait_seconds`` is negative or not below the pump's
            per-layer timeout. The worker's wait for the first layer covers
            both, and it is twice the pump's timeout, so a longer wait would
            let the worker give up first.
    """

    enabled: bool = False
    max_chunks: int = 64
    shared_keys: SharedKeyPolicy = SharedKeyPolicy.RECOMPUTE
    shared_wait_seconds: float = 1.0

    def __post_init__(self) -> None:
        if self.max_chunks <= 0:
            raise ValueError(
                f"pipelined max_chunks must be positive, got {self.max_chunks}"
            )
        if not 0 <= self.shared_wait_seconds < DEFAULT_LAYER_TIMEOUT_SECONDS:
            raise ValueError(
                "pipelined shared_wait_seconds must be in "
                f"[0, {DEFAULT_LAYER_TIMEOUT_SECONDS}), got "
                f"{self.shared_wait_seconds}"
            )


@dataclass(frozen=True)
class PipelinedModel:
    """What one registered model needs for the pipelined retrieve.

    Built at ``register_kv_cache`` when the pipelined path is enabled and
    ready for the model.

    Attributes:
        fetch_model: The model's layout and attention windows.
        placer: Leases windows and places this model's objects in them.
        max_record_bytes: The record cap the objects were written under.
        max_slots: Most slots one fetch may carry.
        adapter_id: The L2 adapter with the pipelined path.
        max_chunks: Most chunks one request may defer.
    """

    fetch_model: FetchModel
    placer: ChunkPlacer
    max_record_bytes: int
    max_slots: int
    adapter_id: int
    max_chunks: int


class PipelinedDeferral:
    """Defers a lookup's L2 hits when one pipelined fetch can serve them all.

    Implements :class:`~lmcache.v1.distributed.api.L2Deferral`. Accepts only
    if every key comes from the pipelined adapter, the keys span at most
    ``max_chunks`` chunks, and fetching them needs at most ``max_slots``
    slots (one slot per stored record).
    """

    def __init__(self, model: PipelinedModel) -> None:
        """Create the deferral for one model's lookups.

        Args:
            model: The registered model the lookup is for.
        """
        self._model = model

    def accepts(self, adapter_ids: Sequence[int], keys: Sequence[ObjectKey]) -> bool:
        """Report whether one pipelined fetch can serve ``keys``.

        Args:
            adapter_ids: The L2 adapters the load would read from.
            keys: Every key the load would read.

        Returns:
            ``True`` if the retrieve should fetch them layer by layer.
        """
        model = self._model
        if list(adapter_ids) != [model.adapter_id] or not keys:
            return False
        if len({key.chunk_hash for key in keys}) > model.max_chunks:
            return False
        layout = model.fetch_model.layout
        try:
            slots = sum(
                layout.record_count(key.object_group_id, model.max_record_bytes)
                for key in keys
            )
        except (KeyError, ValueError):
            logger.warning(
                "Cannot count the records of a lookup's keys; loading them whole",
                exc_info=True,
            )
            return False
        return slots <= model.max_slots
