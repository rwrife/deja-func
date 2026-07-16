"""Tests for :mod:`deja.gitmeta` — git "last touched" metadata (issue #41).

Covers the pure parsing/caching path with an injected fake git runner, plus a
real-``git init`` integration to prove the default runner reads a live repo and
degrades gracefully outside one.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from deja.gitmeta import GitAge, GitAgeCache, parse_git_age


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)


# -- parse_git_age ---------------------------------------------------------


def test_parse_splits_relative_date_and_hash() -> None:
    age = parse_git_age("3 weeks ago\t2026-06-25\ta1b2c3d")
    assert age == GitAge(relative="3 weeks ago", iso_date="2026-06-25", short_hash="a1b2c3d")


def test_parse_summary_format() -> None:
    age = GitAge(relative="3 weeks ago", iso_date="2026-06-25", short_hash="a1b2c3d")
    assert age.summary() == "3 weeks ago (2026-06-25, a1b2c3d)"


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "only two\tfields",  # too few
        "a\tb\tc\td",  # too many
        "\t2026-06-25\ta1b2c3d",  # blank relative
    ],
)
def test_parse_returns_none_on_bad_input(raw: str | None) -> None:
    assert parse_git_age(raw) is None


# -- GitAgeCache -----------------------------------------------------------


def test_cache_memoizes_per_file() -> None:
    calls: list[str] = []

    def runner(rel: str) -> str | None:
        calls.append(rel)
        return "1 day ago\t2026-07-15\tdeadbee"

    cache = GitAgeCache(".", runner=runner)
    first = cache.get("f.py")
    second = cache.get("f.py")
    assert first is second  # same object, cached
    assert first == GitAge("1 day ago", "2026-07-15", "deadbee")
    assert calls == ["f.py"]  # git invoked exactly once


def test_cache_memoizes_misses_too() -> None:
    calls: list[str] = []

    def runner(rel: str) -> str | None:
        calls.append(rel)
        return None  # e.g. untracked / non-git

    cache = GitAgeCache(".", runner=runner)
    assert cache.get("f.py") is None
    assert cache.get("f.py") is None
    assert calls == ["f.py"]  # a miss is cached, not retried


def test_cache_relative_convenience() -> None:
    cache = GitAgeCache(".", runner=lambda rel: "2 hours ago\t2026-07-16\tcafef00")
    assert cache.relative("f.py") == "2 hours ago"
    assert GitAgeCache(".", runner=lambda rel: None).relative("f.py") is None


# -- default runner (real git) --------------------------------------------


def test_default_runner_reads_committed_file(tmp_path: Path) -> None:
    (tmp_path / "f.py").write_text("def f():\n    return 1\n")
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "Tester")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "initial")

    age = GitAgeCache(str(tmp_path)).get("f.py")
    assert age is not None
    assert "ago" in age.relative
    # ISO date is YYYY-MM-DD and hash is non-empty.
    assert len(age.iso_date.split("-")) == 3
    assert age.short_hash


def test_default_runner_none_outside_git(tmp_path: Path) -> None:
    (tmp_path / "f.py").write_text("def f():\n    return 1\n")
    assert GitAgeCache(str(tmp_path)).get("f.py") is None
