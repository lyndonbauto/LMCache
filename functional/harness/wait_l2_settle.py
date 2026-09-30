# SPDX-License-Identifier: Apache-2.0
"""Wait until an Aerospike namespace's object count stops changing.

LMCache writes to L2 asynchronously after a store, so a test that restarts
the LMCache server to force an L2 hit must first wait for those writes to
land. This polls the namespace's ``objects`` statistic and returns once it
has been unchanged for ``--quiet-seconds``.

Usage::

    python wait_l2_settle.py --host 127.0.0.1 --port 3000 --namespace lmcache
"""

# Standard
import argparse
import sys
import time

# Third Party
import aerospike


def namespace_objects(client: "aerospike.Client", namespace: str) -> int:
    """Return the namespace's ``objects`` statistic on one node."""
    info = client.info_random_node(f"namespace/{namespace}")
    for field in info.split("\t")[-1].strip().split(";"):
        name, _, value = field.partition("=")
        if name == "objects":
            return int(value)
    raise RuntimeError(f"no objects statistic for namespace {namespace}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3000)
    parser.add_argument("--namespace", default="lmcache")
    parser.add_argument("--quiet-seconds", type=float, default=10.0)
    parser.add_argument("--timeout-seconds", type=float, default=900.0)
    args = parser.parse_args()
    client = aerospike.client({"hosts": [(args.host, args.port)]}).connect()
    try:
        deadline = time.monotonic() + args.timeout_seconds
        last = namespace_objects(client, args.namespace)
        last_change = time.monotonic()
        while time.monotonic() < deadline:
            time.sleep(1.0)
            current = namespace_objects(client, args.namespace)
            if current != last:
                last, last_change = current, time.monotonic()
            elif time.monotonic() - last_change >= args.quiet_seconds:
                print(f"L2 settled at {current} objects")
                return
        print(f"L2 still changing after {args.timeout_seconds} s ({last} objects)")
        sys.exit(1)
    finally:
        client.close()


if __name__ == "__main__":
    main()
