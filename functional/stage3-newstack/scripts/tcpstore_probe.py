# SPDX-License-Identifier: Apache-2.0
"""Check that a TCPStore master can listen on a pre-bound loopback socket.

Prints torch's ``_create_c10d_store`` source and the TCPStore signature,
then creates a master store on a socket bound to 127.0.0.1 (passed as
``master_listen_fd``) and lists the listeners on its port.
"""

# Standard
import importlib
import inspect
import socket
import subprocess

# Third Party
import torch.distributed as dist


def main() -> None:
    """Run the probe and print what it finds."""
    rendezvous = importlib.import_module("torch.distributed.rendezvous")
    print(inspect.getsource(rendezvous._create_c10d_store))
    print(dist.TCPStore.__init__.__doc__)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = sock.getsockname()[1]
    for libuv in (True, False):
        try:
            store = dist.TCPStore(
                "127.0.0.1",
                port,
                1,
                True,
                master_listen_fd=sock.fileno(),
                use_libuv=libuv,
                wait_for_workers=False,
            )
        except RuntimeError as e:
            print("libuv", libuv, "failed:", repr(e))
            continue
        store.set("a", "b")
        print("libuv", libuv, "ok", store.get("a"))
        table = subprocess.run(["ss", "-Hltn"], capture_output=True, text=True)
        print([line for line in table.stdout.splitlines() if f":{port} " in line])
        break


if __name__ == "__main__":
    main()
