"""Tests for `deja diff`: two-snapshot comparison (diff.py), serialization, CLI.

Covers the acceptance criteria from issue #36: added / removed / renamed
detection (rename via signature + docstring, *not* name), a no-change case,
``--json`` output, and the CLI end-to-end (exit codes, error handling).
"""

from __future__ import annotations

import json
from pathlib import Path

from deja.cli import main
from deja.diff import (
    RENAME_THRESHOLD,
    diff_indexes,
    rename_score,
)
from deja.index import Index
from deja.parsers import FunctionRecord
from deja.render import format_diff
from deja.serialize import diff_to_dict


def _rec(
    name: str,
    *,
    doc: str = "",
    qualname: str = "",
    file: str = "m.py",
    line: int = 1,
    sig: str = "()",
):
    return FunctionRecord(
        name=name,
        file=file,
        line=line,
        signature=sig,
        docstring=doc,
        lang="python",
        qualname=qualname,
    )


def _index(*records: FunctionRecord) -> Index:
    return Index(records=list(records))


# --- core diff logic -------------------------------------------------------


def test_no_change_identical_indexes():
    idx = _index(_rec("a", file="x.py", line=1), _rec("b", file="x.py", line=2))
    report = diff_indexes(idx, idx)
    assert report.added == ()
    assert report.removed == ()
    assert report.renamed == ()
    assert report.unchanged == 2
    assert report.any_changes is False


def test_added_function():
    old = _index(_rec("a", file="x.py", line=1))
    new = _index(_rec("a", file="x.py", line=1), _rec("b", file="x.py", line=2))
    report = diff_indexes(old, new)
    assert [r.name for r in report.added] == ["b"]
    assert report.removed == ()
    assert report.renamed == ()
    assert report.unchanged == 1
    assert report.any_changes is True


def test_removed_function():
    old = _index(_rec("a", file="x.py", line=1), _rec("b", file="x.py", line=2))
    new = _index(_rec("a", file="x.py", line=1))
    report = diff_indexes(old, new)
    assert report.added == ()
    assert [r.name for r in report.removed] == ["b"]
    assert report.renamed == ()
    assert report.unchanged == 1


def test_rename_detected_by_signature_and_docstring():
    # Same signature + docstring, different name → a rename, not remove+add.
    old = _index(
        _rec(
            "parse_iso",
            file="dates.py",
            line=10,
            sig="(value: str) -> datetime",
            doc="Parse an ISO-8601 timestamp into a datetime.",
        )
    )
    new = _index(
        _rec(
            "from_iso_string",
            file="dates.py",
            line=10,
            sig="(value: str) -> datetime",
            doc="Parse an ISO-8601 timestamp into a datetime.",
        )
    )
    report = diff_indexes(old, new)
    assert report.added == ()
    assert report.removed == ()
    assert len(report.renamed) == 1
    rn = report.renamed[0]
    assert rn.old.name == "parse_iso"
    assert rn.new.name == "from_iso_string"
    assert rn.score >= RENAME_THRESHOLD


def test_move_across_files_is_a_rename():
    # Same name/sig/doc but a different file → still a rename (moved).
    old = _index(_rec("slugify", file="old/text.py", line=5, sig="(s: str) -> str", doc="Slug it."))
    new = _index(_rec("slugify", file="new/text.py", line=9, sig="(s: str) -> str", doc="Slug it."))
    report = diff_indexes(old, new)
    assert len(report.renamed) == 1
    assert report.renamed[0].old.file == "old/text.py"
    assert report.renamed[0].new.file == "new/text.py"


def test_unrelated_add_and_remove_not_a_rename():
    # Totally different signatures/docs → independent remove + add.
    old = _index(
        _rec("gone", file="a.py", line=1, sig="(x: int, y: int) -> int", doc="Add two ints.")
    )
    new = _index(
        _rec("fresh", file="b.py", line=1, sig="() -> None", doc="Do something unrelated.")
    )
    report = diff_indexes(old, new)
    assert report.renamed == ()
    assert [r.name for r in report.removed] == ["gone"]
    assert [r.name for r in report.added] == ["fresh"]


def test_rename_matching_is_one_to_one():
    # One removed function, two similar additions: only the best pairs.
    old = _index(_rec("orig", file="a.py", line=1, sig="(s: str) -> str", doc="Transform text."))
    new = _index(
        _rec("orig_v2", file="a.py", line=2, sig="(s: str) -> str", doc="Transform text."),
        _rec("orig_v3", file="a.py", line=3, sig="(s: str) -> str", doc="Transform text."),
    )
    report = diff_indexes(old, new)
    assert len(report.renamed) == 1
    # The unmatched addition falls through to `added`.
    assert len(report.added) == 1


