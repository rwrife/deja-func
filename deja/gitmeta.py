"""Git "last touched" metadata for indexed files (PLAN.md §8 #6, issue #41).

When ``deja find`` surfaces an existing function, the single most useful signal
for *"is this safe to reuse?"* is **when it was last touched**: a function edited
last week is fresh-and-trusted; one untouched for two years is
ancient-and-forgotten. This module answers that question cheaply and uniformly
for both ``deja explain`` (one function) and ``deja find --age`` (a whole result
set), by shelling out to git *once per file* and caching the answer.

Design:

* :class:`GitAge` bundles the three facts a reader wants — git's own human
  *relative* age (``"3 weeks ago"``, via ``%cr``), the ISO commit *date*
  (``%cs`` → ``"2026-06-01"``), and the short *hash* (``%h``) — pulled in a
  single ``git log -1`` call so one subprocess yields everything.
* :class:`GitAgeCache` memoizes by repo-relative path, so a large ``deja find``
  result set spanning N files spawns at most N git processes (and only for the
  files actually shown), never one-per-result. Misses (non-git repo, untracked /
  deleted file, git absent) cache as ``None`` so they aren't retried either.
* Everything degrades gracefully to ``None`` outside a git working tree, keeping
  callers branch-free: no age → the age line/column is simply omitted.

The subprocess boundary is injectable (``runner``) so tests drive the parsing
with a fake git rather than a real repo, mirroring the pure/testable split used
across :mod:`deja.explain` and :mod:`deja.stale`.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass

#: ``git log -1`` pretty format packing all three facts into one line, tab-
#: separated: relative age (%cr), ISO commit date (%cs), short hash (%h).
_LOG_FORMAT = "%cr%x09%cs%x09%h"

#: A callable ``(rel_path) -> raw stdout | None`` wrapping the git call. ``None``
#: signals the lookup failed (no repo / untracked / git missing) so the age is
#: unavailable. Injected so tests stay disk/git-free.
GitRunner = Callable[[str], "str | None"]


@dataclass(frozen=True, slots=True)
class GitAge:
    """A file's "last touched" facts from its most recent commit.

    Attributes:
        relative: git's own human phrasing of the age (``"3 weeks ago"``), from
            ``%cr`` — kept verbatim so the report reads like ``git log``.
        iso_date: The commit's committer date as ``YYYY-MM-DD`` (``%cs``).
        short_hash: The abbreviated commit hash (``%h``).
    """

    relative: str
    iso_date: str
    short_hash: str

    def summary(self) -> str:
        """One-line human summary: ``"3 weeks ago (2026-06-01, a1b2c3d)"``.

        This is the exact phrasing the ``deja explain`` "Last touched:" line and
        the ``deja find --age`` column render, so the format lives in one place.
        """
        return f"{self.relative} ({self.iso_date}, {self.short_hash})"


def parse_git_age(raw: str | None) -> GitAge | None:
    """Parse one ``git log -1 --format`` line into a :class:`GitAge`.

    Expects the tab-separated ``%cr\\t%cs\\t%h`` shape emitted by
    :data:`_LOG_FORMAT`. Returns ``None`` for empty output (no history) or a
    malformed line (fewer than three fields, or blank age), so a partial/odd git
    response degrades to "age unavailable" rather than crashing.

    Args:
        raw: Raw stdout from the git call, or ``None`` when the call failed.

    Returns:
        A populated :class:`GitAge`, or ``None`` when the age can't be determined.
    """
    if not raw:
        return None
    line = raw.strip()
    if not line:
        return None
    parts = line.split("\t")
    if len(parts) != 3:
        return None
    relative, iso_date, short_hash = (p.strip() for p in parts)
    if not relative or not iso_date or not short_hash:
        return None
    return GitAge(relative=relative, iso_date=iso_date, short_hash=short_hash)


def _default_runner(root: str) -> GitRunner:
    """Build a :data:`GitRunner` that shells out to ``git log`` under *root*.

    Runs ``git log -1 --format=<fmt> -- <rel>`` in *root*. Returns ``None`` (so
    the age is unavailable) when *root* isn't a git working tree, git isn't
    installed, or the path has no commit history / was deleted since indexing.
    """

    def run(rel: str) -> str | None:
        try:
            proc = subprocess.run(
                ["git", "log", "-1", f"--format={_LOG_FORMAT}", "--", rel],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
            )
        except (OSError, subprocess.CalledProcessError):  # pragma: no cover - env-dependent
            return None
        return proc.stdout

    return run


class GitAgeCache:
    """Per-file, memoized "last touched" lookups for one repo root.

    Instantiate once per command run and call :meth:`get` for each file: repeated
    lookups of the same path reuse the first result (hit *or* miss), so a
    ``deja find --age`` over many results touching a handful of files spawns at
    most one git process per distinct file, and none at all once cached. This is
    the acceptance-criteria "batches/caches per-file git calls" guarantee.

    The git boundary is injectable via *runner* for tests; the default shells out
    to real git under *root*.
    """

    __slots__ = ("_cache", "_runner")

    def __init__(self, root: str, *, runner: GitRunner | None = None) -> None:
        """Bind the cache to *root* (used to build the default git runner)."""
        self._runner: GitRunner = runner if runner is not None else _default_runner(root)
        self._cache: dict[str, GitAge | None] = {}

    def get(self, rel: str) -> GitAge | None:
        """Return the cached :class:`GitAge` for *rel*, computing it once.

        A first lookup runs git (via the runner) and parses the result; the
        outcome — a :class:`GitAge` or ``None`` (unavailable) — is cached so the
        same path is never queried twice within a run.
        """
        if rel in self._cache:
            return self._cache[rel]
        age = parse_git_age(self._runner(rel))
        self._cache[rel] = age
        return age

    def relative(self, rel: str) -> str | None:
        """Convenience: just the human relative age for *rel* (or ``None``).

        Lets callers that only want the ``%cr`` string (e.g. the back-compatible
        ``explain`` age field) share the same cache without unpacking a
        :class:`GitAge`.
        """
        age = self.get(rel)
        return age.relative if age is not None else None
