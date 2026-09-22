# SPDX-License-Identifier: Apache-2.0
"""
Class for distributed storage manager internal API data structures
"""

# Standard
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import enum

# First Party
from lmcache.v1.distributed.api import L1BackendType, ObjectKey


class MemoryRegistrationTransport(enum.Enum):
    """Transport that owns a memory-registration handle.

    ``UNREGISTERED`` is the explicit "no registration" sentinel, preferred over
    ``Optional`` so that callers never have to branch on ``None``.
    """

    UNREGISTERED = enum.auto()
    """No transport has registered this memory."""

    IB_VERBS = enum.auto()
    """libibverbs; the handle is an ``ibv_mr`` rkey."""

    MOONCAKE = enum.auto()
    """Mooncake transfer engine; the handle is engine-defined."""

    NIXL = enum.auto()
    """NIXL agent; the handle is agent-defined."""


@dataclass(frozen=True)
class MemoryRegistration:
    """A transport-agnostic handle for one registered memory window.

    The handle is deliberately not named after any one transport: the same
    struct is published to the Mooncake, NIXL, and libibverbs paths. Consumers
    must check ``transport`` before interpreting ``handle``.

    Attributes:
        transport: Transport that produced ``handle``. ``UNREGISTERED`` means
            the remaining fields carry no meaning.
        handle: Opaque transport-defined registration token. For
            ``IB_VERBS`` this is the ``rkey`` published to the remote writer.
        base: Virtual address of the first byte covered by this registration.
        size: Number of bytes covered by this registration.
    """

    transport: MemoryRegistrationTransport = MemoryRegistrationTransport.UNREGISTERED
    handle: int = 0
    base: int = 0
    size: int = 0

    def is_registered(self) -> bool:
        """Report whether this registration refers to real registered memory.

        Returns:
            ``True`` when a transport has registered a non-empty window.
        """
        return (
            self.transport is not MemoryRegistrationTransport.UNREGISTERED
            and self.size > 0
        )

    def contains(self, base: int, size: int) -> bool:
        """Report whether ``[base, base + size)`` lies inside this window.

        Args:
            base: Virtual address of the first byte to test.
            size: Number of bytes to test. Must be non-negative.

        Returns:
            ``True`` when the whole range is covered by this registration.

        Raises:
            ValueError: If ``size`` is negative.
        """
        if size < 0:
            raise ValueError(f"size must be non-negative, got {size}")
        if not self.is_registered():
            return False
        return base >= self.base and base + size <= self.base + self.size


UNREGISTERED_MEMORY = MemoryRegistration()
"""Shared sentinel for memory that no transport has registered."""


class MemoryGrowthPolicy(enum.Enum):
    """Whether an L1 slab can move or grow after it is first described.

    A transport that pins and registers the slab at initialization can only do
    so safely for ``FIXED`` slabs: re-basing or extending a registered region
    silently invalidates the remote writer's rkey and destination offsets.
    """

    FIXED = enum.auto()
    """The slab keeps its base address and length for the process lifetime."""

    GROWABLE = enum.auto()
    """The slab may be extended or re-based after this descriptor was made."""


@dataclass(frozen=True)
class L1MemoryDesc:
    """
    Describes the L1 memory buffer registered with an external backend (e.g. Nixl).

    Attributes:
        ptr: Virtual address of the first byte of the L1 slab.
        size: Length of the L1 slab in bytes.
        align_bytes: Alignment guaranteed for allocations within the slab.
        growth: Whether the slab may grow or move after this snapshot. Transports
            that register memory once at init must refuse ``GROWABLE`` slabs.
        registration: Slab-wide registration handle, or ``UNREGISTERED_MEMORY``
            when no transport has registered the slab as a single region.
    """

    ptr: int
    size: int
    align_bytes: int
    growth: MemoryGrowthPolicy = MemoryGrowthPolicy.FIXED
    registration: MemoryRegistration = UNREGISTERED_MEMORY


@dataclass(frozen=True)
class L1ObjectMeta:
    """Per-object metadata published alongside keys in L1 cache events.

    Attributes:
        size_bytes: Logical byte size of the object.
        backend: The storage medium backing the object.
    """

    size_bytes: int
    backend: L1BackendType


class EventListener(ABC):  # noqa: B024
    pass


