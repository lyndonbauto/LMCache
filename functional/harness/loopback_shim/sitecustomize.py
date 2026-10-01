# SPDX-License-Identifier: Apache-2.0
"""Harness shim: keep torch.distributed's rendezvous store on loopback.

vLLM's single-process executor calls ``init_process_group`` with
``tcp://<VLLM_HOST_IP>:<port>``. torch's TCPStore master ignores that host
and listens on every interface (``*:<port>``), which ufw's allow-incoming
policy on the test box exposes to the internet. When this directory is on
``PYTHONPATH`` (``loopback_env.sh`` puts it there for ``vllm serve`` only),
the rank-0 store for a loopback host is created on a socket bound to that
host and handed to TCPStore through ``master_listen_fd``.

The patch is applied when ``torch.distributed.rendezvous`` is first
imported, so processes that never import torch are not slowed down.
"""

# Standard
from collections.abc import Sequence
from datetime import timedelta
from importlib.abc import Loader, MetaPathFinder
from importlib.machinery import ModuleSpec
from types import ModuleType
import importlib.util
import ipaddress
import socket
import sys

_TARGET = "torch.distributed.rendezvous"
# TCPStore takes the fd but not the Python socket object; keep the objects
# alive so their finalizers never close a listening fd under the store.
_LISTEN_SOCKETS: list[socket.socket] = []


def _is_loopback(host: str) -> bool:
    """Return whether ``host`` is a loopback IP literal or ``localhost``.

    Args:
        host: The rendezvous host name from the init method URL.

    Returns:
        True for 127.0.0.0/8, ::1 and ``localhost``; False otherwise.
    """
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _patch(module: ModuleType) -> None:
    """Wrap ``module._create_c10d_store`` to bind rank 0 to loopback.

    Args:
        module: The just-executed ``torch.distributed.rendezvous`` module.
    """
    original = module._create_c10d_store

    def create_store(
        hostname: str,
        port: int,
        rank: int,
        world_size: int,
        timeout: timedelta,
        use_libuv: bool = True,
    ) -> object:
        if (
            rank != 0
            or not _is_loopback(hostname)
            or module._torchelastic_use_agent_store()
        ):
            return original(hostname, port, rank, world_size, timeout, use_libuv)
        family = socket.AF_INET6 if ":" in hostname else socket.AF_INET
        bind_host = "127.0.0.1" if hostname == "localhost" else hostname
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((bind_host, port))
        sock.listen(4096)
        _LISTEN_SOCKETS.append(sock)
        print(
            f"loopback_shim: TCPStore master bound to {bind_host}:{port}",
            file=sys.stderr,
            flush=True,
        )
        return module.TCPStore(
            host_name=hostname,
            port=port,
            world_size=world_size,
            is_master=True,
            timeout=timeout,
            multi_tenant=True,
            master_listen_fd=sock.fileno(),
            use_libuv=use_libuv,
        )

    module._create_c10d_store = create_store


class _PatchingLoader(Loader):
    """Run the real loader, then apply :func:`_patch`."""

    def __init__(self, inner: Loader) -> None:
        self._inner = inner

    def create_module(self, spec: ModuleSpec) -> ModuleType | None:
        return self._inner.create_module(spec)

    def exec_module(self, module: ModuleType) -> None:
        self._inner.exec_module(module)
        _patch(module)


class _Finder(MetaPathFinder):
    """Intercept the import of ``torch.distributed.rendezvous`` once."""

    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None,
        target: ModuleType | None = None,
    ) -> ModuleSpec | None:
        if fullname != _TARGET:
            return None
        sys.meta_path.remove(self)
        spec = importlib.util.find_spec(fullname)
        if spec is None or spec.loader is None:
            return spec
        spec.loader = _PatchingLoader(spec.loader)
        return spec


sys.meta_path.insert(0, _Finder())
