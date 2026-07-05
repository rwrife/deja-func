"""Tests for `deja export` / `deja import`: the portable index (PLAN.md §8 #12).

The portable artifact lets an agent on a **fresh clone** hydrate the inventory in
one command instead of paying for a cold ``deja index``. These tests cover the
pure ``export.py`` layer (shape, schema validation, round-trip losslessness) and
the CLI surface (file + stdout export, import hydrating ``deja find`` with no
prior index run, and the clean error on a schema mismatch).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from deja.cli import main
from deja.export import (
    DEFAULT_EXPORT_FILE,
    ExportError,
    export_index,
    export_to_dict,
    load_export,
    read_export,
)
from deja.index import SCHEMA_VERSION, Index, build_index, index_path, load_index
from deja.parsers import FunctionRecord


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


def _sample_index() -> Index:
    return Index(
        records=[
            _rec("slugify", file="text.py", line=42, sig="(value: str) -> str", doc="Slug it."),
            _rec("parse_iso", file="dates.py", line=7, sig="(s: str)", doc="Parse ISO."),
        ]
    )


def _write_fixture_tree(root: Path) -> None:
    """A tiny mixed tree so a native index has something real to compare against."""
    (root / "text.py").write_text(
        'def slugify(value: str) -> str:\n    """Turn a string into a slug."""\n    return value\n',
        encoding="utf-8",
    )
    (root / "util.js").write_text(
        "function validateEmail(addr) {\n  return addr.includes('@');\n}\n",
        encoding="utf-8",
    )


# -- export shape -----------------------------------------------------------


def test_export_to_dict_is_index_shape_plus_metadata() -> None:
    doc = export_to_dict(_sample_index(), root_name="proj")
    # Everything the on-disk index carries is still present, untouched.
    assert doc["schema_version"] == SCHEMA_VERSION
    assert doc["count"] == 2
    assert len(doc["functions"]) == 2
    assert doc["functions"][0]["name"] == "slugify"
    # Plus the two export-only keys.
    assert doc["root_name"] == "proj"
    assert "exported_at" in doc
    # exported_at is an ISO-8601 UTC stamp (parseable, ends in +00:00).
    assert doc["exported_at"].endswith("+00:00")


def test_export_omits_root_name_when_absent() -> None:
    doc = export_to_dict(_sample_index())
    assert "root_name" not in doc


def test_export_index_text_is_pretty_json_with_trailing_newline() -> None:
    text = export_index(_sample_index(), root_name="proj")
    assert text.endswith("\n")
    # Valid JSON, indented like save_index (diff-friendly).
    parsed = json.loads(text)
    assert parsed["count"] == 2
    assert "\n  " in text  # indent=2


# -- import / load ----------------------------------------------------------


def test_load_export_round_trips_records() -> None:
    original = _sample_index()
    doc = export_to_dict(original)
    loaded = load_export(doc)
    assert [r.to_dict() for r in loaded.records] == [r.to_dict() for r in original.records]


def test_load_export_ignores_export_only_keys() -> None:
    doc = export_to_dict(_sample_index(), root_name="proj")
    loaded = load_export(doc)
    # exported_at / root_name are provenance, not index content.
    assert loaded.schema_version == SCHEMA_VERSION
    assert len(loaded.records) == 2


def test_load_export_missing_version_defaults_to_current() -> None:
    doc = export_to_dict(_sample_index())
    del doc["schema_version"]
    loaded = load_export(doc)  # lenient, like Index.from_dict
    assert loaded.schema_version == SCHEMA_VERSION


def test_load_export_rejects_newer_schema() -> None:
    doc = export_to_dict(_sample_index())
    doc["schema_version"] = SCHEMA_VERSION + 1
    with pytest.raises(ExportError) as exc:
        load_export(doc)
    assert "schema_version" in str(exc.value)


def test_load_export_rejects_non_integer_schema() -> None:
    doc = export_to_dict(_sample_index())
    doc["schema_version"] = "banana"
    with pytest.raises(ExportError):
        load_export(doc)


def test_load_export_rejects_non_object() -> None:
    with pytest.raises(ExportError):
        load_export([1, 2, 3])  # type: ignore[arg-type]


def test_read_export_missing_file_raises_filenotfound(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_export(tmp_path / "nope.json")


def test_read_export_bad_json_raises_export_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ExportError):
        read_export(bad)


# -- round-trip vs. a native index -----------------------------------------


def test_export_then_import_matches_native_index(tmp_path: Path) -> None:
    """export → import writes records byte-identical to a fresh `deja index`."""
    src = tmp_path / "src"
    src.mkdir()
    _write_fixture_tree(src)

    native = build_index(src)
    exported = export_index(native, root_name="src")

    # Simulate a fresh clone: a new dir with the same sources but no index.
    clone = tmp_path / "clone"
    clone.mkdir()
    _write_fixture_tree(clone)

    export_file = tmp_path / DEFAULT_EXPORT_FILE
    export_file.write_text(exported, encoding="utf-8")

    imported = read_export(export_file)
    assert [r.to_dict() for r in imported.records] == [r.to_dict() for r in native.records]


# -- CLI: export ------------------------------------------------------------


def test_cli_export_writes_default_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_fixture_tree(tmp_path)
    code = main(["export", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0
    written = tmp_path / DEFAULT_EXPORT_FILE
    assert written.is_file()
    doc = json.loads(written.read_text(encoding="utf-8"))
    assert doc["count"] >= 1
    assert "exported_at" in doc
    assert "Exported" in out


def test_cli_export_custom_output(tmp_path: Path) -> None:
    _write_fixture_tree(tmp_path)
    dest = tmp_path / "share" / "inv.json"
    code = main(["export", str(tmp_path), "-o", str(dest)])
    assert code == 0
    assert dest.is_file()  # nested parent dir created
    assert json.loads(dest.read_text(encoding="utf-8"))["count"] >= 1


def test_cli_export_stdout_is_pipe_clean(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_fixture_tree(tmp_path)
    code = main(["export", str(tmp_path), "-o", "-"])
    captured = capsys.readouterr()
    assert code == 0
    # stdout is ONLY the JSON document (parseable, no personality/log noise).
    doc = json.loads(captured.out)
    assert doc["schema_version"] == SCHEMA_VERSION
    assert doc["count"] >= 1
    # The human note went to stderr instead.
    assert "Exported" in captured.err
    assert "Exported" not in captured.out


# -- CLI: import ------------------------------------------------------------


def test_cli_import_hydrates_find_without_indexing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The headline behavior: import → `deja find` works with no `deja index` run."""
    # Build + export from a "source" repo.
    src = tmp_path / "src"
    src.mkdir()
    _write_fixture_tree(src)
    export_file = tmp_path / "deja-index.json"
    assert main(["export", str(src), "-o", str(export_file)]) == 0
    capsys.readouterr()  # drain

    # Fresh clone: same sources, but crucially NO index on disk.
    clone = tmp_path / "clone"
    clone.mkdir()
    _write_fixture_tree(clone)
    assert not index_path(clone).exists()

    # Import hydrates .dejafunc/index.json.
    code = main(["import", str(export_file), str(clone)])
    imp_out = capsys.readouterr().out
    assert code == 0
    assert index_path(clone).is_file()
    assert "Imported" in imp_out

    # Now find works. Prove it didn't rebuild by asserting no "No index yet" note
    # appears on stderr (find only prints that when it auto-builds).
    find_code = main(["find", "slugify", str(clone)])
    find_captured = capsys.readouterr()
    assert find_code == 0
    assert "slugify" in find_captured.out
    assert "No index yet" not in find_captured.err


