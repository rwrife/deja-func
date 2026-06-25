"""Near-duplicate function clustering for `deja dupes` (PLAN.md §8 #1).

`deja find` answers "have I written *this* before?" for a query you type.
`deja dupes` flips it around and asks the question of the index against *itself*:
**"which functions in this repo are near-duplicates of each other?"** — the
redundancy report, so you can see "you have 6 date parsers" at a glance.

The approach is deliberately simple and dependency-light, mirroring the rest of
the tool:

* **Pairwise similarity** between two :class:`~deja.parsers.base.FunctionRecord`
  blends the same signals :mod:`deja.search` already trusts — fuzzy *name*,
  fuzzy *docstring*, and structural *signature shape*
  (:func:`deja.sigshape.shape_score`) — into a single 0-100 score.
* **Clustering** is **greedy complete-linkage**: candidate pairs are considered
  best-score-first, and a function only joins an existing cluster if it clears
  ``--threshold`` against *every* current member. This deliberately avoids the
  classic single-linkage trap where a few generic "bridge" functions (lots of
  things look like ``(self) -> dict``) chain otherwise-unrelated code into one
  giant blob. Each reported cluster is therefore mutually similar throughout.
* Clusters of size 1 (functions with no near-twin) are dropped; only the actual
  redundancy is reported, **largest cluster first**.

Kept separate from :mod:`deja.search` (which ranks records against an *external*
query) so the all-pairs logic and its scoring stay pure and unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rapidfuzz import fuzz

from .parsers import FunctionRecord
from .sigshape import parse_signature, shape_score

#: Default similarity (0-100) two functions must reach to be called near-dupes.
#: Tuned to be strict-ish: catches renamed/reshuffled twins without flagging
#: every pair that merely shares a common verb. Override with ``--threshold``.
DEFAULT_THRESHOLD = 75.0

#: How the three signals combine into one pairwise score. Name carries the most
#: weight (same-named functions are the clearest redundancy), docstring next
#: (two prose descriptions of the same job), signature shape least (lots of
#: unrelated functions share ``(str) -> str``, so on its own it's weak evidence).
_NAME_WEIGHT = 0.5
_DOC_WEIGHT = 0.3
_SIG_WEIGHT = 0.2


@dataclass(frozen=True, slots=True)
class Cluster:
    """A group of mutually near-duplicate functions.

    Attributes:
        members: The clustered records, ordered by ``(file, line)`` so output is
            deterministic.
        score: Representative similarity for the cluster — the average pairwise
            score across all member pairs (a rough "how tight is this pile?").
    """

    members: tuple[FunctionRecord, ...] = field(default_factory=tuple)
    score: float = 0.0

    @property
    def size(self) -> int:
        """Number of functions in the cluster (always >= 2 for a real dupe)."""
        return len(self.members)


def _name_sim(a: FunctionRecord, b: FunctionRecord) -> float:
    """Fuzzy similarity (0-100) between two function names/qualnames.

    Underscores are spaced out so word boundaries are visible to the matcher
    (``parse_iso_date`` vs ``parse_date_iso``), matching :mod:`deja.search`.
    """
    an = a.name.replace("_", " ")
    bn = b.name.replace("_", " ")
    return fuzz.token_set_ratio(an, bn)


def _doc_sim(a: FunctionRecord, b: FunctionRecord) -> float:
    """Fuzzy similarity (0-100) between two docstrings (0 if either is missing)."""
    if not a.docstring or not b.docstring:
        return 0.0
    return fuzz.token_set_ratio(a.docstring, b.docstring)


def _sig_sim(a: FunctionRecord, b: FunctionRecord) -> float:
    """Structural similarity (0-100) of two signatures via shape scoring.

    Reuses :func:`deja.sigshape.shape_score` with one side treated as the
    "query" shape; symmetric enough for clustering purposes.
    """
    shape_a = parse_signature(a.signature)
    shape_b = parse_signature(b.signature)
    # shape_score wants a query shape with at least one param or a return hint;
    # a no-arg / unannotated function carries no structural signal, so skip it.
    if not shape_a.params and not shape_a.has_return_hint:
        return 0.0
    return shape_score(shape_a, shape_b)


def pair_score(a: FunctionRecord, b: FunctionRecord) -> float:
    """Blended near-duplicate similarity (0-100) between two functions.

    Combines name, docstring, and signature-shape signals with fixed weights
    (see module constants). The weights are *renormalized* over whichever signals
    actually apply, so a pair of well-named functions with no docstrings isn't
    silently penalised for the missing prose — the name simply carries full
    responsibility for that pair.
    """
    signals: list[tuple[float, float]] = [(_name_sim(a, b), _NAME_WEIGHT)]

    doc = _doc_sim(a, b)
    if a.docstring and b.docstring:
        signals.append((doc, _DOC_WEIGHT))

    sig = _sig_sim(a, b)
    if sig > 0.0:
        signals.append((sig, _SIG_WEIGHT))

    total_w = sum(w for _, w in signals)
    if total_w <= 0:  # pragma: no cover - name signal always present
        return 0.0
    blended = sum(score * w for score, w in signals) / total_w
    return round(min(blended, 100.0), 2)


def find_clusters(
    records: list[FunctionRecord],
    *,
    threshold: float = DEFAULT_THRESHOLD,
) -> list[Cluster]:
    """Group *records* into clusters of mutually near-duplicate functions.

    Uses **greedy complete-linkage**: every pair scoring at/above *threshold* is
    considered best-first, and a function is added to a cluster only when it is
    similar (>= *threshold*) to *all* of that cluster's current members. This
    keeps each cluster internally coherent and avoids single-linkage "bridge"
    chaining that would otherwise merge unrelated functions sharing a generic
    shape.

    Args:
        records: The function inventory (typically ``index.records``).
        threshold: Minimum pairwise score (0-100) for two functions to be linked.

    Returns:
        Clusters of size >= 2 only (singletons aren't redundancy), sorted by
        size descending, then by tighter average score, then by the first
        member's ``(file, line)`` so ordering is deterministic for tests.

    Complexity is O(n²) in the number of functions for the initial pair scan —
    fine for the single-repo scope this tool targets (PLAN.md §1). A
    blocking/LSH pre-pass is a future optimisation if huge repos ever need it.
    """
    n = len(records)
    if n < 2:
        return []

    # Score every pair once; keep only the linking edges, best score first so the
    # strongest evidence seeds and grows clusters before weaker pairs are tried.
    edges: list[tuple[float, int, int]] = []
    pair_cache: dict[tuple[int, int], float] = {}
    for i in range(n):
        for j in range(i + 1, n):
            score = pair_score(records[i], records[j])
            pair_cache[(i, j)] = score
            if score >= threshold:
                edges.append((score, i, j))
    edges.sort(key=lambda e: (-e[0], e[1], e[2]))

    def linked(a: int, b: int) -> bool:
        """Whether indices *a* and *b* clear the threshold (cached lookup)."""
        key = (a, b) if a < b else (b, a)
        return pair_cache[key] >= threshold

    members_of: list[set[int]] = []  # cluster id -> member indices
    cluster_id: dict[int, int] = {}  # function index -> cluster id

    def fits(cid: int, x: int) -> bool:
        """Complete-linkage test: *x* must be similar to all members of *cid*."""
        return all(linked(m, x) for m in members_of[cid])

    for _score, i, j in edges:
        ci = cluster_id.get(i)
        cj = cluster_id.get(j)
        if ci is None and cj is None:
            members_of.append({i, j})
            new_id = len(members_of) - 1
            cluster_id[i] = new_id
            cluster_id[j] = new_id
        elif ci is not None and cj is None:
            if fits(ci, j):
                members_of[ci].add(j)
                cluster_id[j] = ci
        elif cj is not None and ci is None:
            if fits(cj, i):
                members_of[cj].add(i)
                cluster_id[i] = cj
        # Both already clustered: don't force-merge two complete-linkage
        # clusters (that could reintroduce non-mutual members); leave them.

    clusters: list[Cluster] = []
    for member_idxs in members_of:
        if len(member_idxs) < 2:  # pragma: no cover - seeds always start at 2
            continue
        member_list = sorted(member_idxs)
        # Average score over all in-cluster pairs (every pair is >= threshold
        # under complete-linkage), as a "how tight is this pile?" readout.
        pair_scores = [pair_cache[(a, b)] for a, b in _index_pairs(member_list)]
        avg = round(sum(pair_scores) / len(pair_scores), 2) if pair_scores else threshold
        members = tuple(sorted((records[k] for k in member_list), key=lambda r: (r.file, r.line)))
        clusters.append(Cluster(members=members, score=avg))

    clusters.sort(
        key=lambda c: (-c.size, -c.score, c.members[0].file, c.members[0].line),
    )
    return clusters


def _index_pairs(indices: list[int]):
    """Yield every unordered ``(a, b)`` index pair with ``a < b``."""
    for a in range(len(indices)):
        for b in range(a + 1, len(indices)):
            yield indices[a], indices[b]
