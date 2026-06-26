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
from .search import ScoredRecord

# Minimal ANSI; only used on a TTY (see _style).
_BOLD = "\033[1m"
_DIM = "\033[2m"
_CYAN = "\033[36m"
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
