# SPDX-License-Identifier: Apache-2.0
"""Write a mixed-load trace for ``perf_client.py --trace`` (LW-MIXED-LOAD-PROPOSAL.md).

A trace is a list of requests, each with a send time, a class, an output
length and a prompt given as segments:

- ``["d", L, id]``: stored document ``id`` of length ``L`` (8192 or 16384),
  the same tokens in every run, so the L2 set is stored once per data file;
- ``["f", n, key]``: ``n`` fresh tokens seeded by the run's salt and ``key``,
  so what one run stores (misses, suffixes) is never a hit for the next.

Classes (``CLASSES``): ``miss``; ``l2_full``, ``l2_long``, ``l2_short`` and
``l2_border`` use a document no earlier request of the trace touched;
``l1_full`` resends, and ``l1_part`` extends, an earlier ``miss`` or ``l1_*``
prompt (whose KV was computed or loaded from L1, so it sits in general L1 in
every config). An ``l1_*`` request takes a base sent at least ``MIN_AGE_S``
earlier with at most ``L1_BUDGET_GB`` of KV entering L1 since the base was
last used (LRU); with no such base it becomes a ``miss`` (counted as
``l1_fallbacks``).

Usage::

    python make_trace.py trace --scenario S1 --rate 1.5 --duration 240 \\
        --seed 1 --out S1_r1.5.json
    python make_trace.py store --docs8 120 --docs16 200 --out store.json
    python make_trace.py needs S1_r1.5.json S1_r2.json   # docs and GB to store

Output JSON::

    {"kind": "trace" | "store", "scenario": str, "rate": float,
     "duration": float, "seed": int,
     "requests": [{"i": int, "t": float, "cls": str, "max_tokens": int,
                   "prompt": [["d" | "f", int, int], ...],
                   "hit_tokens": int, "src": "none" | "l1" | "l2",
                   "base": int}],   # l1_*: the request reused, else -1
     "summary": {"classes": {cls: int}, "docs8": int, "docs16": int,
                 "l1_fallbacks": int, "new_tokens": int, "l2_load_tokens": int}}
"""

# Standard
import argparse
import json
import random
import sys

CHUNK = 256
KV_BYTES_PER_TOKEN = 32 * 2 * 8 * 128 * 2  # Llama-3.1-8B, bf16
DOC_LENS = (8192, 16384)
CLASSES = (
    "miss",
    "l2_full",
    "l2_long",
    "l2_short",
    "l2_border",
    "l1_full",
    "l1_part",
)
SCENARIOS: dict[str, dict[str, int]] = {
    "S1": {
        "miss": 15,
        "l2_full": 5,
        "l2_long": 15,
        "l2_short": 5,
        "l2_border": 5,
        "l1_full": 15,
        "l1_part": 40,
    },
    "S2": {
        "miss": 10,
        "l2_full": 10,
        "l2_long": 10,
        "l2_short": 35,
        "l2_border": 15,
        "l1_full": 5,
        "l1_part": 15,
    },
    "S3": {
        "miss": 10,
        "l2_full": 25,
        "l2_long": 10,
        "l2_short": 10,
        "l2_border": 5,
        "l1_full": 30,
        "l1_part": 10,
    },
    "S4": {
        "miss": 60,
        "l2_full": 10,
        "l2_long": 20,
        "l2_short": 5,
        "l2_border": 5,
        "l1_full": 0,
        "l1_part": 0,
    },
}
MISS_LENS = (2048, 4096, 8192, 16384)
LONG_SUFFIXES = (2048, 4096, 8192)
SHORT_SUFFIXES = (256, 512, 768)
BORDER_SUFFIX = 1024
L1_SUFFIXES = (256, 512, 1024, 2048)
MAX_PROMPT = 32768
OUTPUTS = ((128, 50), (1024, 30), (4096, 15), (8192, 5))
MIN_AGE_S = 15.0
L1_BUDGET_GB = 50.0


def prompt_len(prompt: list[list]) -> int:
    """Return the token count of a segment list."""
    return sum(seg[1] for seg in prompt)


