"""Stable JSON serialization for scripting and agents (PLAN.md §6 M6).

Both ``deja find --json`` and the ``deja mcp`` server (``deja/mcp.py``) emit the
*same* result shape, defined here so the schema lives in exactly one place.

The contract (``SCHEMA_VERSION``):

A ``find`` result document is::

    {
      "schema_version": 1,
      "query": "slugify",          # the textual query (may be "")
      "sig": "(str)->bool",        # the --sig shape, or null
      "intent": false,             # whether intent weighting was on
      "semantic": false,           # whether embedding-based ranking was used
      "count": 2,                  # number of matches returned
      "results": [ <match>, ... ]  # best first
    }

Each ``<match>`` is::

    {
      "name": "slugify",
      "qualname": "text.slugify",
      "file": "src/text.py",
      "line": 42,
      "signature": "(value: str) -> str",
      "docstring": "Turn a string into a URL-safe slug.",
      "lang": "python",
      "score": 88.0,              # blended 0-100 relevance
      "breakdown": {              # per-signal scores; key omitted if N/A
        "name": 88.0,
        "doc": 60.0,
        "sig": null
      }
    }

``deja dupes --json`` (PLAN.md §8 #1) emits a sibling document defined here too::

    {
      "schema_version": 1,
      "threshold": 75.0,         # similarity cutoff used
      "count": 2,                # number of clusters returned
      "clusters": [
        {
          "size": 3,            # members in this cluster
          "score": 88.0,        # representative (avg pairwise) tightness
          "members": [ <record>, ... ]  # by file:line
        },
        ...
      ]
    }

where each ``<record>`` is the function shape below *without* the search-only
``score``/``breakdown`` keys.

``deja stats --json`` (PLAN.md §8 #10) emits a sibling document defined here::

    {
      "schema_version": 1,
      "total_functions": 180,        # total indexed function records
      "total_files": 24,             # distinct files represented
      "top": 10,                     # the --top cap applied to leaderboards
      "languages": [                 # full breakdown, most functions first
        {"lang": "python", "count": 142},
        {"lang": "javascript", "count": 38}
      ],
      "top_names": [                 # most-duplicated bare names (count >= 2)
        {"name": "parse", "count": 6},
        {"name": "slugify", "count": 4}
      ],
      "biggest_files": [            # files with the most functions
        {"file": "src/text.py", "count": 19}
      ]
    }

``deja hook check --json`` (PLAN.md §8 #3) emits a sibling document defined here::

    {
      "schema_version": 1,
      "threshold": 75.0,         # similarity cutoff used
      "strict": false,           # whether the hook blocks on a match
      "count": 1,                # number of staged dupes found
      "matches": [
        {
          "score": 88.0,        # blended similarity of the pair
          "staged": <record>,   # the about-to-be-committed function
          "existing": <record>  # the function it resembles
        },
        ...
      ]
    }

``deja stale --json`` (PLAN.md §8 #9) emits a sibling document defined here::

    {
      "schema_version": 1,
      "lang": null,              # the --lang filter, or null for all
      "ignore": ["__*__", ...],  # effective ignore globs applied
      "scanned": 180,            # functions considered (after --lang filter)
      "ignored": 12,             # functions skipped by an ignore pattern
      "count": 3,                # number of dead-code candidates
      "candidates": [
        {
          "references": 0,      # external word-boundary references found
          "function": <record>  # the bare function shape (no score/breakdown)
        },
        ...
      ]
    }

Keeping rendering (``render.py``) and serialization (here) separate means the
terminal output and the machine output can evolve independently, and the schema
is trivial for an agent to consume.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .dupes import Cluster
from .hook import Match
from .parsers import FunctionRecord
from .search import ScoredRecord
from .stale import StaleFunction, StaleReport
from .stats import Stats

#: Bump when the emitted JSON shape changes incompatibly. Independent of the
#: on-disk index ``SCHEMA_VERSION`` — this versions the *wire* format.
SCHEMA_VERSION = 1


def _round(value: float | None) -> float | None:
    """Round a 0-100 score to one decimal (or pass through ``None``)."""
    return None if value is None else round(value, 1)


def scored_to_dict(scored: ScoredRecord) -> dict[str, Any]:
    """Serialize one :class:`~deja.search.ScoredRecord` to the stable match shape."""
    r = scored.record
    breakdown = {
        "name": _round(scored.breakdown.name),
        "doc": _round(scored.breakdown.docstring),
        "sig": _round(scored.breakdown.signature),
    }
    return {
        "name": r.name,
        "qualname": r.qualname or r.name,
        "file": r.file,
        "line": r.line,
        "signature": r.signature,
        "docstring": r.docstring,
        "lang": r.lang,
        "score": _round(scored.score),
        "breakdown": breakdown,
    }


def results_to_dict(
    results: Iterable[ScoredRecord],
    *,
    query: str = "",
    sig: str | None = None,
    intent: bool = False,
    semantic: bool = False,
) -> dict[str, Any]:
    """Build the top-level ``find`` result document (see module docstring).

    Args:
        results: Ranked matches from :func:`deja.search.search` (or
            :func:`deja.semantic.semantic_search`).
        query: The textual query that produced *results* (echoed back).
        sig: The ``--sig`` shape string, if any.
        intent: Whether intent weighting was applied.
        semantic: Whether embedding-based semantic ranking produced *results*
            (when true, per-match ``breakdown`` values are all ``null``).

    Returns:
        A JSON-serializable dict with a stable, documented schema.
    """
    matches = [scored_to_dict(s) for s in results]
    return {
        "schema_version": SCHEMA_VERSION,
        "query": query,
        "sig": sig,
        "intent": bool(intent),
        "semantic": bool(semantic),
        "count": len(matches),
        "results": matches,
    }


def record_to_dict(record: FunctionRecord) -> dict[str, Any]:
    """Serialize a bare :class:`~deja.parsers.base.FunctionRecord` (no scoring).

    Used by the ``dupes`` document, where members carry no per-query relevance —
    only their location and shape. Mirrors the field set of
    :func:`scored_to_dict` minus ``score``/``breakdown``.
    """
    return {
        "name": record.name,
        "qualname": record.qualname or record.name,
        "file": record.file,
        "line": record.line,
        "signature": record.signature,
        "docstring": record.docstring,
        "lang": record.lang,
    }


def cluster_to_dict(cluster: Cluster) -> dict[str, Any]:
    """Serialize one :class:`~deja.dupes.Cluster` to the stable cluster shape."""
    return {
        "size": cluster.size,
        "score": _round(cluster.score),
        "members": [record_to_dict(r) for r in cluster.members],
    }


def clusters_to_dict(
    clusters: Iterable[Cluster],
    *,
    threshold: float,
) -> dict[str, Any]:
    """Build the top-level ``dupes`` result document (see module docstring).

    Args:
        clusters: Near-duplicate clusters from :func:`deja.dupes.find_clusters`,
            already sorted largest-first.
        threshold: The similarity cutoff (0-100) that produced *clusters*.

    Returns:
        A JSON-serializable dict with a stable, documented schema.
    """
    items = [cluster_to_dict(c) for c in clusters]
    return {
        "schema_version": SCHEMA_VERSION,
        "threshold": _round(threshold),
        "count": len(items),
        "clusters": items,
    }


def stats_to_dict(stats: Stats) -> dict[str, Any]:
    """Build the top-level ``stats`` result document (see module docstring).

    The numbers exactly mirror the text view rendered by
    :func:`deja.render.format_stats` (same totals, same already-capped
    leaderboards), so an agent consuming JSON sees what a human would.

    Args:
        stats: Aggregated inventory from :func:`deja.stats.compute_stats`.

    Returns:
        A JSON-serializable dict with a stable, documented schema.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "total_functions": stats.total_functions,
        "total_files": stats.total_files,
        "top": stats.top,
        "languages": [{"lang": lang, "count": n} for lang, n in stats.languages],
        "top_names": [{"name": name, "count": n} for name, n in stats.top_names],
        "biggest_files": [{"file": path, "count": n} for path, n in stats.biggest_files],
    }


