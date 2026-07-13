"""Tests for `deja why <query>`: per-signal score + weight breakdown (issue #35)."""

from __future__ import annotations

import json
from pathlib import Path

from deja.cli import main
from deja.config import SearchWeights
from deja.parsers import FunctionRecord
from deja.render import format_why
from deja.search import score_record, search


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


# -- breakdown carries weights + weighted contributions --------------------


def test_breakdown_records_applied_weights() -> None:
    rec = _rec("slugify", doc="turn text into a url slug")
    _score, bd = score_record("slugify", rec)
    # Name signal applied → its weight is recorded, not None.
    assert bd.name is not None
    assert bd.name_weight is not None
    # Signature signal not applied (no --sig) → weight stays None.
    assert bd.signature is None
    assert bd.signature_weight is None


def test_weighted_parts_total_matches_winning_signal() -> None:
    rec = _rec("slugify", doc="turn text into a url slug")
    score, bd = score_record("slugify", rec)
    rows = bd.weighted_parts()
    assert rows, "expected at least one signal"
    # Rows are sorted by weighted contribution desc; the top one is the blend.
    top_label, top_raw, top_weight, top_weighted = rows[0]
    assert top_weighted == top_raw * top_weight
    # Blended total == the winning weighted signal (blend is a max), rounded.
    assert round(min(top_weighted, 100.0), 6) == round(score, 6)


def test_config_weights_flow_into_breakdown() -> None:
    rec = _rec("slugify", doc="turn text into a url slug")
    weights = SearchWeights(name=2.0, docstring=0.1, signature=None)
    _score, bd = score_record("slugify", rec, weights=weights)
    assert bd.name_weight == 2.0
    if bd.docstring is not None:
        assert bd.docstring_weight == 0.1


# -- render ----------------------------------------------------------------


def test_format_why_lists_signals_and_total() -> None:
    rec = _rec("slugify", doc="turn text into a url slug")
    results = search("slugify", [rec])
    out = format_why("slugify", results, color=False)
    assert "slugify" in out
    assert "total" in out
    # A weighted line like "name  88.0 × 1.00 = 88.0".
    assert "×" in out and "=" in out


def test_format_why_empty_is_friendly() -> None:
    out = format_why("nope", [], color=False)
    assert "nope" in out


# -- CLI text + JSON -------------------------------------------------------


def _seed_repo(tmp_path: Path) -> Path:
    (tmp_path / "mod.py").write_text(
        '''
def slugify(text):
    """Turn arbitrary text into a URL-safe slug."""
    return text.lower().replace(" ", "-")
''',
        encoding="utf-8",
    )
    return tmp_path


def test_cli_why_text(tmp_path: Path, capsys) -> None:
    _seed_repo(tmp_path)
    code = main(["why", "slugify", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0
    assert "slugify" in out
    assert "total" in out


def test_cli_why_json_has_scores_object(tmp_path: Path, capsys) -> None:
    _seed_repo(tmp_path)
    code = main(["why", "slugify", str(tmp_path), "--json"])
    out = capsys.readouterr().out
    assert code == 0
    doc = json.loads(out)
    match = doc["results"][0]
    assert "scores" in match
    scores = match["scores"]
    assert "signals" in scores and "total" in scores
    # Each signal exposes raw, weight, and weighted; weighted == raw * weight.
    for _label, sig in scores["signals"].items():
        assert set(sig) == {"raw", "weight", "weighted"}
        assert round(sig["raw"] * sig["weight"], 1) == sig["weighted"]
    # Total equals the top weighted signal (blend is a max).
    top = max(s["weighted"] for s in scores["signals"].values())
    assert scores["total"] == top


def test_cli_why_no_match_exits_one(tmp_path: Path, capsys) -> None:
    _seed_repo(tmp_path)
    code = main(["why", "zzzznotathing", str(tmp_path)])
    assert code == 1
