# SPDX-License-Identifier: Apache-2.0
"""The order per-layer transfers are launched in, and what a layer waits on.

Multiprocess mode loads a whole object group per launch: the daemon calls
``transfer_kv_per_object_group`` once per object group, which dispatches
``multi_layer_block_kv_transfer`` over every layer in the group at once. The
worker then blocks on a single completion event before vLLM runs the request
at all, so no attention layer ever overlaps the load.

Layer-by-layer loading replaces that single wait with one per layer, and this
module owns the ordering question that then becomes load-bearing.

Why the order matters
---------------------

Transfers are enqueued on one CUDA stream, so they complete in the order they
were launched. A worker waiting for layer *L* therefore implicitly waits for
everything launched before it -- waiting is a watermark whether or not it is
written as one.

Today's launch order is object-group-major, and for a hybrid model that is not
model-layer order. Grouping is by transfer identity, so a Mamba/GDN hybrid
splits into an attention group holding layers ``[0, 2, 4, ...]`` and a
recurrent group holding ``[1, 3, 5, ...]``. Launching group by group means
every attention layer is enqueued before any recurrent layer, so vLLM asking
for layer 1 -- the second layer it computes -- would wait behind all sixteen
attention layers. The result is correct and pipelines nothing: the first wait
absorbs almost the entire transfer.

So launches are ordered by **global layer index**, interleaving across kernel
groups, which makes stream order match the order vLLM consumes layers in. A
plain watermark is then both correct and tight. This is the same reason the
RDMA fetch schedule is layer-major rather than chunk-major.

Note this module decides order and nothing else. It holds no CUDA state and
performs no transfer, so it is testable without a device -- which matters,
because the failure it prevents is a silent loss of overlap rather than an
error.
"""

# Standard
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # First Party
    from lmcache.v1.kv_layer_groups import KernelGroupInfo


@dataclass(frozen=True)
class LayerLaunch:
    """One per-layer transfer: which layer, and where its bytes sit.

    A layer belongs to exactly one kernel group, so these three fields
    identify it unambiguously.
    """

    layer_id: int
    """Global layer index, as vLLM registration order assigns it."""

    kernel_group_index: int
    """Index of the kernel group holding this layer, into the sequence the
    schedule was built from."""

    position_in_group: int
    """This layer's position along its kernel group's layer dimension, i.e.
    its index within that group's ``layer_indices``. This is the stride index
    the transfer needs; it is not the global layer index, and for a hybrid
    model the two differ for every layer but the first."""


