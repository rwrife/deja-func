"""Tests for `deja find`: ranking (search.py), rendering (render.py), CLI (M3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from deja.cli import main
from deja.parsers import FunctionRecord
from deja.render import format_results
from deja.search import MIN_SCORE, ScoredRecord, score_record, search


def _rec(name: str, *, doc: str = "", qualname: str = "", file: str = "m.py", line: int = 1):
    return FunctionRecord(
        name=name,
        file=file,
        line=line,
        signature="()",
        docstring=doc,
        lang="python",
        qualname=qualname or name,
    )


# -- ranking ---------------------------------------------------------------


def test_exact_name_outranks_unrelated() -> None:
    records = [_rec("slugify", line=1), _rec("compute_tax", line=2)]
    results = search("slugify", records)
    assert results
    assert results[0].record.name == "slugify"


def test_underscored_name_matches_spaced_query() -> None:
    records = [_rec("parse_iso_date", doc="", line=1), _rec("send_email", line=2)]
    results = search("parse date", records)
    assert results
    assert results[0].record.name == "parse_iso_date"


def test_docstring_drives_intent_query() -> None:
    # Query matches no identifier, only the human description.
    records = [
        _rec("xyz", doc="Convert a string into a URL-safe slug.", line=1),
        _rec("totally_different", doc="Adds two integers.", line=2),
    ]
    results = search("url safe slug", records)
    assert results
    assert results[0].record.name == "xyz"


def test_results_sorted_by_score_desc() -> None:
    records = [
        _rec("slug", line=1),
        _rec("slugify", line=2),
        _rec("slugify_path", line=3),
    ]
    results = search("slugify", records)
    scores = [r.score for r in results]
    assert scores == sorted(scores, reverse=True)


def test_limit_caps_results() -> None:
    records = [_rec(f"slugify_{i}", line=i) for i in range(20)]
    results = search("slugify", records, limit=3)
    assert len(results) == 3


def test_noise_is_filtered_by_min_score() -> None:
    records = [_rec("completely_unrelated_thing", doc="nope", line=1)]
    results = search("zzzzz", records)
    assert results == []


def test_empty_query_returns_nothing() -> None:
    assert search("   ", [_rec("slugify")]) == []


def test_score_record_in_range() -> None:
    s = score_record("slugify", _rec("slugify"))
    assert 0.0 <= s <= 100.0
    assert s >= MIN_SCORE


# -- rendering -------------------------------------------------------------


def test_format_results_no_color_has_location_and_name() -> None:
    res = [ScoredRecord(record=_rec("slugify", doc="Slugify text.", line=7), score=95.0)]
    out = format_results("slugify", res, color=False)
    assert "slugify" in out
    assert "m.py:7" in out
    assert "Slugify text." in out
    assert "\033[" not in out  # no ANSI when color is off


def test_format_results_empty_is_friendly() -> None:
    out = format_results("nope", [], color=False)
    assert "nope" in out
    assert out  # non-empty header even with zero matches


# -- CLI -------------------------------------------------------------------


def _sample_repo(root: Path) -> None:
    (root / "strings.py").write_text(
        '''
def slugify(text: str) -> str:
    """Convert a string into a URL-safe slug."""
    return text.lower().replace(" ", "-")


def shout(text: str) -> str:
    """Uppercase a string loudly."""
    return text.upper()
''',
    )


def test_cli_find_builds_index_and_finds(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    _sample_repo(tmp_path)
    rc = main(["find", "slugify", str(tmp_path)])
    captured = capsys.readouterr()
    assert rc == 0
    assert "slugify" in captured.out
    assert "strings.py:" in captured.out
    # index was auto-built on first find
    assert (tmp_path / ".dejafunc" / "index.json").is_file()


def test_cli_find_no_match_exits_nonzero(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    _sample_repo(tmp_path)
    rc = main(["find", "zzzqqq_no_such_function", str(tmp_path)])
    assert rc == 1


def test_cli_find_missing_dir_exits_2(capsys: pytest.CaptureFixture) -> None:
    rc = main(["find", "x", "/no/such/path/deja"])
    assert rc == 2


def test_cli_find_limit_flag(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    _sample_repo(tmp_path)
    rc = main(["find", "string", str(tmp_path), "--limit", "1"])
    captured = capsys.readouterr()
    assert rc == 0
    # Header + exactly one match line (+ optional docstring line); at most 1 location shown.
    assert captured.out.count("strings.py:") == 1
