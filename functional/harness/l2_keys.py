# SPDX-License-Identifier: Apache-2.0
"""Snapshot the record digests of an Aerospike set, delete the records that
appeared between two snapshots, or truncate the set.

T-LKP-03 needs a gap at the storage level: one chunk of a stored prompt
missing from L2 while the chunks after it are present. Chunk keys are
prefix hashes, so the gap cannot be made by choosing prompts; instead the
test stores the prompt one chunk at a time, snapshots the set between
stores, and deletes the records that the store of chunk k added (all of
them: the manifest and every shard).

Usage::

    python l2_keys.py dump --out keys_before.json
    python l2_keys.py delete keys_before.json keys_after.json
    python l2_keys.py truncate
"""

# Standard
import argparse
import json

# Third Party
import aerospike


def set_digests(host: str, port: int, namespace: str, set_name: str) -> list[str]:
    """Return the hex digest of every record in one set.

    Args:
        host: Aerospike host.
        port: Aerospike service port.
        namespace: Namespace name.
        set_name: Set name.

    Returns:
        Sorted hex digests.
    """
    client = aerospike.client({"hosts": [(host, port)]}).connect()
    digests: list[str] = []
    try:
        query = client.query(namespace, set_name)
        query.foreach(
            lambda record: digests.append(bytes(record[0][3]).hex()),
            options={"nobins": True},
        )
    finally:
        client.close()
    return sorted(digests)


def delete_digests(
    host: str, port: int, namespace: str, set_name: str, digests: list[str]
) -> int:
    """Delete the records with the given digests.

    Args:
        host: Aerospike host.
        port: Aerospike service port.
        namespace: Namespace name.
        set_name: Set name.
        digests: Hex digests to delete.

    Returns:
        The number of records deleted.

    Raises:
        aerospike.exception.RecordNotFound: A digest has no record.
    """
    client = aerospike.client({"hosts": [(host, port)]}).connect()
    try:
        for digest in digests:
            client.remove((namespace, set_name, None, bytearray.fromhex(digest)))
    finally:
        client.close()
    return len(digests)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3000)
    parser.add_argument("--namespace", default="lmcache")
    parser.add_argument("--set", dest="set_name", default="kv_chunks")
    sub = parser.add_subparsers(dest="command", required=True)
    dump = sub.add_parser("dump")
    dump.add_argument("--out", required=True)
    delete = sub.add_parser("delete")
    delete.add_argument("before")
    delete.add_argument("after")
    sub.add_parser("truncate")
    args = parser.parse_args()
    if args.command == "truncate":
        client = aerospike.client({"hosts": [(args.host, args.port)]}).connect()
        try:
            client.truncate(args.namespace, args.set_name, 0)
        finally:
            client.close()
        print(f"truncated {args.namespace}.{args.set_name}")
        return
    if args.command == "dump":
        digests = set_digests(args.host, args.port, args.namespace, args.set_name)
        with open(args.out, "w") as f:
            json.dump(digests, f)
        print(f"{len(digests)} records")
        return
    with open(args.before) as f:
        before = set(json.load(f))
    with open(args.after) as f:
        added = sorted(set(json.load(f)) - before)
    deleted = delete_digests(args.host, args.port, args.namespace, args.set_name, added)
    print(f"deleted {deleted} records added between the two snapshots")


if __name__ == "__main__":
    main()
