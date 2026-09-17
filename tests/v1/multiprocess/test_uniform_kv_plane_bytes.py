# SPDX-License-Identifier: Apache-2.0
"""Tests for deriving a single K/V plane size from registered layouts.

Storage backends use the plane size to keep a record inside one model layer.
Reporting a plane size that does not describe the model is worse than
reporting none, because records would then straddle layers while the backend
believed they did not, so these tests lean on the disagreement cases.
"""

# Third Party
import pytest
import torch

# First Party
from lmcache.v1.distributed.api import MemoryLayoutDesc
from lmcache.v1.multiprocess.modules.lmcache_driven_transfer import (
    uniform_kv_plane_bytes,
)


def _layout(*shapes: tuple[int, ...], dtype: torch.dtype = torch.float16):
    """Build a MemoryLayoutDesc over the given shapes, all of one dtype.

    Args:
        shapes: One shape per kernel group.
        dtype: Element type for every kernel group.

    Returns:
        A ``MemoryLayoutDesc`` describing those kernel groups.
    """
    return MemoryLayoutDesc(
        shapes=[torch.Size(s) for s in shapes],
        dtypes=[dtype] * len(shapes),
    )


class TestTheStandardLayout:
    """The usual ``(kv_size, num_layers, num_slots, hidden_dim)`` shape."""

    def test_plane_is_the_two_innermost_dimensions(self) -> None:
        """The plane size ignores kv_size and num_layers."""
        # 256 slots x 1024 hidden x 2 bytes = 512 KiB, the documented default.
        layout = _layout((2, 32, 256, 1024))
        assert uniform_kv_plane_bytes([layout]) == 512 * 1024

    def test_layer_and_kv_counts_do_not_change_the_plane(self) -> None:
        """Only the innermost two dimensions and the dtype matter."""
        assert uniform_kv_plane_bytes([_layout((2, 32, 256, 1024))]) == (
            uniform_kv_plane_bytes([_layout((1, 80, 256, 1024))])
        )

    def test_dtype_scales_the_plane(self) -> None:
        """Element size is part of the plane size."""
        as_fp16 = uniform_kv_plane_bytes([_layout((2, 4, 256, 1024))])
        as_fp8 = uniform_kv_plane_bytes(
            [_layout((2, 4, 256, 1024), dtype=torch.float8_e4m3fn)]
        )
        assert as_fp16 == 2 * as_fp8

    def test_the_three_dimensional_variant_is_handled(self) -> None:
        """``NL_X_NB_BS_HS`` drops the leading kv_size and still works.

        The plane is the innermost two dimensions either way, so this shape
        must produce the same answer as its four-dimensional counterpart.
        """
        assert uniform_kv_plane_bytes([_layout((32, 256, 1024))]) == 512 * 1024


class TestAgreementAcrossGroups:
    """One number is only reportable while every kernel group agrees."""

    def test_groups_sharing_a_plane_size_agree(self) -> None:
        """A sliding-window and a full-attention group sharing heads agree.

        This is the common hybrid case: differing layer counts, same plane.
        """
        layout = _layout((2, 8, 256, 1024), (2, 24, 256, 1024))
        assert uniform_kv_plane_bytes([layout]) == 512 * 1024

    def test_differing_hidden_dims_report_no_plane_size(self) -> None:
        """Groups with different head geometry have no shared plane size."""
        layout = _layout((2, 8, 256, 1024), (2, 24, 256, 512))
        assert uniform_kv_plane_bytes([layout]) == 0

    def test_differing_slot_counts_report_no_plane_size(self) -> None:
        """A compressed group's slot count differs, so there is no one plane."""
        layout = _layout((2, 8, 256, 1024), (2, 24, 128, 1024))
        assert uniform_kv_plane_bytes([layout]) == 0

    def test_differing_dtypes_report_no_plane_size(self) -> None:
        """Element size is part of the plane, so mixed dtypes disagree."""
        layout = MemoryLayoutDesc(
            shapes=[torch.Size((2, 8, 256, 1024)), torch.Size((2, 8, 256, 1024))],
            dtypes=[torch.float16, torch.float8_e4m3fn],
        )
        assert uniform_kv_plane_bytes([layout]) == 0

    def test_disagreement_across_object_groups_is_caught(self) -> None:
        """Agreement is required across every layout, not within each one.

        Separate object groups are each internally uniform here, so a check
        that only compared groups within a layout would wrongly report a
        plane size.
        """
        layouts = [_layout((2, 8, 256, 1024)), _layout((2, 24, 256, 512))]
        assert uniform_kv_plane_bytes(layouts) == 0


class TestDegenerateInput:
    """Cases where no plane size can be derived."""

    def test_no_layouts_report_no_plane_size(self) -> None:
        """Nothing registered means nothing to align to."""
        assert uniform_kv_plane_bytes([]) == 0

    def test_an_empty_layout_reports_no_plane_size(self) -> None:
        """A layout with no kernel groups yields no plane size."""
        assert uniform_kv_plane_bytes([_layout()]) == 0

    @pytest.mark.parametrize("shape", [(1024,), ()])
    def test_a_shape_with_too_few_dimensions_reports_no_plane_size(
        self, shape: tuple[int, ...]
    ) -> None:
        """A plane needs both a slot count and a hidden dimension.

        Args:
            shape: A shape with fewer than two dimensions.
        """
        assert uniform_kv_plane_bytes([_layout(shape)]) == 0
