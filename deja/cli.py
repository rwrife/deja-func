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
`deja stale` (PLAN.md §8 #9) is the complement to `find`: it lists dead-code
candidates — indexed functions whose names are never referenced anywhere else
in the same tree (a fast string-level heuristic, not a call graph).
`deja explain <name>` (PLAN.md §8 #6) closes the loop after `find`: it shows one
function's definition, a textual caller list, and a git "last touched" age so you
know whether it's safe to reuse and who depends on it.
`deja export` / `deja import` (PLAN.md §8 #12) make the index portable: export
writes a checked-in-able JSON so an agent on a *fresh clone* can `deja import`
the inventory instantly instead of paying for a cold `deja index`.
`deja diff <old> <new>` (PLAN.md §8 #36) compares two index snapshots and
reports functions added / removed / renamed — "git diff, but for your function
inventory" — so a reviewer can see whether an agent PR quietly added a redundant
function or moved/deleted one.
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
    index.add_argument(
        "--no-config",
        action="store_true",
        help="Ignore any .dejafunc.toml (use built-in defaults / flags only).",
    )
    index.add_argument(
        "--root",
        action="append",
        default=None,
        metavar="PATH",
        dest="roots",
        help="Monorepo mode: index this package root (repeatable). Each root gets "
        "its own .dejafunc/index.json. Omit to index just <path>.",
    )
    index.add_argument(
        "--all",
        action="store_true",
        dest="all_roots",
        help="Monorepo mode: auto-discover every package root under <path> "
        "(dirs with pyproject.toml/package.json/go.mod/Cargo.toml) and index each.",
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
    find.add_argument(
        "--age",
        action="store_true",
        help="Add a git 'last touched' relative-age column to each result "
        "(off by default; one cached git call per distinct file, no perf hit "
        "when unset; blank outside a git repo).",
    )
    find.add_argument(
        "--no-config",
        action="store_true",
        help="Ignore any .dejafunc.toml (use built-in defaults / flags only).",
    )
    find.add_argument(
        "--all",
        action="store_true",
        dest="all_roots",
        help="Monorepo mode: search every discovered package index under <path> "
        "and tag each result with its package.",
    )
    find.add_argument(
        "--root",
        action="append",
        default=None,
        metavar="PATH",
        dest="roots",
        help="Monorepo mode: search this package root's index (repeatable). "
        "Implies merged multi-root search.",
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

    stale = subparsers.add_parser(
        "stale",
        help="List dead-code candidates: indexed functions whose names are never used elsewhere.",
    )
    stale.add_argument(
        "path",
        nargs="?",
        default=".",
        help="Repo root to scan (default: current directory).",
    )
    stale.add_argument(
        "--ignore",
        action="append",
        default=[],
        metavar="GLOB",
        help="Extra name glob to treat as never-stale (repeatable), layered on "
        "the built-in defaults (__*__, main, test_*, setUp/tearDown, …).",
    )
    stale.add_argument(
        "--lang",
        default=None,
        metavar="LANG",
        help="Only report functions in this language (e.g. python, javascript). "
        "The reference scan still covers every file.",
    )
    stale.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit structured JSON (stable schema) instead of pretty text.",
    )
    stale.set_defaults(func=cmd_stale)

    explain = subparsers.add_parser(
        "explain",
        help="Explain a function: where it's defined, who calls it, and how stale it is.",
    )
    explain.add_argument(
        "name",
        help="Function name to explain, e.g. 'slugify' (fuzzy if no exact match).",
    )
    explain.add_argument(
        "path",
        nargs="?",
        default=".",
        help="Repo root to search (default: current directory).",
    )
    explain.add_argument(
        "--max-callers",
        type=int,
        default=None,
        metavar="N",
        help="Cap the caller list shown (default: 20); the rest summarize as "
        "'+K more'. JSON always carries the full list. 0 = no cap.",
    )
    explain.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit structured JSON (stable schema) instead of pretty text.",
    )
    explain.set_defaults(func=cmd_explain)

    why = subparsers.add_parser(
        "why",
        help="Explain WHY matches ranked: per-signal score + weight breakdown.",
    )
    why.add_argument(
        "query",
        nargs="?",
        default="",
        help="What you're about to (re)write, e.g. 'slugify'. Optional with --sig.",
    )
    why.add_argument(
        "path",
        nargs="?",
        default=".",
        help="Repo root to search (default: current directory).",
    )
    why.add_argument(
        "-n",
        "--limit",
        type=int,
        default=None,
        help="Maximum number of matches to explain (default: 10).",
    )
    why.add_argument(
        "-s",
        "--sig",
        default=None,
        metavar="SHAPE",
        help="Match by signature shape, e.g. '(str)->bool' or '(int, int)'.",
    )
    why.add_argument(
        "-i",
        "--intent",
        action="store_true",
        help="Weight the docstring higher for natural-language intent queries.",
    )
    why.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit the structured breakdown (with a 'scores' object) as JSON.",
    )
    why.add_argument(
        "--no-config",
        action="store_true",
        help="Ignore any .dejafunc.toml (use built-in defaults / flags only).",
    )
    why.set_defaults(func=cmd_why)

    export = subparsers.add_parser(
        "export",
        help="Write the index to a portable file for fresh clones / agents to import.",
    )
    export.add_argument(
        "path",
        nargs="?",
        default=".",
        help="Repo root whose index to export (default: current directory).",
    )
    export.add_argument(
        "-o",
        "--output",
        default=None,
        metavar="FILE",
        help="Destination file (default: deja-index.json). Use '-' to stream "
        "JSON to stdout (pipe-safe; notes go to stderr).",
    )
    export.add_argument(
        "--no-config",
        action="store_true",
        help="Ignore any .dejafunc.toml when auto-building an index first.",
    )
    export.set_defaults(func=cmd_export)

    imp = subparsers.add_parser(
        "import",
        help="Load a portable export into .dejafunc/index.json (no `deja index` needed).",
    )
    imp.add_argument(
        "file",
        help="Portable export file to import (as produced by `deja export`).",
    )
    imp.add_argument(
        "path",
        nargs="?",
        default=".",
        help="Repo root to hydrate the index for (default: current directory).",
    )
    imp.set_defaults(func=cmd_import)

    diff = subparsers.add_parser(
        "diff",
        help="Compare two index snapshots: added / removed / renamed functions.",
    )
    diff.add_argument(
        "old",
        metavar="OLD",
        help="Baseline index/export JSON (as produced by `deja export`).",
    )
    diff.add_argument(
        "new",
        metavar="NEW",
        help="Newer index/export JSON to compare against the baseline.",
    )
    diff.add_argument(
        "--rename-threshold",
        type=float,
        default=None,
        metavar="PCT",
        help="Signature+docstring similarity (0-100) needed to call a "
        "remove+add a rename/move (default: 70).",
    )
    diff.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Emit machine-readable JSON (stable schema) instead of text.",
    )
    diff.set_defaults(func=cmd_diff)

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


def _load_project_config(root, *, no_config: bool):
    """Load ``.dejafunc.toml`` for *root*, honoring ``--no-config`` (issue #25).

    Returns an inert default :class:`~deja.config.Config` when ``--no-config`` is
    set, when no file is found, or when the runtime lacks ``tomllib`` (py3.10).
    On malformed TOML it prints a clear one-line note to stderr and *also* falls
    back to defaults, so a bad config never crashes a command — it just doesn't
    take effect (precedence: absent/ignored config → today's behavior).
    """
    from .config import Config, ConfigError, load_config

    if no_config:
        return Config()
    try:
        return load_config(root)
    except ConfigError as exc:
        print(f"\U0001f9ea deja: {exc}", file=sys.stderr)
        return Config()


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
        if getattr(args, "roots", None) or getattr(args, "all_roots", False):
            print("deja: --watch cannot be combined with --all or --root", file=sys.stderr)
            return 2
        # NOTE: the incremental watcher keeps its own snapshot/reindex path and
        # does not yet apply .dejafunc.toml include/exclude (issue #25 scoped the
        # config to one-shot `index`/`find`). A plain `deja index` re-run always
        # honors the config; watch parity can follow in a later slice.
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

    config = _load_project_config(root, no_config=getattr(args, "no_config", False))

    # Monorepo mode (issue #42): index one .dejafunc/index.json per package root.
    explicit_roots = getattr(args, "roots", None)
    all_roots = getattr(args, "all_roots", False)
    if explicit_roots or all_roots:
        from .monorepo import discover_roots, package_label

        if explicit_roots:
            targets = [Path(r) for r in explicit_roots]
            bad = [t for t in targets if not t.is_dir()]
            if bad:
                print(f"deja: not a directory: {bad[0]}", file=sys.stderr)
                return 2
        else:
            targets = discover_roots(root)

        total = 0
        for target in targets:
            tconfig = _load_project_config(target, no_config=getattr(args, "no_config", False))
            idx = build_index(target, exclude=tconfig.exclude, include=tconfig.include)
            out = save_index(idx, target)
            total += len(idx)
            label = package_label(root, target)
            print(f"\U0001f9e0 [{label}] indexed {len(idx)} functions \u2192 {out}")
        print(f"\U0001f4e6 {len(targets)} package(s), {total} functions total")
        return 0

    index = build_index(root, exclude=config.exclude, include=config.include)
    out = save_index(index, root)
    print(f"\U0001f9e0 Indexed {len(index)} functions \u2192 {out}")
    return 0


def _find_merged(
    args: argparse.Namespace,
    root: Path,
    query: str,
    sig,
    config,
    *,
    limit: int,
) -> int:
    """Handle ``deja find --all`` / ``--root`` (monorepo mode, issue #42).

    Loads (or builds) every discovered package index under *root*, ranks across
    all of them at once, and tags each hit with its package label. Honors the
    same ``--json`` / ``--explain`` output modes as single-root find. Semantic
    search and ``--age`` are not (yet) part of merged mode and are rejected here.
    """
    from .index import build_index, index_path, save_index
    from .monorepo import discover_roots, merged_search, package_label
    from .render import format_results
    from .serialize import results_to_dict

    if getattr(args, "semantic", False) or getattr(args, "age", False):
        print("deja: --semantic and --age cannot be combined with --all or --root", file=sys.stderr)
        return 2

    explicit = getattr(args, "roots", None)
    roots = discover_roots(root) if not explicit else [Path(r) for r in explicit]
    if explicit:
        bad = [r for r in roots if not r.is_dir()]
        if bad:
            print(f"deja: not a directory: {bad[0]}", file=sys.stderr)
            return 2

    for target in roots:
        if not index_path(target).is_file():
            tconfig = _load_project_config(target, no_config=getattr(args, "no_config", False))
            index = build_index(target, exclude=tconfig.exclude, include=tconfig.include)
            save_index(index, target)
            label = package_label(root, target)
            print(
                f"\U0001f50e [{label}] no index yet \u2014 built one first\u2026", file=sys.stderr
            )

    merged = merged_search(
        root,
        query,
        roots=roots,
        limit=limit,
        sig=sig,
        intent=args.intent,
        weights=config.weights,
        exclude=config.exclude,
        include=config.include,
    )
    for label in merged.built:
        print(f"\U0001f50e [{label}] no index yet \u2014 built one first\u2026", file=sys.stderr)

    scored = [m.scored for m in merged.results]
    # Map ScoredRecord identity -> package label so render/serialize can tag hits.
    packages = {id(m.scored): m.package for m in merged.results}

    if getattr(args, "as_json", False):
        import json as _json

        doc = results_to_dict(scored, query=query, sig=sig, intent=args.intent, semantic=False)
        doc["monorepo"] = True
        doc["packages"] = sorted(merged.roots)
        for entry, m in zip(doc.get("results", []), merged.results, strict=True):
            entry["package"] = m.package
        print(_json.dumps(doc, indent=2, ensure_ascii=False))
        return 0 if scored else 1

    header_query = query if query.strip() else (sig or "")
    print(format_results(header_query, scored, explain=args.explain, packages=packages))
    return 0 if scored else 1


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

    config = _load_project_config(root, no_config=getattr(args, "no_config", False))
    limit = args.limit if args.limit is not None else DEFAULT_LIMIT

    # Monorepo mode (issue #42): merged multi-root search across package indexes.
    if getattr(args, "all_roots", False) or getattr(args, "roots", None):
        return _find_merged(args, root, query, sig, config, limit=limit)

    try:
        index = load_index(root)
    except FileNotFoundError:
        # No index yet: build one on the fly so first-run is friction-free.
        print("\U0001f50e No index yet \u2014 building one first\u2026", file=sys.stderr)
        index = build_index(root, exclude=config.exclude, include=config.include)
        save_index(index, root)

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
            weights=config.weights,
        )

    if getattr(args, "as_json", False):
        # Machine-readable path (M6): stable schema, no ANSI, no personality.
        import json as _json

        from .serialize import results_to_dict

        doc = results_to_dict(
            results, query=query, sig=sig, intent=args.intent, semantic=semantic_used
        )
        if getattr(args, "age", False):
            # Attach the git "last touched" facts per result (cached per file).
            from .gitmeta import GitAgeCache

            cache = GitAgeCache(str(Path(root).resolve()))
            for entry, s in zip(doc.get("results", []), results, strict=True):
                meta = cache.get(s.record.file)
                entry["last_touched"] = (
                    {
                        "relative": meta.relative,
                        "iso_date": meta.iso_date,
                        "short_hash": meta.short_hash,
                    }
                    if meta is not None
                    else None
                )
        print(_json.dumps(doc, indent=2, ensure_ascii=False))
        return 0 if results else 1

    # Header still reads naturally when only a shape was given.
    header_query = query if query.strip() else (sig or "")
    ages = None
    if getattr(args, "age", False):
        # Build a {file: GitAge} map, one cached git call per distinct file, so a
        # large result set never spawns one process per result (issue #41).
        from .gitmeta import GitAgeCache

        cache = GitAgeCache(str(Path(root).resolve()))
        ages = {s.record.file: cache.get(s.record.file) for s in results}
    print(format_results(header_query, results, explain=args.explain, ages=ages))
    return 0 if results else 1


def cmd_why(args: argparse.Namespace) -> int:
    """Handle `deja why <query>`: explain WHY each match ranked (issue #35).

    Runs the same fuzzy/shape ranking as ``deja find`` but presents the *score
    breakdown* front-and-center: each applicable signal (name / doc / sig) with
    its raw 0-100 score, the effective weight it was multiplied by (honoring
    ``.dejafunc.toml`` ``[search].weights`` when present), and the resulting
    weighted contribution. The blended total is the ``max`` of those weighted
    values, so the winning signal makes the ranking basis inspectable — exactly
    what agents deciding *reuse vs write new* need. ``--json`` emits the stable
    ``scores`` object for CI/agent consumption. Exit code mirrors ``find`` (0
    when there's at least one match, 1 when none) so it stays scriptable.
    """
    from .index import build_index, load_index, save_index
    from .render import format_why
    from .search import DEFAULT_LIMIT, search

    query = args.query or ""
    sig = args.sig

    # Same lone-positional-is-a-path disambiguation as ``deja find``.
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

    config = _load_project_config(root, no_config=getattr(args, "no_config", False))

    try:
        index = load_index(root)
    except FileNotFoundError:
        print("\U0001f50e No index yet — building one first…", file=sys.stderr)
        index = build_index(root, exclude=config.exclude, include=config.include)
        save_index(index, root)

    limit = args.limit if args.limit is not None else DEFAULT_LIMIT
    results = search(
        query,
        index.records,
        limit=limit,
        sig=sig,
        intent=args.intent,
        weights=config.weights,
    )

    if getattr(args, "as_json", False):
        import json as _json

        from .serialize import results_to_dict

        doc = results_to_dict(results, query=query, sig=sig, intent=args.intent)
        print(_json.dumps(doc, indent=2, ensure_ascii=False))
        return 0 if results else 1

    header_query = query if query.strip() else (sig or "")
    print(format_why(header_query, results))
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


def cmd_stale(args: argparse.Namespace) -> int:
    """Handle `deja stale`: list dead-code candidates (PLAN.md §8 #9).

    Loads the index for the target repo (auto-building it first if none exists,
    exactly like ``deja find``/``deja dupes``/``deja stats`` so first-run just
    works), then scans the *same* file set the index was built from for
    word-boundary references to each function's name. Functions whose names are
    never referenced outside their own definition are reported as heuristic
    dead-code *candidates*.

    The reference scan always covers every indexed file; ``--lang`` narrows only
    which functions are reported, not which files are searched (so a Python
    helper called only from JS is not falsely flagged). Exit code is 0 when any
    candidate is found and 1 when nothing is stale, so it's scriptable / CI-
    friendly.
    """
    # Imported lazily so `deja --version` / `deja hello` stay dependency-free.
    from .index import build_index, load_index, save_index
    from .render import format_stale
    from .stale import find_stale
    from .walker import iter_source_files

    root = Path(args.path)
    if not root.is_dir():
        print(f"deja: not a directory: {root}", file=sys.stderr)
        return 2

    try:
        index = load_index(root)
    except FileNotFoundError:
        # No index yet: build one on the fly so first-run is friction-free
        # (mirrors `deja find` / `deja dupes` / `deja stats`).
        print("\U0001f50e No index yet — building one first…", file=sys.stderr)
        index = build_index(root)
        save_index(index, root)

    # Read source relative to the repo root, matching how `build_index` reads it
    # (same encoding + error policy). Returning None skips an unreadable file.
    root_path = Path(root).resolve()

    def read_source(rel: str) -> str | None:
        try:
            return (root_path / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    # Scan exactly the files `deja index` would parse (same gitignore/skip rules).
    files = iter_source_files(root_path)

    report = find_stale(
        index.records,
        files,
        read_source,
        ignore=args.ignore,
        lang=args.lang,
    )

    if getattr(args, "as_json", False):
        # Machine-readable path: stable schema, no ANSI, no personality.
        import json as _json

        from .serialize import stale_to_dict

        print(_json.dumps(stale_to_dict(report), indent=2, ensure_ascii=False))
        return 0 if not report.is_empty else 1

    print(format_stale(report))
    return 0 if not report.is_empty else 1


def cmd_explain(args: argparse.Namespace) -> int:
    """Handle `deja explain <name>`: definition, callers, and staleness (PLAN.md §8 #6).

    Loads the target repo's index (auto-building it first if none exists, exactly
    like ``deja find``/``deja stale`` so first-run just works), resolves *name*
    against it (exact, else fuzzy), then reports the definition (file:line,
    signature, docstring), a textual caller list scanned over the same files
    ``deja index`` parsed, and a git "last touched" relative age.

    Exit codes are scriptable: ``0`` for a single resolved function, ``2`` for an
    ambiguous name (candidate list printed to stdout, prompting disambiguation),
    and ``1`` for an unknown name (friendly miss, matching ``deja find``).
    """
    # Imported lazily so `deja --version` / `deja hello` stay dependency-free.
    from .explain import DEFAULT_MAX_CALLERS, Ambiguous, explain
    from .gitmeta import GitAgeCache
    from .index import build_index, load_index, save_index
    from .render import (
        format_ambiguous,
        format_explain_miss,
        format_explanation,
    )
    from .walker import iter_source_files

    root = Path(args.path)
    if not root.is_dir():
        print(f"deja: not a directory: {root}", file=sys.stderr)
        return 2

    max_callers = args.max_callers if args.max_callers is not None else DEFAULT_MAX_CALLERS

    try:
        index = load_index(root)
    except FileNotFoundError:
        # No index yet: build one on the fly so first-run is friction-free
        # (mirrors `deja find` / `deja stale`).
        print("\U0001f50e No index yet — building one first…", file=sys.stderr)
        index = build_index(root)
        save_index(index, root)

    # Read source relative to the repo root, matching how `build_index` reads it.
    root_path = Path(root).resolve()

    def read_source(rel: str) -> str | None:
        try:
            return (root_path / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    # Staleness reader: full git "last touched" facts (relative age + ISO date +
    # short hash) via a per-file cached git call, degrading to None (age omitted)
    # outside a git repo or for a deleted file. Bound to this repo root.
    age_cache = GitAgeCache(str(root_path))

    def git_age(rel: str):
        return age_cache.get(rel)

    # Scan exactly the files `deja index` would parse (same gitignore/skip rules).
    files = iter_source_files(root_path)

    result = explain(
        args.name,
        index.records,
        files,
        read_source,
        git_age=git_age,
        max_callers=max_callers,
    )

    if getattr(args, "as_json", False):
        # Machine-readable path: stable schema, no ANSI, no personality.
        import json as _json

        from .serialize import (
            ambiguous_to_dict,
            explain_miss_to_dict,
            explanation_to_dict,
        )

        if result is None:
            print(_json.dumps(explain_miss_to_dict(args.name), indent=2, ensure_ascii=False))
            return 1
        if isinstance(result, Ambiguous):
            print(_json.dumps(ambiguous_to_dict(result), indent=2, ensure_ascii=False))
            return 2
        print(_json.dumps(explanation_to_dict(result), indent=2, ensure_ascii=False))
        return 0

    if result is None:
        print(format_explain_miss(args.name))
        return 1
    if isinstance(result, Ambiguous):
        print(format_ambiguous(result))
        return 2
    print(format_explanation(result))
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    """Handle `deja export`: write the index to a portable file (PLAN.md §8 #12).

    Loads the target repo's index (auto-building it first if none exists, exactly
    like ``deja find`` so a fresh checkout exports cleanly), then writes a
    portable JSON document — the on-disk index shape plus ``exported_at`` and an
    optional ``root_name``. With ``-o -`` it streams that JSON to **stdout** and
    keeps stdout pipe-clean (the only chatter — the auto-build note and the
    success line — goes to stderr), so ``deja export -o - | ...`` is safe.
    """
    # Imported lazily so `deja --version` / `deja hello` stay dependency-free.
    from .export import DEFAULT_EXPORT_FILE, export_index, write_export
    from .index import build_index, load_index, save_index

    root = Path(args.path)
    if not root.is_dir():
        print(f"deja: not a directory: {root}", file=sys.stderr)
        return 2

    try:
        index = load_index(root)
    except FileNotFoundError:
        # No index yet: build one on the fly so first-run / fresh-clone just works
        # (mirrors `deja find`). The note goes to stderr so `-o -` stays clean.
        print("\U0001f50e No index yet \u2014 building one first\u2026", file=sys.stderr)
        config = _load_project_config(root, no_config=getattr(args, "no_config", False))
        index = build_index(root, exclude=config.exclude, include=config.include)
        save_index(index, root)

    root_name = root.resolve().name or None
    output = args.output

    if output == "-":
        # stdout mode: only the JSON goes to stdout; the confirmation is stderr.
        sys.stdout.write(export_index(index, root_name=root_name))
        print(
            f"\U0001f4e6 Exported {len(index)} functions to stdout.",
            file=sys.stderr,
        )
        return 0

    if output is not None:
        # Explicit -o FILE is taken as given (relative to the CWD, like any tool).
        out_path = Path(output)
    else:
        # Default lands at the *repo root* so `deja-index.json` sits alongside the
        # code it describes (ready to check in), regardless of the CWD.
        out_path = root / DEFAULT_EXPORT_FILE
    written = write_export(index, out_path, root_name=root_name)
    print(f"\U0001f4e6 Exported {len(index)} functions \u2192 {written}")
    print("   Check it in so a fresh clone can `deja import` it (skip the cold index run).")
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    """Handle `deja import`: hydrate `.dejafunc/index.json` from a portable export.

    Reads a file produced by ``deja export`` and writes it to the target repo's
    ``.dejafunc/index.json`` so a subsequent ``deja find`` returns results with
    **no** prior ``deja index`` run. ``schema_version`` is validated first: an
    incompatible/newer version is a clean, non-crashing error (exit 2), matching
    the forward-compat tone of the config/index loaders.
    """
    # Imported lazily so `deja --version` / `deja hello` stay dependency-free.
    from .export import ExportError, read_export
    from .index import save_index

    root = Path(args.path)
    if not root.is_dir():
        print(f"deja: not a directory: {root}", file=sys.stderr)
        return 2

    try:
        index = read_export(args.file)
    except FileNotFoundError as exc:
        print(f"deja: {exc}", file=sys.stderr)
        return 2
    except ExportError as exc:
        # Schema mismatch or malformed JSON: a clear line, never a traceback.
        print(f"\U0001f9ea deja: {exc}", file=sys.stderr)
        return 2

    out = save_index(index, root)
    print(f"\U0001f4e5 Imported {len(index)} functions \u2192 {out}")
    print("   `deja find <query>` now works here without running `deja index` first.")
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    """Handle `deja diff`: compare two index snapshots (issue #36).

    Loads two index/export JSON files (the same portable shape ``deja export``
    writes; a live ``.dejafunc/index.json`` also loads, since exports are a
    superset), reports functions added / removed / renamed, and prints them.
    Exit code is 1 when the snapshots differ and 0 when they're identical, so
    it's scriptable / CI-friendly ("fail the check if the inventory changed").
    """
    # Imported lazily so `deja --version` / `deja hello` stay dependency-free.
    from .diff import RENAME_THRESHOLD, diff_indexes
    from .export import ExportError, read_export
    from .render import format_diff

    threshold = args.rename_threshold if args.rename_threshold is not None else RENAME_THRESHOLD
    if not 0.0 <= threshold <= 100.0:
        print("deja: --rename-threshold must be between 0 and 100", file=sys.stderr)
        return 2

    snapshots = {}
    for label, path in (("old", args.old), ("new", args.new)):
        try:
            snapshots[label] = read_export(path)
        except FileNotFoundError as exc:
            print(f"deja: {exc}", file=sys.stderr)
            return 2
        except ExportError as exc:
            print(f"\U0001f9ea deja: {path}: {exc}", file=sys.stderr)
            return 2

    report = diff_indexes(snapshots["old"], snapshots["new"], rename_threshold=threshold)

    if getattr(args, "as_json", False):
        # Machine-readable path: stable schema, no ANSI, no personality.
        import json as _json

        from .serialize import diff_to_dict

        print(_json.dumps(diff_to_dict(report), indent=2, ensure_ascii=False))
        return 1 if report.any_changes else 0

    print(format_diff(report))
    return 1 if report.any_changes else 0


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
