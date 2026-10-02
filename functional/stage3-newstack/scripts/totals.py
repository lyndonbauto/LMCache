# SPDX-License-Identifier: Apache-2.0
"""Sum the Totals line of hit_report .md files per report.

Usage: python3 totals.py <report.md>...
Prints n / exact / ok / errors / hit_as_expected over all sends, the
pipelined outcomes, and the sends whose ok count is below n.
"""

# Standard
import re
import sys

METRICS = ("n", "exact", "ok", "errors", "hit_as_expected")


def main() -> None:
    """Print one summary line per report given on the command line."""
    for path in sys.argv[1:]:
        with open(path) as f:
            totals_line = f.read().strip().splitlines()[-1]
        counts: dict[str, int] = {}
        for send, metric, value in re.findall(r"(\S+?):(\S+?)=(\d+)", totals_line):
            counts[f"{send}:{metric}"] = int(value)
        sends = sorted({k.split(":")[0] for k in counts})
        total = {m: sum(counts.get(f"{s}:{m}", 0) for s in sends) for m in METRICS}
        outcomes: dict[str, int] = {}
        for key, value in counts.items():
            if ":outcome=" in key:
                name = key.split("outcome=")[1]
                outcomes[name] = outcomes.get(name, 0) + value
        short = [
            (s, counts.get(f"{s}:ok"), counts.get(f"{s}:n"), counts.get(f"{s}:exact"))
            for s in sends
            if counts.get(f"{s}:ok", 0) != counts.get(f"{s}:n", 0)
        ]
        name = path.split("/")[-1]
        print(f"{name}: {total} outcomes={outcomes} sends_not_all_ok={short}")


if __name__ == "__main__":
    main()
