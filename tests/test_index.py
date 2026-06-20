"""Tests for index build/save/load and the `deja index` command (M2)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from deja.cli import main
from deja.index import INDEX_DIR, INDEX_FILE, build_index, index_path, load_index, save_index


def _sample_repo(root: Path) -> None:
    (root / "mod.py").write_text(
        '''
def slugify(text: str) -> str:
    """Slugify."""
    return text


class C:
    def m(self) -> int:
        return 1
''',
    )
    (root / "ignored").mkdir()
    (root / "ignored" / "skip.py").write_text("def skip(): pass\n")
    (root / ".gitignore").write_text("ignored/\n")


def test_build_index_collects_records(tmp_path: Path) -> None:
    _sample_repo(tmp_path)
    idx = build_index(tmp_path)
    names = {r.name for r in idx.records}
    assert {"slugify", "m"} <= names
    assert "skip" not in names  # gitignored


def test_index_is_sorted_by_file_then_line(tmp_path: Path) -> None:
    _sample_repo(tmp_path)
    idx = build_index(tmp_path)
    keys = [(r.file, r.line) for r in idx.records]
    assert keys == sorted(keys)


def test_save_and_load_roundtrip(tmp_path: Path) -> None:
    _sample_repo(tmp_path)
    idx = build_index(tmp_path)
    out = save_index(idx, tmp_path)

    assert out == index_path(tmp_path)
    assert out.is_file()

    # File is valid JSON with the expected envelope.
    data = json.loads(out.read_text())
    assert data["schema_version"] == 1
    assert data["count"] == len(idx)
    assert isinstance(data["functions"], list)

    loaded = load_index(tmp_path)
    assert len(loaded) == len(idx)
    assert {r.qualname for r in loaded.records} == {r.qualname for r in idx.records}


def test_load_without_index_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_index(tmp_path)


def test_index_path_layout(tmp_path: Path) -> None:
    p = index_path(tmp_path)
    assert p.parent.name == INDEX_DIR
    assert p.name == INDEX_FILE


def test_cli_index_writes_file_and_reports_count(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _sample_repo(tmp_path)
    exit_code = main(["index", str(tmp_path)])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "Indexed" in out
    assert "functions" in out
    assert (tmp_path / INDEX_DIR / INDEX_FILE).is_file()


def test_cli_index_rejects_missing_dir(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["index", str(tmp_path / "nope")])
    err = capsys.readouterr().err
    assert exit_code == 2
    assert "not a directory" in err
