# SPDX-License-Identifier: Apache-2.0
"""Tests for the per-layer launch order in multiprocess mode.

These cover the public contract of :mod:`lmcache.v1.multiprocess.
layerwise_schedule` and need no CUDA device, which is the point: the failure
the schedule prevents is a silent loss of pipelining rather than an error, so
it has to be caught by assertion rather than by observation.
"""

# Third Party
import pytest

# First Party
from lmcache.v1.multiprocess.layerwise_schedule import LayerwiseSchedule


class TestLaunchOrder:
    """The schedule is ordered by global layer index, not by kernel group."""

    def test_a_uniform_model_launches_in_layer_order(self) -> None:
        """One kernel group holding every layer launches them in order."""
        schedule = LayerwiseSchedule([[0, 1, 2, 3]])

        assert [launch.layer_id for launch in schedule.launches] == [0, 1, 2, 3]
        assert schedule.launch_count() == 4

    def test_a_hybrid_model_interleaves_its_groups(self) -> None:
        """Interleaved groups are launched interleaved, not group by group.

        A Mamba/GDN hybrid splits by transfer identity into an attention group
        and a recurrent group whose layer indices alternate. Launching group
        by group would enqueue every attention layer before any recurrent one,
        so vLLM's second attention call -- for layer 1 -- would wait behind
        every layer of the first group. Correct, and pipelining nothing.
        """
        schedule = LayerwiseSchedule([[0, 2, 4, 6], [1, 3, 5, 7]])

        assert [launch.layer_id for launch in schedule.launches] == [
            0,
            1,
            2,
            3,
            4,
            5,
            6,
            7,
        ]
        # The group each successive launch belongs to alternates, which is the
        # observable difference from a group-major order.
        assert [launch.kernel_group_index for launch in schedule.launches] == [
            0,
            1,
            0,
            1,
            0,
            1,
            0,
            1,
        ]

    def test_waiting_for_an_early_layer_does_not_wait_for_a_later_one(self) -> None:
        """Ordinals rise with the layer index, across group boundaries.

        This is the property that makes a watermark wait tight. Under a
        group-major order layer 1 would sit after every layer of group 0, so
        its ordinal would exceed layer 2's.
        """
        schedule = LayerwiseSchedule([[0, 2, 4, 6], [1, 3, 5, 7]])

        ordinals = [schedule.wait_ordinal(layer) for layer in range(8)]
        assert ordinals == sorted(ordinals)
        assert schedule.wait_ordinal(1) < schedule.wait_ordinal(2)

    def test_group_order_does_not_change_the_schedule(self) -> None:
        """The same layers give the same order however the groups are passed.

        Only ``position_in_group`` and ``kernel_group_index`` track which
        group a layer came from; the launch order itself is a property of the
        model, so it must not depend on how the transfer path happens to hold
        its groups.
        """
        one_way = LayerwiseSchedule([[0, 2], [1, 3]])
        other_way = LayerwiseSchedule([[1, 3], [0, 2]])

        assert [launch.layer_id for launch in one_way.launches] == [
            launch.layer_id for launch in other_way.launches
        ]


class TestPositionWithinAGroup:
    """A launch carries the stride index its transfer needs."""

    def test_position_is_the_index_within_its_own_group(self) -> None:
        """Position is the offset along the group's layer dimension.

        For a hybrid this differs from the global layer index for every layer
        but the first, and using one for the other would read another layer's
        bytes -- plausible-looking KV, no error.
        """
        schedule = LayerwiseSchedule([[0, 2, 4, 6], [1, 3, 5, 7]])

        attention = schedule.launch_for(4)
        assert attention.kernel_group_index == 0
        assert attention.position_in_group == 2

        recurrent = schedule.launch_for(5)
        assert recurrent.kernel_group_index == 1
        assert recurrent.position_in_group == 2

    def test_position_follows_the_order_the_group_declares(self) -> None:
        """Order within a group is significant and is preserved.

        ``layer_indices`` is documented as the order the kernel iterates, so
        position must come from that order rather than from sorting.
        """
        schedule = LayerwiseSchedule([[5, 1, 9]])

        assert schedule.launch_for(5).position_in_group == 0
        assert schedule.launch_for(1).position_in_group == 1
        assert schedule.launch_for(9).position_in_group == 2
        # The launch order is still by layer, independent of that.
        assert [launch.layer_id for launch in schedule.launches] == [1, 5, 9]


class TestOrdinals:
    """Ordinals count completed launches, so they can be compared to progress."""

    def test_an_ordinal_counts_the_layer_itself(self) -> None:
        """The first layer needs one launch to have completed, not zero."""
        schedule = LayerwiseSchedule([[0, 1, 2]])

        assert schedule.wait_ordinal(0) == 1
        assert schedule.wait_ordinal(2) == 3
        assert schedule.wait_ordinal(2) == schedule.launch_count()

    def test_a_layer_outside_the_layout_is_reported_absent(self) -> None:
        """An unscheduled layer is absent rather than an error to ask about.

        Layers excluded from transfer are never loaded, so a caller must be
        able to check and skip waiting without handling an exception.
        """
        schedule = LayerwiseSchedule([[0, 1]])

        assert 0 in schedule
        assert 7 not in schedule
        with pytest.raises(KeyError):
            schedule.wait_ordinal(7)
        with pytest.raises(KeyError):
            schedule.launch_for(7)


class TestRejectedLayouts:
    """Ambiguous layouts are refused rather than resolved arbitrarily."""

    def test_a_layer_in_two_groups_is_refused(self) -> None:
        """A layer belongs to exactly one kernel group.

        A duplicate would give the layer two stride indices, and the schedule
        would pick one silently.
        """
        with pytest.raises(ValueError, match="more than one kernel group"):
            LayerwiseSchedule([[0, 1], [1, 2]])

    def test_a_layout_with_no_layers_is_refused(self) -> None:
        """There is nothing to schedule, so say so rather than return empty."""
        with pytest.raises(ValueError, match="nothing to"):
            LayerwiseSchedule([])
        with pytest.raises(ValueError, match="nothing to"):
            LayerwiseSchedule([[], []])

    def test_an_empty_group_is_skipped(self) -> None:
        """A group holding no layers contributes no launches.

        Bench bookkeeping groups exist that never transfer, so an empty group
        alongside real ones is not an error.
        """
        schedule = LayerwiseSchedule([[], [0, 1], []])

        assert schedule.launch_count() == 2
        assert [launch.kernel_group_index for launch in schedule.launches] == [1, 1]
