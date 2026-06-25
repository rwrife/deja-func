"""Command-line entry point for deja-func.

M1 shipped the plumbing (`deja --version`, `deja hello`). M2 added `deja index`
(walk the repo, write `.dejafunc/index.json`). M3 added `deja find`, the core
lookup: fuzzy-search the index by name + docstring. M4 extends `find` with
signature-shape search (`--sig`), intent weighting (`--intent`), and `--explain`.
M6 adds machine-readable output (`deja find --json`) and `deja mcp`, a stdio
MCP server so AI agents query the inventory before writing code (see PLAN.md §6).
Later commands (`stats`, `watch`, ...) arrive in subsequent milestones — see PLAN.md §7.
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
        help="Fuzzy-search the index by name, docstring, or signature shape.",
    )
    find.add_argument(
        "query",
        nargs="?",
        default="",
        help="What you're about to (re)write, e.g. 'slugify'. Optional with --sig.",
    )
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
    find.add_argument(
        "-s",
        "--sig",
        default=None,
        metavar="SHAPE",
        help="Match by signature shape, e.g. '(str)->bool' or '(int, int)'.",
    )
    find.add_argument(
        "-i",
        "--intent",
        action="store_true",
        help="Weight the docstring higher for natural-language intent queries.",
    )
    find.add_argument(
        "--explain",
        action="store_true",
        help="Show why each result matched (per-signal score breakdown).",
    )
    find.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit structured JSON (stable schema) instead of pretty text.",
    )
    find.set_defaults(func=cmd_find)

    mcp = subparsers.add_parser(
        "mcp",
        help="Run a stdio MCP server exposing find_function for AI agents.",
    )
    mcp.add_argument(
        "path",
        nargs="?",
        default=".",
        help="Repo root to serve (default: current directory).",
    )
    mcp.set_defaults(func=cmd_mcp)

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
    """Handle `deja find`: rank indexed functions against a query and/or shape.

    Loads the index for the target repo, auto-building it first if none exists
    yet (so the very first `deja find` just works). You can search by text
    (name/docstring), by signature shape (``--sig``), or both. Exit code is 0
    when at least one match is found and 1 when none are, so it's scriptable.
    """
    # Imported lazily so `deja --version` / `deja hello` stay dependency-free.
    from .index import build_index, load_index, save_index
    from .render import format_results
    from .search import DEFAULT_LIMIT, search

    query = args.query or ""
    sig = args.sig

    # Disambiguate the positionals for shape search. ``deja find`` has two
    # optional positionals (``query`` then ``path``), so when ``--sig`` supplies
    # the search and the user passes a single positional, argparse binds it to
    # ``query`` even though they meant the path — e.g.
    # ``deja find --sig "(str)->bool" ./proj``. If no explicit path was given and
    # that lone positional is an existing directory, treat it as the path. (Plain
    # text-query usage, ``deja find slugify [path]``, is left exactly as in M3.)
    if sig and query and args.path == "." and Path(query).is_dir():
        args.path = query
        query = ""

    if not query.strip() and not sig:
        print("deja: provide a search query or --sig SHAPE", file=sys.stderr)
        return 2

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
    results = search(
        query,
        index.records,
        limit=limit,
        sig=sig,
        intent=args.intent,
    )

    if getattr(args, "as_json", False):
        # Machine-readable path (M6): stable schema, no ANSI, no personality.
        import json as _json

        from .serialize import results_to_dict

        doc = results_to_dict(results, query=query, sig=sig, intent=args.intent)
        print(_json.dumps(doc, indent=2, ensure_ascii=False))
        return 0 if results else 1

    # Header still reads naturally when only a shape was given.
    header_query = query if query.strip() else (sig or "")
    print(format_results(header_query, results, explain=args.explain))
    return 0 if results else 1


def cmd_mcp(args: argparse.Namespace) -> int:
    """Handle `deja mcp`: run the stdio MCP server until EOF (M6)."""
    # Imported lazily so `deja --version` / `deja hello` stay dependency-free.
    from .mcp import serve

    root = Path(args.path)
    if not root.is_dir():
        print(f"deja: not a directory: {root}", file=sys.stderr)
        return 2
    return serve(root)


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
