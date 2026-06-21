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
    if n == 0:
        return f"🤔 Nothing like {query!r} yet — looks new. Go write it."
    if n == 1:
        lead = "🫠 You already wrote this:"
    else:
        lead = f"🫠 You already wrote {n} of these:"
    return _style(lead, _BOLD, color=color)


def format_results(
    query: str,
    results: Iterable[ScoredRecord],
    *,
    color: bool | None = None,
    stream=None,
) -> str:
    """Render *results* into a printable block of text.

    Args:
        query: The original search query (used in the header).
        results: Ranked matches from :func:`deja.search.search`.
        color: Force ANSI on/off; ``None`` auto-detects from *stream*.
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
    return "\n".join(lines)
