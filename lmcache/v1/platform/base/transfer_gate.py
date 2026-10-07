# SPDX-License-Identifier: Apache-2.0
"""Serializes the transfer units enqueued on one cache context.

A cache context has one transfer stream and one set of temp staging
buffers. A transfer unit -- staging copies into those buffers plus the
kernel that reads them -- must reach the stream without another transfer's
operations in between, or the second transfer would overwrite staging the
first one's kernel has not read yet. When every transfer of a context ran on
one thread that held trivially; once a layerwise retrieve launches layers
from its own thread while stores and other retrieves run, each unit holds
the context's :class:`TransferGate`.

Holders that keep staging contents across units (whole-object layerwise
staging reuses a batch already in the slots) pass a token, and learn from
:meth:`TransferGate.hold` whether anyone else held the gate since.
"""

# Standard
from collections.abc import Iterator
from contextlib import contextmanager
from enum import Enum
import itertools
import threading

#: Holder token for one-shot transfers, which never reuse staging contents.
ONE_SHOT_HOLDER = 0


class StagingSlots(Enum):
    """Whether a gate holder's staging buffers are as it left them."""

    #: The previous holder was this one; nothing else staged in between.
    INTACT = "intact"
    #: Another transfer may have overwritten the staging buffers.
    OVERWRITTEN = "overwritten"


class TransferGate:
    """Serializes the GPU transfer units enqueued on one cache context.

    One gate per cache context; every transfer that uses its staging buffers
    holds the gate while it enqueues a unit. Not reentrant. Safe for
    concurrent use.
    """

    def __init__(self) -> None:
        """Build an unheld gate whose staging buffers belong to nobody."""
        self._lock = threading.Lock()
        self._last_holder = ONE_SHOT_HOLDER
        self._tokens = itertools.count(1)

    def new_holder(self) -> int:
        """Return a holder token no other caller of this gate has.

        Returns:
            A positive token for :meth:`hold`.
        """
        return next(self._tokens)

    @contextmanager
    def hold(self, holder: int = ONE_SHOT_HOLDER) -> Iterator[StagingSlots]:
        """Hold the gate while enqueuing one transfer unit.

        Args:
            holder: Token from :meth:`new_holder` for a transfer that reuses
                staging contents across units, or :data:`ONE_SHOT_HOLDER`.

        Yields:
            :attr:`StagingSlots.INTACT` if ``holder`` held the gate last and
            is not one-shot, else :attr:`StagingSlots.OVERWRITTEN`.
        """
        with self._lock:
            intact = holder != ONE_SHOT_HOLDER and holder == self._last_holder
            self._last_holder = holder
            yield StagingSlots.INTACT if intact else StagingSlots.OVERWRITTEN
