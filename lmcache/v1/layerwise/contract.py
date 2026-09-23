# SPDX-License-Identifier: Apache-2.0
"""The frozen interfaces between the three layerwise workstreams.

Layerwise loading spans two subsystems that run on different hardware and are
developed by different people. This module is the agreed boundary between
them. Both sides code against the protocols here, so neither has to wait for
the other to exist, and either can be swapped for a fake in tests.

The data flows in one direction::

    Aerospike cluster
        | RDMA writes, one immediate per slot
        v
    LayerArrivalSource      "layer N has fully landed in host memory"
        | polled by
        v
    LayerArrivalPump        the junction; the only two-sided component
        | calls
        v
    LayerLoadSink           "copy layer N from host memory to the GPU"
        | unblocks
        v
    vLLM attention for layer N

Two decisions here are deliberate and should not be relaxed without agreement
across all three workstreams, because code on both sides is written to rely on
them:

Arrival is three-valued, not a boolean.
    A boolean cannot distinguish "not yet, keep waiting" from "never, give
    up". The loader's response to those is opposite -- stall versus fall back
    to a whole-request load -- so collapsing them strands the loader with no
    way to choose. See :class:`LayerArrivalStatus`.

Generations are explicit and non-zero.
    Every call that refers to an in-flight fetch carries the generation it
    believes is active. A late reply from an abandoned fetch is then rejected
    rather than silently credited to its successor, which would surface as a
    layer appearing ready before its data arrived. ``0`` is reserved to mean
    "no fetch", so a defaulted or zero-initialised generation fails loudly
    instead of aliasing a real one.
"""

# Standard
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable
import types

#: Reserved generation meaning "there is no active fetch". Never returned by
#: :meth:`LayerArrivalSource.begin_fetch` for a fetch that actually started.
NO_GENERATION = 0


class LayerwiseContractError(Exception):
    """Base class for violations of the layerwise contract."""


class LayerNotInPlanError(LayerwiseContractError):
    """A layer was referenced that the active plan does not cover."""


class StaleGenerationError(LayerwiseContractError):
    """A call named a generation that is not the active one.

    Raised rather than ignored: a caller working from a stale generation has
    lost track of which fetch it is driving, and continuing would attribute
    one fetch's arrivals to another.
    """


class LayerUnservableError(LayerwiseContractError):
    """A layer of the active fetch will never arrive.

    The fetch is finished as far as this layer is concerned. The caller must
    abandon the layerwise path and fall back to a whole-request load.
    """


class LayerArrivalTimeoutError(LayerwiseContractError):
    """A layer did not arrive within the caller's deadline."""


class LayerArrivalStatus(Enum):
    """Whether one layer of an in-flight fetch has landed in host memory.

    ``PENDING`` and ``UNSERVABLE`` are both "not ready", but they call for
    opposite responses, so they are distinct values. Treating ``UNSERVABLE``
    as "keep polling" produces a hang rather than an error, which is the
    failure this enum exists to prevent.
    """

    #: Some slot of this layer has not landed yet. Poll again.
    PENDING = "pending"
    #: Every slot of this layer landed. Safe to read the host buffer.
    RESIDENT = "resident"
    #: A slot of this layer was declined or lost. It will never arrive.
    UNSERVABLE = "unservable"


@dataclass(frozen=True)
class SlotPlacement:
    """One RDMA write: a contiguous byte range of one layer of one chunk.

    A slot is the unit the transport accounts for. A layer is resident once
    every slot carrying part of it has landed, which is why the plan is
    expressed as slots rather than as layers.

    One slot is exactly one stored record, and ``(chunk_id, layer_id, plane,
    piece)`` is that record's identity -- the same four fields the native
    fetch path keys digests by. There is deliberately no object group in that
    key: a layer belongs to exactly one object group, so naming the layer
    already names the group.

    Attributes:
        layer_id: Global layer index in the model.
        chunk_id: Index of the KV chunk this range belongs to.
        node_index: Index into :attr:`LayerFetchPlan.node_names` identifying
            which cluster node holds this slot. Slot indices are
            request-scoped, so two slots on different nodes still have
            distinct indices.
        digest: Record digest the transport asks the node for.
        plane: Which K/V plane of the layer this range belongs to. Zero for a
            format that stores a layer as a single plane.
        piece: Which record of that plane this range is, counting from zero
            in ascending offset order.
        offset: Byte offset of this range within the destination host buffer.
        length: Length of this range in bytes.

    Raises:
        ValueError: If the digest is empty, or ``plane`` or ``piece`` is
            negative, since none of those name a record that exists.
    """

    layer_id: int
    chunk_id: int
    node_index: int
    digest: bytes
    plane: int
    piece: int
    offset: int
    length: int

    def __post_init__(self) -> None:
        if not self.digest:
            raise ValueError(
                f"slot for layer {self.layer_id} of chunk {self.chunk_id} has "
                "an empty digest, which names no record"
            )
        if self.plane < 0 or self.piece < 0:
            raise ValueError(
                f"slot for layer {self.layer_id} of chunk {self.chunk_id} has "
                f"a negative plane/piece ({self.plane}, {self.piece})"
            )


