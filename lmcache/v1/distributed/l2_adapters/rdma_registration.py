# SPDX-License-Identifier: Apache-2.0
"""Bounded RDMA registration windows carved out of the L1 slab.

A remote writer that holds an rkey for the *entire* L1 slab can write anywhere
in the KV cache. A wrong destination offset, or a late write from a request
that was already abandoned, then silently overwrites an unrelated request's KV.
Corrupted KV does not crash; it produces confidently wrong tokens, which is the
hardest possible failure to attribute.

So this module registers a fixed pool of bounded windows at initialization and
hands out one window per in-flight request. A misdirected write is still
possible, but its blast radius is one request's own buffer. See
``docs/design/v1/distributed/l2_adapters/aerospike_rdma.md`` for the full
rationale and the alternatives that were rejected.
"""

# Standard
from dataclasses import dataclass
from typing import Any
import enum

# First Party
from lmcache.v1.distributed.internal_api import L1MemoryDesc, MemoryGrowthPolicy

_DEFAULT_WINDOW_COUNT = 8
_DEFAULT_WINDOW_BYTES = 8 << 20
_DEFAULT_FETCH_TIMEOUT_SECONDS = 30.0


def _require_positive_int(raw: object, name: str) -> int:
    """Coerce ``raw`` to a positive int or explain why it is unusable.

    Args:
        raw: Value taken from a raw configuration dict.
        name: Field name, used in the error message.

    Returns:
        The validated integer.

    Raises:
        ValueError: If ``raw`` is not an int, is a bool, or is not positive.
    """
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError(f"{name} must be a positive integer, got {raw!r}")
    if raw <= 0:
        raise ValueError(f"{name} must be a positive integer, got {raw}")
    return raw


class RdmaTransport(enum.Enum):
    """RDMA transport used to receive KV payloads into L1.

    ``RC`` is the portable path: it works on InfiniBand, RoCE, and the
    Soft-RoCE (``rdma_rxe``) software device used for local testing. ``SRD`` is
    available only on AWS EFA and is compiled in optionally.
    """

    DISABLED = enum.auto()
    """No RDMA; payloads arrive through the normal Aerospike client path."""

    RC = enum.auto()
    """Reliable Connected queue pair; portable, including Soft-RoCE."""

    SRD = enum.auto()
    """Scalable Reliable Datagram queue pair; AWS EFA only."""


@dataclass(frozen=True)
class RdmaWindowPlan:
    """A pool of equally sized registration windows inside the L1 slab.

    Each window is registered once at initialization with
    ``LOCAL_WRITE | REMOTE_WRITE`` and is leased to at most one in-flight
    request at a time, so the rkey published to a remote writer never covers
    more than that one request's destination buffer.

    Attributes:
        window_count: Number of windows to register. Also the maximum number of
            concurrently outstanding RDMA fetches.
        window_bytes: Size of each window in bytes. Must be large enough to hold
            the largest single fetch a request can issue.
    """

    window_count: int
    window_bytes: int

    def total_bytes(self) -> int:
        """Return the total number of bytes covered by the whole pool.

        Returns:
            ``window_count * window_bytes``.
        """
        return self.window_count * self.window_bytes

    def window_offsets(self) -> tuple[int, ...]:
        """Return each window's byte offset from the start of the slab.

        Windows are laid out back to back from offset zero, so a window index
        maps to a destination offset without a lookup table.

        Returns:
            One offset per window, in window-index order.
        """
        return tuple(i * self.window_bytes for i in range(self.window_count))

    def validate_against(self, l1_memory_desc: L1MemoryDesc) -> None:
        """Check that this plan can be registered against ``l1_memory_desc``.

        Args:
            l1_memory_desc: Descriptor for the L1 slab that will host the pool.

        Raises:
            ValueError: If the plan is degenerate, if the slab is too small to
                hold the pool, if the slab pointer is null, if ``window_bytes``
                is not a multiple of the slab alignment, or if the slab may grow
                or move after registration.
        """
        if self.window_count <= 0:
            raise ValueError(
                f"rdma window_count must be positive, got {self.window_count}"
            )
        if self.window_bytes <= 0:
            raise ValueError(
                f"rdma window_bytes must be positive, got {self.window_bytes}"
            )
        if l1_memory_desc.ptr == 0:
            raise ValueError(
                "cannot register RDMA windows against a null L1 slab pointer; "
                "the L1 memory manager did not expose a mapped buffer"
            )
        if l1_memory_desc.growth is not MemoryGrowthPolicy.FIXED:
            raise ValueError(
                "RDMA registration requires a fixed-size L1 slab, but the "
                "configured L1 allocator may grow or re-base the slab after "
                "registration (growth="
                f"{l1_memory_desc.growth.name}). A slab that grows or moves "
                "after ibv_reg_mr leaves the remote writer holding an rkey for "
                "memory LMCache no longer owns, which corrupts KV silently "
                "instead of failing. Use MixedMemoryAllocator (set "
                "use_lazy=False on the L1 memory manager) or disable RDMA."
            )
        if l1_memory_desc.align_bytes > 0 and (
            self.window_bytes % l1_memory_desc.align_bytes != 0
        ):
            raise ValueError(
                f"rdma window_bytes ({self.window_bytes}) must be a multiple of "
                f"the L1 slab alignment ({l1_memory_desc.align_bytes}) so that "
                "every window starts on an aligned boundary"
            )
        if self.total_bytes() > l1_memory_desc.size:
            raise ValueError(
                f"rdma window pool needs {self.total_bytes()} bytes "
                f"({self.window_count} x {self.window_bytes}) but the L1 slab "
                f"is only {l1_memory_desc.size} bytes; reduce window_count or "
                "window_bytes, or grow the L1 slab"
            )


