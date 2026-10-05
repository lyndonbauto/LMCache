# SPDX-License-Identifier: Apache-2.0
"""Summarize a kv-sink server op trace (perf2 follow-up D, trace.sh).

Each trace line is one finished placement: region, seq, priority (the layer's
position in its retrieve), off, len, rc, then the ns timestamps submit, pick
(scheduler admitted it), place (a placement thread started), post (the RDMA
write was posted) and complete (the completion was reaped), and the ops and
bytes in flight right after the pick.

Retrieves are split per region at submit gaps of more than GAP_MS, which
holds at c=1, where each request decodes before the next retrieve. For each
run the
script prints the median retrieve's rate and where its ops spent their time:

  sched wait   submit -> pick      queued on the region, over the budget
  pool wait    pick -> place       waiting for a placement thread
  place        place -> post       record read, copy to staging, post
  wire         post -> complete    RDMA write until its completion is reaped

plus time-weighted bytes in flight (pick -> complete) and queued
(submit -> pick), and the share of the retrieve with no op of it on the
server at all (the client had not asked yet).

Usage: trace_analyze.py <run-dir>...   (each holding asd_trace.txt)
"""

# Standard
from dataclasses import dataclass
from pathlib import Path
import statistics
import sys

GAP_MS = 200.0
MIB = 1024 * 1024


@dataclass(frozen=True)
class Op:
    """One traced placement."""

    region: int
    priority: int
    length: int
    t_submit: int
    t_pick: int
    t_place: int
    t_post: int
    t_complete: int


@dataclass(frozen=True)
class RetrieveStats:
    """Where one retrieve's time went."""

    n_ops: int
    duration_ms: float
    gib_per_s: float
    sched_wait_ms: float
    pool_wait_ms: float
    place_ms: float
    wire_ms: float
    in_flight_ops: float
    in_flight_mib: float
    queued_mib: float
    idle_share: float


def read_ops(path: Path) -> list[Op]:
    """Read the successful, completed ops of one trace file.

    Args:
        path: An asd_trace.txt written by the trace patch.

    Returns:
        The ops with rc 0 and every timestamp set.
    """
    ops = []
    for line in path.read_text().splitlines()[1:]:
        f = line.split()
        if len(f) != 13 or f[5] != "0":
            continue
        t = [int(x) for x in f[6:11]]
        if 0 in t:
            continue
        ops.append(Op(int(f[0]), int(f[2]), int(f[4]), *t))
    return ops


def split_retrieves(ops: list[Op]) -> list[list[Op]]:
    """Group ops into retrieves: per region, split at submit gaps > GAP_MS.

    Layer position alone does not split them: the client's batch workers
    submit their first layers together, in no fixed order.

    Args:
        ops: Ops from one trace.

    Returns:
        One list of ops per retrieve, in submit order.
    """
    out: list[list[Op]] = []
    for region in sorted({o.region for o in ops}):
        cur: list[Op] = []
        for o in sorted(
            (o for o in ops if o.region == region), key=lambda o: o.t_submit
        ):
            if cur and (o.t_submit - cur[-1].t_submit) / 1e6 > GAP_MS:
                out.append(cur)
                cur = []
            cur.append(o)
        if cur:
            out.append(cur)
    return out


def _union_ns(intervals: list[tuple[int, int]]) -> int:
    total, end = 0, -1
    for a, b in sorted(intervals):
        if a > end:
            total += b - a
            end = b
        elif b > end:
            total += b - end
            end = b
    return total


def _mib_per_ns(spans: list[tuple[int, int]]) -> float:
    return sum(length * ns for length, ns in spans) / MIB


def retrieve_stats(ops: list[Op]) -> RetrieveStats:
    """Compute one retrieve's rate and time breakdown.

    Args:
        ops: The retrieve's ops.

    Returns:
        The retrieve's stats; per-op stage times are medians.
    """
    start = min(o.t_submit for o in ops)
    end = max(o.t_complete for o in ops)
    dur = max(end - start, 1)
    total = sum(o.length for o in ops)
    busy = _union_ns([(o.t_submit, o.t_complete) for o in ops])

    def med(xs: list[int]) -> float:
        return statistics.median(xs) / 1e6

    return RetrieveStats(
        n_ops=len(ops),
        duration_ms=dur / 1e6,
        gib_per_s=total / (dur / 1e9) / (1024 * MIB),
        sched_wait_ms=med([o.t_pick - o.t_submit for o in ops]),
        pool_wait_ms=med([o.t_place - o.t_pick for o in ops]),
        place_ms=med([o.t_post - o.t_place for o in ops]),
        wire_ms=med([o.t_complete - o.t_post for o in ops]),
        in_flight_ops=sum(o.t_complete - o.t_pick for o in ops) / dur,
        in_flight_mib=_mib_per_ns([(o.length, o.t_complete - o.t_pick) for o in ops])
        / dur,
        queued_mib=_mib_per_ns([(o.length, o.t_pick - o.t_submit) for o in ops]) / dur,
        idle_share=1 - busy / dur,
    )


def main(run_dirs: list[str]) -> None:
    """Print one line per run: the median retrieve by duration.

    Args:
        run_dirs: Directories holding asd_trace.txt.
    """
    print(
        "run | retrieves | ops | ms | GiB/s | sched wait | pool wait | place "
        "| wire (ms, median op) | in flight ops | in flight MiB | queued MiB "
        "| idle"
    )
    for d in run_dirs:
        retrieves = [
            r
            for r in split_retrieves(read_ops(Path(d) / "asd_trace.txt"))
            if len(r) >= 64
        ]
        if not retrieves:
            print(f"{Path(d).name} | no retrieves")
            continue
        stats = sorted(
            (retrieve_stats(r) for r in retrieves), key=lambda s: s.duration_ms
        )
        s = stats[len(stats) // 2]
        cells = [
            Path(d).name,
            str(len(stats)),
            str(s.n_ops),
            f"{s.duration_ms:.1f}",
            f"{s.gib_per_s:.2f}",
            f"{s.sched_wait_ms:.2f}",
            f"{s.pool_wait_ms:.2f}",
            f"{s.place_ms:.2f}",
            f"{s.wire_ms:.2f}",
            f"{s.in_flight_ops:.1f}",
            f"{s.in_flight_mib:.1f}",
            f"{s.queued_mib:.1f}",
            f"{s.idle_share:.0%}",
        ]
        print(" | ".join(cells))


if __name__ == "__main__":
    main(sys.argv[1:])
