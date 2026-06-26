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

Keeping rendering (``render.py``) and serialization (here) separate means the
terminal output and the machine output can evolve independently, and the schema
is trivial for an agent to consume.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .dupes import Cluster
from .parsers import FunctionRecord
from .search import ScoredRecord

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
) -> dict[str, Any]:
    """Build the top-level ``find`` result document (see module docstring).

    Args:
        results: Ranked matches from :func:`deja.search.search`.
        query: The textual query that produced *results* (echoed back).
        sig: The ``--sig`` shape string, if any.
        intent: Whether intent weighting was applied.

    Returns:
        A JSON-serializable dict with a stable, documented schema.
    """
    matches = [scored_to_dict(s) for s in results]
    return {
        "schema_version": SCHEMA_VERSION,
        "query": query,
        "sig": sig,
        "intent": bool(intent),
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
