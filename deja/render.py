"""Pretty terminal output for `deja find`, with a little personality (PLAN.md §4).

Rendering is kept separate from ranking so the scoring logic stays pure and
testable, and so a future ``--json`` mode (M6) can skip this module entirely.

Output per match::

    name — file:line — signature
        summary

We avoid hard ANSI-color dependencies: a couple of ANSI codes are emitted only
when stdout is a TTY, so piping into other tools stays clean and scriptable.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable

from .dupes import Cluster
from .hook import Match
from .search import ScoredRecord
from .stale import StaleReport
from .stats import Stats

# Minimal ANSI; only used on a TTY (see _style).
_BOLD = "\033[1m"
_DIM = "\033[2m"
_CYAN = "\033[36m"
_MAGENTA = "\033[35m"
_RESET = "\033[0m"


def _use_color(stream) -> bool:
    """True if we should emit ANSI codes for *stream* (a real terminal)."""
    return bool(getattr(stream, "isatty", lambda: False)())


def _style(text: str, code: str, *, color: bool) -> str:
    return f"{code}{text}{_RESET}" if color else text


def _header(query: str, n: int, *, color: bool) -> str:
    """A personality-laden header line summarizing the result count."""
    # Pure shape searches arrive with an empty textual query; describe the
    # "nothing found" case without printing an empty ``''``.
    label = repr(query) if query.strip() else "that shape"
    if n == 0:
        return f"🤔 Nothing like {label} yet — looks new. Go write it."
    if n == 1:
        lead = "🫠 You already wrote this:"
    else:
        lead = f"🫠 You already wrote {n} of these:"
    return _style(lead, _BOLD, color=color)


def _explain_line(s: ScoredRecord) -> str:
    """A ``--explain`` breakdown like ``score 88.0  (name 88.0 · sig 75.0)``."""
    parts = s.breakdown.parts()
    detail = " · ".join(f"{label} {val:.0f}" for label, val in parts)
    base = f"score {s.score:.0f}"
    return f"{base}  ({detail})" if detail else base


def format_results(
    query: str,
    results: Iterable[ScoredRecord],
    *,
    color: bool | None = None,
    explain: bool = False,
    stream=None,
) -> str:
    """Render *results* into a printable block of text.

    Args:
        query: The original search query (used in the header).
        results: Ranked matches from :func:`deja.search.search`.
        color: Force ANSI on/off; ``None`` auto-detects from *stream*.
        explain: When true, append a per-signal score breakdown under each match
            (the ``--explain`` flag, M4).
        stream: Stream used for TTY detection when *color* is ``None``
            (defaults to ``sys.stdout``).

    Returns:
        A newline-joined string ready to ``print``.
    """
    results = list(results)
    if color is None:
        color = _use_color(stream if stream is not None else sys.stdout)

    lines = [_header(query, len(results), color=color)]
    for s in results:
        r = s.record
        loc = _style(f"{r.file}:{r.line}", _CYAN, color=color)
        name = _style(r.qualname or r.name, _BOLD, color=color)
        sig = r.signature or "()"
        lines.append(f"  {name} — {loc} — {sig}")
        if r.docstring:
            lines.append(_style(f"      {r.docstring}", _DIM, color=color))
        if explain:
            lines.append(_style(f"      {_explain_line(s)}", _DIM, color=color))
    return "\n".join(lines)


def _dupes_header(n: int, *, color: bool) -> str:
    """A personality-laden header summarizing the redundancy verdict."""
    if n == 0:
        return "\u2728 No near-duplicate functions found — your inventory is lean."
    if n == 1:
        lead = "\u267b\ufe0f Found 1 cluster of near-duplicate functions:"
    else:
        lead = f"\u267b\ufe0f Found {n} clusters of near-duplicate functions:"
    return _style(lead, _BOLD, color=color)


def format_clusters(
    clusters: Iterable[Cluster],
    *,
    color: bool | None = None,
    stream=None,
) -> str:
    """Render near-duplicate *clusters* into a printable block of text.

    Output per cluster::

        ×3 · ~88% similar
          name — file:line — signature
              summary
          name — file:line — signature
          ...

    Args:
        clusters: Clusters from :func:`deja.dupes.find_clusters` (largest first).
        color: Force ANSI on/off; ``None`` auto-detects from *stream*.
        stream: Stream used for TTY detection when *color* is ``None``
            (defaults to ``sys.stdout``).

    Returns:
        A newline-joined string ready to ``print``.
    """
    clusters = list(clusters)
    if color is None:
        color = _use_color(stream if stream is not None else sys.stdout)

    lines = [_dupes_header(len(clusters), color=color)]
    for c in clusters:
        summary = _style(f"\u00d7{c.size} · ~{c.score:.0f}% similar", _BOLD, color=color)
        lines.append(f"  {summary}")
        for r in c.members:
            loc = _style(f"{r.file}:{r.line}", _CYAN, color=color)
            name = _style(r.qualname or r.name, _BOLD, color=color)
            sig = r.signature or "()"
            lines.append(f"    {name} — {loc} — {sig}")
            if r.docstring:
                lines.append(_style(f"        {r.docstring}", _DIM, color=color))
    return "\n".join(lines)


def _matches_header(n: int, *, strict: bool, color: bool) -> str:
    """A personality-laden header summarizing the redundancy-hook verdict."""
    if n == 0:
        return "\u2728 No staged function looks like existing code — carry on."
    noun = "function" if n == 1 else "functions"
    verb = "blocking" if strict else "heads-up"
    lead = f"\U0001fae0 {n} staged {noun} already exist(s) ({verb}):"
    return _style(lead, _BOLD, color=color)


def format_matches(
    matches: Iterable[Match],
    *,
    strict: bool = False,
    color: bool | None = None,
    stream=None,
) -> str:
    """Render redundancy-hook *matches* into a printable block of text.

    Output per match::

        new_func — path/new.py:12 — (s: str)
            ~88% similar to existing:
            old_func — path/old.py:40 — (text: str)
                Existing function's summary.

    Args:
        matches: Matches from :func:`deja.hook.check_staged` (strongest first).
        strict: Whether the hook is running in blocking mode (affects wording
            and the closing hint only; exit-code policy lives in the CLI).
        color: Force ANSI on/off; ``None`` auto-detects from *stream*.
        stream: Stream used for TTY detection when *color* is ``None``
            (defaults to ``sys.stdout``).

    Returns:
        A newline-joined string ready to ``print``.
    """
    matches = list(matches)
    if color is None:
        color = _use_color(stream if stream is not None else sys.stdout)

    lines = [_matches_header(len(matches), strict=strict, color=color)]
    for m in matches:
        s = m.staged
        e = m.existing
        s_loc = _style(f"{s.file}:{s.line}", _CYAN, color=color)
        s_name = _style(s.qualname or s.name, _BOLD, color=color)
        lines.append(f"  {s_name} — {s_loc} — {s.signature or '()'}")
        lines.append(_style(f"      ~{m.score:.0f}% similar to existing:", _DIM, color=color))
        e_loc = _style(f"{e.file}:{e.line}", _CYAN, color=color)
        e_name = _style(e.qualname or e.name, _BOLD, color=color)
        lines.append(f"      {e_name} — {e_loc} — {e.signature or '()'}")
        if e.docstring:
            lines.append(_style(f"          {e.docstring}", _DIM, color=color))
    if matches and not strict:
        lines.append(
            _style(
                "  (warning only — commit proceeds; run with --strict to block)",
                _DIM,
                color=color,
            )
        )
    return "\n".join(lines)


def _stats_header(stats: Stats, *, color: bool) -> str:
    """A personality-laden one-liner summarizing the inventory size."""
    if stats.is_empty:
        return "\U0001f9e0 Nothing indexed yet — run `deja index` to build your inventory."
    funcs = stats.total_functions
    files = stats.total_files
    f_noun = "function" if funcs == 1 else "functions"
    file_noun = "file" if files == 1 else "files"
    lead = f"\U0001f9e0 {funcs} {f_noun} across {files} {file_noun}."
    return _style(lead, _BOLD, color=color)


def _stats_leaderboard(
    title: str,
    rows: Iterable[tuple[str, int]],
    *,
    color: bool,
) -> list[str]:
    """Render one ``title`` + ``label (×count)`` leaderboard block (or nothing)."""
    rows = list(rows)
    if not rows:
        return []
    out = [_style(f"  {title}", _BOLD, color=color)]
    for label, count in rows:
        name = _style(label, _CYAN, color=color)
        out.append(f"    {name} {_style(f'(×{count})', _DIM, color=color)}")
    return out


def format_stats(
    stats: Stats,
    *,
    color: bool | None = None,
    stream=None,
) -> str:
    """Render an inventory ``stats`` summary into a printable block of text.

    Output::

        \U0001f9e0 180 functions across 24 files.
        python: 142 · javascript: 38
          Most duplicated names
            parse (×6)
            slugify (×4)
          Biggest files by function count
            src/text.py (×19)
        \U0001f62c "parse" shows up 6 times — sure you need all of them?

    Args:
        stats: Aggregated inventory from :func:`deja.stats.compute_stats`
            (leaderboards already sorted and capped to ``--top``).
        color: Force ANSI on/off; ``None`` auto-detects from *stream*.
        stream: Stream used for TTY detection when *color* is ``None``
            (defaults to ``sys.stdout``).

    Returns:
        A newline-joined string ready to ``print``.
    """
    if color is None:
        color = _use_color(stream if stream is not None else sys.stdout)

    lines = [_stats_header(stats, color=color)]
    if stats.is_empty:
        return "\n".join(lines)

    # Language breakdown reads naturally inline: ``python: 142 · javascript: 38``.
    if stats.languages:
        breakdown = " · ".join(f"{lang}: {n}" for lang, n in stats.languages)
        lines.append(_style(f"  {breakdown}", _DIM, color=color))

    lines.extend(_stats_leaderboard("Most duplicated names", stats.top_names, color=color))
    lines.extend(
        _stats_leaderboard("Biggest files by function count", stats.biggest_files, color=color)
    )

    # One-line nudge when a single name is conspicuously over-represented.
    callout = stats.top_duplicate
    if callout is not None:
        name, count = callout
        line = f'\U0001f62c "{name}" shows up {count} times — sure you need all of them?'
        lines.append(_style(line, _MAGENTA, color=color))

    return "\n".join(lines)


def _stale_header(report: StaleReport, *, color: bool) -> str:
    """A personality-laden header summarizing the dead-code verdict."""
    n = len(report)
    scope = f" ({report.lang})" if report.lang else ""
    if n == 0:
        return _style(
            f"\U0001f9f9 No dead-code candidates{scope} — every function is referenced somewhere.",
            _BOLD,
            color=color,
        )
    noun = "candidate" if n == 1 else "candidates"
    lead = f"\U0001faa6 {n} dead-code {noun}{scope} — names never referenced elsewhere:"
    return _style(lead, _BOLD, color=color)


def format_stale(
    report: StaleReport,
    *,
    color: bool | None = None,
    stream=None,
) -> str:
    """Render a :class:`~deja.stale.StaleReport` into a printable block of text.

    Output::

        \U0001faa6 2 dead-code candidates — names never referenced elsewhere:
          old_helper — src/util.py:42 — (x: int) -> int
          legacy_parse — src/text.py:88 — (s: str) -> Date
        \U0001f9ea Heuristic: string-level scan, not a call graph — reflection or
           dynamic dispatch can hide real uses. Review before deleting.

    Args:
        report: Result of :func:`deja.stale.find_stale` (candidates already
            deterministically sorted by file, line, then name).
        color: Force ANSI on/off; ``None`` auto-detects from *stream*.
        stream: Stream used for TTY detection when *color* is ``None``
            (defaults to ``sys.stdout``).

    Returns:
        A newline-joined string ready to ``print``.
    """
    if color is None:
        color = _use_color(stream if stream is not None else sys.stdout)

    lines = [_stale_header(report, color=color)]
    for stale in report.candidates:
        r = stale.record
        loc = _style(f"{r.file}:{r.line}", _CYAN, color=color)
        name = _style(r.qualname or r.name, _BOLD, color=color)
        sig = r.signature or "()"
        lines.append(f"  {name} — {loc} — {sig}")

    # Always print the caveat when there's something to review: these are
    # candidates, not proof (acceptance criterion: clearly label as heuristic).
    if not report.is_empty:
        caveat = (
            "\U0001f9ea Heuristic: string-level scan, not a call graph — reflection or "
            "dynamic dispatch can hide real uses. Review before deleting."
        )
        lines.append(_style(caveat, _DIM, color=color))

    return "\n".join(lines)
