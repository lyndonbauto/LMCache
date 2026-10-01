# SPDX-License-Identifier: Apache-2.0
"""Send one Aerospike info command to one node and print the reply.

Speaks the info protocol directly (no client, no cluster tending), so it
asks exactly the node named, even while the cluster is re-forming. Used by
functional/harness/kvsink_cluster.sh, whose container has no asinfo.

    python as_info.py <port> <command> [host]

Exits 1 if the node does not answer within 2 s.
"""

# Standard
import socket
import struct
import sys


def info(host: str, port: int, command: str) -> str:
    """Return the node's reply to ``command`` (name and tab stripped).

    Raises:
        OSError: if the node cannot be reached or closes the connection.
    """
    body = (command + "\n").encode()
    header = struct.pack(">Q", (2 << 56) | (1 << 48) | len(body))
    with socket.create_connection((host, port), timeout=2) as sock:
        sock.sendall(header + body)
        size = struct.unpack(">Q", _read(sock, 8))[0] & 0xFFFFFFFFFFFF
        reply = _read(sock, size).decode(errors="replace")
    _, _, value = reply.partition("\t")
    return value.strip()


def _read(sock: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise OSError("connection closed")
        data += chunk
    return data


if __name__ == "__main__":
    try:
        print(
            info(
                sys.argv[3] if len(sys.argv) > 3 else "127.0.0.1",
                int(sys.argv[1]),
                sys.argv[2],
            )
        )
    except OSError as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)
