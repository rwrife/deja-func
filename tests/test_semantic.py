"""Tests for optional semantic search (`deja find --semantic`, PLAN.md §8 #4, issue #9).

No real embedding model is ever loaded: a deterministic :class:`FakeBackend`
stands in for sentence-transformers/Ollama, so these tests are fast and run in
CI without heavy deps or network. We assert the four acceptance criteria:

* ranking by embedding similarity,
* an incrementally-updated ``.dejafunc/`` cache,
* zero heavy import on the default (non-``--semantic``) path,
* graceful fallback + clear message when no backend is installed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from deja.cli import main
from deja.parsers import FunctionRecord
from deja.semantic import (
    EmbeddingBackend,
    cosine,
    embed_cache_path,
    embed_records,
    fingerprint,
    load_backend,
    record_text,
    semantic_search,
)


def _rec(
    name: str,
    *,
    doc: str = "",
    qualname: str = "",
    file: str = "m.py",
    line: int = 1,
    sig: str = "()",
) -> FunctionRecord:
    return FunctionRecord(
        name=name,
        file=file,
        line=line,
        signature=sig,
        docstring=doc,
        lang="python",
        qualname=qualname or name,
    )


class FakeBackend:
    """A deterministic, dependency-free embedding backend for tests.

    Maps each input text to a small vector via keyword presence, so semantically
    related texts (sharing keywords) land near each other under cosine — enough
    to exercise ranking without a real model. Records call counts so we can prove
    the cache avoids re-embedding.
    """

    #: Axes the toy embedding spans; presence of a keyword lights up its axis.
    AXES = ("slug", "url", "date", "parse", "email", "html", "text", "tax")

    def __init__(self) -> None:
        self.name = "fake:test-model"
        self.calls = 0
        self.embedded_texts: list[str] = []

    def _vector(self, text: str) -> list[float]:
        low = text.lower()
        return [1.0 if axis in low else 0.0 for axis in self.AXES]

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        self.embedded_texts.extend(texts)
        return [self._vector(t) for t in texts]


def test_fake_backend_satisfies_protocol() -> None:
    assert isinstance(FakeBackend(), EmbeddingBackend)


# -- text + fingerprint ----------------------------------------------------


def test_record_text_folds_name_sig_and_doc() -> None:
    r = _rec("parse_iso_date", doc="Parse an ISO 8601 date.", sig="(s: str) -> date")
    text = record_text(r)
    # Underscores become spaces so word boundaries survive embedding.
    assert "parse iso date" in text
    assert "Parse an ISO 8601 date." in text
    assert "(s: str) -> date" in text


def test_record_text_skips_empty_signature_and_doc() -> None:
    r = _rec("foo", sig="()")
    assert record_text(r) == "foo"


def test_fingerprint_is_stable_for_same_content() -> None:
    r1 = _rec("foo", doc="bar", sig="(x: int)")
    r2 = _rec("foo", doc="bar", sig="(x: int)")
    assert fingerprint(r1) == fingerprint(r2)


def test_fingerprint_changes_when_docstring_changes() -> None:
    before = fingerprint(_rec("foo", doc="old summary"))
    after = fingerprint(_rec("foo", doc="new summary"))
    assert before != after


def test_fingerprint_distinguishes_same_body_in_different_files() -> None:
    a = fingerprint(_rec("foo", doc="same", file="a.py"))
    b = fingerprint(_rec("foo", doc="same", file="b.py"))
    assert a != b


# -- cosine + scaling ------------------------------------------------------


def test_cosine_identical_vectors_is_one() -> None:
    assert cosine([1.0, 0.0, 1.0], [1.0, 0.0, 1.0]) == pytest.approx(1.0)


def test_cosine_orthogonal_is_zero() -> None:
    assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_handles_degenerate_inputs() -> None:
    assert cosine([], [1.0]) == 0.0
    assert cosine([0.0, 0.0], [0.0, 0.0]) == 0.0
    assert cosine([1.0, 2.0], [1.0]) == 0.0  # mismatched length


# -- ranking ---------------------------------------------------------------


def test_semantic_search_ranks_by_meaning() -> None:
    records = [
        _rec("strip_markup", doc="Convert HTML into plain text.", line=1),
        _rec("compute_tax", doc="Compute sales tax for an order.", line=2),
        _rec("slugify", doc="Make a URL-safe slug.", line=3),
    ]
    backend = FakeBackend()
    # Query shares keywords with the HTML/text function, nothing else.
    results = semantic_search("turn html into text", records, backend)
    assert results
    assert results[0].record.name == "strip_markup"


def test_semantic_search_empty_query_returns_nothing() -> None:
    backend = FakeBackend()
    assert semantic_search("   ", [_rec("foo")], backend) == []


def test_semantic_search_no_records_returns_nothing() -> None:
    backend = FakeBackend()
    assert semantic_search("anything", [], backend) == []


def test_semantic_search_respects_limit() -> None:
    records = [_rec("slugify", doc="url slug", line=i) for i in range(5)]
    backend = FakeBackend()
    results = semantic_search("url slug", records, backend, limit=2, min_score=0.0)
    assert len(results) == 2


def test_semantic_search_results_are_deterministic_on_ties() -> None:
    # Identical text → identical score; ties must break on (file, line).
    records = [
        _rec("a", doc="url slug", file="z.py", line=9),
        _rec("b", doc="url slug", file="a.py", line=1),
    ]
    backend = FakeBackend()
    results = semantic_search("url slug", records, backend, min_score=0.0)
    assert [r.record.file for r in results] == ["a.py", "z.py"]


# -- cache + incrementality ------------------------------------------------


def test_embed_records_writes_cache(tmp_path: Path) -> None:
    records = [_rec("slugify", doc="url slug")]
    backend = FakeBackend()
    embed_records(records, backend, root=tmp_path)
    assert embed_cache_path(tmp_path).is_file()
    data = json.loads(embed_cache_path(tmp_path).read_text())
    assert data["backend"] == "fake:test-model"
    assert data["count"] == 1


def test_embed_records_reuses_cache_without_reembedding(tmp_path: Path) -> None:
    records = [_rec("slugify", doc="url slug"), _rec("parse_date", doc="parse a date")]
    backend = FakeBackend()

    embed_records(records, backend, root=tmp_path)
    assert backend.calls == 1  # one batched call for both

    # Second run, same content → cache hit, backend NOT called again.
    embed_records(records, backend, root=tmp_path)
    assert backend.calls == 1


def test_embed_records_only_embeds_changed_function(tmp_path: Path) -> None:
    records = [_rec("slugify", doc="url slug"), _rec("parse_date", doc="parse a date")]
    backend = FakeBackend()
    embed_records(records, backend, root=tmp_path)
    assert backend.calls == 1
    backend.embedded_texts.clear()

    # Edit one docstring; only that record should be re-embedded.
    changed = [records[0], _rec("parse_date", doc="parse an ISO date instead")]
    embed_records(changed, backend, root=tmp_path)
    assert backend.calls == 2
    assert len(backend.embedded_texts) == 1
    assert "ISO" in backend.embedded_texts[0]


def test_embed_records_switching_backend_invalidates_cache(tmp_path: Path) -> None:
    records = [_rec("slugify", doc="url slug")]
    a = FakeBackend()
    embed_records(records, a, root=tmp_path)

    b = FakeBackend()
    b.name = "fake:other-model"
    embed_records(records, b, root=tmp_path)
    # Different vector space → must re-embed under the new backend.
    assert b.calls == 1


def test_embed_records_prunes_vanished_functions(tmp_path: Path) -> None:
    backend = FakeBackend()
    embed_records([_rec("a", doc="x", line=1), _rec("b", doc="y", line=2)], backend, root=tmp_path)
    # Re-run with only one record; cache should shrink to just that one.
    embed_records([_rec("a", doc="x", line=1)], backend, root=tmp_path)
    data = json.loads(embed_cache_path(tmp_path).read_text())
    assert data["count"] == 1


def test_embed_records_no_root_skips_cache(tmp_path: Path) -> None:
    records = [_rec("slugify", doc="url slug")]
    backend = FakeBackend()
    out = embed_records(records, backend, root=None)
    assert len(out) == 1
    # No cache file written anywhere when root is None.
    assert not (tmp_path / ".dejafunc").exists()


def test_semantic_search_uses_cache_across_calls(tmp_path: Path) -> None:
    records = [_rec("slugify", doc="url slug"), _rec("parse_date", doc="parse a date")]
    backend = FakeBackend()
    semantic_search("url slug", records, backend, root=tmp_path)
    record_calls = backend.calls
    # A second query reuses cached record vectors; only the query gets embedded.
    semantic_search("a date", records, backend, root=tmp_path)
    # +1 call for the new query embedding, record embeddings reused (no batch call).
    assert backend.calls == record_calls + 1


# -- backend loader --------------------------------------------------------


def test_load_backend_missing_returns_none_with_message(monkeypatch: pytest.MonkeyPatch) -> None:
    # No sentence-transformers installed and force the ST backend so we don't
    # accidentally reach a real Ollama daemon on the test host.
    monkeypatch.setenv("DEJA_EMBED_BACKEND", "sentence-transformers")
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)
    backend, message = load_backend()
    assert backend is None
    assert "sentence-transformers" in message
    assert "semantic search unavailable" in message


def test_load_backend_unknown_choice_is_reported() -> None:
    backend, message = load_backend(prefer="banana")
    assert backend is None
    assert "unknown embedding backend" in message


def test_load_backend_prefers_sentence_transformers(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the ST import succeeds, that backend is chosen (no Ollama probe)."""
    import types

    fake_mod = types.ModuleType("sentence_transformers")

    class _ST:
        def __init__(self, name: str) -> None:
            self._name = name

        def encode(self, texts, **_kw):
            return [[0.0, 1.0] for _ in texts]

    fake_mod.SentenceTransformer = _ST  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_mod)
    monkeypatch.setenv("DEJA_EMBED_MODEL", "tiny-test")

    backend, message = load_backend(prefer="sentence-transformers")
    assert backend is not None
    assert backend.name == "sentence-transformers:tiny-test"
    assert backend.embed(["hi"]) == [[0.0, 1.0]]
    assert "using" in message