@dataclass(frozen=True)
class LayerFetchPlan:
    """The complete set of RDMA writes one pipelined fetch expects.

    This replaces the three parallel untyped lists the earlier prototype
    passed around. Holding the slots in one object lets the transport check
    its own arrival accounting against :meth:`slots_for_layer` instead of
    trusting a separately supplied count, and lets the loader learn the layer
    order without reaching into transport internals.

    Attributes:
        slots: Every slot the fetch will request. **Order is significant**: a
            slot's index -- the value the RDMA immediate carries in its low 16
            bits -- is its position in this tuple. That numbering therefore
            spans the whole request rather than restarting per node, which is
            what keeps two nodes' notifications distinguishable when they land
            on one queue pair. Producers must be deterministic; consumers must
            not reorder. Layer order is given separately by :meth:`layer_ids`,
            so nothing needs to infer it from this order.
        node_names: The cluster nodes this fetch talks to, in the order
            :attr:`SlotPlacement.node_index` numbers them. Carried on the
            plan so the transport can resolve a slot to a node without
            holding the planner's request, which is a Track C type it should
            not depend on.

    Raises:
        ValueError: If ``slots`` is empty, if any slot has a non-positive
            length, if ``node_names`` is empty or repeats a name, or if a
            slot names a node outside ``node_names`` -- the last of which
            would otherwise surface as a fetch addressed to the wrong node.
    """

    slots: tuple[SlotPlacement, ...]
    node_names: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.slots:
            raise ValueError("a fetch plan must contain at least one slot")
        if not self.node_names:
            raise ValueError("a fetch plan must name at least one node")
        if len(set(self.node_names)) != len(self.node_names):
            raise ValueError(
                f"fetch plan repeats a node name: {self.node_names}, so a "
                "slot's node index would be ambiguous"
            )
        for slot in self.slots:
            if slot.length <= 0:
                raise ValueError(
                    f"slot for layer {slot.layer_id} has non-positive "
                    f"length {slot.length}"
                )
            if not 0 <= slot.node_index < len(self.node_names):
                raise ValueError(
                    f"slot for layer {slot.layer_id} names node index "
                    f"{slot.node_index}, but the plan has "
                    f"{len(self.node_names)} nodes"
                )

    def node_name_for(self, slot: SlotPlacement) -> str:
        """Return the cluster node holding ``slot``.

        Args:
            slot: A slot of this plan.

        Returns:
            The node name :attr:`SlotPlacement.node_index` refers to.

        Raises:
            IndexError: If ``slot`` names a node this plan does not have,
                which means it came from a different plan.
        """
        if not 0 <= slot.node_index < len(self.node_names):
            raise IndexError(
                f"slot node index {slot.node_index} is not in this plan's "
                f"{len(self.node_names)} nodes; the slot is from another plan"
            )
        return self.node_names[slot.node_index]

    def layer_ids(self) -> tuple[int, ...]:
        """Return the layers this fetch covers, in ascending layer order.

        Ascending order is the order the loader must issue copies in, because
        transfers share a stream and attention consumes layers in that order.

        Returns:
            Each covered global layer index exactly once, ascending.
        """
        return tuple(sorted({slot.layer_id for slot in self.slots}))

    def slots_for_layer(self, layer_id: int) -> int:
        """Return how many slots must land before ``layer_id`` is resident.

        Args:
            layer_id: Global layer index in the model.

        Returns:
            The number of slots carrying part of ``layer_id``.

        Raises:
            LayerNotInPlanError: If no slot in the plan carries ``layer_id``.
        """
        count = sum(1 for slot in self.slots if slot.layer_id == layer_id)
        if count == 0:
            raise LayerNotInPlanError(f"layer {layer_id} is not in this fetch plan")
        return count

    def slot_counts(self) -> Mapping[int, int]:
        """Return the slot count for every covered layer.

        Returns:
            A mapping from global layer index to number of slots.
        """
        counts: dict[int, int] = {}
        for slot in self.slots:
            counts[slot.layer_id] = counts.get(slot.layer_id, 0) + 1
        return types.MappingProxyType(counts)


