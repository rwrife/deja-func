"""Filesystem walk with ``.gitignore`` awareness.

Yields repo-relative paths of files we have a parser for, skipping anything the
repo's ``.gitignore`` excludes plus a hardcoded set of noise directories
(``.git``, ``.venv``, ``node_modules`` …) so we never descend into vendored or
virtual-env trees even when they aren't gitignored. Uses ``pathspec`` for proper
gitignore semantics (PLAN.md §5/§6).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

try:  # pathspec is a runtime dep (M2); degrade gracefully if missing.
    import pathspec
except ImportError:  # pragma: no cover - exercised only without the dep
    pathspec = None  # type: ignore[assignment]

# Directories we always skip, gitignore or not. These are virtual-env / vendor /
# tooling trees that would otherwise drown the index in noise.
ALWAYS_SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "env",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".eggs",
        "build",
        "dist",
        ".dejafunc",
    }
)


def _load_gitignore(root: Path):
    """Return a compiled PathSpec for *root*/.gitignore, or ``None``."""
    if pathspec is None:
        return None
    gitignore = root / ".gitignore"
    if not gitignore.is_file():
        return None
    try:
        lines = gitignore.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:  # pragma: no cover
        return None
    # Prefer the newer "gitignore" pattern factory (pathspec >= 0.12 renamed it);
    # fall back to the long-standing "gitwildmatch" name on older releases.
    for style in ("gitignore", "gitwildmatch"):
        try:
            return pathspec.PathSpec.from_lines(style, lines)
        except (KeyError, ValueError, LookupError):  # pragma: no cover
            continue
    return None  # pragma: no cover


def _default_has_parser(rel: str) -> bool:
    """Default parseability predicate: does a language parser claim *rel*?"""
    from .parsers import get_parser_for_path

    return get_parser_for_path(rel) is not None


def is_indexable_file(
    root: str | os.PathLike[str],
    rel_path: str,
    *,
    has_parser=None,
    spec=None,
) -> bool:
    """Return whether a single repo-relative path belongs in the index.

    Applies exactly the same rules as :func:`iter_source_files` — a registered
    parser, no :data:`ALWAYS_SKIP_DIRS` component, and not matched by the root
    ``.gitignore`` — but for one arbitrary path. The incremental watcher uses
    this to decide whether a just-touched file should be (re)parsed, so a watched
    edit honors the very same exclude rules as a full ``deja index`` run
    (issue #10).

    Args:
        root: Repo root the path is relative to.
        rel_path: Repo-relative path (POSIX or OS separators both accepted).
        has_parser: Optional parseability predicate; defaults to the registry.
        spec: Optional pre-compiled gitignore ``PathSpec`` to reuse across many
            calls (avoids re-reading ``.gitignore`` per file). Loaded on demand
            when ``None``.
    """
    if has_parser is None:
        has_parser = _default_has_parser

    rel_posix = Path(rel_path).as_posix()
    parts = rel_posix.split("/")

    # Any skipped directory anywhere in the path disqualifies it (mirrors the
    # in-place pruning os.walk does during a full walk).
    if any(part in ALWAYS_SKIP_DIRS for part in parts[:-1]):
        return False

    if not has_parser(rel_posix):
        return False

    if spec is None:
        spec = _load_gitignore(Path(root).resolve())
    if spec is not None:
        # A file is ignored if it — or any ancestor directory — is gitignored.
        if spec.match_file(rel_posix):
            return False
        for i in range(1, len(parts)):
            ancestor = "/".join(parts[:i]) + "/"
            if spec.match_file(ancestor):
                return False

    return True


def iter_source_files(
    root: str | os.PathLike[str],
    *,
    has_parser=None,
) -> Iterator[str]:
    """Yield repo-relative POSIX paths of parseable files under *root*.

    Args:
        root: Directory to walk.
        has_parser: Predicate ``(rel_path) -> bool`` deciding whether a file is
            parseable. Defaults to the parser registry, so only files with a
            registered language parser are yielded. Injectable for tests.

    Skips :data:`ALWAYS_SKIP_DIRS` and any path matched by the root
    ``.gitignore``. Directory pruning happens in-place so we never walk into an
    ignored subtree.
    """
    root_path = Path(root).resolve()
    spec = _load_gitignore(root_path)

    if has_parser is None:
        has_parser = _default_has_parser

    def is_ignored(rel_posix: str, *, is_dir: bool) -> bool:
        if spec is None:
            return False
        candidate = rel_posix + "/" if is_dir else rel_posix
        return spec.match_file(candidate)

    for dirpath, dirnames, filenames in os.walk(root_path):
        cur = Path(dirpath)
        rel_dir = cur.relative_to(root_path)

        # Prune unwanted directories in-place (affects os.walk descent).
        kept: list[str] = []
        for d in dirnames:
            if d in ALWAYS_SKIP_DIRS:
                continue
            rel_child = (rel_dir / d).as_posix()
            if is_ignored(rel_child, is_dir=True):
                continue
            kept.append(d)
        dirnames[:] = kept

        for name in filenames:
            rel_file = (rel_dir / name).as_posix()
            if is_ignored(rel_file, is_dir=False):
                continue
            if not has_parser(rel_file):
                continue
            yield rel_file