# -- CLI integration -------------------------------------------------------


def _seed_repo(tmp_path: Path) -> Path:
    src = tmp_path / "lib.py"
    src.write_text(
        '''
def strip_markup(s):
    """Convert HTML into plain text."""
    return s


def compute_tax(amount):
    """Compute sales tax for an order."""
    return amount
''',
        encoding="utf-8",
    )
    return tmp_path


def test_cli_semantic_uses_backend_and_ranks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _seed_repo(tmp_path)
    # Build the index first.
    assert main(["index", str(repo)]) == 0
    capsys.readouterr()

    # Inject our fake backend so no real model loads.
    import deja.semantic as semantic

    fake = FakeBackend()
    monkeypatch.setattr(semantic, "load_backend", lambda *a, **k: (fake, "using fake:test-model"))

    code = main(["find", "turn html into text", str(repo), "--semantic"])
    out = capsys.readouterr()
    assert code == 0
    assert "strip_markup" in out.out
    assert "semantic search" in out.err  # backend message on stderr


def test_cli_semantic_falls_back_when_backend_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _seed_repo(tmp_path)
    assert main(["index", str(repo)]) == 0
    capsys.readouterr()

    import deja.semantic as semantic

    monkeypatch.setattr(
        semantic,
        "load_backend",
        lambda *a, **k: (None, "semantic search unavailable: backend missing"),
    )

    # "strip_markup" shares the literal word "markup" with the query, so fuzzy
    # search still finds it — proving we fell back rather than crashing.
    code = main(["find", "strip markup", str(repo), "--semantic"])
    out = capsys.readouterr()
    assert code == 0
    assert "falling back to fuzzy search" in out.err
    assert "strip_markup" in out.out


def test_cli_semantic_json_sets_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _seed_repo(tmp_path)
    assert main(["index", str(repo)]) == 0
    capsys.readouterr()

    import deja.semantic as semantic

    fake = FakeBackend()
    monkeypatch.setattr(semantic, "load_backend", lambda *a, **k: (fake, "using fake:test-model"))

    code = main(["find", "html text", str(repo), "--semantic", "--json"])
    out = capsys.readouterr()
    assert code == 0
    doc = json.loads(out.out)
    assert doc["semantic"] is True
    assert doc["query"] == "html text"
    # Semantic matches carry no per-signal breakdown.
    if doc["results"]:
        bd = doc["results"][0]["breakdown"]
        assert bd["name"] is None and bd["doc"] is None and bd["sig"] is None


def test_cli_non_semantic_path_does_not_import_backend(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The default `deja find` must not import the heavy embedding stack (issue #9)."""
    repo = _seed_repo(tmp_path)
    assert main(["index", str(repo)]) == 0
    capsys.readouterr()

    sys.modules.pop("sentence_transformers", None)
    assert main(["find", "compute_tax", str(repo)]) == 0
    # Fuzzy path never touches sentence-transformers.
    assert "sentence_transformers" not in sys.modules
