"""Command-line entry point for deja-func.

M1 shipped the plumbing (`deja --version`, `deja hello`). M2 added `deja index`
(walk the repo, write `.dejafunc/index.json`). M3 adds `deja find`, the core
lookup: fuzzy-search the index by name + docstring. Later commands (`stats`,
`mcp`, ...) arrive in subsequent milestones — see PLAN.md §7.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__

# A little personality, per PLAN.md §4.
HELLO_MESSAGE = (
    "🫠 deja-func is awake.\n"
    "I remember every function you've written so you stop rewriting "
    "slugify for the fourth time.\n"
    "Run `deja index` to build the index, then `deja find <query>` to search it."
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

    find = subparsers.add_parser(
        "find",
        help="Fuzzy-search the index by function name + docstring.",
    )
    find.add_argument("query", help="What you're about to (re)write, e.g. 'slugify'.")
    find.add_argument(
        "path",
        nargs="?",
        default=".",
        help="Repo root to search (default: current directory).",
    )
    find.add_argument(
        "-n",
        "--limit",
        type=int,
        default=None,
        help="Maximum number of matches to show (default: 10).",
    )
    find.set_defaults(func=cmd_find)

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


def cmd_find(args: argparse.Namespace) -> int:
    """Handle `deja find`: rank indexed functions against a query.

    Loads the index for the target repo, auto-building it first if none exists
    yet (so the very first `deja find` just works). Exit code is 0 when at least
    one match is found and 1 when none are, so it's scriptable.
    """
    # Imported lazily so `deja --version` / `deja hello` stay dependency-free.
    from .index import build_index, load_index, save_index
    from .render import format_results
    from .search import DEFAULT_LIMIT, search

    root = Path(args.path)
    if not root.is_dir():
        print(f"deja: not a directory: {root}", file=sys.stderr)
        return 2

    try:
        index = load_index(root)
    except FileNotFoundError:
        # No index yet: build one on the fly so first-run is friction-free.
        print("\U0001f50e No index yet \u2014 building one first\u2026", file=sys.stderr)
        index = build_index(root)
        save_index(index, root)

    limit = args.limit if args.limit is not None else DEFAULT_LIMIT
    results = search(args.query, index.records, limit=limit)
    print(format_results(args.query, results))
    return 0 if results else 1


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