# For L1 manager event notifications
class L1ManagerListener(EventListener):
    """
    Listener for L1 manager events
    """

    @abstractmethod
    def on_l1_keys_reserved_read(self, keys: list[ObjectKey]):
        """
        Notify the listener that new keys have been reserved for read on L1.

        Args:
            keys (list[ObjectKey]): The keys that have been successfully reserved
        """
        pass

    @abstractmethod
    def on_l1_keys_read_finished(self, keys: list[ObjectKey]):
        """
        Notify the listener that keys have been accessed on L1.

        Args:
            keys (list[ObjectKey]): The keys that have been successfully read
        """
        pass

    @abstractmethod
    def on_l1_keys_reserved_write(self, keys: list[ObjectKey]):
        """
        Notify the listener that keys have been reserved for write on L1.

        Args:
            keys (list[ObjectKey]): The keys that have been successfully reserved
        """
        pass

    @abstractmethod
    def on_l1_keys_write_finished(self, keys: list[ObjectKey]):
        """
        Notify the listener that keys have been finished for writing on L1.

        Args:
            keys (list[ObjectKey]): The keys that have been successfully written
        """
        pass

    @abstractmethod
    def on_l1_keys_finish_write_and_reserve_read(self, keys: list[ObjectKey]):
        """
        Notify the listener that keys have been finished for writing
        and reserved for read on L1.

        This will only be trigger by the prefetch operation now.

        Args:
            keys (list[ObjectKey]): The keys that have been successfully
                finished for writing and reserved for read
        """
        # NOTE (ApostaC): may consider renaming this to `on_l1_keys_finish_prefetch`
        # for better clarity
        pass

    @abstractmethod
    def on_l1_keys_deleted_by_manager(self, keys: list[ObjectKey]):
        """
        Notify the listener that keys have been deleted from L1.

        Args:
            keys (list[ObjectKey]): The keys that have been deleted
        """
        pass

    @abstractmethod
    def on_l1_keys_accessed(self, keys: list[ObjectKey]):
        """
        Notify the listener that keys have been accessed on L1.

        Args:
            keys (list[ObjectKey]): The keys that have been accessed
        """
        pass


class L2AdapterListener(EventListener):
    """Listener for L2 adapter events, analogous to L1ManagerListener."""

    @abstractmethod
    def on_l2_keys_stored(self, keys: list[ObjectKey], sizes: list[int]):
        """
        Notify the listener that keys have been successfully stored in L2.

        Args:
            keys (list[ObjectKey]): The keys that have been stored.
            sizes (list[int]): The byte size of each stored key.
        """
        pass

    @abstractmethod
    def on_l2_keys_accessed(self, keys: list[ObjectKey]):
        """
        Notify the listener that keys have been accessed (lookup hit) in L2.

        Args:
            keys (list[ObjectKey]): The keys that have been accessed.
        """
        pass

    @abstractmethod
    def on_l2_keys_deleted(self, keys: list[ObjectKey]):
        """
        Notify the listener that keys have been deleted from L2.

        Args:
            keys (list[ObjectKey]): The keys that have been deleted.
        """
        pass


# For Eviction
class EvictionDestination(enum.Enum):
    """
    The destination of evicted objects
    """

    DISCARD = enum.auto()
    """Discard the evicted objects"""

    L2_CACHE = enum.auto()
    """Evict to L2 storage"""


@dataclass(frozen=True)
class EvictionAction:
    """
    An action to be taken for eviction
    """

    destination: EvictionDestination
    """The destination of the evicted object"""

    keys: list[ObjectKey] = field(default_factory=list)
    """The key of the object to be evicted"""


class L2StoreResult(int):
    """Immutable result of a completed L2 store task.

    Encodes both the success flag and bytes transferred in the int
    value: ``>= 0`` means success (value = bytes transferred);
    ``-1`` means failure.

    Args:
        success: Whether the store task succeeded.
        bytes_transferred: Bytes actually written to L2. Must be >= 0.

    Raises:
        ValueError: If ``bytes_transferred`` is negative.
    """

    def __new__(cls, success: bool, bytes_transferred: int) -> "L2StoreResult":
        if bytes_transferred < 0:
            raise ValueError(f"bytes_transferred must be >= 0, got {bytes_transferred}")
        return super().__new__(cls, bytes_transferred if success else -1)

    def is_successful(self) -> bool:
        """Return ``True`` when the store task succeeded."""
        return int(self) >= 0

    def bytes_transferred(self) -> int:
        """Return the number of bytes actually written, or 0 on failure."""
        value = int(self)
        return value if value >= 0 else 0


@dataclass(frozen=True)
class QuotaEntry:
    """Snapshot of a single quota registration."""

    cache_salt: str
    limit_bytes: int
