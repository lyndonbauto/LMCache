# SPDX-License-Identifier: Apache-2.0
"""A vLLM-shaped retrieve request and the hybrid model it is planned against.

The request is shaped the way vLLM sends one: an ``IPCCacheServerKey`` with
the prompt's token ids, a worker id, and a range ending on the last full
chunk. Its object keys come from the production hasher and key expansion.
"""

# Third Party
import torch

# First Party
from lmcache.v1.distributed.api import (
    AttnWindowDesc,
    MemoryLayoutDesc,
    ObjectKey,
    ipc_key_to_object_keys,
)
from lmcache.v1.layerwise import ModelLayout
from lmcache.v1.layerwise.request_fetch import FetchModel
from lmcache.v1.multiprocess.custom_types import IPCCacheServerKey
from lmcache.v1.multiprocess.token_hasher import TokenHasher

CHUNK_TOKENS = 16
NUM_CHUNKS = 5
MODEL_NAME = "google/gemma-3-hybrid"
#: Small enough that the attention planes are cut into several records.
MAX_RECORD_BYTES = 3000

#: Group 0: full attention, 2 layers. Group 1: sliding window of 2 chunks,
#: 2 layers. Group 2: a connector-private aux group, never retrieved.
GROUP_LAYOUTS = {
    0: MemoryLayoutDesc(shapes=[torch.Size([2, 2, 16, 64])], dtypes=[torch.float16]),
    1: MemoryLayoutDesc(shapes=[torch.Size([2, 2, 16, 32])], dtypes=[torch.float16]),
    2: MemoryLayoutDesc(shapes=[torch.Size([1, 1, 16, 8])], dtypes=[torch.float16]),
}
KERNEL_LAYERS = {0: [[0, 2]], 1: [[1, 3]], 2: [[4]]}
ATTN = AttnWindowDesc(
    num_chunks_in_sw=[-1, 2, -1],
    world_size=2,
    group_kinds=("attention", "attention", "aux"),
)


def vllm_request(cache_salt: str = "") -> IPCCacheServerKey:
    """A prompt of five full chunks plus a partial one, from worker 1 of 2."""
    tokens = tuple(range(1000, 1000 + NUM_CHUNKS * CHUNK_TOKENS + 7))
    return IPCCacheServerKey(
        model_name=MODEL_NAME,
        world_size=2,
        worker_id=1,
        token_ids=tokens,
        start=0,
        end=NUM_CHUNKS * CHUNK_TOKENS,
        request_id="cmpl-7f3a",
        cache_salt=cache_salt,
    )


def resolve_obj_keys(key: IPCCacheServerKey) -> list[list[ObjectKey]]:
    """What ``MPCacheServerContext.resolve_obj_keys`` returns for ``key``."""
    hasher = TokenHasher(chunk_size=CHUNK_TOKENS)
    chunk_hashes = [
        TokenHasher.hash_to_bytes(h)
        for h in hasher.compute_chunk_hashes(list(key.token_ids), end=key.end)
    ]
    return ipc_key_to_object_keys(
        key, chunk_hashes, list(range(ATTN.num_object_groups))
    )


def fetch_model() -> FetchModel:
    """The hybrid model the request is planned against."""
    return FetchModel(ModelLayout.from_registration(GROUP_LAYOUTS, KERNEL_LAYERS), ATTN)
