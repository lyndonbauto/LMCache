# SPDX-License-Identifier: Apache-2.0
"""``lmcache tool cache-simulator hash-trace`` subcommand."""

# Standard
import argparse

# First Party
from lmcache.cli.commands.base import BaseCommand


class HashTraceCommand(BaseCommand):
    """
    Convert a request trace into lookup-hash JSONL, so the simulator can be
    run on a workload that has never been deployed.
    """

    def name(self) -> str:
        return "hash-trace"

    def help(self) -> str:
        return (
            "Convert a request trace (token IDs or prompts) into lookup-hash "
            "JSONL using LMCache's real rolling chunk hashing, for offline "
            "hit-rate simulation without a running deployment."
        )

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        # First Party
        from lmcache.tools.cache_simulator.trace_hasher import (
            add_trace_hasher_arguments,
        )

        add_trace_hasher_arguments(parser)

    def register(self, subparsers: argparse._SubParsersAction) -> None:
        # Skip _add_output_args — this command defines its own --output.
        # Gracefully skip if optional dependencies are missing.
        try:
            parser = subparsers.add_parser(self.name(), help=self.help())
            self.add_arguments(parser)
            parser.set_defaults(func=self.execute)
        except ImportError:
            return

    def execute(self, args: argparse.Namespace) -> None:
        # First Party
        from lmcache.tools.cache_simulator.trace_hasher import run_trace_hasher

        run_trace_hasher(args)