@runtime_checkable
class LayerArrivalSource(Protocol):
    """Reports which layers of an in-flight fetch have landed in host memory.

    Implemented by the transport. Consumed by
    :class:`~lmcache.v1.layerwise.pump.LayerArrivalPump`.

    An implementation owns at most one active fetch at a time. It may be
    polled from a different thread than the one that began the fetch, so
    implementations must be safe for concurrent :meth:`poll_layer` calls.
    """

    def begin_fetch(self, plan: LayerFetchPlan) -> int:
        """Start fetching every slot in ``plan``.

        Args:
            plan: The slots to fetch.

        Returns:
            A generation identifying this fetch, never :data:`NO_GENERATION`.
            Every later call about this fetch must quote it.

        Raises:
            LayerwiseContractError: If a fetch is already active, or the
                backend cannot serve a pipelined fetch at all. Callers that
                can fall back should catch this; returning a sentinel instead
                would let an unsupported backend be mistaken for a slow one.
        """
        ...

    def poll_layer(self, layer_id: int, generation: int) -> LayerArrivalStatus:
        """Report whether one layer has fully landed.

        This is a non-blocking query and is expected to be called repeatedly.

        Args:
            layer_id: Global layer index in the model.
            generation: The generation returned by :meth:`begin_fetch`.

        Returns:
            The arrival status of ``layer_id``.

        Raises:
            StaleGenerationError: If ``generation`` is not the active fetch.
            LayerNotInPlanError: If the active plan does not cover ``layer_id``.
        """
        ...

    def finish_fetch(self, generation: int) -> None:
        """Release a fetch whose layers have all been consumed.

        Args:
            generation: The generation returned by :meth:`begin_fetch`.

        Raises:
            StaleGenerationError: If ``generation`` is not the active fetch.
        """
        ...

    def abandon_fetch(self, generation: int) -> None:
        """Release a fetch without waiting for its outstanding slots.

        Must be safe to call with a generation that has already been finished
        or abandoned, so that error paths can unwind without first working out
        how far the fetch got.

        Args:
            generation: The generation returned by :meth:`begin_fetch`.
        """
        ...


@runtime_checkable
class LayerLoadSink(Protocol):
    """Copies layers from host memory to the GPU as they become available.

    Implemented by the multiprocess loader. Driven by
    :class:`~lmcache.v1.layerwise.pump.LayerArrivalPump`.

    Calls are made from a single thread and are strictly ordered:
    :meth:`begin_load`, then :meth:`load_layer` once per layer in the order
    given to :meth:`begin_load`, then :meth:`finish_load` or
    :meth:`abandon_load`.
    """

    def begin_load(self, generation: int, layer_ids: Sequence[int]) -> None:
        """Prepare to load ``layer_ids`` for one fetch.

        Args:
            generation: The fetch generation these layers belong to.
            layer_ids: Global layer indices in the order they will be loaded.

        Raises:
            LayerwiseContractError: If a load is already in progress.
        """
        ...

    def load_layer(self, layer_id: int) -> None:
        """Copy one layer from host memory to the GPU.

        Must be called in the order given to :meth:`begin_load`. Copies share
        a stream, so a consumer waiting on layer N implicitly waits on every
        layer issued before it; issuing out of order therefore makes an
        earlier layer appear ready before its copy was queued.

        Args:
            layer_id: Global layer index in the model.

        Raises:
            LayerNotInPlanError: If ``layer_id`` was not passed to
                :meth:`begin_load`.
            LayerwiseContractError: If called out of order, or before
                :meth:`begin_load`.
        """
        ...

    def finish_load(self, generation: int) -> None:
        """Complete a load whose layers were all issued.

        Args:
            generation: The generation passed to :meth:`begin_load`.

        Raises:
            StaleGenerationError: If ``generation`` is not the active load.
            LayerwiseContractError: If some layer was never issued, since a
                consumer would then wait forever on a layer nobody copied.
        """
        ...

    def abandon_load(self, generation: int) -> None:
        """Give up on a load, releasing anything waiting on its layers.

        Implementations must wake every waiter with a failure rather than
        leaving them blocked; abandoning is the path taken when the transport
        reports a layer unservable, and a silent abandon converts a
        recoverable miss into a hang.

        Must be safe to call when no load is active.

        Args:
            generation: The generation passed to :meth:`begin_load`.
        """
        ...
