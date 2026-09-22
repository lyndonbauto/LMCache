# SPDX-License-Identifier: Apache-2.0
"""Pytest wrapper for the pipelined-fetch client harness.

Build plumbing lives in ``conftest.py``. This covers the command codec and
the handling of a declined write, both of which are string and bookkeeping
work, so it needs no RDMA device. The same codec is driven over a real fabric
by ``test_rdma_pipeline.py``.
"""

# Standard
from collections.abc import Callable


def test_the_pipelined_fetch_command_and_declined_writes(
    logic_harness: Callable[[str], str],
) -> None:
    """A pipelined fetch is distinguishable, and a declined write is visible.

    The command is a separate name rather than a flag on ``kv-sink-fetch``
    because a server that does not implement it must fail outright. Were it to
    fall back to the all-or-nothing fetch, the bytes would land but carry no
    immediate data, so no notification would arrive and the client would wait
    out its deadline on data already sitting in its buffer.

    The declined-write half matters for the same reason in reverse: a slot the
    server refused and a slot still in flight look identical from the client's
    side, since both are simply an arrival that has not happened. So the reply
    names what it declined, and the tracker records it.

    The C++ harness checks, against the production codec and readiness
    tracker, that:

    - the command carries its own name, one generation for the whole command,
      and a slot index per sink,
    - a fetch with no sinks or with a repeated slot index is refused, since a
      repeat would be counted as a duplicate arrival and strand its layer,
    - a reply parses with failed slots present, empty, or absent, and a reply
      missing its counts is refused rather than read as "the server did
      nothing",
    - a layer holding a declined write is never reported ready, even once
      every write that will ever arrive has arrived, because the rest of its
      buffer holds whatever the previous tenant left there,
    - layers unaffected by the declined write still complete, so the request
      recomputes only what is missing,
    - a write that lands anyway for an already-declined slot is not counted as
      progress, and a declined slot outside the plan is ignored rather than
      crashing the client, and
    - a request-scoped plan splits into per-node sink lists that keep the
      request's slot numbering, which is what keeps two nodes' notifications
      distinguishable.

    Args:
        logic_harness: Fixture that builds and runs a named harness.
    """
    assert "PASS" in logic_harness("pipelined_fetch_test")
