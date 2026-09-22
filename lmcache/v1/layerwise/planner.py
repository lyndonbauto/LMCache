# SPDX-License-Identifier: Apache-2.0
"""Track C's side of the layerwise work: turning a request into a fetch plan.

This is the skeleton Track C fills in. Planning is the one piece with no
counterpart on the other side of a contract -- Track A consumes the plan and
Track B consumes the layer order derived from it, but neither produces
anything Track C has to wait for. That makes this the part that can be
finished first, and both other tracks are blocked on realistic plans, so it
should be.

The arithmetic already exists in C++, in
``csrc/storage_backends/aerospike/slot_planner.*`` and ``shard_plan.*``. What
is missing is the path from a vLLM request to a
:class:`~lmcache.v1.layerwise.contract.LayerFetchPlan`. Nothing in production
builds a plan today.
"""

# Standard
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

# Local
from .contract import LayerFetchPlan


@dataclass(frozen=True)
class PlanRequest:
    """What the planner needs to know to lay out one fetch.

    This is Track C's to extend. It starts deliberately small: the fields here
    are the ones no plan can be built without. Add to it as the real planner
    takes shape rather than passing extra context around the side, so that the
    inputs to a plan stay visible in one place.

    Attributes:
        layer_ids: Global layer indices the request needs, ascending.
        chunk_ids: KV chunk indices the request needs, ascending. Under a
            sliding window this is the participating subset, not every chunk
            of the prompt -- use the planner's window helper to derive it
            rather than computing it at the call site.
        node_index_by_chunk: Which cluster node holds each chunk, as an index
            into the fetch's node list.
        digest_by_chunk: The Aerospike record digest for each chunk.
        kv_planes: Number of independent planes per layer: two for separate
            key and value tensors, one for MLA. A layer occupies this many
            disjoint byte ranges per chunk, not one, which is the detail most
            easily got wrong -- the fetch still succeeds and the data is wrong.
        plane_bytes: Size in bytes of one layer's one plane within one chunk.
    """

    layer_ids: tuple[int, ...]
    chunk_ids: tuple[int, ...]
    node_index_by_chunk: Mapping[int, int]
    digest_by_chunk: Mapping[int, bytes]
    kv_planes: int
    plane_bytes: int


class FetchPlanner:
    """Builds the slot layout for one pipelined fetch.

    Every method raises :class:`NotImplementedError` today. Track A and
    Track B can already build :class:`LayerFetchPlan` objects by hand for
    tests, so this class is not on their critical path -- but realistic plans
    are, so it is worth finishing early.
    """

    def plan(self, request: PlanRequest) -> LayerFetchPlan:
        """Lay out every slot the fetch will request.

        Implementation notes for whoever fills this in:

        - A layer yields ``kv_planes`` disjoint ranges per chunk, not one.
        - Slot indices are request-scoped and unique across the whole
          request, not per node. The RDMA immediate encodes
          ``(generation << 16) | slot``, giving 65536 slots and 16 bits of
          generation. Per-node numbering passes a single-node test and
          corrupts a multi-node fetch.
        - Reject a request that would exceed the slot space rather than
          truncating it.

        Args:
            request: What the fetch needs to cover.

        Returns:
            A plan whose slots cover exactly the requested bytes, with no
            gaps and no overlaps.

        Raises:
            ValueError: If the request cannot be expressed as a valid plan,
                for instance because it needs more slots than the immediate
                can address.
        """
        raise NotImplementedError("Track C: lay out slots from the request")

    def participating_chunks(
        self,
        chunk_ids: Sequence[int],
        window_start_token: int,
        window_end_token: int,
        tokens_per_chunk: int,
    ) -> tuple[int, ...]:
        """Return the chunks a sliding window actually touches.

        This exists so call sites do not each re-derive it. Window arithmetic
        is easy to get subtly wrong -- particularly a window starting
        mid-chunk -- and a wrong answer here silently fetches the wrong
        tokens rather than failing.

        Args:
            chunk_ids: Candidate chunk indices, ascending.
            window_start_token: First token index in the window, inclusive.
            window_end_token: Last token index in the window, inclusive.
            tokens_per_chunk: Number of tokens each chunk covers.

        Returns:
            The subset of ``chunk_ids`` overlapping the window, ascending.

        Raises:
            ValueError: If ``tokens_per_chunk`` is not positive or the window
                bounds are inverted.
        """
        raise NotImplementedError("Track C: derive the participating chunks")
