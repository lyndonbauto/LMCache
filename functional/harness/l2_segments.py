# SPDX-License-Identifier: Apache-2.0
"""Name, check or delete the Aerospike records of one stored prompt's chunks.

The keys are derived the way the LMCache server derives them
(``TokenHasher`` over 256-token chunks, then ``ipc_key_to_object_keys`` for
world size 1, worker 0, no salt). Each object is a meta record ``<key>|m``
and, when sharded, segment records ``<key>|s|<i>``. The model name in the
key is what vLLM sent; ``--model-url`` reads vLLM's served name, and the
tool also tries each local snapshot path of that model, keeping the first
name whose chunk 0 is present.

T-PIPE-05 needs one slot the server declines while the lookup still sees
the chunk: ``delete`` removes one segment record and leaves the meta.

Usage::

    python l2_segments.py --port 3100 --corpus corpus.json \\
        --model-url http://localhost:8000 model prompt=P-exact-10
    python l2_segments.py ... check prompt=P-exact-10
    python l2_segments.py ... delete prompt=P-exact-10 chunk=1 seg=5

``model`` prints the model name the keys use; ``check`` prints, per chunk,
whether its meta is present and its segment count; ``delete`` removes one
segment and prints its key. Exits 1 if no candidate model name matches or
the record is absent.
"""

# Standard
from pathlib import Path
import argparse
import json
import os
import sys
import urllib.request

# Third Party
import aerospike

# First Party
from lmcache.v1.distributed.api import ipc_key_to_object_keys
from lmcache.v1.distributed.l2_adapters.native_connector_l2_adapter import (
    object_key_to_string,
)
from lmcache.v1.multiprocess.custom_types import IPCCacheServerKey
from lmcache.v1.multiprocess.token_hasher import TokenHasher

CHUNK_TOKENS = 256


def chunk_keys(model_name: str, token_ids: list[int]) -> list[str]:
    """Return the Aerospike key string of each full chunk of a prompt.

    Args:
        model_name: Model name as vLLM sends it.
        token_ids: The prompt's token IDs.

    Returns:
        One key string per full 256-token chunk, in order.
    """
    end = (len(token_ids) // CHUNK_TOKENS) * CHUNK_TOKENS
    key = IPCCacheServerKey(
        model_name=model_name,
        world_size=1,
        worker_id=0,
        token_ids=tuple(token_ids),
        start=0,
        end=end,
        request_id="l2_segments",
        cache_salt="",
    )
    hasher = TokenHasher(chunk_size=CHUNK_TOKENS)
    hashes = [
        TokenHasher.hash_to_bytes(h)
        for h in hasher.compute_chunk_hashes(token_ids, end=end)
    ]
    objects = ipc_key_to_object_keys(key, hashes, [0])[0]
    return [object_key_to_string(o) for o in objects]


def candidate_names(model_url: str) -> list[str]:
    """Return vLLM's served model name, then its local snapshot paths.

    Args:
        model_url: vLLM base URL, or "" to use ``MODEL_NAME`` from the
            environment only.

    Returns:
        Candidate model names in the order to try.
    """
    names: list[str] = []
    if os.environ.get("MODEL_NAME"):
        names.append(os.environ["MODEL_NAME"])
    if model_url:
        with urllib.request.urlopen(f"{model_url}/v1/models", timeout=30) as reply:
            names.append(json.load(reply)["data"][0]["id"])
    hub = Path(os.environ.get("HF_HOME", "/work/hf")) / "hub"
    for name in list(names):
        repo = hub / ("models--" + name.replace("/", "--")) / "snapshots"
        if repo.is_dir():
            names.extend(str(p) for p in sorted(repo.iterdir()))
    return names


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3000)
    parser.add_argument("--namespace", default="lmcache")
    parser.add_argument("--set", dest="set_name", default="kv_chunks")
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--model-url", default="")
    parser.add_argument("command", choices=["model", "check", "delete"])
    parser.add_argument("params", nargs="*", help="prompt=<id> [chunk=<c> seg=<s>]")
    args = parser.parse_args()
    params = dict(p.split("=", 1) for p in args.params)
    with open(args.corpus) as f:
        corpus = json.load(f)
    prompts = {p["id"]: p for s in corpus["sets"].values() for p in s}
    tokens = prompts[params["prompt"]]["token_ids"]

    client = aerospike.client({"hosts": [(args.host, args.port)]}).connect()
    try:
        chosen = ""
        for name in candidate_names(args.model_url):
            first = chunk_keys(name, tokens)[0]
            _, meta = client.exists((args.namespace, args.set_name, first + "|m"))
            if meta is not None:
                chosen = name
                break
        if not chosen:
            print(f"no stored chunk 0 of {params['prompt']} under any candidate name")
            sys.exit(1)
        keys = chunk_keys(chosen, tokens)
        if args.command == "model":
            print(chosen)
            return
        if args.command == "check":
            for i, key in enumerate(keys):
                _, meta = client.exists((args.namespace, args.set_name, key + "|m"))
                nseg = -1
                if meta is not None:
                    _, _, bins = client.select(
                        (args.namespace, args.set_name, key + "|m"), ["nseg"]
                    )
                    nseg = int(bins.get("nseg", 0))
                print(f"chunk {i}: meta={'yes' if meta else 'no'} nseg={nseg}")
            return
        chunk, seg = int(params["chunk"]), int(params["seg"])
        target = (args.namespace, args.set_name, f"{keys[chunk]}|s|{seg}")
        _, meta = client.exists(target)
        if meta is None:
            print(f"absent: {target[2]}")
            sys.exit(1)
        client.remove(target)
        print(f"deleted {target[2]} (model name {chosen})")
    finally:
        client.close()


if __name__ == "__main__":
    main()