def stale_to_dict(report: StaleReport) -> dict[str, Any]:
    """Build the top-level ``stale`` result document (see module docstring).

    Args:
        report: Result of :func:`deja.stale.find_stale` (candidates already
            deterministically sorted).

    Returns:
        A JSON-serializable dict with a stable, documented schema.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "lang": report.lang,
        "ignore": list(report.ignore_patterns),
        "scanned": report.scanned_functions,
        "ignored": report.ignored,
        "count": len(report.candidates),
        "candidates": [stale_function_to_dict(s) for s in report.candidates],
    }


def stale_function_to_dict(stale: StaleFunction) -> dict[str, Any]:
    """Serialize one :class:`~deja.stale.StaleFunction` to the stable shape."""
    return {
        "references": stale.references,
        "function": record_to_dict(stale.record),
    }


def match_to_dict(match: Match) -> dict[str, Any]:
    """Serialize one :class:`~deja.hook.Match` to the stable hook-match shape."""
    return {
        "score": _round(match.score),
        "staged": record_to_dict(match.staged),
        "existing": record_to_dict(match.existing),
    }


def matches_to_dict(
    matches: Iterable[Match],
    *,
    threshold: float,
    strict: bool = False,
) -> dict[str, Any]:
    """Build the top-level ``hook check`` result document (see module docstring).

    Args:
        matches: Redundancy matches from :func:`deja.hook.check_staged`,
            already sorted strongest-first.
        threshold: The similarity cutoff (0-100) that produced *matches*.
        strict: Whether the hook is configured to block (echoed for tooling).

    Returns:
        A JSON-serializable dict with a stable, documented schema.
    """
    items = [match_to_dict(m) for m in matches]
    return {
        "schema_version": SCHEMA_VERSION,
        "threshold": _round(threshold),
        "strict": bool(strict),
        "count": len(items),
        "matches": items,
    }
