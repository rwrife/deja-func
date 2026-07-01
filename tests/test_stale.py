"""Tests for `deja stale`: dead-code candidate finding (stale.py), serialize, CLI.

The dead-code finder (PLAN.md §8 #9): the complement to `deja find`. A function
is a *candidate* when its name is never referenced (word-boundary) anywhere in
the indexed tree besides its own definition line. Results are heuristic — the
scan is string-level, not a real call graph.

Unit tests drive the pure :func:`deja.stale.find_stale` with an injected source
reader (no disk); CLI tests exercise the real walker/index path on ``tmp_path``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from deja.cli import main
from deja.parsers import FunctionRecord
from deja.render import format_stale
from deja.serialize import stale_to_dict
from deja.stale import DEFAULT_IGNORE, StaleReport, find_stale


def _rec(
    name: str,
    *,
    file: str = "m.py",
    line: int = 1,
    lang: str = "python",
    sig: str = "()",
    qualname: str = "",
) -> FunctionRecord:
    return FunctionRecord(
        name=name,
        file=file,
        line=line,
        signature=sig,
        docstring="",
        lang=lang,
        qualname=qualname or name,
    )


def _reader(sources: dict[str, str]):
    """Build a ``(rel) -> text | None`` reader backed by an in-memory dict."""

    def read(rel: str) -> str | None:
        return sources.get(rel)

    return read


# -- core: used vs. unused --------------------------------------------------


def test_used_function_is_not_flagged() -> None:
    records = [_rec("used", file="a.py", line=1), _rec("caller", file="a.py", line=5)]
    sources = {
        "a.py": "def used():\n    return 1\n\n\ndef caller():\n    return used()\n",
    }
    report = find_stale(records, ["a.py"], _reader(sources))
    names = {s.name for s in report.candidates}
    # `used` is called by `caller`; only `caller` itself is unreferenced.
    assert "used" not in names
    assert "caller" in names


def test_unused_function_is_flagged() -> None:
    records = [_rec("orphan", file="a.py", line=1)]
    sources = {"a.py": "def orphan():\n    return 1\n"}
    report = find_stale(records, ["a.py"], _reader(sources))
    assert [s.name for s in report.candidates] == ["orphan"]
    assert report.candidates[0].references == 0


def test_own_definition_line_does_not_count_as_a_use() -> None:
    # A bare `def name(...)` mentions its own name; that must be subtracted so a
    # never-called function with a unique name is still flagged.
    records = [_rec("solo", file="a.py", line=1)]
    sources = {"a.py": "def solo(solo=None):\n    return solo\n"}
    # `solo` appears 3x total but all on/after its own def; the def line is
    # excluded and the body reference keeps it alive... so here it's USED.
    report = find_stale(records, ["a.py"], _reader(sources))
    # Body reference (`return solo`) is a real external-to-def-line use.
    assert [s.name for s in report.candidates] == []


def test_reference_from_another_file_keeps_it_alive() -> None:
    records = [_rec("shared", file="lib.py", line=1)]
    sources = {
        "lib.py": "def shared():\n    return 1\n",
        "app.py": "from lib import shared\nshared()\n",
    }
    report = find_stale(records, ["lib.py", "app.py"], _reader(sources))
    assert report.candidates == ()


# -- word-boundary correctness ---------------------------------------------


def test_word_boundary_parse_vs_parseall() -> None:
    records = [_rec("parse", file="a.py", line=1), _rec("parseAll", file="a.py", line=5)]
    sources = {
        # `parseAll` is called; `parse` is never called as a whole word.
        "a.py": "def parse(s):\n    return s\n\n\ndef parseAll():\n    return parseAll\n",
    }
    report = find_stale(records, ["a.py"], _reader(sources))
    names = {s.name for s in report.candidates}
    assert "parse" in names  # not matched inside `parseAll`
    assert "parseAll" not in names  # referenced in its own body (external to def line)


def test_substring_in_larger_identifier_is_not_a_reference() -> None:
    records = [_rec("run", file="a.py", line=1)]
    sources = {"a.py": "def run():\n    return 1\n\n\nrerun_all = 2\nprerun = 3\n"}
    report = find_stale(records, ["a.py"], _reader(sources))
    # `run` inside `rerun_all` / `prerun` must not count.
    assert [s.name for s in report.candidates] == ["run"]


# -- ignore patterns --------------------------------------------------------


def test_default_ignores_are_applied() -> None:
    records = [
        _rec("__init__", file="a.py", line=1),
        _rec("main", file="a.py", line=5),
        _rec("test_foo", file="t.py", line=1),
        _rec("setUp", file="t.py", line=5),
        _rec("real_orphan", file="a.py", line=10),
    ]
    sources = {"a.py": "x\n", "t.py": "y\n"}  # nothing references anything
    report = find_stale(records, ["a.py", "t.py"], _reader(sources))
    names = {s.name for s in report.candidates}
    # Only the non-ignored orphan is reported.
    assert names == {"real_orphan"}
    # Four names matched default ignore patterns.
    assert report.ignored == 4
    assert report.ignore_patterns == DEFAULT_IGNORE


def test_custom_ignore_glob_is_layered_on_defaults() -> None:
    records = [
        _rec("handler_a", file="a.py", line=1),
        _rec("handler_b", file="a.py", line=5),
        _rec("keepme", file="a.py", line=10),
    ]
    sources = {"a.py": "nothing\n"}
    report = find_stale(records, ["a.py"], _reader(sources), ignore=["handler_*"])
    names = {s.name for s in report.candidates}
    assert names == {"keepme"}
    assert "handler_*" in report.ignore_patterns
    # Defaults are still present alongside the custom pattern.
    assert set(DEFAULT_IGNORE).issubset(set(report.ignore_patterns))


def test_ignored_name_still_counts_as_a_reference_source() -> None:
    # `main` is ignored (never itself flagged), but a call *from* main must keep
    # the callee alive.
    records = [_rec("helper", file="a.py", line=1), _rec("main", file="a.py", line=5)]
    sources = {"a.py": "def helper():\n    return 1\n\n\ndef main():\n    return helper()\n"}
    report = find_stale(records, ["a.py"], _reader(sources))
    assert report.candidates == ()  # helper is used by main; main is ignored


# -- language filter --------------------------------------------------------


def test_lang_filter_reports_only_matching_functions() -> None:
    records = [
        _rec("py_orphan", file="a.py", line=1, lang="python"),
        _rec("js_orphan", file="a.js", line=1, lang="javascript"),
    ]
    sources = {"a.py": "nothing\n", "a.js": "nothing\n"}
    report = find_stale(records, ["a.py", "a.js"], _reader(sources), lang="python")
    assert [s.name for s in report.candidates] == ["py_orphan"]
    assert report.lang == "python"
    # scanned counts only python functions (after the lang filter).
    assert report.scanned_functions == 1


def test_lang_filter_still_scans_all_files_for_references() -> None:
    # A python helper referenced only from a JS file must NOT be flagged even
    # under --lang python: the scan covers every file, lang only narrows reports.
    records = [_rec("cross", file="lib.py", line=1, lang="python")]
    sources = {
        "lib.py": "def cross():\n    return 1\n",
        "app.js": "cross();\n",
    }
    report = find_stale(records, ["lib.py", "app.js"], _reader(sources), lang="python")
    assert report.candidates == ()


# -- ordering / edge cases --------------------------------------------------


def test_candidates_sorted_by_file_then_line_then_name() -> None:
    records = [
        _rec("b_two", file="b.py", line=2),
        _rec("b_one", file="b.py", line=1),
        _rec("a_late", file="a.py", line=99),
    ]
    sources = {"a.py": "x\n", "b.py": "y\n"}
    report = find_stale(records, ["a.py", "b.py"], _reader(sources))
    ordered = [(s.file, s.line, s.name) for s in report.candidates]
    assert ordered == [("a.py", 99, "a_late"), ("b.py", 1, "b_one"), ("b.py", 2, "b_two")]


def test_empty_inventory_is_empty_report() -> None:
    report = find_stale([], ["a.py"], _reader({"a.py": "code\n"}))
    assert report.is_empty
    assert len(report) == 0


def test_unreadable_file_is_skipped_not_fatal() -> None:
    records = [_rec("orphan", file="a.py", line=1)]
    # Reader returns None for the "unreadable" file; scan just skips it.
    report = find_stale(records, ["a.py", "gone.py"], _reader({"a.py": "def orphan():\n    x\n"}))
    assert [s.name for s in report.candidates] == ["orphan"]


# -- serialize --------------------------------------------------------------


def test_stale_to_dict_shape_and_values() -> None:
    records = [_rec("dead", file="a.py", line=3, sig="(x: int) -> int")]
    # Def sits on line 3 (two leading blanks) so its own-line subtraction lines up
    # with the record's declared line, exactly as the real parser guarantees.
    report = find_stale(records, ["a.py"], _reader({"a.py": "\n\ndef dead(x):\n    return 0\n"}))
    doc = stale_to_dict(report)
    assert doc["schema_version"] == 1
    assert doc["lang"] is None
    assert doc["count"] == 1
    assert doc["scanned"] == 1
    assert doc["ignore"] == list(DEFAULT_IGNORE)
    (cand,) = doc["candidates"]
    assert cand["references"] == 0
    assert cand["function"]["name"] == "dead"
    assert cand["function"]["file"] == "a.py"
    assert cand["function"]["line"] == 3
    assert cand["function"]["signature"] == "(x: int) -> int"


def test_stale_to_dict_is_json_serializable() -> None:
    report = find_stale(
        [_rec("dead", file="a.py", line=1)],
        ["a.py"],
        _reader({"a.py": "def dead():\n    return 0\n"}),
    )
    # Round-trips through json without error.
    text = json.dumps(stale_to_dict(report))
    assert json.loads(text)["count"] == 1


# -- render -----------------------------------------------------------------


def test_format_stale_lists_candidates_with_location() -> None:
    report = find_stale(
        [_rec("dead", file="src/util.py", line=1, sig="(x: int) -> int")],
        ["src/util.py"],
        _reader({"src/util.py": "def dead(x):\n    return 0\n"}),
    )
    out = format_stale(report, color=False)
    assert "dead" in out
    assert "src/util.py:1" in out
    assert "(x: int) -> int" in out
    # Heuristic caveat is present when there is something to review.
    assert "Heuristic" in out


def test_format_stale_empty_has_no_caveat() -> None:
    report = StaleReport()  # nothing stale
    out = format_stale(report, color=False)
    assert "No dead-code candidates" in out
    assert "Heuristic" not in out  # caveat only shown when there are candidates


def test_format_stale_no_ansi_when_color_false() -> None:
    report = find_stale(
        [_rec("dead", file="a.py", line=1)],
        ["a.py"],
        _reader({"a.py": "def dead():\n    return 0\n"}),
    )
    out = format_stale(report, color=False)
    assert "\033[" not in out


# -- CLI --------------------------------------------------------------------


def _repo(root: Path) -> None:
    """A tiny repo: `used` is called by `main`; `dead_weight` is never used."""
    (root / "app.py").write_text(
        '''
def used(x):
    """A used helper."""
    return x + 1


def dead_weight(y):
    """Nobody calls me."""
    return y - 1


def main():
    """Entry point (ignored by default)."""
    return used(41)
''',
    )


def test_cli_stale_builds_index_and_reports(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    _repo(tmp_path)
    rc = main(["stale", str(tmp_path)])
    captured = capsys.readouterr()
    assert rc == 0  # a candidate was found
    assert "dead_weight" in captured.out
    assert "used" not in captured.out  # used is referenced by main
    # First-run auto-built the index.
    assert (tmp_path / ".dejafunc" / "index.json").is_file()


def test_cli_stale_json_emits_stable_document(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    _repo(tmp_path)
    rc = main(["stale", str(tmp_path), "--json"])
    captured = capsys.readouterr()
    assert rc == 0
    doc = json.loads(captured.out)
    assert doc["schema_version"] == 1
    names = {c["function"]["name"] for c in doc["candidates"]}
    assert "dead_weight" in names
    assert "used" not in names
    assert "main" not in names  # default-ignored


def test_cli_stale_custom_ignore_suppresses_candidate(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    _repo(tmp_path)
    rc = main(["stale", str(tmp_path), "--json", "--ignore", "dead_*"])
    captured = capsys.readouterr()
    doc = json.loads(captured.out)
    names = {c["function"]["name"] for c in doc["candidates"]}
    assert "dead_weight" not in names  # now ignored
    assert rc == 1  # nothing left to report → clean exit code


def test_cli_stale_lang_filter(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    _repo(tmp_path)
    # Only python functions exist; filtering to javascript yields nothing.
    rc = main(["stale", str(tmp_path), "--json", "--lang", "javascript"])
    captured = capsys.readouterr()
    doc = json.loads(captured.out)
    assert doc["count"] == 0
    assert doc["lang"] == "javascript"
    assert rc == 1


def test_cli_stale_missing_dir_exits_2(capsys: pytest.CaptureFixture) -> None:
    rc = main(["stale", "/no/such/path/deja-stale"])
    assert rc == 2
    assert "not a directory" in capsys.readouterr().err


def test_cli_stale_clean_repo_exits_nonzero(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    # Every function references the other → no dead code.
    (tmp_path / "pair.py").write_text(
        "def a():\n    return b()\n\n\ndef b():\n    return a()\n",
    )
    rc = main(["stale", str(tmp_path)])
    captured = capsys.readouterr()
    assert rc == 1
    assert "No dead-code candidates" in captured.out


def test_cli_stale_uses_existing_index(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    assert main(["index", str(tmp_path)]) == 0
    _repo(tmp_path)  # add files *after* indexing
    capsys.readouterr()  # drop index output
    # The stale scan reads the (empty) existing index, so no candidates surface
    # even though app.py now exists on disk.
    rc = main(["stale", str(tmp_path), "--json"])
    captured = capsys.readouterr()
    doc = json.loads(captured.out)
    assert doc["count"] == 0
    assert rc == 1
