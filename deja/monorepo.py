"""Monorepo mode: multi-root indexing + a merged ``deja find`` (issue #42).

Real work happens in monorepos with many packages, and redundancy is worst
exactly there — the same ``slugify`` written independently per package is the
pain ``deja-func`` exists to kill, but it's invisible across package boundaries
when each package has its own isolated index.

This module adds three small pieces on top of the single-root core:

* :func:`discover_roots` — find package roots under a base directory (dirs with a
  ``pyproject.toml`` / ``package.json`` / ``go.mod`` / ``Cargo.toml`` marker),
  so ``deja index`` can build a per-package ``.dejafunc/index.json`` for each.
* :func:`package_label` — a stable, human-readable label for a root relative to
  the base (``"."`` for the base itself, else the POSIX relative path).
* :func:`merged_search` — load every discovered package index (building any that
  are missing) and rank across all of them at once, tagging each hit with its
  package so a merged view stays legible.

Single-root behavior is untouched: with no extra roots configured the caller
uses the plain :func:`deja.search.search` path exactly as before.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .index import Index, build_index, index_path, load_index, save_index
from .search import DEFAULT_LIMIT, MIN_SCORE, ScoredRecord, search
from .walker import ALWAYS_SKIP_DIRS

#: Files whose presence marks a directory as a package root. One per ecosystem
#: we already parse (Python, JS/TS, Go, Rust).
PACKAGE_MARKERS = (
    "pyproject.toml",
    "package.json",
    "go.mod",
    "Cargo.toml",
)


def _skip_dirs() -> frozenset[str]:
    """Directory names discovery never descends into (vendored / build noise).

    Mirrors the walker's skip set so root auto-detection doesn't wander into
    ``node_modules`` / ``.git`` / build output and surface junk package roots.
    Falls back to a small built-in set if the walker exposes none.
    """
    try:
        return frozenset(ALWAYS_SKIP_DIRS)
    except Exception:  # pragma: no cover - defensive; walker always defines it
        return frozenset({".git", "node_modules", ".dejafunc", "__pycache__"})


def package_label(base: str | os.PathLike[str], root: str | os.PathLike[str]) -> str:
    """Return the package label for *root* relative to *base*.

    The base directory itself labels as ``"."``; every other root is its POSIX
    relative path (e.g. ``packages/api``). Roots outside *base* fall back to the
    root's own basename so a label is always non-empty and readable.
    """
    base_p = Path(base).resolve()
    root_p = Path(root).resolve()
    if root_p == base_p:
        return "."
    try:
        return root_p.relative_to(base_p).as_posix()
    except ValueError:
        return root_p.name


def discover_roots(base: str | os.PathLike[str]) -> list[Path]:
    """Discover package roots under *base* by walking for marker files.

    A directory is a package root if it directly contains any of
    :data:`PACKAGE_MARKERS`. We descend into subdirectories even after finding a
    marker so nested packages (a monorepo whose root also has a ``pyproject.toml``)
    are all found. Vendored / build dirs (:func:`_skip_dirs`) are pruned.

    Returns:
        Resolved directory paths, sorted for determinism. If *base* itself has no
        marker and no marked subdirectory is found, returns ``[base]`` so callers
        always get at least one root (single-root fallback, no regression).
    """
    base_p = Path(base).resolve()
    skip = _skip_dirs()
    roots: list[Path] = []

    for dirpath, dirnames, filenames in os.walk(base_p):
        # Prune skip dirs in place so os.walk never descends into them.
        dirnames[:] = sorted(d for d in dirnames if d not in skip)
        if any(marker in filenames for marker in PACKAGE_MARKERS):
            roots.append(Path(dirpath).resolve())

    if not roots:
        return [base_p]
    return sorted(set(roots))


@dataclass(frozen=True, slots=True)
class MergedResult:
    """A ranked match plus the package (root label) it came from (issue #42)."""

    scored: ScoredRecord
    package: str


@dataclass
class MergedSearch:
    """Outcome of a merged multi-root search: the roots hit and the results."""

    results: list[MergedResult] = field(default_factory=list)
    #: ``{label: root Path}`` for every package index that was searched.
    roots: dict[str, Path] = field(default_factory=dict)
    #: Labels of roots whose index had to be built on the fly (were missing).
    built: list[str] = field(default_factory=list)


def _load_or_build(root: Path, *, exclude=(), include=()) -> tuple[Index, bool]:
    """Load *root*'s index, building+saving it first if none exists yet.

    Returns ``(index, built)`` where *built* is ``True`` when the index had to be
    created on the fly (so the caller can report first-run builds).
    """
    if index_path(root).is_file():
        return load_index(root), False
    idx = build_index(root, exclude=exclude, include=include)
    save_index(idx, root)
    return idx, True


def merged_search(
    base: str | os.PathLike[str],
    query: str,
    *,
    roots: list[str | os.PathLike[str]] | None = None,
    limit: int = DEFAULT_LIMIT,
    min_score: float = MIN_SCORE,
    sig=None,
    intent: bool = False,
    weights: object = None,
    exclude=(),
    include=(),
) -> MergedSearch:
    """Search every discovered package index under *base* and merge the results.

    Each root's index is loaded (or built on first use), searched independently
    with the shared :func:`deja.search.search` ranking, and its hits tagged with
    the package label. Results across all packages are then merged and re-ranked
    globally, de-duplicating identical ``(package, file, line)`` records (the
    same function reachable via overlapping roots) while keeping cross-package
    near-dupes — the *same* file path in two different packages — visible, which
    feeds naturally into ``deja dupes`` later.

    Args:
        base: The monorepo base directory (used to compute package labels).
        query: The search query (name/intent text). May be empty with *sig*.
        roots: Explicit roots to search; when ``None`` they're auto-discovered
            via :func:`discover_roots`.
        limit: Max merged results to return.
        min_score / sig / intent / weights: Forwarded to
            :func:`deja.search.search` unchanged (per-root scoring is identical
            to single-root find).
        exclude / include: Walker globs used only when a root's index must be
            built on the fly.

    Returns:
        A :class:`MergedSearch` with the merged, globally-ranked results and the
        set of roots searched.
    """
    base_p = Path(base).resolve()
    root_paths = [Path(r).resolve() for r in roots] if roots is not None else discover_roots(base_p)

    out = MergedSearch()
    merged: list[MergedResult] = []
    seen: set[tuple[str, str, int]] = set()

    for root in root_paths:
        label = package_label(base_p, root)
        out.roots[label] = root
        try:
            index, built = _load_or_build(root, exclude=exclude, include=include)
        except FileNotFoundError:  # pragma: no cover - _load_or_build builds instead
            continue
        if built:
            out.built.append(label)

        # Search this package with the full result set (limit applied after the
        # global merge) so a strong hit in one package can't be crowded out by a
        # weaker sibling before merging.
        hits = search(
            query,
            index.records,
            limit=len(index.records) or 1,
            min_score=min_score,
            sig=sig,
            intent=intent,
            weights=weights,
        )
        for s in hits:
            key = (label, s.record.file, s.record.line)
            if key in seen:
                continue
            seen.add(key)
            merged.append(MergedResult(scored=s, package=label))

    # Global re-rank: score desc, then package/file/line for a stable, readable
    # ordering across packages (deterministic for tests and diffs).
    merged.sort(
        key=lambda m: (-m.scored.score, m.package, m.scored.record.file, m.scored.record.line)
    )
    out.results = merged[: max(0, limit)]
    return out
