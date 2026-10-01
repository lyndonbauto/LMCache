# SPDX-License-Identifier: Apache-2.0
"""Diagnostic: do kv-sink RDMA writes land in the L1 window after close()?

Runs one pipelined retrieve through a real StorageManager (as the
integration test does), closes the manager, and keeps the slab alive with an
extra reference so that bytes written after close() can be observed instead
of corrupting the heap. The window is filled with a sentinel right after
close(); any byte that changes afterwards was written by the server through
the client's still-registered memory region.

Usage (inside aero-kvsink, cwd = LMCache tree):
  python late_write_probe.py [hold|nohold] [watch_seconds]
"""

# Standard
import ctypes
import sys
import time
import uuid

sys.path.insert(0, ".")

# Third Party

# First Party
from lmcache.v1.distributed.l2_adapters.factory import (  # noqa: E402
    create_l2_adapter_from_registry,
)
from lmcache.v1.distributed.l2_adapters.rdma_registration import (  # noqa: E402
    L1RdmaConfig,
)
from lmcache.v1.platform import torch_ops  # noqa: E402
from tests.v1.distributed import (  # noqa: E402
    test_aerospike_pipelined_rdma_integration as T,
)
import lmcache  # noqa: E402


def maps_line(ptr: int) -> str:
    with open("/proc/self/maps") as f:
        for line in f:
            lo, hi = (int(x, 16) for x in line.split()[0].split("-"))
            if lo <= ptr < hi:
                return line.strip() + f"  (region {hi - lo} bytes)"
    return "not mapped"


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "hold"
    watch = float(sys.argv[2]) if len(sys.argv) > 2 else 10.0
    print("device_ops:", type(lmcache.device_ops).__name__, flush=True)
    set_name = f"probe_{uuid.uuid4().hex[:12]}"
    adapter = create_l2_adapter_from_registry(
        T._adapter_config(set_name, L1RdmaConfig())
    )
    adapter.set_object_group_layouts(dict(T.GROUP_LAYOUTS), T.KERNEL_LAYERS)
    for index, o in enumerate(T._objects()):
        T._store(adapter, o.key, T._payload(index, o.object_bytes))
    adapter.close()

    manager = T._build_manager(set_name)
    desc = manager._l1_memory_desc  # diagnostic only
    ptr, size = desc.ptr, desc.size
    print(f"slab ptr=0x{ptr:x} size={size}", flush=True)
    print("maps:", maps_line(ptr), flush=True)
    held = torch_ops._tensor_registry.get(ptr) if mode in ("hold", "warmhold") else None
    if mode == "warmhold":
        T._retrieve(manager)  # warm the server's stripes, then measure
        print("warm-up retrieve done", flush=True)
    print("held a reference to the slab:", held is not None, flush=True)

    t0 = time.monotonic()
    retrieved = T._retrieve(manager)
    print(
        f"retrieve: {retrieved.completion.name} in {time.monotonic() - t0:.2f}s",
        flush=True,
    )
    window = (ctypes.c_uint8 * T.WINDOW_BYTES).from_address(ptr)
    if mode == "longlived":
        # One manager survives the fallback: watch the quarantined window,
        # then retrieve again after the quarantine and check every byte.
        t_ab = time.monotonic()
        before = bytes(window)
        last_change = None
        prev = before
        while time.monotonic() - t_ab < T.FETCH_TIMEOUT + 2:
            cur = bytes(window)
            if cur != prev:
                last_change = time.monotonic() - t_ab
                prev = cur
            time.sleep(0.05)
        changed = sum(1 for a, b in zip(before, prev, strict=True) if a != b)
        print(
            f"quarantined window: {changed} bytes changed after the fallback; "
            f"last change {last_change} s after it",
            flush=True,
        )
        again = T._retrieve(manager)
        stored = {
            o.key: T._payload(i, o.object_bytes) for i, o in enumerate(T._objects())
        }
        ok = all(again.l1_bytes.get(k) == v for k, v in stored.items())
        print(
            f"second retrieve: {again.completion.name}; bytes equal: {ok}", flush=True
        )
        manager.close()
        return
    manager.close()
    t_close = time.monotonic()
    if mode == "reuse":
        # Ordinary mallocs after close(): does glibc hand out the freed slab,
        # and do the server's late writes then overwrite live allocations?
        libc = ctypes.CDLL(None)
        libc.malloc.restype = ctypes.c_void_p
        libc.malloc.argtypes = [ctypes.c_size_t]
        blocks = []
        for _ in range(2048):
            p = libc.malloc(4000)
            ctypes.memset(p, 0x5A, 4000)
            blocks.append(p)
        inside = [p for p in blocks if ptr <= p < ptr + T.WINDOW_BYTES]
        print(
            f"{len(inside)} of 2048 new malloc(4000) blocks lie in the freed "
            f"window (allocated {time.monotonic() - t_close:.3f}s after close)",
            flush=True,
        )
        time.sleep(5)
        bad = [p for p in blocks if ctypes.string_at(p, 4000) != b"\x5a" * 4000]
        print(
            f"{len(bad)} live malloc blocks overwritten after close; "
            f"{sum(1 for p in bad if p in inside)} of them in the window",
            flush=True,
        )
        return
    if held is None:
        print("closed without holding the slab; exiting", flush=True)
        return
    ctypes.memset(ptr, 0xAB, T.WINDOW_BYTES)
    first_change = None
    last = 0
    while time.monotonic() - t_close < watch:
        changed = sum(1 for b in bytes(window) if b != 0xAB)
        if changed and first_change is None:
            first_change = time.monotonic() - t_close
        last = changed
        time.sleep(0.05)
    print(
        f"after close: {last} of {T.WINDOW_BYTES} window bytes overwritten; "
        f"first change {None if first_change is None else round(first_change, 3)} "
        "s "
        "after close()",
        flush=True,
    )
    if last:
        data = bytes(window)
        stored = b"".join(
            T._payload(i, o.object_bytes) for i, o in enumerate(T._objects())
        )
        hits = sum(
            1
            for off in range(0, T.WINDOW_BYTES - 64, 64)
            if data[off : off + 64] != b"\xab" * 64 and data[off : off + 64] in stored
        )
        print(f"64-byte blocks matching stored payload bytes: {hits}", flush=True)
    del held


if __name__ == "__main__":
    main()
