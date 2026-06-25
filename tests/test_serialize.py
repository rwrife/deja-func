"""Tests for the stable JSON serialization layer (serialize.py, M6)."""

from __future__ import annotations

import json

from deja.parsers import FunctionRecord
from deja.search import ScoreBreakdown, ScoredRecord
from deja.serialize import SCHEMA_VERSION, results_to_dict, scored_to_dict


def _scored(
    name: str = "slugify",
    *,
    qualname: str = "",
    file: str = "src/text.py",
    line: int = 42,
    sig: str = "(value: str) -> str",
    doc: str = "Turn a string into a URL-safe slug.",
    score: float = 88.0,
    breakdown: ScoreBreakdown | None = None,
) -> ScoredRecord:
    rec = FunctionRecord(
        name=name,
        file=file,
        line=line,
        signature=sig,
        docstring=doc,
        lang="python",
        qualname=qualname or name,
    )
    return ScoredRecord(
        record=rec,
        score=score,
        breakdown=breakdown if breakdown is not None else ScoreBreakdown(name=88.0, docstring=60.0),
    )


def test_scored_to_dict_has_stable_keys() -> None:
    d = scored_to_dict(_scored())
    assert set(d) == {
        "name",
        "qualname",
        "file",
        "line",
        "signature",
        "docstring",
        "lang",
        "score",
        "breakdown",
    }
    assert d["name"] == "slugify"
    assert d["file"] == "src/text.py"
    assert d["line"] == 42
    assert d["lang"] == "python"
    assert set(d["breakdown"]) == {"name", "doc", "sig"}


def test_scores_are_rounded_to_one_decimal() -> None:
    d = scored_to_dict(_scored(score=88.04, breakdown=ScoreBreakdown(name=88.04, docstring=60.06)))
    assert d["score"] == 88.0
    assert d["breakdown"]["name"] == 88.0
    assert d["breakdown"]["doc"] == 60.1


def test_qualname_falls_back_to_name() -> None:
    d = scored_to_dict(_scored(name="parse", qualname=""))
    assert d["qualname"] == "parse"


def test_absent_signal_serializes_as_null() -> None:
    d = scored_to_dict(_scored(breakdown=ScoreBreakdown(name=88.0)))
    assert d["breakdown"]["doc"] is None
    assert d["breakdown"]["sig"] is None


def test_results_to_dict_document_shape() -> None:
    doc = results_to_dict([_scored(), _scored(name="dasherize")], query="slug", sig=None)
    assert doc["schema_version"] == SCHEMA_VERSION
    assert doc["query"] == "slug"
    assert doc["sig"] is None
    assert doc["intent"] is False
    assert doc["count"] == 2
    assert len(doc["results"]) == 2


def test_results_to_dict_echoes_sig_and_intent() -> None:
    doc = results_to_dict([], query="", sig="(str)->bool", intent=True)
    assert doc["sig"] == "(str)->bool"
    assert doc["intent"] is True
    assert doc["count"] == 0
    assert doc["results"] == []


def test_document_is_json_serializable() -> None:
    doc = results_to_dict([_scored()], query="slug")
    # Round-trips through json without error and preserves the count.
    again = json.loads(json.dumps(doc))
    assert again["count"] == 1