DEFAULT_RDMA_WINDOW_PLAN = RdmaWindowPlan(
    window_count=_DEFAULT_WINDOW_COUNT,
    window_bytes=_DEFAULT_WINDOW_BYTES,
)
"""Window pool used when a config omits ``window_count`` / ``window_bytes``."""


@dataclass(frozen=True)
class L1RdmaConfig:
    """Opt-in settings for receiving KV payloads by RDMA write into L1.

    Defaults to ``RdmaTransport.DISABLED`` so that an existing non-RDMA
    deployment behaves exactly as before.

    Attributes:
        transport: Queue-pair type to use, or ``DISABLED`` to turn RDMA off.
        device_name: libibverbs device to open, e.g. ``rxe0``. Empty selects
            the first device the driver reports.
        gid_index: Port GID index passed to ``ibv_query_gid``. Index 0 is the
            link-local GID, which is what Soft-RoCE exposes.
        window_plan: The bounded registration windows to pre-register at init.
        fetch_timeout_seconds: Deadline for one ``kv-sink-fetch`` round trip,
            DMA included. Must stay strictly below the L1 write-lock TTL; see
            :func:`validate_fetch_timeout_against_write_ttl`.
    """

    transport: RdmaTransport = RdmaTransport.DISABLED
    device_name: str = ""
    gid_index: int = 0
    window_plan: RdmaWindowPlan = DEFAULT_RDMA_WINDOW_PLAN
    fetch_timeout_seconds: float = _DEFAULT_FETCH_TIMEOUT_SECONDS

    def is_enabled(self) -> bool:
        """Report whether RDMA reception is turned on.

        Returns:
            ``True`` unless the transport is ``DISABLED``.
        """
        return self.transport is not RdmaTransport.DISABLED

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "L1RdmaConfig":
        """Build a config from the ``rdma`` sub-dict of an adapter config.

        Args:
            raw: Raw mapping with optional ``transport``, ``device_name``,
                ``gid_index``, ``window_count``, and ``window_bytes`` keys.

        Returns:
            A validated ``L1RdmaConfig``. An empty mapping yields the disabled
            default.

        Raises:
            ValueError: If ``transport`` is not a known transport name, if
                ``gid_index`` is negative, or if a window field is not a
                positive integer.
        """
        transport_name = str(raw.get("transport", RdmaTransport.DISABLED.name)).upper()
        try:
            transport = RdmaTransport[transport_name]
        except KeyError:
            supported = ", ".join(member.name for member in RdmaTransport)
            raise ValueError(
                f"unknown rdma transport {transport_name!r}; supported: {supported}"
            ) from None

        gid_index = raw.get("gid_index", 0)
        if isinstance(gid_index, bool) or not isinstance(gid_index, int):
            raise ValueError(f"gid_index must be an integer, got {gid_index!r}")
        if gid_index < 0:
            raise ValueError(f"gid_index must be non-negative, got {gid_index}")

        timeout = raw.get("fetch_timeout_seconds", _DEFAULT_FETCH_TIMEOUT_SECONDS)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError(
                f"fetch_timeout_seconds must be a positive number, got {timeout!r}"
            )
        if timeout <= 0:
            raise ValueError(
                f"fetch_timeout_seconds must be a positive number, got {timeout}"
            )

        return cls(
            transport=transport,
            device_name=str(raw.get("device_name", "")),
            gid_index=gid_index,
            window_plan=RdmaWindowPlan(
                window_count=_require_positive_int(
                    raw.get("window_count", _DEFAULT_WINDOW_COUNT), "window_count"
                ),
                window_bytes=_require_positive_int(
                    raw.get("window_bytes", _DEFAULT_WINDOW_BYTES), "window_bytes"
                ),
            ),
            fetch_timeout_seconds=float(timeout),
        )

    @classmethod
    def help(cls) -> str:
        """Return human-readable documentation for the ``rdma`` config block.

        Returns:
            A multi-line description of every supported key.
        """
        return (
            "Aerospike L2 adapter 'rdma' block (optional, default disabled):\n"
            "- transport (str): DISABLED | RC | SRD (default DISABLED).\n"
            "  RC is portable and works on Soft-RoCE; SRD is AWS EFA only.\n"
            "- device_name (str): libibverbs device, e.g. rxe0 "
            "(default: first device)\n"
            "- gid_index (int): port GID index (default 0)\n"
            "- window_count (int): pre-registered windows / max in-flight "
            f"RDMA fetches (default {_DEFAULT_WINDOW_COUNT})\n"
            "- window_bytes (int): bytes per window; must hold the largest "
            f"single fetch (default {_DEFAULT_WINDOW_BYTES})\n"
            "- fetch_timeout_seconds (float): deadline for one fetch round "
            f"trip (default {_DEFAULT_FETCH_TIMEOUT_SECONDS}). Must be "
            "strictly less than the L1 write-lock TTL "
            "(--l1-write-ttl-seconds), which is checked at startup.\n\n"
            "RDMA requires a fixed-size L1 slab (MixedMemoryAllocator). "
            "Enabling it with a growth-capable allocator raises ValueError."
        )


