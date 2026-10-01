# SPDX-License-Identifier: Apache-2.0
"""Check that the L2 objects of stored prompts are whole, and find orphans.

Since D-14 every store names its segments by its own write ID
(``<key>|s|<wid>|<i>``), the meta record ``<key>|m`` carries that ID in its
``wid`` bin and is created only once: the first writer wins and a loser
deletes its own segments. When two LMCache hosts store the same prefix at
once (T-SHR-02), the result is right only if, for every chunk:

- the meta record exists and every segment it names (``nseg`` of them,
  under its ``wid``) exists: the object is whole and came from one writer;
- no other record is left behind: the loser removed its segments.

The second point is checked against the whole set: its digests (a scan)
must be exactly the meta and segment records of the prompts named here,
so run it on a set that holds only those prompts (a fresh server per
group). Prompts that are exact multiples of 256 tokens (P-exact) are the
safe choice, because vLLM also stores chunks completed by generated tokens
(D-09), and those keys cannot be derived from the prompt alone.

T-EVT-06 uses ``present`` mode: per prompt, how many of its chunks are
whole, without the orphan check.

Usage::

    python l2_integrity.py --port 3100 --corpus corpus.json \\
        --model-url http://localhost:8000 whole --ids P-exact-10,P-exact-11
    python l2_integrity.py ... present --ids P-exact-00,P-ragged-03

Prints one line per prompt and a summary line starting with ``PASS`` or
``FAIL``; exits 1 on ``FAIL``. ``whole`` fails on any missing or partial
object and on any orphan record; ``present`` fails on any chunk that is not
whole.
"""

# Standard
import argparse
import json
import sys

# Third Party
import aerospike

# Local
from l2_keys import set_digests
from l2_segments import candidate_names, chunk_keys


def object_records(
    client: "aerospike.Client", namespace: str, set_name: str, key: str
) -> tuple[str, list[str], int]:
    """Return the state of one chunk object.

    Args:
        client: Connected Aerospike client.
        namespace: Namespace name.
        set_name: Set name.
        key: The chunk's object key string (without ``|m``).

    Returns:
        ``(state, digests, missing)``: ``state`` is ``absent`` (no meta
        record), ``whole`` or ``partial``; ``digests`` are the hex digests
        of the meta record and of every segment it names; ``missing`` is
        how many of those segments do not exist.
    """
    meta = (namespace, set_name, key + "|m")
    _, record_meta = client.exists(meta)
    if record_meta is None:
        return "absent", [], 0
    _, _, bins = client.select(meta, ["wid", "nseg"])
    wid, nseg = bins.get("wid", ""), int(bins.get("nseg", 0))
    digests = [aerospike.calc_digest(namespace, set_name, key + "|m").hex()]
    missing = 0
    for i in range(nseg):
        name = f"{key}|s|{wid}|{i}" if wid else f"{key}|s|{i}"
        digests.append(aerospike.calc_digest(namespace, set_name, name).hex())
        _, record_seg = client.exists((namespace, set_name, name))
        if record_seg is None:
            missing += 1
    return ("partial" if missing else "whole"), digests, missing


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3000)
    parser.add_argument("--namespace", default="lmcache")
    parser.add_argument("--set", dest="set_name", default="kv_chunks")
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--model-url", default="")
    parser.add_argument("command", choices=["whole", "present"])
    parser.add_argument("--ids", required=True, help="comma-separated prompt ids")
    args = parser.parse_args()
    with open(args.corpus) as f:
        corpus = json.load(f)
    prompts = {p["id"]: p for s in corpus["sets"].values() for p in s}
    ids = [i for i in args.ids.split(",") if i]

    client = aerospike.client({"hosts": [(args.host, args.port)]}).connect()
    expected: set[str] = set()
    bad = 0
    try:
        names = candidate_names(args.model_url)
        chosen = ""
        for name in names:
            for pid in ids:
                keys = chunk_keys(name, prompts[pid]["token_ids"])
                if keys and client.exists(
                    (args.namespace, args.set_name, keys[0] + "|m")
                )[1]:
                    chosen = name
                    break
            if chosen:
                break
        if not chosen:
            print(f"FAIL: no chunk 0 of {args.ids} stored under any of {names}")
            sys.exit(1)
        seen: set[str] = set()
        for pid in ids:
            counts = {"whole": 0, "partial": 0, "absent": 0}
            missing_segments = 0
            keys = chunk_keys(chosen, prompts[pid]["token_ids"])
            for key in keys:
                state, digests, missing = object_records(
                    client, args.namespace, args.set_name, key
                )
                counts[state] += 1
                missing_segments += missing
                if key not in seen:
                    seen.add(key)
                    expected.update(digests)
            bad += counts["partial"] + counts["absent"]
            print(
                f"{pid}: chunks={len(keys)} whole={counts['whole']} "
                f"partial={counts['partial']} (missing segments {missing_segments}) "
                f"absent={counts['absent']}"
            )
    finally:
        client.close()

    if args.command == "present":
        verdict = "PASS" if bad == 0 else "FAIL"
        print(f"{verdict}: {bad} chunk object(s) not whole, model name {chosen}")
        sys.exit(0 if bad == 0 else 1)
    present = set(set_digests(args.host, args.port, args.namespace, args.set_name))
    orphans = len(present - expected)
    lost = len(expected - present)
    ok = bad == 0 and orphans == 0 and lost == 0
    print(
        f"{'PASS' if ok else 'FAIL'}: {len(seen)} chunk objects, {bad} not whole, "
        f"set holds {len(present)} records, {len(expected)} expected, "
        f"{orphans} orphan(s), {lost} expected record(s) missing, model name {chosen}"
    )
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
