"""Incremental ``deja index --watch`` (PLAN.md §8 #2).

Keeps ``.dejafunc/index.json`` fresh while you work: when a source file is
created, modified, or deleted, only *that* file is reparsed and merged into the
existing index — never the whole tree. Edits are debounced (a short quiet period
after the last change) so a burst of saves, a ``git checkout``, or an editor's
atomic-rename dance collapse into a single reindex.

Design choices (matching PLAN.md §5 "no heavy toolchain"):

* **Stdlib-only, polling watcher.** We snapshot file mtimes/sizes via ``os.scandir``
  and diff successive snapshots, rather than pulling in ``watchdog``/inotify as a
  dependency. Polling is portable, dependency-free, and plenty for a dev-loop
  index; the cost is a configurable poll interval (default 1s).
* **Same exclude rules as a full index.** The snapshot reuses
  :func:`deja.walker.iter_source_files`, so ``.gitignore`` and the hardcoded
  skip-dirs apply identically — a watched edit can never sneak a vendored file
  into the index (issue #10).
* **Clean shutdown.** Ctrl-C (``KeyboardInterrupt``) stops the loop and prints a
  summary: how many reindex passes ran and the net function delta.

The pure snapshot/diff helpers and the injectable loop hooks (``sleep``/``now``)
keep this fully unit-testable without real timers or signals.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .index import Index, apply_changes, build_index, load_index, save_index
from .walker import iter_source_files

#: Default seconds between filesystem polls.
DEFAULT_INTERVAL = 1.0
#: Default debounce: wait this long after the last observed change before
#: reindexing, so rapid successive saves collapse into one pass.
DEFAULT_DEBOUNCE = 0.4

#: A file's "stamp" in a snapshot: ``(mtime_ns, size)``. Comparing both catches
#: same-second edits that leave mtime resolution ambiguous.
Stamp = tuple[int, int]
#: A snapshot maps repo-relative POSIX path -> stamp.
Snapshot = dict[str, Stamp]


@dataclass(frozen=True, slots=True)
class Diff:
    """The set of paths that changed between two snapshots."""

    created: frozenset[str] = frozenset()
    modified: frozenset[str] = frozenset()
    removed: frozenset[str] = frozenset()

    def __bool__(self) -> bool:
        return bool(self.created or self.modified or self.removed)

    @property
    def changed(self) -> frozenset[str]:
        """Created + modified (the files that need reparsing)."""
        return self.created | self.modified

    @property
    def touched(self) -> frozenset[str]:
        """Every path involved (created + modified + removed)."""
        return self.created | self.modified | self.removed


@dataclass
class WatchStats:
    """Running totals reported in the shutdown summary."""

    passes: int = 0
    files_reindexed: int = 0
    records_added: int = 0
    records_dropped: int = 0
    started_at: float = field(default_factory=time.monotonic)

    @property
    def net_records(self) -> int:
        """Net change in indexed functions across the whole session."""
        return self.records_added - self.records_dropped


def take_snapshot(root: str | os.PathLike[str]) -> Snapshot:
    """Return a ``rel_path -> (mtime_ns, size)`` map of all indexable files.

    Uses :func:`deja.walker.iter_source_files`, so the snapshot honors exactly
    the same ``.gitignore`` / skip-dir rules as a full index build. Files that
    vanish between the walk and the ``stat`` are simply omitted (treated as gone).
    """
    root_path = Path(root)
    snap: Snapshot = {}
    for rel in iter_source_files(root_path):
        try:
            st = (root_path / rel).stat()
        except OSError:
            continue
        snap[rel] = (st.st_mtime_ns, st.st_size)
    return snap


def diff_snapshots(old: Snapshot, new: Snapshot) -> Diff:
    """Compute created/modified/removed paths between two snapshots."""
    old_keys = old.keys()
    new_keys = new.keys()
    created = frozenset(new_keys - old_keys)
    removed = frozenset(old_keys - new_keys)
    modified = frozenset(k for k in (old_keys & new_keys) if old[k] != new[k])
    return Diff(created=created, modified=modified, removed=removed)


def _format_diff_line(diff: Diff) -> str:
    """One-line human description of a reindex pass."""
    bits: list[str] = []
    if diff.created:
        bits.append(f"+{len(diff.created)} new")
    if diff.modified:
        bits.append(f"~{len(diff.modified)} changed")
    if diff.removed:
        bits.append(f"-{len(diff.removed)} gone")
    return ", ".join(bits) if bits else "no changes"


def _load_or_build(root: Path, *, log: Callable[[str], None]) -> Index:
    """Load the existing index, or build one if this is the first watch."""
    try:
        return load_index(root)
    except FileNotFoundError:
        log("\U0001f9e0 No index yet — building one first…")
        index = build_index(root)
        save_index(index, root)
        log(f"\U0001f9e0 Indexed {len(index)} functions to start.")
        return index


def watch(
    root: str | os.PathLike[str],
    *,
    interval: float = DEFAULT_INTERVAL,
    debounce: float = DEFAULT_DEBOUNCE,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
    stop: Callable[[], bool] | None = None,
    log: Callable[[str], None] | None = None,
) -> WatchStats:
    """Watch *root* and incrementally reindex changed files until interrupted.

    Builds (or loads) the index once, then polls every *interval* seconds. When a
    change is seen it waits for a *debounce* quiet window (so bursts coalesce),
    reparses only the touched files via :func:`deja.index.apply_changes`, saves,
    and prints a one-line summary of the pass. Stops on ``KeyboardInterrupt`` (or
    when the injected *stop* predicate returns ``True``) and returns aggregate
    :class:`WatchStats` for the caller to print.

    The ``sleep`` / ``now`` / ``stop`` hooks are injectable purely so tests can
    drive the loop deterministically without real time or signals.
    """
    root_path = Path(root)
    if log is None:

        def log(msg: str) -> None:
            print(msg, file=sys.stderr)

    index = _load_or_build(root_path, log=log)
    snapshot = take_snapshot(root_path)
    stats = WatchStats()

    log(
        f"\U0001f441\ufe0f  Watching {root_path} for changes "
        f"(poll {interval:g}s, debounce {debounce:g}s). Press Ctrl-C to stop."
    )

    def _reindex(diff: Diff) -> None:
        added, dropped = apply_changes(index, root_path, diff.changed, diff.removed)
        save_index(index, root_path)
        stats.passes += 1
        stats.files_reindexed += len(diff.touched)
        stats.records_added += added
        stats.records_dropped += dropped
        log(
            f"\U0001f504 reindex: {_format_diff_line(diff)} "
            f"\u2192 {len(index)} functions "
            f"(+{added}/-{dropped})"
        )

    try:
        while True:
            if stop is not None and stop():
                break
            sleep(interval)

            new_snapshot = take_snapshot(root_path)
            diff = diff_snapshots(snapshot, new_snapshot)
            if not diff:
                snapshot = new_snapshot
                continue

            # Debounce: keep polling the quiet window until the filesystem
            # settles, so a flurry of saves becomes a single reindex pass.
            if debounce > 0:
                while True:
                    sleep(debounce)
                    settled = take_snapshot(root_path)
                    if settled == new_snapshot:
                        break
                    new_snapshot = settled
                    if stop is not None and stop():
                        break
                diff = diff_snapshots(snapshot, new_snapshot)

            snapshot = new_snapshot
            if diff:
                _reindex(diff)
    except KeyboardInterrupt:
        log("")  # newline after the inline ^C

    return stats


def format_summary(stats: WatchStats) -> str:
    """Render the end-of-session summary line(s) for a finished watch."""
    elapsed = max(0.0, time.monotonic() - stats.started_at)
    if stats.passes == 0:
        return f"\U0001f44b Stopped watching. No changes in {elapsed:.0f}s."
    delta = stats.net_records
    sign = "+" if delta >= 0 else ""
    return (
        f"\U0001f44b Stopped watching. "
        f"{stats.passes} reindex pass(es), "
        f"{stats.files_reindexed} file event(s), "
        f"net {sign}{delta} function(s) over {elapsed:.0f}s."
    )


def run_watch(
    root: str | os.PathLike[str],
    *,
    interval: float = DEFAULT_INTERVAL,
    debounce: float = DEFAULT_DEBOUNCE,
    log: Callable[[str], None] | None = None,
) -> int:
    """CLI adapter: run :func:`watch` and print the shutdown summary.

    Returns a process exit code (0 — a clean Ctrl-C is a normal, successful end
    of a watch session, not a failure).
    """
    if log is None:

        def log(msg: str) -> None:
            print(msg, file=sys.stderr)

    stats = watch(root, interval=interval, debounce=debounce, log=log)
    log(format_summary(stats))
    return 0