def make_trace(scenario: str, rate: float, duration: float, seed: int) -> dict:
    """Generate one trace.

    Args:
        scenario: A key of ``SCENARIOS``.
        rate: Mean arrival rate in requests per second (Poisson).
        duration: Send window in seconds.
        seed: Seed for arrivals, classes and lengths.

    Returns:
        The trace dict (module docstring schema).

    Raises:
        ValueError: If the scenario is unknown or the rate is not positive.
    """
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario {scenario}; have {sorted(SCENARIOS)}")
    if rate <= 0:
        raise ValueError(f"rate must be positive, got {rate}")
    rng = random.Random(f"{scenario}/{rate}/{duration}/{seed}")
    shares = SCENARIOS[scenario]
    classes = [c for c in CLASSES if shares[c] > 0]
    weights = [shares[c] for c in classes]
    next_doc = {L: 0 for L in DOC_LENS}
    inflow = 0.0  # bytes of KV that entered L1 so far
    # base request index -> (send time, inflow at its last use)
    bases: dict[int, tuple[float, float]] = {}
    requests: list[dict] = []
    fallbacks = 0
    new_tokens = l2_load_tokens = 0
    t = 0.0
    while True:
        t += rng.expovariate(rate)
        if t >= duration:
            break
        i = len(requests)
        cls = rng.choices(classes, weights)[0]
        prompt: list[list] = []
        hit, src, base = 0, "none", -1
        if cls.startswith("l1_"):
            ok = [
                b
                for b, (tb, used) in bases.items()
                if t - tb >= MIN_AGE_S
                and inflow - used <= L1_BUDGET_GB * 2**30
                and (
                    cls == "l1_full"
                    or prompt_len(requests[b]["prompt"]) + min(L1_SUFFIXES)
                    <= MAX_PROMPT
                )
            ]
            if ok:
                base = rng.choice(ok)
                prompt = [list(s) for s in requests[base]["prompt"]]
                hit, src = prompt_len(prompt), "l1"
                if cls == "l1_part":
                    room = MAX_PROMPT - hit
                    sfx = rng.choice([s for s in L1_SUFFIXES if s <= room])
                    prompt.append(["f", sfx, i])
            else:
                fallbacks += 1
                cls = "miss"
        if cls == "miss":
            prompt = [["f", rng.choice(MISS_LENS), i]]
        elif cls.startswith("l2_"):
            L = 16384 if cls in ("l2_short", "l2_border") else rng.choice(DOC_LENS)
            prompt = [["d", L, next_doc[L]]]
            next_doc[L] += 1
            hit, src = L, "l2"
            if cls == "l2_long":
                prompt.append(["f", rng.choice(LONG_SUFFIXES), i])
            elif cls == "l2_short":
                prompt.append(["f", rng.choice(SHORT_SUFFIXES), i])
            elif cls == "l2_border":
                prompt.append(["f", BORDER_SUFFIX, i])
        total = prompt_len(prompt)
        new = total - hit
        new_tokens += new
        if src == "l2":
            l2_load_tokens += hit
        inflow += (new + (hit if src == "l2" else 0)) * KV_BYTES_PER_TOKEN
        if base >= 0:
            bases[base] = (bases[base][0], inflow)
        if cls in ("miss", "l1_full", "l1_part"):
            bases[i] = (t, inflow)
        out = rng.choices([o for o, _ in OUTPUTS], [w for _, w in OUTPUTS])[0]
        requests.append(
            {
                "i": i,
                "t": round(t, 4),
                "cls": cls,
                "max_tokens": out,
                "prompt": prompt,
                "hit_tokens": hit,
                "src": src,
                "base": base,
            }
        )
    counts = {c: sum(r["cls"] == c for r in requests) for c in CLASSES}
    return {
        "kind": "trace",
        "scenario": scenario,
        "rate": rate,
        "duration": duration,
        "seed": seed,
        "requests": requests,
        "summary": {
            "classes": counts,
            "docs8": next_doc[8192],
            "docs16": next_doc[16384],
            "l1_fallbacks": fallbacks,
            "new_tokens": new_tokens,
            "l2_load_tokens": l2_load_tokens,
        },
    }


def make_store(docs8: int, docs16: int) -> dict:
    """Return a store trace: every document once, all at t=0, 1 output token."""
    reqs = []
    for L, n in ((8192, docs8), (16384, docs16)):
        for d in range(n):
            reqs.append(
                {
                    "i": len(reqs),
                    "t": 0.0,
                    "cls": "store",
                    "max_tokens": 1,
                    "prompt": [["d", L, d]],
                    "hit_tokens": 0,
                    "src": "none",
                }
            )
    return {
        "kind": "store",
        "scenario": "store",
        "rate": 0.0,
        "duration": 0.0,
        "seed": 0,
        "requests": reqs,
        "summary": {"docs8": docs8, "docs16": docs16},
    }


def needs(paths: list[str]) -> dict[str, float]:
    """Return the documents the traces need and the KV GB they write to L2.

    Args:
        paths: Trace files.

    Returns:
        ``docs8``/``docs16``: the most any one trace uses; ``docs_gb``: their
        KV; ``writes_gb``: the new KV of all traces together.
    """
    d8 = d16 = 0
    new = 0
    for p in paths:
        with open(p) as f:
            s = json.load(f)["summary"]
        d8, d16 = max(d8, s["docs8"]), max(d16, s["docs16"])
        new += s["new_tokens"]
    gb = KV_BYTES_PER_TOKEN / 2**30
    return {
        "docs8": d8,
        "docs16": d16,
        "docs_gb": round((d8 * 8192 + d16 * 16384) * gb, 1),
        "writes_gb": round(new * gb, 1),
    }


def main() -> None:
    """Parse arguments and write a trace, a store trace, or print needs."""
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("trace")
    t.add_argument("--scenario", required=True)
    t.add_argument("--rate", type=float, required=True)
    t.add_argument("--duration", type=float, default=240.0)
    t.add_argument("--seed", type=int, default=1)
    t.add_argument("--out", required=True)
    s = sub.add_parser("store")
    s.add_argument("--docs8", type=int, required=True)
    s.add_argument("--docs16", type=int, required=True)
    s.add_argument("--out", required=True)
    n = sub.add_parser("needs")
    n.add_argument("paths", nargs="+")
    args = p.parse_args()
    if args.cmd == "needs":
        print(json.dumps(needs(args.paths)))
        return
    if args.cmd == "trace":
        out = make_trace(args.scenario, args.rate, args.duration, args.seed)
    else:
        out = make_store(args.docs8, args.docs16)
    with open(args.out, "w") as f:
        json.dump(out, f)
    print(f"{args.out}: {len(out['requests'])} requests {json.dumps(out['summary'])}")


if __name__ == "__main__":
    sys.exit(main())
