# SPDX-License-Identifier: Apache-2.0
"""Collect the perf sweep's point files into results.csv and markdown tables.

Reads, under ``--dir`` (``/root/lmc-work/functional/perf`` on the box):

- ``nocache/nocache_L<len>_c<c>.json``          (mode nocache)
- ``L<len>_aon/L<len>_aon_L<len>_c<c>.json``    (mode aon)
- ``L<len>_lw/L<len>_lw_L<len>_c<c>.json``      (mode lw: connector defaults)
- ``L<len>_lwwait<secs>/...``                   (mode lw_wait<secs>: layerwise
  wait timeout raised to <secs>; a bare ``L<len>_lwwait`` is phase 1's 600 s)
- ``<session>/outcomes_<session>_L<len>_c<c>.txt`` (``outcome:count,...``)
- ``<session>/engine_stopped_<session>.txt`` (``c=<c> ...``): the session
  stopped after vLLM's engine stopped at that concurrency

and writes ``results.csv`` (one row per point) and ``summary_tables.md``
(per length: rows = concurrency, columns = mode x {TTFT p50, total p50},
plus each cached mode's TTFT speedup over nocache).

A point is INVALID (``valid=0``, reason in ``note``) when a request failed,
a request generated other than ``max_tokens`` tokens, or (cached modes) vLLM's
external prefix cache hit tokens are under 95% of ``n * (length - 1)`` (vLLM
always computes the last prompt token itself). Concurrencies after an engine
stop get a row with empty metrics, ``valid=0`` and a "not run" note.

Percentiles are NumPy's linear interpolation over the point's requests.

Usage::

    python aggregate.py --dir /root/lmc-work/functional/perf
"""

# Standard
import argparse
import csv
import json
import os
import re

# Third Party
import numpy as np

CONCS = (1, 2, 4, 8, 16, 32)
FIELDS = [
    "length",
    "concurrency",
    "mode",
    "n",
    "ttft_mean",
    "ttft_p50",
    "ttft_p90",
    "total_mean",
    "total_p50",
    "total_p90",
    "req_s",
    "out_tok_s",
    "hit_tokens",
    "expected_hit_tokens",
    "outcome_mix",
    "valid",
    "note",
]
POINT_RE = re.compile(r"_L(\d+)_c(\d+)\.json$")
SESSION_RE = re.compile(r"L(\d+)_(aon|lw|lwwait)(\d*)(?:_qp(\d+))?")


def mode_key(mode: str) -> tuple[int, int, int]:
    """Sort key: nocache, aon, lw, then lw_wait<secs> by wait; then by _qp<n>."""
    base, _, qp = mode.partition("_qp")
    q = int(qp) if qp else 0
    if base.startswith("lw_wait"):
        return (3, int(base[len("lw_wait") :]), q)
    return ({"nocache": 0, "aon": 1, "lw": 2}[base], 0, q)


def session_mode(sess: str) -> str:
    """Return the mode of a session directory name, or "" if it is not one.

    A ``_qp<n>`` suffix (perf2's queue-pair scan) is kept on the mode:
    ``L8192_lw_qp8`` is mode ``lw_qp8``.
    """
    if sess == "nocache":
        return "nocache"
    m = SESSION_RE.fullmatch(sess)
    if not m:
        return ""
    qp = f"_qp{m.group(4)}" if m.group(4) else ""
    if m.group(2) == "lwwait":
        return f"lw_wait{m.group(3) or 600}{qp}"
    return "" if m.group(3) else m.group(2) + qp


def point_files(root: str) -> list[tuple[str, str, str]]:
    """Return ``(mode, session_dir, json_path)`` for every point file found."""
    out: list[tuple[str, str, str]] = []
    for sess in sorted(os.listdir(root)):
        d = os.path.join(root, sess)
        mode = session_mode(sess)
        if not os.path.isdir(d) or not mode:
            continue
        for f in sorted(os.listdir(d)):
            if f.startswith(sess + "_L") and POINT_RE.search(f):
                out.append((mode, d, os.path.join(d, f)))
    return out


def not_run_rows(root: str, rows: list[dict]) -> list[dict]:
    """Return rows for concurrencies skipped after an engine stop.

    Args:
        root: The results directory.
        rows: The measured rows (to skip concurrencies that have a point).

    Returns:
        One row per skipped (length, concurrency, mode), metrics empty.
    """
    have = {(r["length"], r["concurrency"], r["mode"]) for r in rows}
    out: list[dict] = []
    for sess in sorted(os.listdir(root)):
        mode = session_mode(sess)
        f = os.path.join(root, sess, f"engine_stopped_{sess}.txt")
        if not mode or not os.path.exists(f):
            continue
        with open(f) as fh:
            stop_c = int(re.search(r"c=(\d+)", fh.read()).group(1))
        length = int(sess.split("_")[0][1:])
        lw_default = mode.partition("_qp")[0] == "lw"
        wait = "default 5 s wait" if lw_default else f"{mode} wait"
        for c in CONCS:
            if c > stop_c and (length, c, mode) not in have:
                row: dict = {k: "" for k in FIELDS}
                row.update(
                    length=length,
                    concurrency=c,
                    mode=mode,
                    valid=0,
                    note=f"not run: engine stopped at c={stop_c} ({wait})",
                )
                out.append(row)
    return out


