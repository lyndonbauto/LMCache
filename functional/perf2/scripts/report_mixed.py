# SPDX-License-Identifier: Apache-2.0
"""Mixed-load trace results (LW-MIXED-LOAD-PROPOSAL.md).

Commands::

    report_mixed.py stable <result.json>       exit 1 if the trace was unstable
    report_mixed.py pick <result.json>...      print the highest stable rate
    report_mixed.py report <dir> <session>[:<R>]... [--md out.md]

``report`` reads every trace result ``<dir>/<session>/<session>_<name>.json``
and its ``reqlog_``, ``outcomes_`` and ``modes_`` files (perf_session.sh's
``trace`` step) and prints, per trace and class: TTFT p50/p90/p99, the gaps
between streamed tokens (p50/p99/p99.9 over every gap), stalls (gaps over
100 ms and 500 ms, per minute of trace, and the share of requests with one),
goodput (TTFT <= 2 s and the request's p99 gap <= 100 ms), the share of
requests whose lookup found the planned hit, and the load mode the per-request
rule gives for the found hit. ``<R>`` is the session's
``--layerwise-min-new-token-ratio`` (``aon`` for layerwise off); without it the
session name decides (``aon`` in the name: aon; ``R<x>``: x).

A trace is stable if its errors are under 1%, the p90 TTFT of the last third
of requests (by send time) is at most max(2 x, +1 s) that of the first third,
and the overall p90 TTFT is at most 10 s.
"""

# Standard
import json
import os
import re
import sys

CHUNK = 256
CLASS_ORDER = (
    "miss",
    "l1_full",
    "l1_part",
    "l2_full",
    "l2_long",
    "l2_short",
    "l2_border",
)
RID_RE = re.compile(r"session=cmpl-(mx\d+r\d+)-")
L1_HIT_METRIC = "lmcache_mp_lookup_hit_l1_tokens_total"
L2_HIT_METRIC = "lmcache_mp_lookup_hit_l2_tokens_total"


def pct(values: list[float], p: float) -> float:
    """Return the nearest-rank ``p``-th percentile, or -1.0 if empty."""
    if not values:
        return -1.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round(p / 100.0 * len(s) + 0.5)) - 1))
    return s[k]


