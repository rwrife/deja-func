"""Tests for `deja index --watch` incremental reindex (issue #10, PLAN.md §8 #2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from deja.cli import main
from deja.index import Index, apply_changes, build_index, load_index, parse_file, save_index
from deja.walker import is_indexable_file
from deja.watch import (
    Diff,
    WatchStats,
    diff_snapshots,
    format_summary,
    run_watch,
    take_snapshot,
    watch,
)


def _write(root: Path, rel: str, body: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


# --- is_indexable_file (shared walker rule, reused by the watcher) ----------


def test_is_indexable_file_accepts_plain_source(tmp_path: Path) -> None:
    _write(tmp_path, "mod.py", "def f(): pass\n")
    assert is_indexable_file(tmp_path, "mod.py") is True


def test_is_indexable_file_rejects_unparseable_extension(tmp_path: Path) -> None:
    _write(tmp_path, "notes.txt", "hello\n")
    assert is_indexable_file(tmp_path, "notes.txt") is False


def test_is_indexable_file_rejects_skip_dirs(tmp_path: Path) -> None:
    assert is_indexable_file(tmp_path, "node_modules/pkg/index.js") is False
    assert is_indexable_file(tmp_path, ".dejafunc/whatever.py") is False
    assert is_indexable_file(tmp_path, ".venv/lib/x.py") is False


def test_is_indexable_file_respects_gitignore(tmp_path: Path) -> None:
    _write(tmp_path, ".gitignore", "ignored/\nsecret.py\n")
    _write(tmp_path, "ignored/skip.py", "def skip(): pass\n")
    _write(tmp_path, "secret.py", "def s(): pass\n")
    _write(tmp_path, "keep.py", "def k(): pass\n")
    assert is_indexable_file(tmp_path, "ignored/skip.py") is False
    assert is_indexable_file(tmp_path, "secret.py") is False
    assert is_indexable_file(tmp_path, "keep.py") is True


# --- parse_file + apply_changes (incremental index core) --------------------


def test_parse_file_returns_records(tmp_path: Path) -> None:
    _write(tmp_path, "mod.py", "def a(): pass\ndef b(): pass\n")
    recs = parse_file(tmp_path, "mod.py")
    assert {r.name for r in recs} == {"a", "b"}


def test_parse_file_missing_or_unparseable_is_empty(tmp_path: Path) -> None:
    assert parse_file(tmp_path, "gone.py") == []
    _write(tmp_path, "data.txt", "x")
    assert parse_file(tmp_path, "data.txt") == []


def test_apply_changes_adds_new_file(tmp_path: Path) -> None:
    idx = Index()
    _write(tmp_path, "mod.py", "def a(): pass\n")
    added, dropped = apply_changes(idx, tmp_path, ["mod.py"], [])
    assert added == 1
    assert dropped == 0
    assert {r.name for r in idx.records} == {"a"}


def test_apply_changes_replaces_modified_file(tmp_path: Path) -> None:
    _write(tmp_path, "mod.py", "def a(): pass\ndef b(): pass\n")
    idx = build_index(tmp_path)
    assert {r.name for r in idx.records} == {"a", "b"}

    # Edit the file: drop b, add c.
    _write(tmp_path, "mod.py", "def a(): pass\ndef c(): pass\n")
    added, dropped = apply_changes(idx, tmp_path, ["mod.py"], [])
    assert {r.name for r in idx.records} == {"a", "c"}
    # Two old records dropped, two new parsed.
    assert dropped == 2
    assert added == 2


def test_apply_changes_prunes_removed_file(tmp_path: Path) -> None:
    _write(tmp_path, "a.py", "def a(): pass\n")
    _write(tmp_path, "b.py", "def b(): pass\n")
    idx = build_index(tmp_path)
    assert {r.file for r in idx.records} == {"a.py", "b.py"}

    added, dropped = apply_changes(idx, tmp_path, [], ["b.py"])
    assert {r.file for r in idx.records} == {"a.py"}
    assert added == 0
    assert dropped == 1


def test_apply_changes_only_touches_named_files(tmp_path: Path) -> None:
    _write(tmp_path, "a.py", "def a(): pass\n")
    _write(tmp_path, "b.py", "def b(): pass\n")
    idx = build_index(tmp_path)
    a_record = next(r for r in idx.records if r.file == "a.py")

    # Modify only b.py on disk; reindex only b.py. a.py's record must survive
    # untouched even though we never reparsed it.
    _write(tmp_path, "b.py", "def b(): pass\ndef b2(): pass\n")
    apply_changes(idx, tmp_path, ["b.py"], [])
    assert a_record in idx.records
    assert {r.name for r in idx.records if r.file == "b.py"} == {"b", "b2"}


def test_apply_changes_keeps_sorted_order(tmp_path: Path) -> None:
    _write(tmp_path, "z.py", "def z(): pass\n")
    idx = build_index(tmp_path)
    _write(tmp_path, "a.py", "def a(): pass\n")
    apply_changes(idx, tmp_path, ["a.py"], [])
    keys = [(r.file, r.line) for r in idx.records]
    assert keys == sorted(keys)


# --- snapshot + diff --------------------------------------------------------


def test_take_snapshot_lists_only_indexable(tmp_path: Path) -> None:
    _write(tmp_path, "mod.py", "def a(): pass\n")
    _write(tmp_path, "notes.txt", "hi\n")
    _write(tmp_path, ".gitignore", "ignored/\n")
    _write(tmp_path, "ignored/skip.py", "def s(): pass\n")
    snap = take_snapshot(tmp_path)
    assert "mod.py" in snap
    assert "notes.txt" not in snap  # no parser
    assert "ignored/skip.py" not in snap  # gitignored


def test_diff_snapshots_detects_all_three_kinds() -> None:
    old = {"keep.py": (1, 10), "edit.py": (1, 10), "gone.py": (1, 10)}
    new = {"keep.py": (1, 10), "edit.py": (2, 12), "new.py": (1, 5)}
    diff = diff_snapshots(old, new)
    assert diff.created == frozenset({"new.py"})
    assert diff.modified == frozenset({"edit.py"})
    assert diff.removed == frozenset({"gone.py"})
    assert diff.changed == frozenset({"new.py", "edit.py"})
    assert diff.touched == frozenset({"new.py", "edit.py", "gone.py"})
    assert bool(diff) is True


def test_diff_empty_is_falsy() -> None:
    same = {"a.py": (1, 2)}
    assert bool(diff_snapshots(same, same)) is False


# --- the watch loop (driven deterministically via injected hooks) -----------


class _Clock:
    """Fake monotonic clock; advances only when ``sleep`` is called."""

    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


def test_watch_reindexes_on_change_then_stops(tmp_path: Path) -> None:
    _write(tmp_path, "mod.py", "def a(): pass\n")
    save_index(build_index(tmp_path), tmp_path)

    logs: list[str] = []
    clock = _Clock()
    state = {"ticks": 0}

    def stop() -> bool:
        # Let the loop run two iterations, mutating the file before the 2nd poll.
        state["ticks"] += 1
        if state["ticks"] == 1:
            _write(tmp_path, "mod.py", "def a(): pass\ndef b(): pass\n")
            return False
        return state["ticks"] > 2

    stats = watch(
        tmp_path,
        interval=1.0,
        debounce=0.0,
        sleep=clock.sleep,
        now=clock.now,
        stop=stop,
        log=logs.append,
    )

    assert stats.passes == 1
    assert stats.records_added == 2  # a + b reparsed
    assert stats.records_dropped == 1  # old a dropped
    # And the persisted index now reflects the edit.
    loaded = load_index(tmp_path)
    assert {r.name for r in loaded.records} == {"a", "b"}
    assert any("reindex" in m for m in logs)


def test_watch_no_changes_makes_no_passes(tmp_path: Path) -> None:
    _write(tmp_path, "mod.py", "def a(): pass\n")
    save_index(build_index(tmp_path), tmp_path)

    clock = _Clock()
    state = {"ticks": 0}

    def stop() -> bool:
        state["ticks"] += 1
        return state["ticks"] > 3

    stats = watch(
        tmp_path,
        interval=1.0,
        debounce=0.0,
        sleep=clock.sleep,
        now=clock.now,
        stop=stop,
        log=lambda _m: None,
    )
    assert stats.passes == 0
    assert stats.net_records == 0


def test_watch_builds_index_when_missing(tmp_path: Path) -> None:
    _write(tmp_path, "mod.py", "def a(): pass\n")
    # No save_index beforehand: watch must bootstrap one.
    clock = _Clock()
    state = {"ticks": 0}

    stats = watch(
        tmp_path,
        interval=1.0,
        debounce=0.0,
        sleep=clock.sleep,
        now=clock.now,
        stop=lambda: state.__setitem__("ticks", state["ticks"] + 1) or state["ticks"] > 1,
        log=lambda _m: None,
    )
    assert stats.passes == 0
    assert load_index(tmp_path).records  # bootstrapped


def test_watch_debounce_coalesces_into_single_pass(tmp_path: Path) -> None:
    _write(tmp_path, "mod.py", "def a(): pass\n")
    save_index(build_index(tmp_path), tmp_path)

    clock = _Clock()
    calls = {"snap": 0}

    # First debounce check still "in flux" (file changes again), second settles.
    def stop() -> bool:
        calls["snap"] += 1
        if calls["snap"] == 1:
            _write(tmp_path, "mod.py", "def a(): pass\ndef b(): pass\n")
        return calls["snap"] > 2

    stats = watch(
        tmp_path,
        interval=1.0,
        debounce=0.4,
        sleep=clock.sleep,
        now=clock.now,
        stop=stop,
        log=lambda _m: None,
    )
    # Despite multiple polls, the burst collapses into at most one reindex pass.
    assert stats.passes <= 1


def test_watch_clean_keyboard_interrupt(tmp_path: Path) -> None:
    _write(tmp_path, "mod.py", "def a(): pass\n")
    save_index(build_index(tmp_path), tmp_path)

    def boom(_seconds: float) -> None:
        raise KeyboardInterrupt

    logs: list[str] = []
    # Ctrl-C on the very first sleep must not propagate; returns stats cleanly.
    stats = watch(
        tmp_path,
        interval=1.0,
        debounce=0.0,
        sleep=boom,
        now=lambda: 0.0,
        log=logs.append,
    )
    assert isinstance(stats, WatchStats)
    assert stats.passes == 0


# --- summary rendering ------------------------------------------------------


def test_format_summary_no_changes() -> None:
    out = format_summary(WatchStats())
    assert "No changes" in out


def test_format_summary_with_passes() -> None:
    stats = WatchStats(passes=2, files_reindexed=3, records_added=5, records_dropped=1)
    out = format_summary(stats)
    assert "2 reindex pass" in out
    assert "net +4" in out


# --- run_watch + CLI wiring -------------------------------------------------


def test_run_watch_returns_zero_and_prints_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, "mod.py", "def a(): pass\n")
    save_index(build_index(tmp_path), tmp_path)

    # Make the underlying watch a no-op that returns a finished stats object.
    import deja.watch as watch_mod

    def fake_watch(*_a, **_k) -> WatchStats:
        return WatchStats(passes=1, records_added=1)

    monkeypatch.setattr(watch_mod, "watch", fake_watch)
    logs: list[str] = []
    code = run_watch(tmp_path, log=logs.append)
    assert code == 0
    assert any("Stopped watching" in m for m in logs)


def test_cli_index_watch_dispatches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(tmp_path, "mod.py", "def a(): pass\n")
    captured = {}

    import deja.watch as watch_mod

    def fake_run_watch(root, *, interval, debounce, **_k) -> int:
        captured["root"] = Path(root)
        captured["interval"] = interval
        captured["debounce"] = debounce
        return 0

    monkeypatch.setattr(watch_mod, "run_watch", fake_run_watch)
    code = main(["index", str(tmp_path), "--watch", "--interval", "2.5", "--debounce", "0.1"])
    assert code == 0
    assert captured["root"] == Path(str(tmp_path))
    assert captured["interval"] == 2.5
    assert captured["debounce"] == 0.1


def test_cli_index_watch_rejects_bad_interval(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["index", str(tmp_path), "--watch", "--interval", "0"])
    err = capsys.readouterr().err
    assert code == 2
    assert "interval" in err


def test_cli_index_watch_rejects_negative_debounce(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["index", str(tmp_path), "--watch", "--debounce", "-1"])
    err = capsys.readouterr().err
    assert code == 2
    assert "debounce" in err


def test_cli_index_watch_rejects_missing_dir(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["index", str(tmp_path / "nope"), "--watch"])
    err = capsys.readouterr().err
    assert code == 2
    assert "not a directory" in err


def test_diff_dataclass_defaults() -> None:
    # A bare Diff is empty/falsy with empty frozensets (defensive default check).
    d = Diff()
    assert not d
    assert d.changed == frozenset()
    assert d.touched == frozenset()
