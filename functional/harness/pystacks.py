# SPDX-License-Identifier: Apache-2.0
"""Print every thread's Python stack of a running process (read-only).

Run from the host inside the container's namespaces, with the container's
Python 3.14 (``_remote_debugging`` must match the target's version)::

    nsenter -t <host-pid-in-container> -m -p -- \
        /root/.local/share/uv/python/cpython-3.14.7-linux-x86_64-gnu/bin/python3.14 \
        -I pystacks.py <container-pid>
"""

# Standard
import sys

# Third Party
import _remote_debugging

pid = int(sys.argv[1])
unwinder = _remote_debugging.RemoteUnwinder(pid, all_threads=True)
for interp in unwinder.get_stack_trace():
    threads = getattr(interp, "threads", None) or [interp]
    for thread in threads:
        tid = getattr(thread, "thread_id", None)
        frames = getattr(thread, "frame_info", None) or getattr(thread, "frames", [])
        print(f"--- thread {tid}")
        for frame in frames:
            print(f"    {frame}")