def summarize(mode: str, sess_dir: str, path: str) -> dict[str, str | int | float]:
    """Return one results.csv row for a point file.

    Args:
        mode: nocache, aon, lw or lw_wait<secs>.
        sess_dir: The session directory holding the point's outcome file.
        path: The point's JSON (perf_client.py output).

    Returns:
        A dict keyed by ``FIELDS``.
    """
    with open(path) as f:
        p = json.load(f)
    reqs = p["requests"]
    ok = [r for r in reqs if not r["error"]]
    notes: list[str] = []
    valid = True
    if len(ok) < len(reqs):
        valid = False
        first_err = next(r["error"] for r in reqs if r["error"])
        notes.append(f"{len(reqs) - len(ok)} request errors ({first_err[:80]})")
    bad_len = [r for r in ok if r["out_tokens"] != p["max_tokens"]]
    if bad_len:
        valid = False
        notes.append(f"{len(bad_len)} requests not {p['max_tokens']} tokens")
    ttft = np.array([r["ttft_s"] for r in ok]) if ok else np.array([np.nan])
    total = np.array([r["total_s"] for r in ok]) if ok else np.array([np.nan])
    delta = p.get("metrics_delta", {})
    hit = int(delta.get("vllm:external_prefix_cache_hits_total", 0))
    length, n = p["length"], p["n"]
    expected = 0 if mode == "nocache" else n * (length - 1)
    if mode != "nocache" and hit < 0.95 * expected:
        valid = False
        notes.append(f"INVALID: hit tokens {hit} < 95% of {expected}")
    if mode == "nocache" and delta.get("vllm:prefix_cache_hits_total", 0) > 0:
        notes.append("vLLM prefix cache hits > 0")
    if n < max(4, p["concurrency"]):
        notes.append(f"n capped at {n}")
    sess = os.path.basename(sess_dir)
    mix = ""
    of = os.path.join(sess_dir, f"outcomes_{sess}_L{length}_c{p['concurrency']}.txt")
    if os.path.exists(of):
        with open(of) as f:
            mix = f.read().strip()
    out_tokens = sum(r["out_tokens"] for r in ok)
    return {
        "length": length,
        "concurrency": p["concurrency"],
        "mode": mode,
        "n": n,
        "ttft_mean": round(float(np.mean(ttft)), 4),
        "ttft_p50": round(float(np.percentile(ttft, 50)), 4),
        "ttft_p90": round(float(np.percentile(ttft, 90)), 4),
        "total_mean": round(float(np.mean(total)), 4),
        "total_p50": round(float(np.percentile(total, 50)), 4),
        "total_p90": round(float(np.percentile(total, 90)), 4),
        "req_s": round(len(ok) / p["wall_s"], 4),
        "out_tok_s": round(out_tokens / p["wall_s"], 2),
        "hit_tokens": hit,
        "expected_hit_tokens": expected,
        "outcome_mix": mix.replace(",", " "),
        "valid": int(valid),
        "note": "; ".join(notes),
    }


def tables(rows: list[dict]) -> str:
    """Return the per-length markdown tables (TTFT and total p50, speedups)."""
    by = {(r["length"], r["concurrency"], r["mode"]): r for r in rows}
    out: list[str] = []
    for length in sorted({r["length"] for r in rows}):
        out.append(f"### {length} tokens\n")
        modes = sorted({r["mode"] for r in rows if r["length"] == length}, key=mode_key)
        head = ["c"] + [f"{m} TTFT" for m in modes] + [f"{m} total" for m in modes]
        head += [f"{m} TTFT speedup" for m in modes if m != "nocache"]
        out.append("| " + " | ".join(head) + " |")
        out.append("|" + "---|" * len(head))
        for c in sorted({r["concurrency"] for r in rows if r["length"] == length}):
            cells = [str(c)]
            for key in ("ttft_p50", "total_p50"):
                for m in modes:
                    r = by.get((length, c, m))
                    if r is None:
                        cells.append("-")
                    elif r[key] == "":
                        cells.append("not run")
                    else:
                        cells.append(
                            f"{r[key]:.3f}" + ("" if r["valid"] else " (INVALID)")
                        )
            base = by.get((length, c, "nocache"))
            for m in [m for m in modes if m != "nocache"]:
                r = by.get((length, c, m))
                if base is None or r is None or not r["valid"] or not base["valid"]:
                    cells.append("-")
                else:
                    cells.append(f"{base['ttft_p50'] / r['ttft_p50']:.2f}x")
            out.append("| " + " | ".join(cells) + " |")
        out.append("")
    return "\n".join(out)


def main() -> None:
    """Write results.csv and summary_tables.md under --dir."""
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--dir", default="/root/lmc-work/functional/perf")
    args = ap.parse_args()
    rows = [summarize(*t) for t in point_files(args.dir)]
    rows += not_run_rows(args.dir, rows)
    rows.sort(key=lambda r: (r["length"], mode_key(r["mode"]), r["concurrency"]))
    with open(os.path.join(args.dir, "results.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    with open(os.path.join(args.dir, "summary_tables.md"), "w") as f:
        f.write(
            "TTFT and total latency are p50 seconds; "
            "speedup = nocache TTFT p50 / mode TTFT p50.\n\n"
        )
        f.write(tables(rows))
    bad = [r for r in rows if not r["valid"]]
    print(f"{len(rows)} points, {len(bad)} invalid")
    for r in bad:
        print(f"  INVALID L{r['length']} c{r['concurrency']} {r['mode']}: {r['note']}")


if __name__ == "__main__":
    main()