def test_rename_score_excludes_name():
    # Identical everything except name still scores high (name isn't a signal).
    a = _rec("alpha", sig="(x: int) -> bool", doc="Check a thing.")
    b = _rec("beta", sig="(x: int) -> bool", doc="Check a thing.")
    assert rename_score(a, b) >= RENAME_THRESHOLD


def test_high_threshold_forces_separate_add_remove():
    old = _index(_rec("a", file="x.py", line=1, sig="(s: str) -> str", doc="One kind of job."))
    new = _index(_rec("b", file="x.py", line=1, sig="(n: int) -> bool", doc="A different job."))
    report = diff_indexes(old, new, rename_threshold=95.0)
    assert report.renamed == ()
    assert len(report.added) == 1
    assert len(report.removed) == 1


# --- serialization ---------------------------------------------------------


def test_diff_to_dict_shape():
    old = _index(_rec("gone", file="a.py", line=1))
    new = _index(_rec("fresh", file="b.py", line=1))
    doc = diff_to_dict(diff_indexes(old, new))
    assert doc["schema_version"] == 1
    assert doc["unchanged"] == 0
    assert [r["name"] for r in doc["added"]] == ["fresh"]
    assert [r["name"] for r in doc["removed"]] == ["gone"]
    assert doc["renamed"] == []


def test_diff_to_dict_rename_entry():
    old = _index(_rec("x", sig="(s: str) -> str", doc="Slug it."))
    new = _index(_rec("y", sig="(s: str) -> str", doc="Slug it."))
    doc = diff_to_dict(diff_indexes(old, new))
    assert len(doc["renamed"]) == 1
    entry = doc["renamed"][0]
    assert entry["old"]["name"] == "x"
    assert entry["new"]["name"] == "y"
    assert isinstance(entry["score"], float)


# --- rendering -------------------------------------------------------------


def test_format_diff_no_change():
    idx = _index(_rec("a"))
    out = format_diff(diff_indexes(idx, idx), color=False)
    assert "No function changes" in out


def test_format_diff_sections():
    old = _index(_rec("gone", file="a.py", line=1))
    new = _index(_rec("fresh", file="b.py", line=2))
    out = format_diff(diff_indexes(old, new), color=False)
    assert "+ fresh" in out
    assert "- gone" in out
    assert "Inventory diff" in out


def test_format_diff_rename_arrow():
    old = _index(_rec("x", sig="(s: str) -> str", doc="Slug it."))
    new = _index(_rec("y", sig="(s: str) -> str", doc="Slug it."))
    out = format_diff(diff_indexes(old, new), color=False)
    assert "x → y" in out
    assert "match)" in out


# --- CLI end-to-end --------------------------------------------------------


def _write_index(path: Path, *records: FunctionRecord) -> None:
    path.write_text(json.dumps(_index(*records).to_dict()), encoding="utf-8")


def test_cli_diff_changes_exit_1(tmp_path, capsys):
    old = tmp_path / "old.json"
    new = tmp_path / "new.json"
    _write_index(old, _rec("a", file="x.py", line=1))
    _write_index(new, _rec("a", file="x.py", line=1), _rec("b", file="x.py", line=2))
    code = main(["diff", str(old), str(new)])
    assert code == 1
    out = capsys.readouterr().out
    assert "+ b" in out


def test_cli_diff_no_change_exit_0(tmp_path, capsys):
    old = tmp_path / "old.json"
    new = tmp_path / "new.json"
    _write_index(old, _rec("a", file="x.py", line=1))
    _write_index(new, _rec("a", file="x.py", line=1))
    code = main(["diff", str(old), str(new)])
    assert code == 0
    assert "No function changes" in capsys.readouterr().out


def test_cli_diff_json(tmp_path, capsys):
    old = tmp_path / "old.json"
    new = tmp_path / "new.json"
    _write_index(old, _rec("gone", file="a.py", line=1))
    _write_index(new, _rec("fresh", file="b.py", line=1))
    code = main(["diff", str(old), str(new), "--json"])
    assert code == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc["schema_version"] == 1
    assert [r["name"] for r in doc["added"]] == ["fresh"]
    assert [r["name"] for r in doc["removed"]] == ["gone"]


def test_cli_diff_missing_file(tmp_path, capsys):
    old = tmp_path / "old.json"
    _write_index(old, _rec("a"))
    code = main(["diff", str(old), str(tmp_path / "nope.json")])
    assert code == 2
    assert "No such export file" in capsys.readouterr().err


def test_cli_diff_bad_threshold(tmp_path, capsys):
    old = tmp_path / "old.json"
    new = tmp_path / "new.json"
    _write_index(old, _rec("a"))
    _write_index(new, _rec("a"))
    code = main(["diff", str(old), str(new), "--rename-threshold", "150"])
    assert code == 2
    assert "between 0 and 100" in capsys.readouterr().err