DISABLED_L1_RDMA = L1RdmaConfig()
"""Shared default: RDMA reception turned off."""


def validate_fetch_timeout_against_write_ttl(
    adapter_config: object,
    write_ttl_seconds: int,
) -> None:
    """Enforce that an RDMA fetch cannot outlive the L1 write lock.

    An RDMA fetch is safe only because the destination L1 object is
    write-locked for the whole round trip: a write-locked object is neither
    readable (``L1ObjectState.available_for_read`` is false) nor evictable
    (``delete`` returns ``KEY_IS_LOCKED``). That lock is a ``TTLLock``, so it
    expires on a timer rather than being held indefinitely. If a fetch outlives
    the TTL the lock lapses **silently** and the buffer becomes readable and
    evictable while a remote node may still be writing into it; L1Manager only
    logs "potential inconsistent data might be read".

    So the fetch deadline must be strictly below the write-lock TTL. The two
    knobs live in different config objects, which is exactly the kind of
    agreement that should fail loudly at startup rather than produce corrupt KV
    under load.

    Does nothing when ``adapter_config`` carries no RDMA block or RDMA is
    disabled, so non-RDMA adapters are unaffected.

    Args:
        adapter_config: An L2 adapter config, which may carry an ``rdma``
            attribute holding an :class:`L1RdmaConfig`.
        write_ttl_seconds: ``L1ManagerConfig.write_ttl_seconds``, the L1
            write-lock TTL, settable via ``--l1-write-ttl-seconds``.

    Raises:
        ValueError: If RDMA is enabled and the fetch timeout is greater than or
            equal to the write-lock TTL, or if the TTL is not positive.
    """
    rdma = getattr(adapter_config, "rdma", None)
    if not isinstance(rdma, L1RdmaConfig) or not rdma.is_enabled():
        return

    if write_ttl_seconds <= 0:
        raise ValueError(
            "RDMA reception requires a positive L1 write-lock TTL, but "
            f"write_ttl_seconds={write_ttl_seconds}. Without it the write lock "
            "cannot protect the destination buffer for the duration of the "
            "RDMA write. Set --l1-write-ttl-seconds, or disable RDMA."
        )

    if rdma.fetch_timeout_seconds >= write_ttl_seconds:
        raise ValueError(
            "RDMA fetch timeout must be strictly less than the L1 write-lock "
            f"TTL, but fetch_timeout_seconds={rdma.fetch_timeout_seconds} and "
            f"write_ttl_seconds={write_ttl_seconds}. A fetch that outlives the "
            "TTL lets the write lock expire silently, so the destination "
            "buffer becomes readable and evictable while an Aerospike node may "
            "still be writing into it, which corrupts KV without any error. "
            "Lower the adapter's rdma.fetch_timeout_seconds or raise "
            "--l1-write-ttl-seconds."
        )