def test_cli_import_schema_mismatch_is_clean_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    export_file = tmp_path / "future.json"
    doc = export_to_dict(_sample_index(), root_name="x")
    doc["schema_version"] = SCHEMA_VERSION + 1
    export_file.write_text(json.dumps(doc), encoding="utf-8")

    code = main(["import", str(export_file), str(tmp_path)])
    err = capsys.readouterr().err
    # Non-crashing, documented exit code (2), clear message, no index written.
    assert code == 2
    assert "schema_version" in err
    assert not index_path(tmp_path).exists()


def test_cli_import_missing_file_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["import", str(tmp_path / "ghost.json"), str(tmp_path)])
    err = capsys.readouterr().err
    assert code == 2
    assert "No such export file" in err


def test_cli_export_then_import_full_cli_round_trip(tmp_path: Path) -> None:
    """End-to-end via the CLI: exported index reloads to the same records."""
    src = tmp_path / "src"
    src.mkdir()
    _write_fixture_tree(src)
    export_file = tmp_path / "inv.json"
    assert main(["export", str(src), "-o", str(export_file)]) == 0

    clone = tmp_path / "clone"
    clone.mkdir()
    assert main(["import", str(export_file), str(clone)]) == 0

    native = build_index(src)
    loaded = load_index(clone)
    assert [r.to_dict() for r in loaded.records] == [r.to_dict() for r in native.records]
