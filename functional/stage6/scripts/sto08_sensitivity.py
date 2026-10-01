# SPDX-License-Identifier: Apache-2.0
"""T-STO-08 oracle sensitivity on the E4 cluster (RF 2).

After each put returns, is the cluster's replica (prole) object count already
equal to its master count? Run for commit level all (what LMCache's
connector sets) and master, with 64 KiB records, so the T-STO-08 check is
shown to tell the two apart. Runs inside lmc-c::

    python sto08_sensitivity.py [puts]
"""

# Standard
import sys
import uuid

# Third Party
import aerospike

HOSTS = [("127.0.0.1", port) for port in (3300, 3310, 3320)]


def totals(client: aerospike.Client) -> tuple[int, int]:
    """Cluster-wide (master_objects, prole_objects) in namespace lmcache."""
    master = prole = 0
    for _, (_, reply) in client.info_all("namespace/lmcache").items():
        body = reply.split("\t")[-1].strip()
        fields = dict(kv.split("=", 1) for kv in body.split(";") if "=" in kv)
        master += int(fields["master_objects"])
        prole += int(fields["prole_objects"])
    return master, prole


def main(puts: int) -> None:
    client = aerospike.client({"hosts": HOSTS}).connect()
    payload = bytearray(b"x" * 65536)
    levels = (
        ("all", aerospike.POLICY_COMMIT_LEVEL_ALL),
        ("master", aerospike.POLICY_COMMIT_LEVEL_MASTER),
    )
    try:
        for name, level in levels:
            set_name = "sens_" + uuid.uuid4().hex[:8]
            master0, prole0 = totals(client)
            lag = 0
            for i in range(puts):
                client.put(
                    ("lmcache", set_name, i),
                    {"v": payload},
                    policy={"commit_level": level},
                )
                master, prole = totals(client)
                if prole - prole0 < master - master0:
                    lag += 1
            print(
                f"commit_level={name}: {puts} puts, replica count behind "
                f"master right after the put: {lag}"
            )
            client.truncate("lmcache", set_name, 0)
    finally:
        client.close()


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 2000)