def stability(res: dict) -> tuple[bool, str]:
    """Return whether a trace result was stable, and why.

    Args:
        res: A ``perf_client.py --trace`` result.

    Returns:
        ``(stable, reason)``.
    """
    reqs = sorted(res["requests"], key=lambda r: r["t_sched"])
    n = len(reqs)
    errs = sum(1 for r in reqs if r["error"])
    if n == 0:
        return False, "no requests"
    if errs > 0.01 * n:
        return False, f"{errs}/{n} errors"
    ok = [r for r in reqs if not r["error"]]
    third = max(1, len(ok) // 3)
    first = pct([r["ttft_s"] for r in ok[:third]], 90)
    last = pct([r["ttft_s"] for r in ok[-third:]], 90)
    allp = pct([r["ttft_s"] for r in ok], 90)
    why = f"ttft p90 first third {first:.2f}s last third {last:.2f}s all {allp:.2f}s"
    if last > max(2 * first, first + 1.0):
        return False, "growing: " + why
    if allp > 10.0:
        return False, "too slow: " + why
    return True, why


def read_reqlog(path: str) -> tuple[dict[str, int], dict[str, str]]:
    """Return the last lookup ``found_count`` and retrieve outcome per request id."""
    found: dict[str, int] = {}
    outcome: dict[str, str] = {}
    if not os.path.exists(path):
        return found, outcome
    with open(path) as f:
        for line in f:
            m = RID_RE.search(line)
            if not m:
                continue
            rid = m.group(1)
            if "found_count=" in line:
                found[rid] = int(line.rsplit("found_count=", 1)[1])
            elif "pipelined_outcome=" in line:
                outcome[rid] = line.rsplit("pipelined_outcome=", 1)[1].strip()
    return found, outcome


def mode_for(ratio: str, prompt_tokens: int, found: int) -> str:
    """Return the load mode the server's rule gives (``-`` with no hit).

    Args:
        ratio: ``aon`` or the session's R.
        prompt_tokens: The prompt's length.
        found: Chunks the lookup found.

    Returns:
        ``-``, ``async`` or ``lw``.
    """
    if found <= 0:
        return "-"
    if ratio == "aon":
        return "async"
    chunks = max(prompt_tokens // CHUNK, found)
    return "lw" if chunks - found >= float(ratio) * found else "async"


def session_ratio(spec: str) -> tuple[str, str]:
    """Split ``<session>[:<R>]`` into the session and its ratio."""
    if ":" in spec:
        s, r = spec.split(":", 1)
        return s, r
    if "aon" in spec:
        return spec, "aon"
    m = re.search(r"R([0-9.]+)", spec)
    if not m:
        raise ValueError(f"cannot tell R for {spec}; give <session>:<R>")
    return spec, m.group(1)


def class_row(reqs: list[dict], minutes: float, found: dict, ratio: str) -> list[str]:
    """Return the table cells for one class of one trace."""
    ok = [r for r in reqs if not r["error"]]
    gaps = [g for r in ok for g in r["gaps_ms"]]
    ttft = [r["ttft_s"] for r in ok]
    s100 = sum(1 for g in gaps if g > 100)
    s500 = sum(1 for g in gaps if g > 500)
    stalled = sum(1 for r in ok if r["gaps_ms"] and max(r["gaps_ms"]) > 100)
    good = sum(
        1
        for r in ok
        if r["ttft_s"] <= 2.0 and (not r["gaps_ms"] or pct(r["gaps_ms"], 99) <= 100)
    )
    known = [r for r in reqs if r["rid"] in found]
    hit_ok = sum(1 for r in known if found[r["rid"]] >= r["hit_tokens"] // CHUNK)
    modes: dict[str, int] = {}
    for r in known:
        m = mode_for(ratio, r["prompt_tokens"], found[r["rid"]])
        modes[m] = modes.get(m, 0) + 1
    mtxt = " ".join(f"{k}:{v}" for k, v in sorted(modes.items()))
    n = len(reqs)
    return [
        str(n),
        str(n - len(ok)),
        f"{pct(ttft, 50):.2f}",
        f"{pct(ttft, 90):.2f}",
        f"{pct(ttft, 99):.2f}",
        f"{pct(gaps, 50):.1f}",
        f"{pct(gaps, 99):.0f}",
        f"{pct(gaps, 99.9):.0f}",
        f"{s100 / minutes:.1f}",
        f"{s500 / minutes:.1f}",
        f"{100 * stalled / max(1, len(ok)):.0f}",
        f"{100 * good / max(1, n):.0f}",
        f"{100 * hit_ok / len(known):.0f}" if known else "n/a",
        mtxt or "n/a",
    ]


def read_text(path: str) -> str:
    """Return a file's text, stripped, or ``n/a``."""
    if not os.path.exists(path):
        return "n/a"
    with open(path) as f:
        return " ".join(f.read().split())


def trace_results(d: str, session: str) -> list[tuple[str, dict]]:
    """Return ``(name, result)`` for every trace (not store) result of a session."""
    sdir = os.path.join(d, session)
    out = []
    for fn in sorted(os.listdir(sdir)):
        if not (fn.startswith(session + "_") and fn.endswith(".json")):
            continue
        with open(os.path.join(sdir, fn)) as f:
            res = json.load(f)
        if res.get("kind") == "trace":
            out.append((fn[len(session) + 1 : -5], res))
    return out


def report(d: str, specs: list[str]) -> str:
    """Return the markdown report for the given sessions."""
    head = (
        "| config | class | n | err | TTFT p50 | p90 | p99 | gap p50 ms | p99 | "
        "p99.9 | >100ms /min | >500ms /min | req stalled % | goodput % | "
        "hit found % | mode (rule) |"
    )
    sep = "|" + "---|" * 16
    by_name: dict[str, list[tuple[str, str, dict]]] = {}
    for spec in specs:
        session, ratio = session_ratio(spec)
        for name, res in trace_results(d, session):
            by_name.setdefault(name, []).append((session, ratio, res))
    lines: list[str] = []
    for name in sorted(by_name):
        lines.append(f"## {name}\n")
        lines.append(
            "| config | n | err | wall s | out tok/s | send lag p99 s | stable | "
            "L1 hit tok (plan) | L2 hit tok (plan) | evicted chunks | preempt | "
            "outcomes | vLLM modes |"
        )
        lines.append("|" + "---|" * 13)
        for session, ratio, res in by_name[name]:
            reqs = res["requests"]
            md = res["metrics_delta"]
            ok = [r for r in reqs if not r["error"]]
            plan1 = sum(r["hit_tokens"] for r in reqs if r["src"] == "l1")
            plan2 = sum(r["hit_tokens"] for r in reqs if r["src"] == "l2")
            l1 = sum(v for k, v in md.items() if k.startswith(L1_HIT_METRIC))
            l2 = sum(v for k, v in md.items() if k.startswith(L2_HIT_METRIC))
            ev = md.get("lmcache_mp_l1_evicted_chunks_total", 0)
            pre = md.get("vllm:num_preemptions_total", 0)
            st, why = stability(res)
            sdir = os.path.join(d, session)
            lines.append(
                f"| {session} | {len(reqs)} | {len(reqs) - len(ok)} | "
                f"{res['wall_s']:.0f} | "
                f"{sum(r['out_tokens'] for r in ok) / res['wall_s']:.0f} | "
                f"{pct([r['send_lag_s'] for r in reqs], 99):.2f} | "
                f"{'yes' if st else 'NO'} ({why}) | {l1:.0f} ({plan1}) | "
                f"{l2:.0f} ({plan2}) | {ev:.0f} | {pre:.0f} | "
                f"{read_text(os.path.join(sdir, f'outcomes_{session}_{name}.txt'))} | "
                f"{read_text(os.path.join(sdir, f'modes_{session}_{name}.txt'))} |"
            )
        lines.append("")
        lines.append(head)
        lines.append(sep)
        for cls in CLASS_ORDER:
            for session, ratio, res in by_name[name]:
                reqs = [r for r in res["requests"] if r["cls"] == cls]
                if not reqs:
                    continue
                found, _ = read_reqlog(
                    os.path.join(d, session, f"reqlog_{session}_{name}.txt")
                )
                cells = class_row(reqs, res["wall_s"] / 60.0, found, ratio)
                lines.append(f"| {session} | {cls} | " + " | ".join(cells) + " |")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    """Dispatch the command; see the module docstring."""
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "stable":
        with open(args[0]) as f:
            st, why = stability(json.load(f))
        print(f"{'stable' if st else 'UNSTABLE'}: {why}")
        return 0 if st else 1
    if cmd == "pick":
        best = 0.0
        for p in args:
            with open(p) as f:
                res = json.load(f)
            st, why = stability(res)
            print(f"# rate {res['rate']}: {'stable' if st else 'UNSTABLE'} ({why})")
            if st:
                best = max(best, float(res["rate"]))
        print(best)
        return 0 if best > 0 else 1
    if cmd == "report":
        md = ""
        if "--md" in args:
            i = args.index("--md")
            md = args[i + 1]
            args = args[:i] + args[i + 2 :]
        text = report(args[0], args[1:])
        if md:
            with open(md, "w") as f:
                f.write(text + "\n")
        print(text)
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
