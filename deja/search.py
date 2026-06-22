"""Ranking for `deja find`: fuzzy match a query against indexed functions.

The scoring question is always the same (PLAN.md §1): *"Have I already written
something that does this?"* We score each :class:`~deja.parsers.base.FunctionRecord`
against the query on up to three signals and blend them:

* **name / qualname** — catches "I'm about to write ``slugify``" lookups.
* **docstring** — catches intent queries ("parse an ISO date") that won't match
  any identifier but do match the human description.
* **signature shape** (M4) — catches "I don't know the name, but it takes a
  ``str`` and returns a ``bool``" lookups via ``--sig "(str)->bool"``.

We use ``rapidfuzz`` (fast, C-backed) for the fuzzy text comparisons and
:mod:`deja.sigshape` for the structural one. Name matches are weighted a touch
higher than docstring matches so an exact-ish name still wins over a loose prose
hit — but in *intent mode* (``--intent``) docstrings are weighted up so a
natural-language query surfaces the right function. When a ``--sig`` shape is
supplied, its score is blended in alongside the text signals (PLAN.md §7 M4).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rapidfuzz import fuzz

from .parsers import FunctionRecord
from .sigshape import SignatureShape, parse_signature, shape_score

#: Default number of matches `deja find` prints (overridable via ``--limit``).
DEFAULT_LIMIT = 10
#: Matches scoring below this (0-100) are treated as noise and dropped.
MIN_SCORE = 40.0
#: Name hits count for slightly more than docstring hits (see module docstring).
_NAME_WEIGHT = 1.0
_DOC_WEIGHT = 0.9
#: In ``--intent`` mode we lean on the prose description instead of the identifier.
_INTENT_NAME_WEIGHT = 0.7
_INTENT_DOC_WEIGHT = 1.1
#: How much a supplied ``--sig`` shape contributes when blended with text signals.
_SIG_WEIGHT = 1.0


@dataclass(frozen=True, slots=True)
class ScoreBreakdown:
    """Per-signal scores behind a result (surfaced by ``--explain``).

    Each component is on the same 0-100 scale as the final blended score. A
    component is ``None`` when that signal didn't apply (e.g. no ``--sig`` was
    given, or the record has no docstring).
    """

    name: float | None = None
    docstring: float | None = None
    signature: float | None = None

    def parts(self) -> list[tuple[str, float]]:
        """Return the applicable ``(label, score)`` pairs, best first."""
        raw = [
            ("name", self.name),
            ("doc", self.docstring),
            ("sig", self.signature),
        ]
        present = [(label, val) for label, val in raw if val is not None]
        present.sort(key=lambda p: -p[1])
        return present


@dataclass(frozen=True, slots=True)
class ScoredRecord:
    """A :class:`FunctionRecord` paired with its relevance score (0-100)."""

    record: FunctionRecord
    score: float
    breakdown: ScoreBreakdown = field(default_factory=ScoreBreakdown)


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


def score_record(
    query: str,
    record: FunctionRecord,
    *,
    sig: SignatureShape | None = None,
    intent: bool = False,
) -> tuple[float, ScoreBreakdown]:
    """Return the blended relevance score (0-100) of *record* and its breakdown.

    Args:
        query: The textual query (name/intent). May be empty when searching by
            ``sig`` alone.
        record: Candidate function.
        sig: Optional parsed query signature shape; when given, its score is
            blended in and reported.
        intent: When true, weight the docstring above the name (natural-language
            "what does it do" queries).

    Returns:
        ``(score, breakdown)`` where *score* is the max of the weighted signals
        (so any single strong signal can surface a result) and *breakdown*
        records each applicable raw component.
    """
    name_w, doc_w = (
        (_INTENT_NAME_WEIGHT, _INTENT_DOC_WEIGHT) if intent else (_NAME_WEIGHT, _DOC_WEIGHT)
    )

    name_raw: float | None = None
    doc_raw: float | None = None
    sig_raw: float | None = None
    weighted: list[float] = []

    q = query.strip()
    if q:
        name_raw = _name_score(q, record)
        weighted.append(name_raw * name_w)
        if record.docstring:
            doc_raw = _doc_score(q, record)
            weighted.append(doc_raw * doc_w)

    if sig is not None:
        cand_shape = parse_signature(record.signature)
        sig_raw = shape_score(sig, cand_shape)
        weighted.append(sig_raw * _SIG_WEIGHT)
    score = max(weighted) if weighted else 0.0
    breakdown = ScoreBreakdown(name=name_raw, docstring=doc_raw, signature=sig_raw)
    return min(score, 100.0), breakdown


def search(
    query: str,
    records: list[FunctionRecord],
    *,
    limit: int = DEFAULT_LIMIT,
    min_score: float = MIN_SCORE,
    sig: str | SignatureShape | None = None,
    intent: bool = False,
) -> list[ScoredRecord]:
    """Rank *records* against *query* (and optional signature shape), best first.

    Args:
        query: The thing the caller is about to (re)write. May be empty if *sig*
            is given (pure shape search).
        records: Candidate functions (typically ``index.records``).
        limit: Maximum number of matches to return.
        min_score: Drop matches scoring below this (0-100).
        sig: Optional signature-shape query — a string like ``"(str)->bool"`` or
            an already-parsed :class:`~deja.sigshape.SignatureShape`. Blended in
            when present (M4).
        intent: Weight docstrings above names for natural-language queries.

    Returns:
        Up to *limit* :class:`ScoredRecord` s sorted by score (desc). Ties break
        on ``(file, line)`` so output is deterministic for tests and diffs.
    """
    q = query.strip()
    shape: SignatureShape | None
    if sig is None:
        shape = None
    elif isinstance(sig, SignatureShape):
        shape = sig
    else:
        # A raw string here is a user-typed query shape -> parse in query mode.
        shape = parse_signature(sig, query=True)

    # Nothing to search on at all → no results (mirrors the empty-query contract).
    if not q and shape is None:
        return []

    scored: list[ScoredRecord] = []
    for r in records:
        score, breakdown = score_record(q, r, sig=shape, intent=intent)
        scored.append(ScoredRecord(record=r, score=score, breakdown=breakdown))

    scored = [s for s in scored if s.score >= min_score]
    scored.sort(key=lambda s: (-s.score, s.record.file, s.record.line))
    return scored[: max(0, limit)]
