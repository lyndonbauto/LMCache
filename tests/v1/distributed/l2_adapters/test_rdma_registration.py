# SPDX-License-Identifier: Apache-2.0
"""Contract tests for the bounded RDMA registration window plan and config."""

# Third Party
import pytest

# First Party
from lmcache.v1.distributed.internal_api import (
    UNREGISTERED_MEMORY,
    L1MemoryDesc,
    MemoryGrowthPolicy,
    MemoryRegistration,
    MemoryRegistrationTransport,
)
from lmcache.v1.distributed.l2_adapters.rdma_registration import (
    DISABLED_L1_RDMA,
    L1RdmaConfig,
    RdmaTransport,
    RdmaWindowPlan,
)

_SLAB_BYTES = 1 << 20
_ALIGN = 4096


def fixed_slab(size: int = _SLAB_BYTES, align: int = _ALIGN) -> L1MemoryDesc:
    """Return a descriptor for a fixed-size, non-null L1 slab."""
    return L1MemoryDesc(ptr=0x10000, size=size, align_bytes=align)


class TestMemoryRegistration:
    """The transport-agnostic registration handle on L1MemoryDesc."""

    def test_default_registration_is_unregistered(self) -> None:
        assert not UNREGISTERED_MEMORY.is_registered()
        assert UNREGISTERED_MEMORY.transport is MemoryRegistrationTransport.UNREGISTERED

    def test_descriptor_defaults_to_unregistered_and_fixed(self) -> None:
        desc = L1MemoryDesc(ptr=1, size=2, align_bytes=4)
        assert not desc.registration.is_registered()
        assert desc.growth is MemoryGrowthPolicy.FIXED

    def test_handle_is_carried_with_its_transport(self) -> None:
        registration = MemoryRegistration(
            transport=MemoryRegistrationTransport.IB_VERBS,
            handle=0xDEADBEEF,
            base=0x1000,
            size=4096,
        )
        assert registration.is_registered()
        assert registration.handle == 0xDEADBEEF
        assert registration.transport is MemoryRegistrationTransport.IB_VERBS

    def test_empty_window_is_not_registered(self) -> None:
        registration = MemoryRegistration(
            transport=MemoryRegistrationTransport.IB_VERBS, handle=7, size=0
        )
        assert not registration.is_registered()

    def test_contains_bounds_the_blast_radius(self) -> None:
        window = MemoryRegistration(
            transport=MemoryRegistrationTransport.IB_VERBS,
            handle=1,
            base=0x2000,
            size=0x1000,
        )
        assert window.contains(0x2000, 0x1000)
        assert window.contains(0x2500, 0x10)
        assert not window.contains(0x1FFF, 0x10)
        assert not window.contains(0x2000, 0x1001)

    def test_unregistered_contains_nothing(self) -> None:
        assert not UNREGISTERED_MEMORY.contains(0, 0)

    def test_contains_rejects_negative_size(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            UNREGISTERED_MEMORY.contains(0, -1)


class TestRdmaWindowPlan:
    """Window geometry and the validation that protects the slab."""

    def test_offsets_are_back_to_back_from_zero(self) -> None:
        plan = RdmaWindowPlan(window_count=4, window_bytes=_ALIGN)
        assert plan.window_offsets() == (0, 4096, 8192, 12288)
        assert plan.total_bytes() == 4 * _ALIGN

    def test_plan_that_fits_validates(self) -> None:
        RdmaWindowPlan(window_count=8, window_bytes=_ALIGN).validate_against(
            fixed_slab()
        )

    def test_pool_larger_than_slab_is_rejected(self) -> None:
        plan = RdmaWindowPlan(window_count=1024, window_bytes=_ALIGN)
        with pytest.raises(ValueError, match="window pool needs"):
            plan.validate_against(fixed_slab())

    def test_growable_slab_is_rejected_naming_the_hazard(self) -> None:
        growable = L1MemoryDesc(
            ptr=0x10000,
            size=_SLAB_BYTES,
            align_bytes=_ALIGN,
            growth=MemoryGrowthPolicy.GROWABLE,
        )
        with pytest.raises(ValueError) as excinfo:
            RdmaWindowPlan(window_count=1, window_bytes=_ALIGN).validate_against(
                growable
            )
        message = str(excinfo.value)
        assert "MixedMemoryAllocator" in message
        assert "GROWABLE" in message

    def test_null_slab_pointer_is_rejected(self) -> None:
        desc = L1MemoryDesc(ptr=0, size=_SLAB_BYTES, align_bytes=_ALIGN)
        with pytest.raises(ValueError, match="null L1 slab pointer"):
            RdmaWindowPlan(window_count=1, window_bytes=_ALIGN).validate_against(desc)

    def test_misaligned_window_bytes_is_rejected(self) -> None:
        plan = RdmaWindowPlan(window_count=1, window_bytes=_ALIGN + 1)
        with pytest.raises(ValueError, match="multiple of"):
            plan.validate_against(fixed_slab())

    @pytest.mark.parametrize(
        ("count", "size"),
        [(0, _ALIGN), (-1, _ALIGN), (1, 0), (1, -_ALIGN)],
    )
    def test_degenerate_plans_are_rejected(self, count: int, size: int) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            RdmaWindowPlan(window_count=count, window_bytes=size).validate_against(
                fixed_slab()
            )


class TestL1RdmaConfig:
    """Parsing and the default-off guarantee."""

    def test_default_is_disabled(self) -> None:
        assert not DISABLED_L1_RDMA.is_enabled()
        assert DISABLED_L1_RDMA.transport is RdmaTransport.DISABLED

    def test_empty_dict_yields_disabled(self) -> None:
        assert not L1RdmaConfig.from_dict({}).is_enabled()

    @pytest.mark.parametrize("name", ["rc", "RC", "Rc"])
    def test_transport_name_is_case_insensitive(self, name: str) -> None:
        config = L1RdmaConfig.from_dict({"transport": name})
        assert config.transport is RdmaTransport.RC
        assert config.is_enabled()

    def test_srd_is_selectable(self) -> None:
        assert L1RdmaConfig.from_dict({"transport": "SRD"}).transport is (
            RdmaTransport.SRD
        )

    def test_unknown_transport_lists_supported_values(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            L1RdmaConfig.from_dict({"transport": "infiniband"})
        assert "supported" in str(excinfo.value)
        assert "RC" in str(excinfo.value)

    def test_window_fields_are_parsed(self) -> None:
        config = L1RdmaConfig.from_dict(
            {"transport": "RC", "window_count": 3, "window_bytes": 8192}
        )
        assert config.window_plan == RdmaWindowPlan(window_count=3, window_bytes=8192)

    def test_device_and_gid_index_are_parsed(self) -> None:
        config = L1RdmaConfig.from_dict(
            {"transport": "RC", "device_name": "rxe0", "gid_index": 1}
        )
        assert config.device_name == "rxe0"
        assert config.gid_index == 1

    @pytest.mark.parametrize("field", ["window_count", "window_bytes"])
    @pytest.mark.parametrize("bad", [0, -1, True, "4", 1.5])
    def test_non_positive_window_fields_are_rejected(
        self, field: str, bad: object
    ) -> None:
        with pytest.raises(ValueError, match="must be a positive integer"):
            L1RdmaConfig.from_dict({"transport": "RC", field: bad})

    @pytest.mark.parametrize("bad", [-1, "0", True])
    def test_invalid_gid_index_is_rejected(self, bad: object) -> None:
        with pytest.raises(ValueError, match="gid_index"):
            L1RdmaConfig.from_dict({"transport": "RC", "gid_index": bad})

    def test_help_documents_the_allocator_constraint(self) -> None:
        text = L1RdmaConfig.help()
        assert "MixedMemoryAllocator" in text
        assert "Soft-RoCE" in text
