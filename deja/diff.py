"""Compare two function indexes for `deja diff` (PLAN.md §8, issue #36).

`deja find` answers "have I written this?" for a query. `deja dupes` asks the
index of itself. **`deja diff` compares two index snapshots** — an *old* and a
*new* — and reports what changed: functions **added**, **removed**, and likely
**renamed/moved**. Think "git diff, but for your function inventory."

Why it matters (PLAN.md §2): reviewers of agent-generated PRs want to know *did
this diff quietly add a redundant function, or move/delete one?* A
function-inventory diff answers that in one command and slots naturally beside
``export``/``import`` and the hook.

The algorithm, kept dependency-light and mirroring the rest of the tool:

1. **Stable-key match.** Each record has a stable identity of ``(file, qualname)``
   (falling back to ``name`` when there's no qualname). Records present in both
   snapshots under the same key are *unchanged* and drop out immediately — the
   common case, and cheap.
2. **Rename/move detection.** Of the leftovers — keys only in *old* (removal
   candidates) and only in *new* (addition candidates) — we look for pairs that
   are the *same function under a different name or in a different file*. A pair
   is a rename when its similarity (signature shape + docstring, **not** name —
   the name is exactly what changed) clears :data:`RENAME_THRESHOLD`. Matching is
   greedy best-first and one-to-one, so each removed function pairs with at most
   one added function.
3. **Whatever's left** is a genuine **add** (only in new) or **remove** (only in
   old).

Kept separate from :mod:`deja.dupes` (which clusters one index against itself)
so the two-snapshot logic and its scoring stay pure and unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rapidfuzz import fuzz

from .index import Index
from .parsers import FunctionRecord
from .sigshape import parse_signature, shape_score

#: Similarity (0-100) a removed/added pair must reach on **signature + docstring**
#: to be reported as a rename/move rather than an independent remove + add. The
#: function's *name* is deliberately excluded from this score — a rename is
#: precisely the case where the name changed — so we lean on the parts that
#: should have stayed the same. Tuned strict-ish so unrelated churn isn't
#: mislabeled a rename.
RENAME_THRESHOLD = 70.0

#: How the two rename signals combine. Signature shape is the stronger "same
#: function" signal (same inputs/outputs); docstring corroborates. Renormalized
#: over whichever signals actually apply (see :func:`rename_score`).
_SIG_WEIGHT = 0.6
_DOC_WEIGHT = 0.4


@dataclass(frozen=True, slots=True)
class Rename:
    """A function that moved and/or was renamed between two snapshots.

    Attributes:
        old: The record as it appeared in the *old* index.
        new: The record as it appears in the *new* index.
        score: Similarity (0-100) on signature + docstring that linked the pair.
    """

    old: FunctionRecord
    new: FunctionRecord
    score: float = 0.0


@dataclass(frozen=True, slots=True)
class DiffReport:
    """The result of comparing two index snapshots.

    Attributes:
        added: Records only in the *new* index (and not a rename target).
        removed: Records only in the *old* index (and not a rename source).
        renamed: ``old -> new`` pairs judged the same function moved/renamed.
        unchanged: Count of records matched by stable key in both snapshots.
    """

    added: tuple[FunctionRecord, ...] = field(default_factory=tuple)
    removed: tuple[FunctionRecord, ...] = field(default_factory=tuple)
    renamed: tuple[Rename, ...] = field(default_factory=tuple)
    unchanged: int = 0

    @property
    def any_changes(self) -> bool:
        """True when the two snapshots differ in any way."""
        return bool(self.added or self.removed or self.renamed)


def _stable_key(record: FunctionRecord) -> tuple[str, str]:
    """Identity used to match a function across snapshots: ``(file, qualname)``.

    Falls back to the bare ``name`` when a record carries no ``qualname`` (module
    -level functions), matching how the rest of the tool treats the two.
    """
    return (record.file, record.qualname or record.name)


def rename_score(old: FunctionRecord, new: FunctionRecord) -> float:
    """Similarity (0-100) that *old* and *new* are the same function renamed.

    Blends **signature shape** and **docstring** — intentionally *excluding* the
    name, which is what a rename changes. Weights are renormalized over whichever
    signals actually apply, so a pair with no docstrings isn't penalised for the
    missing prose (the signature shape carries full responsibility).
    """
    signals: list[tuple[float, float]] = []

    shape_old = parse_signature(old.signature)
    shape_new = parse_signature(new.signature)
    # A shape with no params and no return hint carries no structural signal;
    # skip it rather than feed shape_score a query it can't score.
    if shape_old.params or shape_old.has_return_hint:
        signals.append((shape_score(shape_old, shape_new), _SIG_WEIGHT))

    if old.docstring and new.docstring:
        signals.append((fuzz.token_set_ratio(old.docstring, new.docstring), _DOC_WEIGHT))

    total_w = sum(w for _, w in signals)
    if total_w <= 0:
        # Neither a usable signature nor matching docstrings: no evidence at all.
        return 0.0
    blended = sum(score * w for score, w in signals) / total_w
    return round(min(blended, 100.0), 2)


def _detect_renames(
    removed: list[FunctionRecord],
    added: list[FunctionRecord],
    *,
    threshold: float,
) -> tuple[list[Rename], list[FunctionRecord], list[FunctionRecord]]:
    """Pair up removals and additions that are the same function moved/renamed.

    Greedy, best-score-first, one-to-one: every cross pair scoring at/above
    *threshold* is a candidate; the strongest available pair is committed first,
    and both of its endpoints are then removed from further consideration. This
    avoids a single popular addition claiming several removals (or vice versa).

    Returns:
        ``(renames, leftover_removed, leftover_added)`` — the confirmed renames
        plus whichever records never found a partner.
    """
    candidates: list[tuple[float, int, int]] = []
    for i, old in enumerate(removed):
        for j, new in enumerate(added):
            score = rename_score(old, new)
            if score >= threshold:
                candidates.append((score, i, j))
    # Best score first; ties broken deterministically by index for stable output.
    candidates.sort(key=lambda c: (-c[0], c[1], c[2]))

    used_old: set[int] = set()
    used_new: set[int] = set()
    renames: list[Rename] = []
    for score, i, j in candidates:
        if i in used_old or j in used_new:
            continue
        used_old.add(i)
        used_new.add(j)
        renames.append(Rename(old=removed[i], new=added[j], score=score))

    leftover_removed = [r for i, r in enumerate(removed) if i not in used_old]
    leftover_added = [a for j, a in enumerate(added) if j not in used_new]
    return renames, leftover_removed, leftover_added


def _sort_records(records: list[FunctionRecord]) -> tuple[FunctionRecord, ...]:
    """Deterministic ``(file, line)`` ordering for report sections."""
    return tuple(sorted(records, key=lambda r: (r.file, r.line)))


def diff_indexes(
    old: Index,
    new: Index,
    *,
    rename_threshold: float = RENAME_THRESHOLD,
) -> DiffReport:
    """Compare *old* and *new* index snapshots into a :class:`DiffReport`.

    Args:
        old: The earlier snapshot (baseline).
        new: The later snapshot.
        rename_threshold: Minimum signature+docstring similarity (0-100) for a
            removed/added pair to be reported as a rename/move instead of a
            separate remove + add.

    Returns:
        A :class:`DiffReport` with added / removed / renamed sections
        deterministically ordered (renames sorted best-score-first, then by the
        old record's ``(file, line)``).
    """
    old_by_key: dict[tuple[str, str], FunctionRecord] = {}
    for r in old.records:
        # First occurrence wins on a duplicate key; the index is already sorted
        # by (file, line) so this is deterministic.
        old_by_key.setdefault(_stable_key(r), r)
    new_by_key: dict[tuple[str, str], FunctionRecord] = {}
    for r in new.records:
        new_by_key.setdefault(_stable_key(r), r)

    old_keys = set(old_by_key)
    new_keys = set(new_by_key)

    unchanged = len(old_keys & new_keys)
    removed = [old_by_key[k] for k in old_keys - new_keys]
    added = [new_by_key[k] for k in new_keys - old_keys]

    renames, removed, added = _detect_renames(removed, added, threshold=rename_threshold)
    renames.sort(key=lambda rn: (-rn.score, rn.old.file, rn.old.line))

    return DiffReport(
        added=_sort_records(added),
        removed=_sort_records(removed),
        renamed=tuple(renames),
        unchanged=unchanged,
    )
