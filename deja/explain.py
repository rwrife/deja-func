"""`deja explain <name>`: where a function lives, who calls it, and how stale it is.

`deja find` answers *"does this exist?"*; once you've found ``slugify`` the next
question is *"is it safe to reuse, and who depends on it?"* (PLAN.md §8 #6).
``deja explain <name>`` closes that loop straight off the existing index plus a
cheap repo scan:

* **Resolve** ``<name>`` against the index — exact match wins; otherwise fall
  back to the fuzzy :mod:`deja.search` ranking. Multiple exact matches (an
  overloaded / re-declared name) are *ambiguous*: we return the candidates for
  the caller to disambiguate rather than guessing.
* **Definition** — the record's ``file:line``, ``signature`` and ``docstring``
  come straight from the index.
* **Callers** — a lightweight *textual* scan for ``<name>(`` across exactly the
  files the walker enumerates, excluding the definition's own line. This is not
  a call graph / go-to-definition (that's an LSP, explicitly out of scope in
  PLAN.md §9) — just the same string-level heuristic that powers ``deja stale``.
* **Staleness** — ``git log -1 --format=%cr -- <file>`` for a human "last touched
  N months ago", tying into the dead-code/stale theme. Degrades gracefully
  outside a git repo, and when the defining file was deleted since indexing.

As with :mod:`deja.stale`, the caller scan is a **string-level** heuristic:
dynamic dispatch, reflection, ``getattr`` and string-keyed registries can hide a
real call site or invent a coincidental one. The logic here stays pure and
unit-testable via injected readers; :mod:`deja.render` formats the text view and
:mod:`deja.serialize` emits the ``--json`` shape, mirroring the ``stale`` split.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .gitmeta import GitAge
from .parsers import FunctionRecord
from .search import search

#: Default cap on how many caller sites the text view lists before summarizing
#: the rest as "+K more" (overridable via ``--max-callers``). JSON always
#: carries the full list plus the total, so agents never lose data to the cap.
DEFAULT_MAX_CALLERS = 20

#: A callable that returns a file's text, or ``None`` when it can't be read
#: (missing/binary/permission). Injected so the scan stays pure and testable.
SourceReader = Callable[[str], "str | None"]

#: A callable ``(rel_path) -> relative-age-string | None`` for a file's "last
#: touched" age (e.g. ``"3 months ago"``). ``None`` means the age is unavailable
#: (not a git repo, file untracked/deleted, or git missing) — the renderer then
#: simply omits the age line. Injected so tests don't shell out to git.
AgeReader = Callable[[str], "str | None"]

#: A callable ``(rel_path) -> GitAge | None`` for a file's full "last touched"
#: metadata (relative age + ISO date + short hash). ``None`` means unavailable
#: (non-git / untracked / deleted / git missing). Preferred over :data:`AgeReader`
#: — when supplied it feeds both the rich ``last_touched_meta`` and the legacy
#: ``last_touched`` string. Injected so tests don't shell out to git.
GitAgeReader = Callable[[str], "GitAge | None"]


@dataclass(frozen=True, slots=True)
class Caller:
    """One textual call site of the explained function.

    Attributes:
        file: Repo-relative path (POSIX separators) of the referencing file.
        line: 1-based line number of the call site.
        text: The stripped source line, for a bit of context in the report.
    """

    file: str
    line: int
    text: str


@dataclass(frozen=True, slots=True)
class Explanation:
    """A resolved ``deja explain`` report, ready to render or serialize.

    Attributes:
        record: The single resolved function this report describes.
        callers: Textual call sites found across the scanned files, sorted by
            ``file`` then ``line`` (the definition's own line is excluded).
        caller_total: Total call sites found (may exceed ``len(callers)`` when a
            ``max_callers`` cap trimmed the list for display).
        last_touched: Human relative age of the defining file (``"3 months
            ago"``), or ``None`` when unavailable (non-git / deleted / untracked).
        last_touched_meta: Full git "last touched" facts (relative age + ISO date
            + short hash) for the defining file, or ``None`` when unavailable.
            Present alongside ``last_touched`` (which mirrors its relative age)
            so JSON consumers get the date/hash while the plain string field
            stays back-compatible.
        fuzzy: True when ``<name>`` had no exact index match and this record was
            the top *fuzzy* hit — surfaced so the report can say "did you mean".
        query: The original name the user asked about (echoed in output).
    """

    record: FunctionRecord
    callers: tuple[Caller, ...] = field(default_factory=tuple)
    caller_total: int = 0
    last_touched: str | None = None
    last_touched_meta: GitAge | None = None
    fuzzy: bool = False
    query: str = ""

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
class Ambiguous:
    """Resolution result when ``<name>`` matched several definitions.

    Attributes:
        query: The name the user asked about.
        candidates: The tied/close records, best-ranked first, for the caller to
            pick from (rendered as a ``file:line`` list with a non-zero exit).
        fuzzy: True when the candidates came from a *fuzzy* fallback (no exact
            match) rather than several exact matches of the same name.
    """

    query: str
    candidates: tuple[FunctionRecord, ...]
    fuzzy: bool = False


def resolve(
    name: str,
    records: list[FunctionRecord],
    *,
    fuzzy_limit: int = 5,
) -> Explanation | Ambiguous | None:
    """Resolve *name* to a single record, an ambiguous set, or ``None``.

    Resolution order (acceptance criteria):

    1. **Exact** name/qualname match. Exactly one → resolvable to that record;
       more than one → :class:`Ambiguous` (overloaded / redeclared name).
    2. **Fuzzy** fallback via :func:`deja.search.search`. The single top hit →
       resolvable (flagged ``fuzzy``); several comparable hits → :class:`Ambiguous`
       (flagged ``fuzzy``) so the user can disambiguate.
    3. No match at all → ``None`` (the CLI prints a friendly "not in the index").

    This function only *selects* the record(s); callers/staleness are filled in
    by :func:`explain`. Returned :class:`Explanation` objects here carry no
    callers yet (that would duplicate the scan) — they signal the chosen record.

    Args:
        name: The function name the user asked about.
        records: The function inventory (typically ``index.records``).
        fuzzy_limit: How many fuzzy candidates to consider when there's no exact
            match (the ambiguity list is capped to this).

    Returns:
        An :class:`Explanation` (single match, ``callers`` unset), an
        :class:`Ambiguous` set, or ``None`` when nothing matches.
    """
    query = name.strip()
    if not query:
        return None

    exact = [r for r in records if r.name == query or r.qualname == query]
    if len(exact) == 1:
        return Explanation(record=exact[0], query=query)
    if len(exact) > 1:
        # Deterministic candidate order for a stable disambiguation prompt.
        ordered = sorted(exact, key=lambda r: (r.file, r.line))
        return Ambiguous(query=query, candidates=tuple(ordered))

    # No exact hit: fall back to the shared fuzzy ranker.
    ranked = search(query, records, limit=max(1, fuzzy_limit))
    if not ranked:
        return None
    if len(ranked) == 1:
        return Explanation(record=ranked[0].record, fuzzy=True, query=query)

    # Multiple fuzzy candidates → let the human choose (best-first, as ranked).
    return Ambiguous(
        query=query,
        candidates=tuple(s.record for s in ranked),
        fuzzy=True,
    )


def _caller_pattern(name: str) -> re.Pattern[str]:
    """Compile a word-boundary ``name(`` call-site pattern.

    The trailing ``(`` (allowing whitespace before it) is what distinguishes a
    *call* from a bare mention, and the leading ``\\b`` keeps ``parse`` from
    matching inside ``reparse`` / ``parseAll`` (mirrors ``stale``'s
    word-boundary contract).
    """
    return re.compile(rf"\b{re.escape(name)}\s*\(")


def find_callers(
    record: FunctionRecord,
    files: Iterable[str],
    read_source: SourceReader,
) -> list[Caller]:
    """Scan *files* for textual call sites of ``record.name`` (``name(``).

    The definition's own line (``record.file`` @ ``record.line``) is excluded so
    a function's own ``def name(...)`` / ``function name(...)`` header is never
    reported as a call to itself. Unreadable files (reader returns ``None``) are
    skipped. Results are sorted by ``file`` then ``line`` for deterministic
    output.

    This is intentionally a lightweight string scan, not a call graph: it can
    both miss dynamic dispatch and occasionally match a coincidental ``name(``
    (e.g. a different scope's same-named symbol). Callers are labeled as such in
    the rendered/JSON output.
    """
    pattern = _caller_pattern(record.name)
    callers: list[Caller] = []
    for rel in files:
        source = read_source(rel)
        if source is None:
            continue
        for lineno, text in enumerate(source.splitlines(), start=1):
            # Skip the function's own definition line — that's a def, not a call.
            if rel == record.file and lineno == record.line:
                continue
            if pattern.search(text):
                callers.append(Caller(file=rel, line=lineno, text=text.strip()))
    callers.sort(key=lambda c: (c.file, c.line))
    return callers


def git_last_touched(root: str | Path, rel_path: str) -> str | None:
    """Return a file's "last touched" relative age via git, or ``None``.

    Runs ``git log -1 --format=%cr -- <rel_path>`` in *root*. Returns ``None``
    (so the renderer omits the age line) when:

    * *root* isn't a git working tree, or git isn't installed;
    * the file is untracked / has no commit history;
    * the file was deleted since indexing (no log for a path git doesn't know).

    ``%cr`` yields git's own human phrasing ("3 months ago"), keeping the report
    consistent with how developers already read ``git log``.
    """
    try:
        proc = subprocess.run(
            ["git", "log", "-1", "--format=%cr", "--", rel_path],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):  # pragma: no cover - env-dependent
        return None
    age = proc.stdout.strip()
    return age or None


def explain(
    name: str,
    records: list[FunctionRecord],
    files: Iterable[str],
    read_source: SourceReader,
    *,
    last_touched: AgeReader | None = None,
    git_age: GitAgeReader | None = None,
    max_callers: int = DEFAULT_MAX_CALLERS,
    fuzzy_limit: int = 5,
) -> Explanation | Ambiguous | None:
    """Resolve *name* and, on a single match, gather callers + staleness.

    Combines :func:`resolve` (pick the record) with :func:`find_callers` (the
    textual call-site scan) and an optional *last_touched* age lookup. Ambiguous
    and unknown names short-circuit before any file scan — there's nothing to
    explain until the user disambiguates.

    Args:
        name: The function name to explain.
        records: The function inventory (typically ``index.records``).
        files: Repo-relative paths to scan for call sites — pass
            :func:`deja.walker.iter_source_files` output so the scanned set is
            identical to what ``deja index`` parsed.
        read_source: ``(rel) -> text | None`` reader for the caller scan.
        last_touched: Optional ``(rel) -> age | None`` reader for the "last
            touched" line (relative age only). When ``None``, the age is simply
            omitted (e.g. the CLI skips it outside a git repo). Injectable so
            tests stay disk/git-free. Ignored when *git_age* is supplied.
        git_age: Optional ``(rel) -> GitAge | None`` reader for the *full* "last
            touched" facts (relative age + ISO date + short hash). Preferred over
            *last_touched*: when given it populates ``last_touched_meta`` and
            derives ``last_touched`` from its relative age.
        max_callers: Cap on how many caller sites the :class:`Explanation`
            carries for display; ``caller_total`` still reflects the full count so
            the renderer can show "+K more" and JSON can report the true total.
            ``0`` or negative means "no cap".
        fuzzy_limit: Forwarded to :func:`resolve` for the no-exact-match path.

    Returns:
        A fully-populated :class:`Explanation`, an :class:`Ambiguous` set, or
        ``None`` when *name* isn't in the index.
    """
    resolved = resolve(name, records, fuzzy_limit=fuzzy_limit)
    if resolved is None or isinstance(resolved, Ambiguous):
        return resolved

    record = resolved.record
    all_callers = find_callers(record, files, read_source)
    total = len(all_callers)
    shown = all_callers if max_callers <= 0 else all_callers[:max_callers]

    if git_age is not None:
        meta = git_age(record.file)
        age = meta.relative if meta is not None else None
    else:
        meta = None
        age = last_touched(record.file) if last_touched is not None else None

    return Explanation(
        record=record,
        callers=tuple(shown),
        caller_total=total,
        last_touched=age,
        last_touched_meta=meta,
        fuzzy=resolved.fuzzy,
        query=resolved.query,
    )
