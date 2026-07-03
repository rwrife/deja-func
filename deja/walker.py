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


def _compile_patterns(patterns):
    """Compile *patterns* into a PathSpec using gitignore semantics, or ``None``.

    Shared by the ``.gitignore`` loader and the ``.dejafunc.toml`` config globs
    so both honor identical ``**`` / directory / negation behavior (pathspec's
    gitwildmatch). Returns ``None`` when there's nothing to match or pathspec is
    unavailable, so callers can cheaply skip the work.
    """
    if pathspec is None:
        return None
    patterns = [p for p in patterns if p and p.strip()]
    if not patterns:
        return None
    # Prefer the newer "gitignore" pattern factory (pathspec >= 0.12 renamed it);
    # fall back to the long-standing "gitwildmatch" name on older releases.
    for style in ("gitignore", "gitwildmatch"):
        try:
            return pathspec.PathSpec.from_lines(style, patterns)
        except (KeyError, ValueError, LookupError):  # pragma: no cover
            continue
    return None  # pragma: no cover


def _load_gitignore(root: Path):
    """Return a compiled PathSpec for *root*/.gitignore, or ``None``."""
    gitignore = root / ".gitignore"
    if not gitignore.is_file():
        return None
    try:
        lines = gitignore.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:  # pragma: no cover
        return None
    return _compile_patterns(lines)


def _spec_matches_path(spec, rel_posix: str, parts: list[str]) -> bool:
    """Return whether *spec* matches *rel_posix* or any of its ancestor dirs.

    A file is considered matched if it — or any ancestor directory — matches, so
    a directory pattern (``build/``) excludes everything beneath it, mirroring
    how gitignore prunes whole subtrees.
    """
    if spec.match_file(rel_posix):
        return True
    for i in range(1, len(parts)):
        ancestor = "/".join(parts[:i]) + "/"
        if spec.match_file(ancestor):
            return True
    return False


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
    exclude_spec=None,
    include_spec=None,
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
        exclude_spec: Optional pre-compiled ``PathSpec`` of extra exclude globs
            from ``.dejafunc.toml`` (issue #25), layered on the gitignore rules.
        include_spec: Optional pre-compiled ``PathSpec`` of ``.dejafunc.toml``
            include globs that re-admit a config-excluded file (include beats the
            config exclude, but never overrides gitignore/skip-dir safety rules).
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
    if spec is not None and _spec_matches_path(spec, rel_posix, parts):
        return False

    # Config-level exclude globs (issue #25): drop unless a config include glob
    # explicitly re-admits the file. Gitignore/skip-dirs above stay authoritative.
    if exclude_spec is not None and _spec_matches_path(exclude_spec, rel_posix, parts):
        if include_spec is None or not include_spec.match_file(rel_posix):
            return False

    return True


def iter_source_files(
    root: str | os.PathLike[str],
    *,
    has_parser=None,
    exclude=(),
    include=(),
) -> Iterator[str]:
    """Yield repo-relative POSIX paths of parseable files under *root*.

    Args:
        root: Directory to walk.
        has_parser: Predicate ``(rel_path) -> bool`` deciding whether a file is
            parseable. Defaults to the parser registry, so only files with a
            registered language parser are yielded. Injectable for tests.
        exclude: Extra exclude globs (``.dejafunc.toml`` ``[index].exclude``,
            issue #25), layered on top of the gitignore / skip-dir rules. Uses
            gitignore glob semantics (``**``, trailing ``/`` for directories).
        include: Include globs (``[index].include``) that re-admit a file the
            config *exclude* dropped. Include beats the config exclude but never
            overrides ``.gitignore`` / :data:`ALWAYS_SKIP_DIRS` (those stay
            authoritative for safety — you can't accidentally index ``.git``).

    Skips :data:`ALWAYS_SKIP_DIRS` and any path matched by the root
    ``.gitignore``. Directory pruning happens in-place so we never walk into an
    ignored subtree.
    """
    root_path = Path(root).resolve()
    spec = _load_gitignore(root_path)
    exclude_spec = _compile_patterns(exclude)
    include_spec = _compile_patterns(include)

    if has_parser is None:
        has_parser = _default_has_parser

    def is_ignored(rel_posix: str, *, is_dir: bool) -> bool:
        if spec is None:
            return False
        candidate = rel_posix + "/" if is_dir else rel_posix
        return spec.match_file(candidate)

    def is_config_excluded(rel_posix: str, *, is_dir: bool) -> bool:
        """Config-exclude a path unless an include glob re-admits it."""
        if exclude_spec is None:
            return False
        candidate = rel_posix + "/" if is_dir else rel_posix
        if not exclude_spec.match_file(candidate):
            return False
        # A directory is only pruned when *no* include glob could reach inside
        # it; otherwise we must descend to let an included file survive.
        if is_dir and include_spec is not None:
            return False
        if include_spec is not None and include_spec.match_file(rel_posix):
            return False
        return True

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
            if is_config_excluded(rel_child, is_dir=True):
                continue
            kept.append(d)
        dirnames[:] = kept

        for name in filenames:
            rel_file = (rel_dir / name).as_posix()
            if is_ignored(rel_file, is_dir=False):
                continue
            if is_config_excluded(rel_file, is_dir=False):
                continue
            if not has_parser(rel_file):
                continue
            yield rel_file
