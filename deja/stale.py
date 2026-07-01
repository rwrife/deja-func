"""Dead-code *candidate* finder for `deja stale` (PLAN.md §8 #9).

`deja find` answers *"have I written this before?"*; the natural complement is
*"is this still used at all?"* `deja stale` surfaces indexed functions whose
names are **never referenced anywhere else** in the indexed source tree — strong
dead-code *candidates* a human (or agent) can review and prune.

This stays deliberately inside scope: we do **not** build a real call graph or do
go-to-definition (that's an LSP, explicitly out of scope in PLAN.md §9). It is a
fast, heuristic *"never mentioned outside its own definition"* scan over exactly
the files the walker already enumerates, so the scanned set matches `deja index`
byte-for-byte (same ``.gitignore`` / :data:`~deja.walker.ALWAYS_SKIP_DIRS` rules).

Because it is a **string-level** heuristic, results are candidates, not proof:
dynamic dispatch, reflection, ``getattr``, string-keyed registries, and
cross-language calls can all hide a real use. The renderer/serializer label the
output accordingly.

The logic lives here (kept pure and unit-testable via an injected source reader);
:mod:`deja.render` formats the text view and :mod:`deja.serialize` emits the
``--json`` shape, mirroring how ``dupes``/``stats`` are split across the same
three modules.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from fnmatch import fnmatch

from .parsers import FunctionRecord

#: Names that look like framework entry points / lifecycle hooks are ignored by
#: default: they're routinely invoked by *something else* (a test runner, a
#: framework, ``python -m``) that our string scan can't see, so flagging them as
#: dead code would be almost always wrong. Users add more via ``--ignore``.
DEFAULT_IGNORE: tuple[str, ...] = (
    "__*__",  # dunders: __init__, __call__, __enter__, …
    "main",  # CLI / module entry points
    "test_*",  # pytest / unittest test functions
    "setUp",  # unittest fixtures
    "tearDown",
    "setUpClass",
    "tearDownClass",
    "setUpModule",
    "tearDownModule",
)

#: A word-boundary reference regex is capped to this many alternation terms per
#: compiled pattern. Splitting into chunks keeps the regex engine happy on very
#: large inventories (thousands of distinct names) without changing results.
_MAX_ALTERNATION = 500


@dataclass(frozen=True, slots=True)
class StaleFunction:
    """One indexed function with no external references — a dead-code candidate.

    Attributes:
        record: The underlying :class:`~deja.parsers.base.FunctionRecord`.
        references: Count of word-boundary mentions of the name found *outside*
            this function's own definition line (always ``0`` for a returned
            candidate; carried explicitly so the schema is self-describing and
            future non-zero "weak use" thresholds stay easy to add).
    """

    record: FunctionRecord
    references: int = 0

    # Convenience passthroughs so callers/renderers read naturally.
    @property
    def name(self) -> str:
        return self.record.name

    @property
    def file(self) -> str:
        return self.record.file

    @property
    def line(self) -> int:
        return self.record.line


@dataclass(frozen=True, slots=True)
class StaleReport:
    """Result of a :func:`find_stale` scan, ready to render or serialize.

    Attributes:
        candidates: Stale functions, sorted by ``file`` then ``line`` then
            ``name`` for deterministic output.
        scanned_functions: How many records were considered (after any ``lang``
            filter but before ignore-pattern exclusion), for a summary line.
        ignored: How many records were skipped because their name matched an
            ignore pattern (default or ``--ignore``).
        ignore_patterns: The effective ignore globs that were applied.
        lang: The language filter that was applied, or ``None`` for all.
    """

    candidates: tuple[StaleFunction, ...] = field(default_factory=tuple)
    scanned_functions: int = 0
    ignored: int = 0
    ignore_patterns: tuple[str, ...] = field(default_factory=tuple)
    lang: str | None = None

    def __len__(self) -> int:
        return len(self.candidates)

    @property
    def is_empty(self) -> bool:
        """True when no dead-code candidates were found."""
        return not self.candidates


def _name_matches_ignore(name: str, patterns: Iterable[str]) -> bool:
    """True if *name* matches any ignore glob (``fnmatch`` semantics)."""
    return any(fnmatch(name, pat) for pat in patterns)


def _iter_reference_patterns(names: Iterable[str]) -> Iterable[re.Pattern[str]]:
    """Yield compiled ``\\b(a|b|…)\\b`` patterns covering all *names*.

    Names are escaped and split into bounded-size alternations so a huge
    inventory doesn't build one pathological megapattern. Word boundaries mean
    ``parse`` never matches inside ``parseAll`` / ``reparse`` (acceptance
    criterion: word-boundary correctness).
    """
    unique = sorted({n for n in names if n})
    for i in range(0, len(unique), _MAX_ALTERNATION):
        chunk = unique[i : i + _MAX_ALTERNATION]
        alternation = "|".join(re.escape(n) for n in chunk)
        yield re.compile(rf"\b(?:{alternation})\b")


def _count_references(
    names: set[str],
    files: Iterable[str],
    read_source: Callable[[str], str | None],
) -> tuple[Counter[str], dict[str, Counter[str]]]:
    """Count word-boundary references to every name across *files*.

    Returns ``(total, per_line)`` where:

    * ``total[name]`` — reference occurrences of ``name`` across *all* files.
    * ``per_line["<file>@<lineno>"][name]`` — occurrences of ``name`` on that one
      line, kept so :func:`find_stale` can subtract a function's own definition
      line (a bare ``def name(...)`` shouldn't count as a use of itself).

    Everything is gathered in a single pass per file. Unreadable files (reader
    returns ``None``) are skipped.
    """
    patterns = list(_iter_reference_patterns(names))
    total: Counter[str] = Counter()
    per_line: dict[str, Counter[str]] = {}

    for rel in files:
        source = read_source(rel)
        if source is None:
            continue
        for lineno, text in enumerate(source.splitlines(), start=1):
            hits: Counter[str] | None = None
            for pat in patterns:
                for m in pat.finditer(text):
                    if hits is None:
                        hits = Counter()
                    hits[m.group(0)] += 1
            if hits:
                total.update(hits)
                per_line[f"{rel}@{lineno}"] = hits

    return total, per_line


def find_stale(
    records: list[FunctionRecord],
    files: Iterable[str],
    read_source: Callable[[str], str | None],
    *,
    ignore: Iterable[str] = (),
    lang: str | None = None,
) -> StaleReport:
    """Find indexed functions with no external references (dead-code candidates).

    Args:
        records: The function inventory (typically ``index.records``).
        files: Repo-relative paths to scan for references — pass
            :func:`deja.walker.iter_source_files` output so the scanned set is
            identical to what ``deja index`` parsed (acceptance criterion).
        read_source: ``(rel_path) -> text | None`` reader. Returning ``None``
            skips an unreadable file. Injectable so the logic stays pure/testable.
        ignore: Extra ignore globs layered on top of :data:`DEFAULT_IGNORE`
            (the ``--ignore`` flag, repeatable). ``fnmatch`` semantics.
        lang: Optional language filter (e.g. ``"python"``); only records whose
            ``lang`` matches are considered (the ``--lang`` flag).

    Returns:
        A :class:`StaleReport`. A function is a candidate when its name has
        **zero** word-boundary references anywhere in *files* other than on its
        own definition line. The reference scan itself always covers the full
        file set (so a Python function called only from JS is *not* flagged even
        under ``--lang python``); ``lang`` narrows only which functions we
        *report on*, never which files we search.
    """
    ignore_patterns = tuple(DEFAULT_IGNORE) + tuple(ignore)

    # Which records are we reporting on? Apply the language filter first.
    considered = [r for r in records if lang is None or r.lang == lang]

    # Split into "ignored by name" vs. "eligible to be flagged".
    eligible: list[FunctionRecord] = []
    ignored_count = 0
    for r in considered:
        if _name_matches_ignore(r.name, ignore_patterns):
            ignored_count += 1
        else:
            eligible.append(r)

    # Reference counting covers *every* candidate name across *all* files. We
    # scan for the full name set (not just eligible) so, e.g., an ignored
    # ``main`` calling ``helper`` still counts as a use of ``helper``.
    all_names = {r.name for r in records}
    materialized_files = list(files)
    total_refs, line_refs = _count_references(all_names, materialized_files, read_source)

    candidates: list[StaleFunction] = []
    for r in eligible:
        name = r.name
        gross = total_refs.get(name, 0)
        # Subtract mentions on this function's own definition line so a bare
        # ``def name(...)`` / ``function name(...)`` isn't a reference to itself.
        own = line_refs.get(f"{r.file}@{r.line}", Counter()).get(name, 0)
        external = gross - own
        if external <= 0:
            candidates.append(StaleFunction(record=r, references=0))

    # Deterministic ordering: by file, then line, then name.
    candidates.sort(key=lambda s: (s.record.file, s.record.line, s.record.name))

    return StaleReport(
        candidates=tuple(candidates),
        scanned_functions=len(considered),
        ignored=ignored_count,
        ignore_patterns=ignore_patterns,
        lang=lang,
    )
