"""Ranking for `deja find`: fuzzy match a query against indexed functions.

The scoring question is always the same (PLAN.md §1): *"Have I already written
something that does this?"* So we score each :class:`~deja.parsers.base.FunctionRecord`
against the query on two signals and keep the best:

* **name / qualname** — catches "I'm about to write ``slugify``" lookups.
* **docstring** — catches intent queries ("parse an ISO date") that won't match
  any identifier but do match the human description.

We use ``rapidfuzz`` (fast, C-backed) for the fuzzy comparisons. Name matches are
weighted a touch higher than docstring matches so an exact-ish name still wins
over a loose prose hit, but docstrings matter enough that natural-language intent
queries surface the right function. Signature-shape scoring is M4 (PLAN.md §7).
"""

from __future__ import annotations

from dataclasses import dataclass

from rapidfuzz import fuzz

from .parsers import FunctionRecord

#: Default number of matches `deja find` prints (overridable via ``--limit``).
DEFAULT_LIMIT = 10
#: Matches scoring below this (0-100) are treated as noise and dropped.
MIN_SCORE = 40.0
#: Name hits count for slightly more than docstring hits (see module docstring).
_NAME_WEIGHT = 1.0
_DOC_WEIGHT = 0.9


@dataclass(frozen=True, slots=True)
class ScoredRecord:
    """A :class:`FunctionRecord` paired with its relevance score (0-100)."""

    record: FunctionRecord
    score: float


def _name_score(query: str, record: FunctionRecord) -> float:
    """Best fuzzy score of *query* against the record's name and qualname.

    ``token_set_ratio`` handles partial / reordered word matches (e.g. query
    ``"parse date"`` vs name ``parse_iso_date``) better than a plain ratio.
    """
    candidates = [record.name]
    if record.qualname and record.qualname != record.name:
        candidates.append(record.qualname)
    # Underscores shouldn't hide word boundaries from the matcher.
    candidates = [c.replace("_", " ") for c in candidates]
    return max(fuzz.token_set_ratio(query, c) for c in candidates)


def _doc_score(query: str, record: FunctionRecord) -> float:
    """Fuzzy score of *query* against the record's docstring (0 if none)."""
    if not record.docstring:
        return 0.0
    # partial_ratio: a short query should match a substring of a longer summary.
    return fuzz.partial_ratio(query, record.docstring)


def score_record(query: str, record: FunctionRecord) -> float:
    """Return the blended relevance score (0-100) of *record* for *query*."""
    name = _name_score(query, record) * _NAME_WEIGHT
    doc = _doc_score(query, record) * _DOC_WEIGHT
    return max(name, doc)


def search(
    query: str,
    records: list[FunctionRecord],
    *,
    limit: int = DEFAULT_LIMIT,
    min_score: float = MIN_SCORE,
) -> list[ScoredRecord]:
    """Rank *records* against *query*, best first.

    Args:
        query: The thing the caller is about to (re)write.
        records: Candidate functions (typically ``index.records``).
        limit: Maximum number of matches to return.
        min_score: Drop matches scoring below this (0-100).

    Returns:
        Up to *limit* :class:`ScoredRecord` s sorted by score (desc). Ties break
        on ``(file, line)`` so output is deterministic for tests and diffs.
    """
    q = query.strip()
    if not q:
        return []

    scored = [ScoredRecord(record=r, score=score_record(q, r)) for r in records]
    scored = [s for s in scored if s.score >= min_score]
    scored.sort(key=lambda s: (-s.score, s.record.file, s.record.line))
    return scored[: max(0, limit)]
