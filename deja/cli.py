"""Command-line entry point for deja-func.

M1 shipped the plumbing (`deja --version`, `deja hello`). M2 adds the first real
command, `deja index`, which walks the repo and writes `.dejafunc/index.json`.
The remaining commands (`find`, ...) arrive in later milestones — see PLAN.md §7.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__

# A little personality, per PLAN.md §4.
HELLO_MESSAGE = (
    "🫠 deja-func is awake.\n"
    "Soon I'll remember every function you've written so you stop rewriting "
    "slugify for the fourth time.\n"
    "Try `deja --version` for now — `index` and `find` are on the way."
)


def build_parser() -> argparse.ArgumentParser:
    """Construct the top-level argument parser."""
    parser = argparse.ArgumentParser(
        prog="deja",
        description="Index your functions so you (and your AI agents) stop reinventing them.",
    )
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"deja-func {__version__}",
    )

    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    hello = subparsers.add_parser("hello", help="Print a friendly hello and confirm install.")
    hello.set_defaults(func=cmd_hello)

    index = subparsers.add_parser(
        "index",
        help="Walk the repo and build the function index (.dejafunc/index.json).",
    )
    index.add_argument(
        "path",
        nargs="?",
        default=".",
        help="Directory to index (default: current directory).",
    )
    index.set_defaults(func=cmd_index)

    return parser


def cmd_hello(_args: argparse.Namespace) -> int:
    """Handle `deja hello`."""
    print(HELLO_MESSAGE)
    return 0


def cmd_index(args: argparse.Namespace) -> int:
    """Handle `deja index`: build the index and write it to disk."""
    # Imported lazily so `deja --version` / `deja hello` stay dependency-free.
    from .index import build_index, save_index

    root = Path(args.path)
    if not root.is_dir():
        print(f"deja: not a directory: {root}", file=sys.stderr)
        return 2

    index = build_index(root)
    out = save_index(index, root)
    print(f"\U0001f9e0 Indexed {len(index)} functions \u2192 {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Program entry point. Returns a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    func = getattr(args, "func", None)
    if func is None:
        # No subcommand given: show help, but don't treat it as a hard error.
        parser.print_help()
        return 0

    return func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
