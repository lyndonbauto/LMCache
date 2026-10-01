# SPDX-License-Identifier: Apache-2.0
"""Warm every node of the kv-sink cluster (server issue 5).

A kv-sink node registers its memory stripes with the RDMA device on its
first fetch, which can outlast a client's info timeout. This stores one
64 KiB record per node, on a key whose master partition that node owns, and
fetches it once from that node with the server branch's
``examples/kv-sink/kvsink_client`` (``kv-sink-fetch``, RC on rxe0 GID 1).
Runs inside aero-kvsink::

    python kvsink_warm.py 3400 3410 3420

Exits non-zero if any node's fetch fails.
"""

# Standard
from pathlib import Path
import base64
import subprocess
import sys

# Third Party
from as_info import info
import aerospike

SRV = Path("/root/lmc-work/aerospike-server-kvsink")
BUILD = Path("/root/lmc-work/aero-cluster/kvsink/kvsink_client_build")
NAMESPACE = "lmcache"
PAYLOAD = bytes((b"kv-sink warm " * 5100)[:65536])


def client_for(port: int) -> Path:
    """The example client built with its compiled-in service port = port."""
    binary = BUILD / f"kvsink_client_{port}"
    if binary.exists():
        return binary
    BUILD.mkdir(parents=True, exist_ok=True)
    source = (SRV / "examples/kv-sink/kvsink_client.c").read_text()
    patched = source.replace(
        "#define SERVICE_PORT 3000\n", f"#define SERVICE_PORT {port}\n"
    )
    if patched == source:
        raise RuntimeError("SERVICE_PORT define not found in kvsink_client.c")
    c_file = BUILD / f"kvsink_client_{port}.c"
    c_file.write_text(patched)
    subprocess.run(
        ["cc", "-O2", "-std=gnu11", "-o", str(binary), str(c_file)]
        + ["-libverbs", "-lefa"],
        check=True,
    )
    return binary


def master_partitions(port: int) -> bytes:
    """The node's master-partition bitmap for the namespace (4096 bits)."""
    for entry in info("127.0.0.1", port, "replicas").split(";"):
        name, _, rest = entry.partition(":")
        if name == NAMESPACE:
            fields = rest.split(",")
            return base64.b64decode(fields[2])
    raise RuntimeError(f"node {port} reports no partitions for {NAMESPACE}")


def partition_of(digest: bytes) -> int:
    return (digest[0] | (digest[1] << 8)) & 0x0FFF


def main(ports: list[int]) -> int:
    seeds = [("127.0.0.1", p) for p in ports]
    client = aerospike.client({"hosts": seeds}).connect()
    failures = 0
    try:
        for port in ports:
            bitmap = master_partitions(port)
            for i in range(10000):
                key = (NAMESPACE, "kvsink_warm", f"warm-{port}-{i}")
                digest = aerospike.calc_digest(*key)
                pid = partition_of(digest)
                if bitmap[pid >> 3] & (0x80 >> (pid & 7)):
                    break
            else:
                raise RuntimeError(f"no key mastered by node {port}")
            client.put(key, {"v": bytearray(PAYLOAD)})
            run = subprocess.run(
                [
                    str(client_for(port)),
                    "--host",
                    "127.0.0.1",
                    "--ns",
                    NAMESPACE,
                    "--digest",
                    digest.hex(),
                    "--len",
                    str(len(PAYLOAD)),
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            ok = run.returncode == 0 and "PASS" in run.stdout
            failures += not ok
            last = (run.stdout.strip().splitlines() or [run.stderr.strip()])[-1]
            verdict = "ok" if ok else "FAILED"
            print(f"warm node {port} partition {pid}: {verdict} ({last})")
    finally:
        client.close()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main([int(p) for p in sys.argv[1:]]))
