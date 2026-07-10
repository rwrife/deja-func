"""Tests for `deja explain`: definition + callers + staleness (explain.py, CLI, JSON).

`deja explain <name>` (PLAN.md §8 #6) is the loop-closer after `deja find`: given
a name it resolves one function (exact, else fuzzy), then reports the definition,
a *textual* caller list scanned over the indexed files, and a git "last touched"
relative age. Like `deja stale`, the caller scan is a string-level heuristic — a
`name(` match, word-boundary bounded — not a real call graph.

Unit tests drive the pure resolver/scanner with injected readers (no disk, no
git); CLI tests exercise the real walker/index path on ``tmp_path``, including a
real ``git init`` so the "last touched" age and its graceful non-git degradation
are both covered.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from deja.cli import main
from deja.explain import (
    Ambiguous,
    Explanation,
    explain,
    find_callers,
    resolve,
)
from deja.parsers import FunctionRecord
from deja.render import format_ambiguous, format_explain_miss, format_explanation
from deja.serialize import (
    ambiguous_to_dict,
    explain_miss_to_dict,
    explanation_to_dict,
)


def _rec(
    name: str,
    *,
    file: str = "m.py",
    line: int = 1,
    lang: str = "python",
    sig: str = "()",
    doc: str = "",
    qualname: str = "",
) -> FunctionRecord:
    return FunctionRecord(
        name=name,
        file=file,
        line=line,
        signature=sig,
        docstring=doc,
        lang=lang,
        qualname=qualname or name,
    )


def _reader(sources: dict[str, str]):
    """Build a ``(rel) -> text | None`` reader backed by an in-memory dict."""

    def read(rel: str) -> str | None:
        return sources.get(rel)

    return read


# -- resolve: exact / ambiguous / fuzzy / miss ------------------------------


def test_resolve_exact_single_match() -> None:
    records = [_rec("slugify", file="a.py", line=3), _rec("other", file="b.py", line=1)]
    result = resolve("slugify", records)
    assert isinstance(result, Explanation)
    assert result.record.name == "slugify"
    assert result.fuzzy is False
    assert result.query == "slugify"


def test_resolve_exact_by_qualname() -> None:
    records = [_rec("parse", file="p.py", line=5, qualname="Foo.parse")]
    result = resolve("Foo.parse", records)
    assert isinstance(result, Explanation)
    assert result.record.qualname == "Foo.parse"


def test_resolve_multiple_exact_is_ambiguous() -> None:
    records = [
        _rec("parse", file="b.py", line=10, qualname="B.parse"),
        _rec("parse", file="a.py", line=20, qualname="A.parse"),
    ]
    result = resolve("parse", records)
    assert isinstance(result, Ambiguous)
    assert result.fuzzy is False
    # Deterministic ordering: by (file, line).
    assert [(r.file, r.line) for r in result.candidates] == [("a.py", 20), ("b.py", 10)]


def test_resolve_fuzzy_single_hit() -> None:
    # No exact "slugfy"; the sole close name is returned as a fuzzy match.
    records = [_rec("slugify", file="a.py", line=1), _rec("totally_unrelated", file="b.py")]
    result = resolve("slugfy", records)
    assert isinstance(result, Explanation)
    assert result.record.name == "slugify"
    assert result.fuzzy is True


def test_resolve_fuzzy_multiple_is_ambiguous() -> None:
    records = [
        _rec("parse_json", file="a.py", line=1),
        _rec("parse_yaml", file="b.py", line=1),
    ]
    result = resolve("parse", records)
    assert isinstance(result, Ambiguous)
    assert result.fuzzy is True
    assert {r.name for r in result.candidates} == {"parse_json", "parse_yaml"}


def test_resolve_unknown_returns_none() -> None:
    records = [_rec("alpha"), _rec("beta")]
    assert resolve("zzz_nomatch_zzz", records) is None


def test_resolve_blank_name_returns_none() -> None:
    assert resolve("   ", [_rec("x")]) is None


# -- find_callers: textual `name(` scan -------------------------------------


def test_find_callers_detects_call_sites() -> None:
    rec = _rec("used", file="a.py", line=1)
    sources = {
        "a.py": "def used():\n    return 1\n",
        "b.py": "from a import used\n\n\ndef caller():\n    return used()\n",
    }
    callers = find_callers(rec, ["a.py", "b.py"], _reader(sources))
    assert [(c.file, c.line) for c in callers] == [("b.py", 5)]
    assert callers[0].text == "return used()"


def test_find_callers_excludes_own_definition_line() -> None:
    # The def line mentions `used(` but must not count as a call to itself.
    rec = _rec("used", file="a.py", line=1)
    sources = {"a.py": "def used():\n    return 1\n"}
    assert find_callers(rec, ["a.py"], _reader(sources)) == []


def test_find_callers_is_word_boundary_bounded() -> None:
    # `parse(` must not match inside `reparse(` or `parseAll(`.
    rec = _rec("parse", file="p.py", line=1)
    sources = {
        "p.py": "def parse():\n    return 1\n",
        "use.py": "reparse()\nparseAll()\nparse()\n",
    }
    callers = find_callers(rec, ["p.py", "use.py"], _reader(sources))
    assert [(c.file, c.line) for c in callers] == [("use.py", 3)]


def test_find_callers_requires_a_call_not_a_bare_mention() -> None:
    # A bare mention (no `(`) is not a call site.
    rec = _rec("thing", file="t.py", line=1)
    sources = {
        "t.py": "def thing():\n    return 1\n",
        "u.py": "x = thing\ny = thing()\n",
    }
    callers = find_callers(rec, ["t.py", "u.py"], _reader(sources))
    assert [(c.line, c.text) for c in callers] == [(2, "y = thing()")]


def test_find_callers_allows_whitespace_before_paren() -> None:
    rec = _rec("f", file="f.py", line=1)
    sources = {"f.py": "def f():\n    return 1\n", "u.py": "f ()\n"}
    callers = find_callers(rec, ["f.py", "u.py"], _reader(sources))
    assert [c.line for c in callers] == [1]


def test_find_callers_skips_unreadable_files() -> None:
    rec = _rec("f", file="f.py", line=1)
    # "gone.py" returns None from the reader → skipped, no crash.
    sources = {"f.py": "def f():\n    return 1\n"}
    callers = find_callers(rec, ["f.py", "gone.py"], _reader(sources))
    assert callers == []


# -- explain: full assembly (callers + cap + age) ---------------------------


def test_explain_resolves_and_scans_callers() -> None:
    records = [_rec("used", file="a.py", line=1), _rec("caller", file="b.py", line=1)]
    sources = {
        "a.py": "def used():\n    return 1\n",
        "b.py": "def caller():\n    return used()\n",
    }
    result = explain("used", records, ["a.py", "b.py"], _reader(sources))
    assert isinstance(result, Explanation)
    assert result.caller_total == 1
    assert [(c.file, c.line) for c in result.callers] == [("b.py", 2)]


def test_explain_caps_callers_but_reports_full_total() -> None:
    rec = _rec("f", file="f.py", line=1)
    # 5 call sites across one file; cap the *shown* list to 2.
    body = "def f():\n    pass\n" + "".join(f"f()  # {i}\n" for i in range(5))
    sources = {"f.py": body}
    result = explain("f", [rec], ["f.py"], _reader(sources), max_callers=2)
    assert isinstance(result, Explanation)
    assert result.caller_total == 5
    assert len(result.callers) == 2


def test_explain_max_callers_zero_means_no_cap() -> None:
    rec = _rec("f", file="f.py", line=1)
    body = "def f():\n    pass\n" + "".join("f()\n" for _ in range(4))
    result = explain("f", [rec], ["f.py"], _reader({"f.py": body}), max_callers=0)
    assert isinstance(result, Explanation)
    assert result.caller_total == 4
    assert len(result.callers) == 4


def test_explain_uses_last_touched_reader() -> None:
    rec = _rec("f", file="f.py", line=1)
    sources = {"f.py": "def f():\n    pass\n"}
    result = explain(
        "f",
        [rec],
        ["f.py"],
        _reader(sources),
        last_touched=lambda rel: "3 months ago" if rel == "f.py" else None,
    )
    assert isinstance(result, Explanation)
    assert result.last_touched == "3 months ago"


def test_explain_last_touched_none_when_reader_absent() -> None:
    rec = _rec("f", file="f.py", line=1)
    result = explain("f", [rec], ["f.py"], _reader({"f.py": "def f():\n    pass\n"}))
    assert isinstance(result, Explanation)
    assert result.last_touched is None


def test_explain_ambiguous_short_circuits_without_scanning() -> None:
    records = [_rec("parse", file="a.py", line=1), _rec("parse", file="b.py", line=1)]

    def boom(_rel: str) -> str | None:  # pragma: no cover - must never be called
        raise AssertionError("caller scan must not run for ambiguous names")

    result = explain("parse", records, ["a.py"], boom)
    assert isinstance(result, Ambiguous)


def test_explain_unknown_returns_none() -> None:
    result = explain("nope", [_rec("x")], ["a.py"], _reader({"a.py": "x\n"}))
    assert result is None


# -- render -----------------------------------------------------------------


def test_format_explanation_plain_text() -> None:
    rec = _rec("slugify", file="src/text.py", line=42, sig="(v: str) -> str", doc="Make a slug.")
    result = Explanation(
        record=rec,
        callers=(),
        caller_total=0,
        last_touched="2 days ago",
        query="slugify",
    )
    out = format_explanation(result, color=False)
    assert "slugify — src/text.py:42 — (v: str) -> str" in out
    assert "Make a slug." in out
    assert "last touched 2 days ago" in out
    # Zero callers reads as a dead-code hint.
    assert "No callers found" in out


def test_format_explanation_lists_callers_and_more() -> None:
    rec = _rec("f", file="f.py", line=1)
    from deja.explain import Caller

    callers = (Caller("a.py", 3, "f()"), Caller("b.py", 9, "f()"))
    result = Explanation(record=rec, callers=callers, caller_total=5)
    out = format_explanation(result, color=False)
    assert "2 callers:" not in out  # header reflects the *total*, not the shown count
    assert "5 callers:" in out
    assert "a.py:3" in out
    assert "+3 more" in out
    assert "Heuristic" in out


def test_format_explanation_flags_fuzzy_resolution() -> None:
    rec = _rec("slugify", file="a.py", line=1)
    result = Explanation(record=rec, fuzzy=True, query="slugfy")
    out = format_explanation(result, color=False)
    assert "No exact match" in out
    assert "slugfy" in out


def test_format_ambiguous_lists_candidates() -> None:
    cands = (
        _rec("parse", file="a.py", line=10, qualname="A.parse"),
        _rec("parse", file="b.py", line=20, qualname="B.parse"),
    )
    out = format_ambiguous(Ambiguous(query="parse", candidates=cands), color=False)
    assert "matches 2 functions" in out
    assert "A.parse — a.py:10" in out
    assert "B.parse — b.py:20" in out


def test_format_explain_miss_is_friendly() -> None:
    out = format_explain_miss("ghost", color=False)
    assert "ghost" in out
    assert "isn't in the index" in out


# -- serialize --------------------------------------------------------------


def test_explanation_to_dict_shape() -> None:
    rec = _rec("slugify", file="src/text.py", line=42, sig="(v: str) -> str", doc="Slug.")
    from deja.explain import Caller

    result = Explanation(
        record=rec,
        callers=(Caller("a.py", 3, "slugify(x)"),),
        caller_total=2,
        last_touched="1 year ago",
        query="slugify",
    )
    doc = explanation_to_dict(result)
    assert doc["schema_version"] == 1
    assert doc["resolution"] == "exact"
    assert doc["query"] == "slugify"
    assert doc["definition"]["file"] == "src/text.py"
    assert doc["definition"]["line"] == 42
    assert "score" not in doc["definition"]  # bare record shape
    assert doc["last_touched"] == "1 year ago"
    assert doc["caller_total"] == 2  # full total even though only 1 caller shown
    assert doc["callers"] == [{"file": "a.py", "line": 3, "text": "slugify(x)"}]


def test_explanation_to_dict_fuzzy_resolution() -> None:
    rec = _rec("slugify", file="a.py", line=1)
    doc = explanation_to_dict(Explanation(record=rec, fuzzy=True, query="slugfy"))
    assert doc["resolution"] == "fuzzy"


def test_ambiguous_to_dict_shape() -> None:
    cands = (_rec("parse", file="a.py", line=1), _rec("parse", file="b.py", line=2))
    doc = ambiguous_to_dict(Ambiguous(query="parse", candidates=cands, fuzzy=True))
    assert doc["resolution"] == "ambiguous"
    assert doc["fuzzy"] is True
    assert doc["count"] == 2
    assert [c["file"] for c in doc["candidates"]] == ["a.py", "b.py"]


def test_explain_miss_to_dict_shape() -> None:
    doc = explain_miss_to_dict("ghost")
    assert doc == {"schema_version": 1, "query": "ghost", "resolution": "unknown"}


# -- CLI --------------------------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True, text=True)


def _repo(root: Path) -> None:
    """A tiny repo: `slugify` is called by `handler`; `orphan` is never called."""
    (root / "app.py").write_text(
        '''
def slugify(text):
    """Turn a string into a URL-safe slug."""
    return text.lower().replace(" ", "-")


def orphan(y):
    """Nobody calls me."""
    return y - 1


def handler():
    """Entry point that uses slugify."""
    return slugify("Hello World")
''',
    )


def _git_repo(root: Path) -> None:
    """Same tiny repo, but committed so `git log` yields a 'last touched' age."""
    _repo(root)
    _git(root, "init")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "Tester")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "initial")


def test_cli_explain_builds_index_and_reports(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    _repo(tmp_path)
    rc = main(["explain", "slugify", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "slugify — app.py:2" in out
    # `handler` calls slugify → at least one caller listed.
    assert "1 caller" in out
    assert "app.py:14" in out
    # Index was created on the fly.
    assert (tmp_path / ".dejafunc" / "index.json").is_file()


def test_cli_explain_json_emits_stable_document(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    _repo(tmp_path)
    rc = main(["explain", "slugify", str(tmp_path), "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    doc = json.loads(out)
    assert doc["schema_version"] == 1
    assert doc["resolution"] == "exact"
    assert doc["definition"]["name"] == "slugify"
    assert doc["caller_total"] == 1
    assert doc["callers"][0]["file"] == "app.py"


def test_cli_explain_ambiguous_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    (tmp_path / "a.py").write_text("class A:\n    def parse(self):\n        return 1\n")
    (tmp_path / "b.py").write_text("class B:\n    def parse(self):\n        return 2\n")
    rc = main(["explain", "parse", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 2
    assert "which one?" in out
    assert "A.parse" in out and "B.parse" in out


def test_cli_explain_unknown_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    _repo(tmp_path)
    rc = main(["explain", "does_not_exist", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "isn't in the index" in out


def test_cli_explain_shows_git_last_touched(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    _git_repo(tmp_path)
    rc = main(["explain", "slugify", str(tmp_path), "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    doc = json.loads(out)
    # Committed file → git %cr yields a non-null relative age.
    assert doc["last_touched"] is not None
    assert "ago" in doc["last_touched"]


def test_cli_explain_non_git_omits_age(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    _repo(tmp_path)  # no `git init` → outside a git repo
    rc = main(["explain", "slugify", str(tmp_path), "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    doc = json.loads(out)
    assert doc["last_touched"] is None


def test_cli_explain_max_callers_caps_display(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    # Define `f` and call it 4 times from another file.
    (tmp_path / "d.py").write_text("def f():\n    return 1\n")
    (tmp_path / "u.py").write_text("from d import f\n" + "".join("f()\n" for _ in range(4)))
    rc = main(["explain", "f", str(tmp_path), "--json", "--max-callers", "2"])
    out = capsys.readouterr().out
    assert rc == 0
    doc = json.loads(out)
    assert doc["caller_total"] == 4
    assert len(doc["callers"]) == 2  # display capped, total preserved


def test_cli_explain_missing_dir_exits_2(capsys: pytest.CaptureFixture) -> None:
    rc = main(["explain", "whatever", "/no/such/path/deja-explain"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "not a directory" in err


def test_cli_explain_uses_existing_index(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    assert main(["index", str(tmp_path)]) == 0
    _repo(tmp_path)  # add files *after* indexing
    capsys.readouterr()
    # The stale index has no `slugify` yet → unknown, exit 1 (no rebuild).
    rc = main(["explain", "slugify", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "isn't in the index" in out
