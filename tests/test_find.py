"""Tests for `deja find`: ranking (search.py), rendering (render.py), CLI (M3)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from deja.cli import main
from deja.parsers import FunctionRecord
from deja.render import format_results
from deja.search import MIN_SCORE, ScoredRecord, score_record, search
from deja.sigshape import parse_signature


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
    s, breakdown = score_record("slugify", _rec("slugify"))
    assert 0.0 <= s <= 100.0
    assert s >= MIN_SCORE
    assert breakdown.name is not None


# -- M4: signature-shape & intent ------------------------------------------


def test_sig_search_matches_by_shape_when_name_is_useless() -> None:
    # Query has no usable text; only the shape distinguishes the candidates.
    records = [
        _rec("aaa", sig="(text: str) -> bool", line=1),
        _rec("bbb", sig="(a: int, b: int) -> int", line=2),
    ]
    results = search("", records, sig="(str)->bool")
    assert results
    assert results[0].record.name == "aaa"


def test_sig_search_ranks_better_shape_first() -> None:
    records = [
        _rec("close", sig="(s: str) -> bool", line=1),
        _rec("far", sig="(a: int, b: int, c: int) -> None", line=2),
    ]
    results = search("", records, sig="(str)->bool")
    assert [r.record.name for r in results][0] == "close"
    assert results[0].score >= results[-1].score


def test_sig_accepts_preparsed_shape() -> None:
    records = [_rec("f", sig="(s: str) -> bool", line=1)]
    results = search("", records, sig=parse_signature("(str)->bool", query=True))
    assert results and results[0].record.name == "f"


def test_blended_query_plus_sig_reports_both_signals() -> None:
    rec = _rec("validate_email", doc="Validate an email address.", sig="(s: str) -> bool")
    score, breakdown = score_record(
        "validate email",
        rec,
        sig=parse_signature("(str)->bool", query=True),
    )
    assert breakdown.name is not None
    assert breakdown.docstring is not None
    assert breakdown.signature is not None
    # Strong on all three; blended score should clear the noise floor.
    assert score >= MIN_SCORE


def test_intent_mode_weights_docstring_over_name() -> None:
    # Name is unrelated; only the docstring expresses the intent.
    records = [
        _rec("xq", doc="Validate an email address and return True if valid.", line=1),
        _rec("validate_email", doc="Adds two numbers together.", line=2),
    ]
    intent = search("validate an email address", records, intent=True)
    assert intent
    assert intent[0].record.name == "xq"


def test_empty_query_and_no_sig_returns_nothing() -> None:
    assert search("", [_rec("slugify")]) == []


def test_breakdown_signature_none_without_sig() -> None:
    _, breakdown = score_record("slugify", _rec("slugify"))
    assert breakdown.signature is None


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


def test_format_results_explain_shows_breakdown() -> None:
    from deja.search import ScoreBreakdown

    res = [
        ScoredRecord(
            record=_rec("slugify", doc="Slugify text.", line=7),
            score=92.0,
            breakdown=ScoreBreakdown(name=92.0, docstring=40.0, signature=None),
        )
    ]
    out = format_results("slugify", res, color=False, explain=True)
    assert "score 92" in out
    assert "name 92" in out
    assert "doc 40" in out
    assert "sig" not in out  # signature signal absent -> not shown


def test_format_results_empty_query_header_mentions_shape() -> None:
    out = format_results("", [], color=False)
    assert "shape" in out  # pure --sig search shouldn't render an empty ''


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


def test_cli_find_by_sig_only(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    _sample_repo(tmp_path)
    # No text query at all — search purely by shape (both sample funcs are (str)->str).
    rc = main(["find", "--sig", "(str)->str", str(tmp_path)])
    captured = capsys.readouterr()
    assert rc == 0
    assert "strings.py:" in captured.out


def test_cli_find_sig_no_match_exits_nonzero(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    _sample_repo(tmp_path)
    # Sample funcs are (str)->str; a wildly different shape should miss.
    rc = main(["find", "--sig", "(int, int, int, int) -> dict", str(tmp_path)])
    assert rc == 1


def test_cli_find_no_query_and_no_sig_errors(capsys: pytest.CaptureFixture) -> None:
    rc = main(["find"])
    assert rc == 2
    assert "query" in capsys.readouterr().err


def test_cli_find_explain_flag_shows_scores(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    _sample_repo(tmp_path)
    rc = main(["find", "slugify", str(tmp_path), "--explain"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "score" in captured.out


def test_cli_find_intent_flag(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    _sample_repo(tmp_path)
    # Intent query matches slugify's docstring ("URL-safe slug"), not its name.
    rc = main(["find", "url safe slug", str(tmp_path), "--intent"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "slugify" in captured.out


# -- M6: --json output -----------------------------------------------------


def test_cli_find_json_emits_stable_document(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    _sample_repo(tmp_path)
    rc = main(["find", "slugify", str(tmp_path), "--json"])
    captured = capsys.readouterr()
    assert rc == 0
    doc = json.loads(captured.out)
    assert doc["schema_version"] == 1
    assert doc["query"] == "slugify"
    assert doc["count"] >= 1
    assert doc["results"][0]["name"] == "slugify"
    assert doc["results"][0]["file"].endswith("strings.py")


def test_cli_find_json_no_match_is_empty_and_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    _sample_repo(tmp_path)
    rc = main(["find", "zzzqqq_no_such_function", str(tmp_path), "--json"])
    captured = capsys.readouterr()
    assert rc == 1
    doc = json.loads(captured.out)
    assert doc["count"] == 0
    assert doc["results"] == []


def test_cli_find_json_echoes_sig(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    _sample_repo(tmp_path)
    rc = main(["find", "--sig", "(str)->str", str(tmp_path), "--json"])
    captured = capsys.readouterr()
    assert rc == 0
    doc = json.loads(captured.out)
    assert doc["sig"] == "(str)->str"
