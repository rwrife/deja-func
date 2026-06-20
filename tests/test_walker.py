"""Tests for the gitignore-aware walker (M2)."""

from __future__ import annotations

from pathlib import Path

from deja.walker import iter_source_files


def _make_tree(root: Path) -> None:
    (root / "pkg").mkdir()
    (root / "pkg" / "a.py").write_text("def a(): pass\n")
    (root / "pkg" / "b.py").write_text("def b(): pass\n")
    (root / "README.md").write_text("# not code\n")
    # Should be skipped by ALWAYS_SKIP_DIRS even without gitignore.
    (root / ".venv").mkdir()
    (root / ".venv" / "lib.py").write_text("def venv_fn(): pass\n")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "dep.py").write_text("def dep(): pass\n")


def test_walks_python_skips_nonparseable_and_noise_dirs(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    found = set(iter_source_files(tmp_path))
    assert found == {"pkg/a.py", "pkg/b.py"}
    # README isn't parseable; .venv/node_modules are pruned.
    assert "README.md" not in found
    assert not any(p.startswith(".venv") for p in found)
    assert not any(p.startswith("node_modules") for p in found)


def test_gitignore_excludes_files_and_dirs(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    (tmp_path / "generated").mkdir()
    (tmp_path / "generated" / "gen.py").write_text("def gen(): pass\n")
    (tmp_path / "pkg" / "secret.py").write_text("def secret(): pass\n")
    (tmp_path / ".gitignore").write_text("generated/\nsecret.py\n")

    found = set(iter_source_files(tmp_path))
    assert "generated/gen.py" not in found
    assert "pkg/secret.py" not in found
    assert {"pkg/a.py", "pkg/b.py"} <= found


def test_custom_has_parser_predicate(tmp_path: Path) -> None:
    (tmp_path / "x.py").write_text("def x(): pass\n")
    (tmp_path / "y.txt").write_text("hello\n")
    # Accept everything: now the .txt shows up too.
    found = set(iter_source_files(tmp_path, has_parser=lambda _rel: True))
    assert {"x.py", "y.txt"} <= found
