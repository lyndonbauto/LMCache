# SPDX-License-Identifier: Apache-2.0
"""Print the Aerospike record layout of one stored prompt: per chunk, the meta
record's bins and the number and byte sizes of its segment records.

T-E2E-11 records "records and plans at production size": for Llama-3.3-70B
one chunk is 80 MiB, so this shows how many segment records one object
needs and how large each is, next to the 8B's 32 MiB chunks. Keys are named
as in ``l2_segments.py`` (meta ``<key>|m``, segments ``<key>|s|<wid>|<i>``).

Usage::

    MODEL_NAME=meta-llama/Llama-3.3-70B-Instruct python l2_sizes.py \\
        --port 3700 --corpus corpus.json prompt=P-exact-15 [chunks=0,63]

Prints one line per chunk listed in ``chunks`` (default: first and last),
the meta bins of the first one, and a total line. Exits 1 if chunk 0 of the
prompt is not stored under any candidate model name.
"""

# Standard
from pathlib import Path
import argparse
import json
import sys

# Third Party
import aerospike

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Third Party
from l2_segments import candidate_names, chunk_keys  # noqa: E402


def bin_sizes(bins: dict[str, object]) -> dict[str, str]:
    """Describe each bin as ``<type>:<length or value>``.

    Args:
        bins: The record's bins.

    Returns:
        Bin name to a short description (byte length for bytes, the value
        for scalars).
    """
    out: dict[str, str] = {}
    for name, value in bins.items():
        if isinstance(value, (bytes, bytearray)):
            out[name] = f"bytes:{len(value)}"
        elif isinstance(value, (list, dict)):
            out[name] = f"{type(value).__name__}:{len(value)}"
        else:
            out[name] = f"{type(value).__name__}:{value}"
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3000)
    parser.add_argument("--namespace", default="lmcache")
    parser.add_argument("--set", dest="set_name", default="kv_chunks")
    parser.add_argument("--corpus", required=True)
    parser.add_argument("params", nargs="*", help="prompt=<id> [chunks=<a,b>]")
    args = parser.parse_args()
    params = dict(p.split("=", 1) for p in args.params)
    with open(args.corpus) as f:
        corpus = json.load(f)
    prompts = {p["id"]: p for s in corpus["sets"].values() for p in s}
    tokens = prompts[params["prompt"]]["token_ids"]

    client = aerospike.client({"hosts": [(args.host, args.port)]}).connect()
    try:
        chosen = ""
        for name in candidate_names(""):
            first = chunk_keys(name, tokens)[0]
            _, meta = client.exists((args.namespace, args.set_name, first + "|m"))
            if meta is not None:
                chosen = name
                break
        if not chosen:
            print(f"no stored chunk 0 of {params['prompt']} under any candidate name")
            sys.exit(1)
        keys = chunk_keys(chosen, tokens)
        wanted = (
            [int(c) for c in params["chunks"].split(",")]
            if "chunks" in params
            else sorted({0, len(keys) - 1})
        )
        print(f"{params['prompt']}: {len(keys)} chunks, model name {chosen}")
        total_bytes = 0
        total_segs = 0
        for i in wanted:
            _, _, mbins = client.get((args.namespace, args.set_name, keys[i] + "|m"))
            if i == wanted[0]:
                print(f"meta bins: {json.dumps(bin_sizes(mbins), sort_keys=True)}")
            wid = mbins.get("wid", "")
            nseg = int(mbins.get("nseg", 0))
            sizes: list[int] = []
            for s in range(nseg):
                name = f"{keys[i]}|s|{wid}|{s}" if wid else f"{keys[i]}|s|{s}"
                _, _, sbins = client.get((args.namespace, args.set_name, name))
                sizes.append(
                    sum(
                        len(v)
                        for v in sbins.values()
                        if isinstance(v, (bytes, bytearray))
                    )
                )
                if i == wanted[0] and s == 0:
                    seg_desc = json.dumps(bin_sizes(sbins), sort_keys=True)
                    print(f"segment 0 bins: {seg_desc}")
            total_bytes += sum(sizes)
            total_segs += nseg
            print(
                f"chunk {i}: nseg={nseg} bytes={sum(sizes)} "
                f"min={min(sizes, default=0)} max={max(sizes, default=0)} "
                f"distinct={sorted(set(sizes))[:6]}"
            )
        print(
            f"total over {len(wanted)} chunk(s): {total_segs} segments, "
            f"{total_bytes} bytes"
        )
    finally:
        client.close()


if __name__ == "__main__":
    main()
