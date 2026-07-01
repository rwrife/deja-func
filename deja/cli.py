"""Command-line entry point for deja-func.

M1 shipped the plumbing (`deja --version`, `deja hello`). M2 added `deja index`
(walk the repo, write `.dejafunc/index.json`). M3 added `deja find`, the core
lookup: fuzzy-search the index by name + docstring. M4 extends `find` with
signature-shape search (`--sig`), intent weighting (`--intent`), and `--explain`.
M6 adds machine-readable output (`deja find --json`) and `deja mcp`, a stdio
MCP server so AI agents query the inventory before writing code (see PLAN.md §6).
`deja dupes` (PLAN.md §8 #1) reports clusters of near-identical functions — the
redundancy report ("you have 6 date parsers"). `deja hook` (PLAN.md §8 #3)
installs a git pre-commit/pre-push hook that warns when a newly added function
strongly matches existing code. `deja index --watch` (PLAN.md §8 #2) keeps the
index fresh by incrementally reparsing only changed files as you work.
`deja stats` (PLAN.md §8 #10) zooms out to an inventory leaderboard: totals,
language breakdown, the most-duplicated names, and the biggest files.
Later commands arrive in subsequent milestones — see PLAN.md §7.
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
    index.add_argument(
        "-w",
        "--watch",
        action="store_true",
        help="Stay running and incrementally reindex files as they change "
        "(debounced). Ctrl-C to stop with a summary.",
    )
    index.add_argument(
        "--interval",
        type=float,
        default=None,
        metavar="SECONDS",
        help="With --watch: seconds between filesystem polls (default: 1.0).",
    )
    index.add_argument(
        "--debounce",
        type=float,
        default=None,
        metavar="SECONDS",
        help="With --watch: quiet window after the last change before reindexing (default: 0.4).",
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
        "--semantic",
        action="store_true",
        help="Rank by meaning using local embeddings (needs the 'semantic' extra "
        "or a running Ollama); falls back to fuzzy search if the backend is missing.",
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

    dupes = subparsers.add_parser(
        "dupes",
        help="Report clusters of near-duplicate functions (the redundancy report).",
    )
    dupes.add_argument(
        "path",
        nargs="?",
        default=".",
        help="Repo root to scan (default: current directory).",
    )
    dupes.add_argument(
        "-t",
        "--threshold",
        type=float,
        default=None,
        metavar="N",
        help="Similarity cutoff 0-100 to link two functions (default: 75). "
        "Lower = more (looser) clusters.",
    )
    dupes.add_argument(
        "-n",
        "--limit",
        type=int,
        default=None,
        help="Maximum number of clusters to show (default: all).",
    )
    dupes.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit structured JSON (stable schema) instead of pretty text.",
    )
    dupes.set_defaults(func=cmd_dupes)

    stats = subparsers.add_parser(
        "stats",
        help="Show an inventory leaderboard: totals, languages, top dupes, biggest files.",
    )
    stats.add_argument(
        "path",
        nargs="?",
        default=".",
        help="Repo root to summarize (default: current directory).",
    )
    stats.add_argument(
        "-t",
        "--top",
        type=int,
        default=None,
        metavar="N",
        help="Cap each leaderboard section to the top N rows (default: 10).",
    )
    stats.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit structured JSON (stable schema) instead of pretty text.",
    )
    stats.set_defaults(func=cmd_stats)

    hook = subparsers.add_parser(
        "hook",
        help="Install / run a git hook that warns when staged code duplicates existing functions.",
    )
    hook_sub = hook.add_subparsers(dest="hook_command", metavar="<action>")
    # Bare `deja hook` prints this parser's help (mirrors top-level behavior).
    hook.set_defaults(func=cmd_hook, _hook_parser=hook)

    hook_install = hook_sub.add_parser(
        "install",
        help="Write a git pre-commit (or pre-push) redundancy-warning hook.",
    )
    hook_install.add_argument(
        "path",
        nargs="?",
        default=".",
        help="Any path inside the target git repo (default: current directory).",
    )
    hook_install.add_argument(
        "--pre-push",
        action="store_const",
        const="pre-push",
        dest="hook_kind",
        default="pre-commit",
        help="Install as a pre-push hook instead of pre-commit.",
    )
    hook_install.add_argument(
        "--strict",
        action="store_true",
        help="Bake in --strict so the hook *fails* the commit on a strong match.",
    )
    hook_install.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing hook even if deja didn't write it.",
    )
    hook_install.set_defaults(func=cmd_hook_install)

    hook_check = hook_sub.add_parser(
        "check",
        help="Diff staged functions against the index and warn on strong matches "
        "(run by the hook).",
    )
    hook_check.add_argument(
        "path",
        nargs="?",
        default=".",
        help="Any path inside the target git repo (default: current directory).",
    )
    hook_check.add_argument(
        "-t",
        "--threshold",
        type=float,
        default=None,
        metavar="N",
        help="Similarity cutoff 0-100 to warn on (default: 75). Lower = more (noisier) warnings.",
    )
    hook_check.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero (fail the commit) when a strong match is found.",
    )
    hook_check.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit structured JSON (stable schema) instead of pretty text.",
    )
    hook_check.set_defaults(func=cmd_hook_check)

    return parser


def cmd_hello(_args: argparse.Namespace) -> int:
    """Handle `deja hello`."""
    print(HELLO_MESSAGE)
    return 0


def cmd_index(args: argparse.Namespace) -> int:
    """Handle `deja index`: build the index and write it to disk.

    With ``--watch`` it instead builds (or loads) the index once and then stays
    running, incrementally reindexing only changed files until Ctrl-C (issue
    #10 / PLAN.md §8 #2).
    """
    # Imported lazily so `deja --version` / `deja hello` stay dependency-free.
    from .index import build_index, save_index

    root = Path(args.path)
    if not root.is_dir():
        print(f"deja: not a directory: {root}", file=sys.stderr)
        return 2

    if getattr(args, "watch", False):
        from .watch import DEFAULT_DEBOUNCE, DEFAULT_INTERVAL, run_watch

        interval = args.interval if args.interval is not None else DEFAULT_INTERVAL
        debounce = args.debounce if args.debounce is not None else DEFAULT_DEBOUNCE
        if interval <= 0:
            print("deja: --interval must be greater than 0", file=sys.stderr)
            return 2
        if debounce < 0:
            print("deja: --debounce must be >= 0", file=sys.stderr)
            return 2
        return run_watch(root, interval=interval, debounce=debounce)

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
    from .search import DEFAULT_LIMIT, ScoredRecord, search

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

    semantic = getattr(args, "semantic", False)
    semantic_used = False
    if semantic and query.strip():
        # Heavy embedding code is imported *only* on this branch, so the default
        # `deja find` path never pays for it (issue #9: zero impact when off).
        from .semantic import load_backend, semantic_search

        backend, message = load_backend()
        if backend is None:
            # Clear message + graceful fallback to fuzzy search (issue #9).
            print(f"\U0001f9ea deja: {message}; falling back to fuzzy search.", file=sys.stderr)
        else:
            print(f"\U0001f9ea semantic search {message}", file=sys.stderr)
            sem = semantic_search(query, index.records, backend, limit=limit, root=root)
            # Adapt to the shared ScoredRecord shape so render/serialize are reused
            # unchanged (semantic ranking carries no per-signal breakdown).
            results = [ScoredRecord(record=s.record, score=s.score) for s in sem]
            semantic_used = True

    if not semantic_used:
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

        doc = results_to_dict(
            results, query=query, sig=sig, intent=args.intent, semantic=semantic_used
        )
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


def cmd_dupes(args: argparse.Namespace) -> int:
    """Handle `deja dupes`: cluster near-duplicate functions (PLAN.md §8 #1).

    Loads the index for the target repo (auto-building it first if none exists,
    like ``deja find``), clusters near-identical functions, and prints them
    largest-cluster-first. Exit code is 0 when any redundancy is found and 1 when
    the inventory is clean, so it's scriptable / CI-friendly.
    """
    # Imported lazily so `deja --version` / `deja hello` stay dependency-free.
    from .dupes import DEFAULT_THRESHOLD, find_clusters
    from .index import build_index, load_index, save_index
    from .render import format_clusters

    root = Path(args.path)
    if not root.is_dir():
        print(f"deja: not a directory: {root}", file=sys.stderr)
        return 2

    threshold = args.threshold if args.threshold is not None else DEFAULT_THRESHOLD
    if not 0.0 <= threshold <= 100.0:
        print("deja: --threshold must be between 0 and 100", file=sys.stderr)
        return 2

    try:
        index = load_index(root)
    except FileNotFoundError:
        # No index yet: build one on the fly so first-run is friction-free.
        print("\U0001f50e No index yet — building one first…", file=sys.stderr)
        index = build_index(root)
        save_index(index, root)

    clusters = find_clusters(index.records, threshold=threshold)
    if args.limit is not None:
        clusters = clusters[: max(0, args.limit)]

    if getattr(args, "as_json", False):
        # Machine-readable path: stable schema, no ANSI, no personality.
        import json as _json

        from .serialize import clusters_to_dict

        doc = clusters_to_dict(clusters, threshold=threshold)
        print(_json.dumps(doc, indent=2, ensure_ascii=False))
        return 0 if clusters else 1

    print(format_clusters(clusters))
    return 0 if clusters else 1


def cmd_stats(args: argparse.Namespace) -> int:
    """Handle `deja stats`: print an inventory leaderboard (PLAN.md §8 #10).

    Loads the index for the target repo (auto-building it first if none exists,
    exactly like ``deja find``/``deja dupes`` so first-run just works), aggregates
    it, and prints totals, language breakdown, the most-duplicated names, and the
    biggest files by function count. Exit code is 0 whenever there's anything to
    report and 1 only on a genuinely empty inventory, so it's scriptable.
    """
    # Imported lazily so `deja --version` / `deja hello` stay dependency-free.
    from .index import build_index, load_index, save_index
    from .render import format_stats
    from .stats import DEFAULT_TOP, compute_stats

    root = Path(args.path)
    if not root.is_dir():
        print(f"deja: not a directory: {root}", file=sys.stderr)
        return 2

    top = args.top if args.top is not None else DEFAULT_TOP
    if top < 0:
        print("deja: --top must be >= 0", file=sys.stderr)
        return 2

    try:
        index = load_index(root)
    except FileNotFoundError:
        # No index yet: build one on the fly so first-run is friction-free
        # (mirrors `deja find` / `deja dupes`).
        print("\U0001f50e No index yet — building one first…", file=sys.stderr)
        index = build_index(root)
        save_index(index, root)

    stats = compute_stats(index.records, top=top)

    if getattr(args, "as_json", False):
        # Machine-readable path: stable schema, no ANSI, no personality.
        import json as _json

        from .serialize import stats_to_dict

        print(_json.dumps(stats_to_dict(stats), indent=2, ensure_ascii=False))
        return 0 if not stats.is_empty else 1

    print(format_stats(stats))
    return 0 if not stats.is_empty else 1


def cmd_hook_install(args: argparse.Namespace) -> int:
    """Handle `deja hook install`: write the git redundancy hook (PLAN.md §8 #3)."""
    # Imported lazily so `deja --version` / `deja hello` stay dependency-free.
    from .hook import install_hook

    try:
        target = install_hook(
            args.path,
            hook=args.hook_kind,
            strict=args.strict,
            force=args.force,
        )
    except FileNotFoundError:
        print("deja: not inside a git repository", file=sys.stderr)
        return 2
    except FileExistsError as exc:
        print(f"deja: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:  # pragma: no cover - guarded by argparse choices
        print(f"deja: {exc}", file=sys.stderr)
        return 2

    mode = "strict (blocks commits)" if args.strict else "warn-only"
    print(f"\U0001fae0 Installed {args.hook_kind} redundancy hook → {target}  [{mode}]")
    print("   It warns when a newly added function strongly matches existing code.")
    print("   Tip: run `deja index` so there's an inventory to compare against.")
    return 0


def cmd_hook_check(args: argparse.Namespace) -> int:
    """Handle `deja hook check`: warn on staged functions that already exist.

    This is what the installed hook invokes. It compares each staged function
    against the existing index and reports strong matches. By default it only
    *warns* (exit 0) so it never blocks a commit; ``--strict`` makes a match
    exit non-zero so git aborts the commit (PLAN.md §9: warn, don't gate).
    """
    # Imported lazily so `deja --version` / `deja hello` stay dependency-free.
    from .dupes import DEFAULT_THRESHOLD
    from .hook import check_staged, git_repo_root
    from .index import index_path
    from .render import format_matches

    threshold = args.threshold if args.threshold is not None else DEFAULT_THRESHOLD
    if not 0.0 <= threshold <= 100.0:
        print("deja: --threshold must be between 0 and 100", file=sys.stderr)
        return 2

    try:
        repo_root = git_repo_root(args.path)
    except FileNotFoundError:
        print("deja: not inside a git repository", file=sys.stderr)
        return 2

    # No index means nothing to compare against. Don't error (that would break
    # commits for anyone who installed the hook before indexing); nudge instead.
    if not index_path(repo_root).is_file():
        if not getattr(args, "as_json", False):
            print(
                "\U0001f50e deja: no index yet — run `deja index` to enable redundancy warnings.",
                file=sys.stderr,
            )
            return 0

    matches = check_staged(repo_root, threshold=threshold)

    if getattr(args, "as_json", False):
        import json as _json

        from .serialize import matches_to_dict

        doc = matches_to_dict(matches, threshold=threshold, strict=args.strict)
        print(_json.dumps(doc, indent=2, ensure_ascii=False))
        # JSON mode mirrors strict exit semantics for tooling/CI.
        return 1 if (matches and args.strict) else 0

    print(format_matches(matches, strict=args.strict))
    # Warn-only by default: a match never blocks the commit unless --strict.
    return 1 if (matches and args.strict) else 0


def cmd_hook(args: argparse.Namespace) -> int:
    """Handle a bare `deja hook` (no action): show the hook help."""
    args._hook_parser.print_help()
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
