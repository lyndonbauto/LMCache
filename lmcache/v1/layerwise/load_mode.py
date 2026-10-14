# SPDX-License-Identifier: Apache-2.0
"""Choosing, per request, whether its cache hits load layer by layer.

Layerwise loading pays off only while the request computes enough new tokens
to hide the load behind; a full or nearly full hit has almost nothing to
compute, so it waits for every layer inside a forward pass that carries the
other requests' decodes too. :class:`LoadModePolicy` sends those requests
down the async path instead (load first, then schedule). See
``docs/design/v1/multiprocess/layerwise-load.md``, "Per-request load mode".

Kept out of the package ``__init__`` for the same import-cycle reason as
:mod:`~lmcache.v1.layerwise.deferral`.
"""

# Standard
from dataclasses import dataclass

# First Party
from lmcache.v1.multiprocess.custom_types import LoadMode

#: Default for :attr:`LoadModePolicy.min_new_token_ratio`. A layer of an 8B
#: model loads at about 57k tokens/s on a 7 GiB/s link and prefill computes
#: 5-13k tokens/s, so a ratio near 0.1-0.2 is where the load stops hiding.
DEFAULT_MIN_NEW_TOKEN_RATIO = 0.1


@dataclass(frozen=True)
class LoadModePolicy:
    """Picks :class:`LoadMode` from a request's hit and prompt length.

    Attributes:
        min_new_token_ratio: A request loads layerwise only if it computes
            at least this many new tokens per hit token. ``0.0`` loads every
            request layerwise (the behavior before per-request modes).

    Raises:
        ValueError: If ``min_new_token_ratio`` is negative.
    """

    min_new_token_ratio: float = DEFAULT_MIN_NEW_TOKEN_RATIO

    def __post_init__(self) -> None:
        if self.min_new_token_ratio < 0:
            raise ValueError(
                "min_new_token_ratio must not be negative, got "
                f"{self.min_new_token_ratio}"
            )

    def mode_for(self, prompt_chunks: int, hit_chunks: int) -> LoadMode:
        """Return the load mode for a request.

        Args:
            prompt_chunks: Chunks of the prompt the lookup covered.
            hit_chunks: Leading chunks of those that hit the cache.

        Returns:
            :attr:`LoadMode.LAYERWISE` if the new chunks are at least
            ``min_new_token_ratio`` times the hit chunks, else
            :attr:`LoadMode.ASYNC`. A request with no hit is layerwise,
            since it loads nothing either way.

        Raises:
            ValueError: If either count is negative or ``hit_chunks``
                exceeds ``prompt_chunks``.
        """
        if prompt_chunks < 0 or hit_chunks < 0 or hit_chunks > prompt_chunks:
            raise ValueError(
                f"invalid chunk counts: prompt={prompt_chunks} hit={hit_chunks}"
            )
        new_chunks = prompt_chunks - hit_chunks
        if new_chunks >= self.min_new_token_ratio * hit_chunks:
            return LoadMode.LAYERWISE
        return LoadMode.ASYNC