class LayerwiseSchedule:
    """The launch order for one request's per-layer transfers.

    Built once from the registered layout and then read, so it is immutable
    after construction and safe to share between threads.
    """

    def __init__(self, kernel_group_layers: Sequence[Sequence[int]]) -> None:
        """Build a schedule over the layers of every kernel group.

        Args:
            kernel_group_layers: One entry per kernel group, holding that
                group's global layer indices in the order the group's kernel
                iterates them -- that is, ``KernelGroupInfo.layer_indices``.
                Position within an entry is the layer's stride index, so the
                order within an entry is significant and is preserved. A group
                with no layers contributes no launches and is skipped.

        Raises:
            ValueError: If no group holds any layer, or if a global layer index
                appears in more than one group. A layer belongs to exactly one
                kernel group, so a duplicate would make its stride index
                ambiguous.
        """
        launches: list[LayerLaunch] = []
        for group_index, layer_indices in enumerate(kernel_group_layers):
            for position, layer_id in enumerate(layer_indices):
                launches.append(LayerLaunch(layer_id, group_index, position))

        seen: set[int] = set()
        for launch in launches:
            if launch.layer_id in seen:
                raise ValueError(
                    f"layer {launch.layer_id} appears in more than one kernel "
                    "group, so its position along a layer dimension would be "
                    "ambiguous"
                )
            seen.add(launch.layer_id)

        if not launches:
            raise ValueError(
                "no kernel group holds any layer, so there is nothing to schedule"
            )

        # Global layer order, not group order. See the module docstring: this
        # is what makes a watermark wait tight rather than merely correct.
        launches.sort(key=lambda launch: launch.layer_id)
        self._launches: tuple[LayerLaunch, ...] = tuple(launches)
        self._ordinals: dict[int, int] = {
            launch.layer_id: ordinal for ordinal, launch in enumerate(self._launches)
        }

    @classmethod
    def from_kernel_groups(
        cls, kernel_groups: Sequence["KernelGroupInfo"]
    ) -> "LayerwiseSchedule":
        """Build a schedule from the registered kernel groups.

        Args:
            kernel_groups: The kernel groups of the model, in the order the
                transfer path holds them.

        Returns:
            The schedule over every layer those groups hold.

        Raises:
            ValueError: As for the constructor.
        """
        return cls([group.layer_indices for group in kernel_groups])

    @property
    def launches(self) -> tuple[LayerLaunch, ...]:
        """Every per-layer transfer, in the order to launch them.

        Ascending by global layer index. The daemon should enqueue them in
        exactly this order, because the worker's waits assume it.
        """
        return self._launches

    def __contains__(self, layer_id: int) -> bool:
        """Report whether `layer_id` is one of the scheduled layers.

        Args:
            layer_id: Global layer index.

        Returns:
            True if the schedule covers that layer. A caller asking about a
            layer the layout does not hold -- one excluded from transfer --
            gets False rather than an exception, so it can skip waiting.
        """
        return layer_id in self._ordinals

    def wait_ordinal(self, layer_id: int) -> int:
        """Return how many launches must complete before `layer_id` has landed.

        The value counts the layer itself, so it is the number of launches to
        have finished rather than an index: layer 0 of a full layout returns 1.
        Because launches share one stream and are enqueued in this order,
        waiting for this many completions is exactly waiting for this layer.

        Args:
            layer_id: Global layer index.

        Returns:
            The count of launches up to and including this layer.

        Raises:
            KeyError: If the schedule does not cover `layer_id`. Use ``in`` to
                check first.
        """
        return self._ordinals[layer_id] + 1

    def launch_for(self, layer_id: int) -> LayerLaunch:
        """Return the launch that carries `layer_id`.

        Args:
            layer_id: Global layer index.

        Returns:
            That layer's kernel group and position within it.

        Raises:
            KeyError: If the schedule does not cover `layer_id`.
        """
        return self._launches[self._ordinals[layer_id]]

    def launch_count(self) -> int:
        """Return the number of per-layer launches in the schedule.

        This is how many completions a full load reports, which is what sizes
        any per-layer progress bookkeeping.
        """
        return len(self._launches)


def assert_registration_schedules_agree(
    engine_group_layers: Sequence[Sequence[int]],
    kernel_groups: Sequence["KernelGroupInfo"],
) -> None:
    """Verify worker engine groups and daemon kernel groups yield one schedule.

    Ordinals are ranks in globally sorted layer order, so matching layer sets
    with consistent per-group layer order must produce identical schedules.

    Args:
        engine_group_layers: Layer indices per engine group from registration.
        kernel_groups: Kernel groups from the daemon cache context.

    Raises:
        ValueError: If the two construction paths diverge.
    """
    from_engine = LayerwiseSchedule(engine_group_layers)
    from_kernels = LayerwiseSchedule.from_kernel_groups(kernel_groups)
    if from_engine.launch_count() != from_kernels.launch_count():
        raise ValueError(
            "engine_group_infos and registered kernel groups disagree on "
            f"launch count ({from_engine.launch_count()} vs "
            f"{from_kernels.launch_count()})"
        )
    for left, right in zip(from_engine.launches, from_kernels.launches, strict=True):
        if left != right:
            raise ValueError(
                "engine_group_infos and registered kernel groups yield "
                f"different schedules at layer {left.layer_id}: "
                f"{left} vs {right}"
            )
