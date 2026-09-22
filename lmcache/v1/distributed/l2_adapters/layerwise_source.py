# SPDX-License-Identifier: Apache-2.0
"""Track A's side of the layerwise contract: arrival reporting over RDMA.

This is the skeleton the Aerospike transport fills in. The shape is fixed --
it is one half of the frozen contract in
:mod:`lmcache.v1.layerwise.contract` -- so that Track C can write the pump
and Track B can write the loader before any of this works.

Every method raises :class:`NotImplementedError` today. Replace them with real
behaviour; do not change the signatures without agreement, because two other
people are building against them.

The transport side of the work already exists in
``csrc/storage_backends/aerospike/`` -- ``PipelinedFetchSession`` issues the
fetch and ``LayerReadiness`` tracks which slots have landed. What is missing
is the adapter that presents that as a
:class:`~lmcache.v1.layerwise.contract.LayerArrivalSource`, which is this
file.
"""

# First Party
from lmcache.v1.layerwise.contract import (
    LayerArrivalStatus,
    LayerFetchPlan,
)


class AerospikeLayerArrivalSource:
    """Reports layer arrival for a pipelined Aerospike RDMA fetch.

    Implements :class:`~lmcache.v1.layerwise.contract.LayerArrivalSource`. See
    that protocol for the full contract; the notes here are the parts specific
    to this transport.

    A layer is resident once every slot carrying part of it has landed. The
    server writes each slot with ``RDMA_WRITE_WITH_IMM`` and the immediate
    encodes ``(generation << 16) | slot``, so arrivals are counted per slot
    and attributed to a generation.

    Instances hold at most one active fetch. :meth:`poll_layer` may be called
    from a different thread than :meth:`begin_fetch`, so the arrival counters
    must be safe for concurrent reads.
    """

    def begin_fetch(self, plan: LayerFetchPlan) -> int:
        """Issue every slot in ``plan`` to the nodes that hold it.

        Implementation notes for whoever fills this in:

        - Derive the receive depth from ``plan``, since the number of
          immediates the server will send is the number of slots requested,
          and clamp it to the device's ``max_qp_wr``. Reject a plan that
          cannot fit rather than letting it deadlock at runtime: on RC with
          ``rnr_retry = 7`` a shortfall is infinite retry, which wedges the
          region instead of raising.
        - Split the request into commands no larger than each node's
          advertised ``max_sinks`` (from the ``kv-sink-register`` reply,
          default 256 when absent). The server hard-refuses an oversized
          command and fails the whole fetch.

        Args:
            plan: The slots to fetch.

        Returns:
            A non-zero generation for this fetch.

        Raises:
            LayerwiseContractError: If a fetch is already active, if the
                backend has no pipelined path, or if the plan exceeds what the
                device can accept.
        """
        raise NotImplementedError(
            "Track A: issue the pipelined fetch and return its generation"
        )

    def poll_layer(self, layer_id: int, generation: int) -> LayerArrivalStatus:
        """Report whether every slot of ``layer_id`` has landed.

        Must return ``RESIDENT`` only when the layer is wholly present, and
        promptly once it is: early is silently wrong output, late costs
        exactly the overlap layerwise loading exists to buy.

        A write belonging to an abandoned generation can land after its
        successor has begun. It must not move this fetch's accounting.

        Args:
            layer_id: Global layer index in the model.
            generation: The generation returned by :meth:`begin_fetch`.

        Returns:
            The arrival status of ``layer_id``.

        Raises:
            StaleGenerationError: If ``generation`` is not the active fetch.
            LayerNotInPlanError: If the active plan does not cover ``layer_id``.
        """
        raise NotImplementedError(
            "Track A: report per-layer arrival from the slot accounting"
        )

    def finish_fetch(self, generation: int) -> None:
        """Release a fetch whose layers have all been consumed.

        Args:
            generation: The generation returned by :meth:`begin_fetch`.

        Raises:
            StaleGenerationError: If ``generation`` is not the active fetch.
        """
        raise NotImplementedError("Track A: release the completed fetch")

    def abandon_fetch(self, generation: int) -> None:
        """Release a fetch without waiting for its outstanding slots.

        Must tolerate a generation that has already been finished or
        abandoned, so error paths can unwind without first working out how far
        the fetch got.

        Args:
            generation: The generation returned by :meth:`begin_fetch`.
        """
        raise NotImplementedError("Track A: tear down the in-flight fetch")
