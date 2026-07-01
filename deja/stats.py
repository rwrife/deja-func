"""Function-inventory aggregation for `deja stats` (PLAN.md §8 #10).

`deja find` answers "have I written *this* before?" one lookup at a time, and
`deja dupes` clusters near-identical functions. `deja stats` zooms all the way
out: it turns the existing ``.dejafunc/index.json`` into a single, skimmable
**leaderboard** of the codebase's function inventory — the
*"you have 6 date parsers"* readout from the PLAN, at a glance.

It is deliberately the cheapest command in the tool: **pure aggregation** over
records we already parsed (``name``, ``file``, ``line``, ``signature``,
``docstring``, ``lang``, ``qualname``). Zero new parsing, zero new dependencies.

The logic lives here (kept pure and unit-testable); :mod:`deja.render` formats
the text view and :mod:`deja.serialize` emits the ``--json`` shape, mirroring how
``dupes`` is split across the same three modules.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from .parsers import FunctionRecord

#: Default cap applied to each leaderboard section (overridden by ``--top``).
DEFAULT_TOP = 10

#: A bare name must appear at least this many times before it earns a
#: personality callout (one duplicate is noise; several is a smell).
_CALLOUT_MIN_COUNT = 3


@dataclass(frozen=True, slots=True)
class Stats:
    """Aggregated, ready-to-render inventory summary.

    All leaderboard lists are already sorted and capped to ``top`` by
    :func:`compute_stats`, so the renderer/serializer just walk them.

    Attributes:
        total_functions: Total number of indexed function records.
        total_files: Number of distinct source files represented in the index.
        languages: ``(lang, count)`` pairs, most functions first. Ties break
            alphabetically by language so output is deterministic.
        top_names: ``(name, count)`` pairs for the most-repeated *bare* function
            names with ``count >= 2`` (the duplication leaderboard), most-repeated
            first; ties break alphabetically. Capped to ``top``.
        biggest_files: ``(file, count)`` pairs for the files holding the most
            functions, largest first; ties break by path. Capped to ``top``.
        top: The cap that was applied to each leaderboard section.
    """

    total_functions: int = 0
    total_files: int = 0
    languages: tuple[tuple[str, int], ...] = field(default_factory=tuple)
    top_names: tuple[tuple[str, int], ...] = field(default_factory=tuple)
    biggest_files: tuple[tuple[str, int], ...] = field(default_factory=tuple)
    top: int = DEFAULT_TOP

    @property
    def is_empty(self) -> bool:
        """True when the index holds no functions at all."""
        return self.total_functions == 0

    @property
    def top_duplicate(self) -> tuple[str, int] | None:
        """The single most-duplicated name (and its count), if any qualifies.

        Returns ``(name, count)`` for the most-repeated bare name when it clears
        the personality-callout threshold, else ``None``. Drives the one-line
        *"validate shows up 7 times"* nudge in the renderer.
        """
        if self.top_names and self.top_names[0][1] >= _CALLOUT_MIN_COUNT:
            return self.top_names[0]
        return None


def _ranked(counter: Counter[str], *, top: int) -> tuple[tuple[str, int], ...]:
    """Return ``counter`` items sorted by count desc, then key asc, capped to ``top``.

    A negative or zero ``top`` yields an empty tuple (no rows requested), which
    keeps ``--top 0`` well-defined.
    """
    if top <= 0:
        return ()
    items = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    return tuple(items[:top])


def compute_stats(
    records: list[FunctionRecord],
    *,
    top: int = DEFAULT_TOP,
) -> Stats:
    """Aggregate *records* into a :class:`Stats` leaderboard summary.

    Args:
        records: The function inventory (typically ``index.records``).
        top: Maximum rows to keep in *each* leaderboard section (names, files).
            The totals and language breakdown are never truncated — a language
            breakdown that hid languages would be misleading — only the two
            "top N" leaderboards honour the cap.

    Returns:
        A fully-populated, deterministically-ordered :class:`Stats`. An empty
        input yields an all-zero ``Stats`` (the CLI/renderer handle the "nothing
        indexed yet" case gracefully).
    """
    lang_counts: Counter[str] = Counter()
    name_counts: Counter[str] = Counter()
    file_counts: Counter[str] = Counter()

    for r in records:
        lang_counts[r.lang] += 1
        name_counts[r.name] += 1
        file_counts[r.file] += 1

    # Language breakdown is reported in full (no `top` cap): every language in
    # the repo should be visible. Sorted most-functions-first, ties alphabetical.
    languages = tuple(sorted(lang_counts.items(), key=lambda kv: (-kv[1], kv[0])))

    # Duplication leaderboard: only names that actually repeat (count >= 2) are
    # "duplicates" worth showing; a wall of count-1 names isn't a leaderboard.
    duplicate_names: Counter[str] = Counter({name: n for name, n in name_counts.items() if n >= 2})

    return Stats(
        total_functions=len(records),
        total_files=len(file_counts),
        languages=languages,
        top_names=_ranked(duplicate_names, top=top),
        biggest_files=_ranked(file_counts, top=top),
        top=top,
    )
