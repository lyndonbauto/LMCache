# SPDX-License-Identifier: Apache-2.0
"""Dump an Aerospike namespace's statistics as JSON, or diff two dumps.

A test that must not touch L2 (T-E2E-01) snapshots the namespace before and
after a send and checks that no read, write or batch counter moved; a test
that must hit L2 (T-E2E-03) checks that reads did.

Usage::

    python l2_stats.py dump --namespace lmcache --out before.json
    python l2_stats.py diff before.json after.json
"""

# Standard
import argparse
import json

# Third Party
import aerospike

# Counter prefixes that move only when a client reads, writes, deletes or
# batch-reads records; uptime-style gauges are left out of the diff.
TRAFFIC_PREFIXES = ("client_", "batch_sub_", "from_proxy_", "xdr_", "udf_")


def namespace_stats(host: str, port: int, namespace: str) -> dict[str, str]:
    """Return every ``namespace/<ns>`` statistic of one node as strings.

    Args:
        host: Aerospike host.
        port: Aerospike service port.
        namespace: Namespace name.

    Returns:
        ``{statistic: value}`` exactly as the server reports them.
    """
    client = aerospike.client({"hosts": [(host, port)]}).connect()
    try:
        info = client.info_random_node(f"namespace/{namespace}")
    finally:
        client.close()
    stats: dict[str, str] = {}
    for field in info.split("\t")[-1].strip().split(";"):
        name, _, value = field.partition("=")
        if name:
            stats[name] = value
    return stats


def traffic_diff(before: dict[str, str], after: dict[str, str]) -> dict[str, int]:
    """Return the non-zero growth of every integer traffic counter.

    Args:
        before: A ``namespace_stats`` dump.
        after: A later dump of the same namespace.

    Returns:
        ``{statistic: after - before}`` for traffic counters that changed.
    """
    diff: dict[str, int] = {}
    for name, value in after.items():
        if not name.startswith(TRAFFIC_PREFIXES):
            continue
        if not (value.isdigit() and before.get(name, "0").isdigit()):
            continue
        delta = int(value) - int(before.get(name, "0"))
        if delta:
            diff[name] = delta
    return diff


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    dump = sub.add_parser("dump")
    dump.add_argument("--host", default="127.0.0.1")
    dump.add_argument("--port", type=int, default=3000)
    dump.add_argument("--namespace", default="lmcache")
    dump.add_argument("--out", required=True)
    diff = sub.add_parser("diff")
    diff.add_argument("before")
    diff.add_argument("after")
    args = parser.parse_args()
    if args.command == "dump":
        with open(args.out, "w") as f:
            json.dump(namespace_stats(args.host, args.port, args.namespace), f)
        return
    with open(args.before) as f:
        before = json.load(f)
    with open(args.after) as f:
        after = json.load(f)
    print(json.dumps(traffic_diff(before, after), sort_keys=True))


if __name__ == "__main__":
    main()
