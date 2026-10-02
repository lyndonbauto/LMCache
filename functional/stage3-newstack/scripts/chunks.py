# SPDX-License-Identifier: Apache-2.0
"""Count the full 256-token chunks of P-exact / P-ragged prompts.

Usage: python3 chunks.py <corpus.json> <comma-separated prompt ids>
Prints the full-chunk count and the count with one chunk less for every
prompt that is an exact multiple of 256 tokens.
"""

# Standard
import json
import sys


def main() -> None:
    """Print the two chunk counts for the prompts named on the command line."""
    with open(sys.argv[1]) as f:
        corpus = json.load(f)
    ids = set(sys.argv[2].split(","))
    prompts = [
        p for s in ("P-exact", "P-ragged") for p in corpus["sets"][s] if p["id"] in ids
    ]
    full = sum(p["n_tokens"] // 256 for p in prompts)
    exact_less_one = sum(
        p["n_tokens"] // 256 - (1 if p["n_tokens"] % 256 == 0 else 0) for p in prompts
    )
    print("full chunks", full, "less one per exact-multiple prompt", exact_less_one)


if __name__ == "__main__":
    main()
