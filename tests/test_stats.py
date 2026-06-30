"""Tests for `deja stats`: aggregation (stats.py), serialization, rendering, CLI.

The inventory leaderboard (PLAN.md §8 #10): turn `.dejafunc/index.json` into a
skimmable summary — totals, language breakdown, the most-duplicated names, and
the biggest files. Fixtures bake in known duplicates so the math is checkable.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from deja.cli import main
from deja.parsers import FunctionRecord
from deja.render import format_stats
from deja.serialize import stats_to_dict
from deja.stats import DEFAULT_TOP, compute_stats


def _rec(
    name: str,
    *,
    file: str = "m.py",
    line: int = 1,
    lang: str = "python",
    doc: str = "",
    sig: str = "()",
    qualname: str = "",
):
    return FunctionRecord(
        name=name,
        file=file,
        line=line,
        signature=sig,
        docstring=doc,
        lang=lang,
        qualname=qualname or name,
    )


def _inventory() -> list[FunctionRecord]:
    """A fixture inventory with known duplicate names and per-file/lang counts.

    Names:  parse ×3, slugify ×2, add ×1, render ×1, walk ×1  (8 funcs)
    Files:  a.py ×4, b.py ×3, c.js ×1
    Langs:  python ×7, javascript ×1
    """
    return [
        _rec("parse", file="a.py", line=1),
        _rec("parse", file="a.py", line=10),
        _rec("slugify", file="a.py", line=20),
        _rec("add", file="a.py", line=30),
        _rec("parse", file="b.py", line=1),
        _rec("slugify", file="b.py", line=10),
        _rec("render", file="b.py", line=20),
        _rec("walk", file="c.js", line=1, lang="javascript"),
    ]


# -- aggregation -----------------------------------------------------------


def test_compute_stats_totals() -> None:
    stats = compute_stats(_inventory())
    assert stats.total_functions == 8
    assert stats.total_files == 3


def test_compute_stats_language_breakdown_full_and_sorted() -> None:
    stats = compute_stats(_inventory())
    # Full breakdown (not capped), most functions first.
    assert stats.languages == (("python", 7), ("javascript", 1))


def test_compute_stats_top_names_counts_and_order() -> None:
    stats = compute_stats(_inventory())
    # Only repeated names (count >= 2), most-duplicated first.
    assert stats.top_names == (("parse", 3), ("slugify", 2))
    # Singletons never appear in the duplication leaderboard.
    flat = {name for name, _ in stats.top_names}
    assert "add" not in flat
    assert "render" not in flat


def test_compute_stats_biggest_files_counts_and_order() -> None:
    stats = compute_stats(_inventory())
    assert stats.biggest_files == (("a.py", 4), ("b.py", 3), ("c.js", 1))


def test_compute_stats_ties_break_alphabetically() -> None:
    # Two names each appearing twice should order alphabetically (apple before zed).
    records = [
        _rec("zed", file="z.py", line=1),
        _rec("zed", file="z.py", line=2),
        _rec("apple", file="a.py", line=1),
        _rec("apple", file="a.py", line=2),
    ]
    stats = compute_stats(records)
    assert stats.top_names == (("apple", 2), ("zed", 2))


def test_compute_stats_empty_inventory() -> None:
    stats = compute_stats([])
    assert stats.is_empty
    assert stats.total_functions == 0
    assert stats.total_files == 0
    assert stats.languages == ()
    assert stats.top_names == ()
    assert stats.biggest_files == ()


def test_compute_stats_default_top_is_ten() -> None:
    stats = compute_stats(_inventory())
    assert stats.top == DEFAULT_TOP == 10


# -- --top capping ---------------------------------------------------------


def test_top_caps_each_leaderboard_section() -> None:
    stats = compute_stats(_inventory(), top=1)
    # Each leaderboard capped to one row...
    assert stats.top_names == (("parse", 3),)
    assert stats.biggest_files == (("a.py", 4),)
    # ...but the language breakdown is reported in full regardless of --top.
    assert stats.languages == (("python", 7), ("javascript", 1))


def test_top_zero_yields_empty_leaderboards_but_keeps_totals() -> None:
    stats = compute_stats(_inventory(), top=0)
    assert stats.top_names == ()
    assert stats.biggest_files == ()
    # Totals + language breakdown still computed.
    assert stats.total_functions == 8
    assert stats.languages == (("python", 7), ("javascript", 1))


def test_top_larger_than_data_returns_everything() -> None:
    stats = compute_stats(_inventory(), top=999)
    assert len(stats.biggest_files) == 3  # only 3 files exist


# -- personality callout ---------------------------------------------------


def test_top_duplicate_callout_triggers_at_threshold() -> None:
    stats = compute_stats(_inventory())
    # "parse" appears 3 times → clears the callout threshold.
    assert stats.top_duplicate == ("parse", 3)


def test_top_duplicate_callout_absent_when_under_threshold() -> None:
    # Max duplication is 2 (< 3), so no callout.
    records = [
        _rec("slug", file="a.py", line=1),
        _rec("slug", file="a.py", line=2),
    ]
    stats = compute_stats(records)
    assert stats.top_duplicate is None


# -- serialization ---------------------------------------------------------


def test_stats_to_dict_stable_shape() -> None:
    doc = stats_to_dict(compute_stats(_inventory()))
    assert doc["schema_version"] == 1
    assert doc["total_functions"] == 8
    assert doc["total_files"] == 3
    assert doc["top"] == 10
    assert doc["languages"] == [
        {"lang": "python", "count": 7},
        {"lang": "javascript", "count": 1},
    ]
    assert doc["top_names"] == [
        {"name": "parse", "count": 3},
        {"name": "slugify", "count": 2},
    ]
    assert doc["biggest_files"][0] == {"file": "a.py", "count": 4}


def test_stats_to_dict_numbers_match_text_view() -> None:
    # The JSON totals must equal what the renderer reports (same Stats source).
    stats = compute_stats(_inventory(), top=2)
    doc = stats_to_dict(stats)
    text = format_stats(stats, color=False)
    assert str(doc["total_functions"]) in text
    assert str(doc["total_files"]) in text
    assert doc["top"] == 2


def test_stats_to_dict_empty() -> None:
    doc = stats_to_dict(compute_stats([]))
    assert doc["total_functions"] == 0
    assert doc["languages"] == []
    assert doc["top_names"] == []
    assert doc["biggest_files"] == []


# -- rendering -------------------------------------------------------------


def test_format_stats_no_color_contains_sections() -> None:
    out = format_stats(compute_stats(_inventory()), color=False)
    assert "8 functions across 3 files" in out
    assert "python: 7" in out and "javascript: 1" in out
    assert "Most duplicated names" in out
    assert "parse" in out and "(×3)" in out
    assert "Biggest files by function count" in out
    assert "a.py" in out and "(×4)" in out
    assert "\033[" not in out  # no ANSI when color is off


def test_format_stats_includes_personality_callout() -> None:
    out = format_stats(compute_stats(_inventory()), color=False)
    assert "parse" in out
    assert "shows up 3 times" in out


def test_format_stats_empty_is_friendly() -> None:
    out = format_stats(compute_stats([]), color=False)
    assert "Nothing indexed yet" in out
    assert "deja index" in out


def test_format_stats_color_emits_ansi() -> None:
    out = format_stats(compute_stats(_inventory()), color=True)
    assert "\033[" in out


def test_format_stats_singular_wording() -> None:
    out = format_stats(compute_stats([_rec("solo", file="one.py")]), color=False)
    assert "1 function across 1 file" in out


# -- CLI -------------------------------------------------------------------


def _repo(root: Path) -> None:
    (root / "dates.py").write_text(
        '''
def parse(s: str):
    """Parse an ISO 8601 date string into a date."""
    ...


def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


class Reader:
    def parse(self, text: str):
        """Parse a record from text."""
        ...
''',
    )
    (root / "more.py").write_text(
        '''
def parse(value: str):
    """Parse a value."""
    ...
''',
    )


def test_cli_stats_builds_index_and_reports(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    _repo(tmp_path)
    rc = main(["stats", str(tmp_path)])
    captured = capsys.readouterr()
    assert rc == 0
    assert "functions across" in captured.out
    assert "Most duplicated names" in captured.out
    # Index was auto-built on first run (same as `deja find` / `deja dupes`).
    assert (tmp_path / ".dejafunc" / "index.json").is_file()


def test_cli_stats_json_emits_stable_document(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    _repo(tmp_path)
    rc = main(["stats", str(tmp_path), "--json"])
    captured = capsys.readouterr()
    assert rc == 0
    doc = json.loads(captured.out)
    assert doc["schema_version"] == 1
    assert doc["total_functions"] == 4
    assert doc["total_files"] == 2
    # "parse" appears 3x across the two files.
    assert {"name": "parse", "count": 3} in doc["top_names"]


def test_cli_stats_top_flag_caps_sections(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    _repo(tmp_path)
    rc = main(["stats", str(tmp_path), "--json", "--top", "1"])
    captured = capsys.readouterr()
    assert rc == 0
    doc = json.loads(captured.out)
    assert doc["top"] == 1
    assert len(doc["top_names"]) <= 1
    assert len(doc["biggest_files"]) <= 1


def test_cli_stats_missing_dir_exits_2(capsys: pytest.CaptureFixture) -> None:
    rc = main(["stats", "/no/such/path/deja"])
    assert rc == 2
    assert "not a directory" in capsys.readouterr().err


def test_cli_stats_bad_top_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    _repo(tmp_path)
    rc = main(["stats", str(tmp_path), "--top", "-1"])
    assert rc == 2
    assert "top" in capsys.readouterr().err


def test_cli_stats_empty_repo_exits_nonzero(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    # A repo with no indexable functions yields an empty inventory → exit 1.
    (tmp_path / "notes.txt").write_text("just prose, no code here\n")
    rc = main(["stats", str(tmp_path)])
    captured = capsys.readouterr()
    assert rc == 1
    assert "Nothing indexed yet" in captured.out


def test_cli_stats_uses_existing_index(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    # Pre-build the index, then confirm stats reads it without rebuilding noise.
    assert main(["index", str(tmp_path)]) == 0
    _repo(tmp_path)  # add files *after* indexing
    capsys.readouterr()  # drop index output
    rc = main(["stats", str(tmp_path), "--json"])
    captured = capsys.readouterr()
    assert rc == 1  # stale index was empty; stats reflects what was indexed
    # No "building one first" message because an index already existed.
    assert "building one first" not in captured.err
    doc = json.loads(captured.out)
    assert doc["total_functions"] == 0
