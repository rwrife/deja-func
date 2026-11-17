"""Tests for monorepo mode: multi-root indexing + merged find (issue #42)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from deja.cli import main
from deja.index import index_path
from deja.monorepo import discover_roots, merged_search, package_label


def _make_monorepo(base: Path) -> None:
    """Create a 2-package monorepo, each with an independent ``slugify``."""
    (base / "pkg_a").mkdir()
    (base / "pkg_b").mkdir()
    (base / "pkg_a" / "pyproject.toml").write_text("[project]\nname='a'\n")
    (base / "pkg_b" / "package.json").write_text('{"name": "b"}\n')
    (base / "pkg_a" / "text.py").write_text(
        'def slugify(s):\n    "Turn a string into a URL-safe slug."\n    return s\n'
    )
    (base / "pkg_b" / "util.py").write_text(
        'def slugify(s):\n    "Make a slug from text."\n    return s\n'
    )


# -- discovery -------------------------------------------------------------


def test_discover_roots_finds_marked_packages(tmp_path: Path) -> None:
    _make_monorepo(tmp_path)
    roots = discover_roots(tmp_path)
    labels = sorted(package_label(tmp_path, r) for r in roots)
    assert labels == ["pkg_a", "pkg_b"]


def test_discover_roots_prunes_node_modules(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='root'\n")
    nm = tmp_path / "node_modules" / "dep"
    nm.mkdir(parents=True)
    (nm / "package.json").write_text('{"name": "dep"}\n')
    roots = discover_roots(tmp_path)
    labels = [package_label(tmp_path, r) for r in roots]
    assert labels == ["."]  # node_modules pruned, only the base root remains


def test_discover_roots_fallback_to_base_when_no_markers(tmp_path: Path) -> None:
    (tmp_path / "loose.py").write_text("def f():\n    pass\n")
    assert discover_roots(tmp_path) == [tmp_path.resolve()]


# -- merged search ---------------------------------------------------------


def test_merged_search_tags_each_hit_with_package(tmp_path: Path) -> None:
    _make_monorepo(tmp_path)
    merged = merged_search(tmp_path, "slugify")
    packages = sorted(m.package for m in merged.results)
    assert packages == ["pkg_a", "pkg_b"]
    # Both packages' indexes were built on the fly (first run).
    assert sorted(merged.built) == ["pkg_a", "pkg_b"]
    for m in merged.results:
        assert m.scored.record.name == "slugify"


def test_merged_search_keeps_cross_package_near_dupes(tmp_path: Path) -> None:
    # Same file *name* in two packages must stay visible (feeds `deja dupes`).
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "go.mod").write_text("module a\n")
    (tmp_path / "b" / "go.mod").write_text("module b\n")
    (tmp_path / "a" / "text.py").write_text('def norm(x):\n    "normalize"\n    return x\n')
    (tmp_path / "b" / "text.py").write_text('def norm(x):\n    "normalize"\n    return x\n')
    merged = merged_search(tmp_path, "norm")
    files = sorted((m.package, m.scored.record.file) for m in merged.results)
    assert files == [("a", "text.py"), ("b", "text.py")]


def test_merged_search_respects_limit(tmp_path: Path) -> None:
    _make_monorepo(tmp_path)
    merged = merged_search(tmp_path, "slugify", limit=1)
    assert len(merged.results) == 1


def test_merged_search_deduplicates_overlapping_roots(tmp_path: Path) -> None:
    _make_monorepo(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='workspace'\n")
    for roots in (None, [tmp_path, tmp_path / "pkg_a", tmp_path / "pkg_a"]):
        merged = merged_search(tmp_path, "slugify", roots=roots)
        paths = [(merged.roots[m.package] / m.scored.record.file).resolve() for m in merged.results]
        assert len(paths) == len(set(paths)) == 2
        assert next(m.package for m in merged.results if m.scored.record.file == "text.py") == (
            "pkg_a"
        )


def test_merged_search_external_roots_have_distinct_labels(tmp_path: Path) -> None:
    roots = [tmp_path / parent / "pkg" for parent in ("one", "two")]
    for root in roots:
        root.mkdir(parents=True)
        (root / "text.py").write_text("def slugify(s):\n    return s\n")
    base = tmp_path / "workspace"
    base.mkdir()
    merged = merged_search(base, "slugify", roots=roots)
    assert len(merged.roots) == len(merged.results) == 2
    assert len({m.package for m in merged.results}) == 2


# -- CLI -------------------------------------------------------------------


def test_cli_index_all_writes_per_package_indexes(tmp_path: Path, capsys) -> None:
    _make_monorepo(tmp_path)
    rc = main(["index", str(tmp_path), "--all"])
    assert rc == 0
    assert index_path(tmp_path / "pkg_a").is_file()
    assert index_path(tmp_path / "pkg_b").is_file()
    out = capsys.readouterr().out
    assert "2 package(s)" in out


def test_cli_index_explicit_roots(tmp_path: Path) -> None:
    _make_monorepo(tmp_path)
    rc = main(["index", str(tmp_path), "--root", str(tmp_path / "pkg_a")])
    assert rc == 0
    assert index_path(tmp_path / "pkg_a").is_file()
    assert not index_path(tmp_path / "pkg_b").is_file()


def test_cli_find_all_labels_results(tmp_path: Path, capsys) -> None:
    _make_monorepo(tmp_path)
    main(["index", str(tmp_path), "--all"])
    capsys.readouterr()  # drop the index-build chatter
    rc = main(["find", "slugify", str(tmp_path), "--all"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[pkg_a]" in out
    assert "[pkg_b]" in out


def test_cli_find_all_json_carries_package_labels(tmp_path: Path, capsys) -> None:
    _make_monorepo(tmp_path)
    main(["index", str(tmp_path), "--all"])
    capsys.readouterr()  # drop the index-build chatter
    rc = main(["find", "slugify", str(tmp_path), "--all", "--json"])
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["monorepo"] is True
    assert sorted(doc["packages"]) == ["pkg_a", "pkg_b"]
    pkgs = sorted(r["package"] for r in doc["results"])
    assert pkgs == ["pkg_a", "pkg_b"]


def test_cli_find_all_builds_missing_indexes(tmp_path: Path) -> None:
    # No `deja index` first — merged find should build each package index itself.
    _make_monorepo(tmp_path)
    rc = main(["find", "slugify", str(tmp_path), "--all"])
    assert rc == 0
    assert index_path(tmp_path / "pkg_a").is_file()
    assert index_path(tmp_path / "pkg_b").is_file()


def test_cli_find_all_no_match_exit_code(tmp_path: Path) -> None:
    _make_monorepo(tmp_path)
    main(["index", str(tmp_path), "--all"])
    rc = main(["find", "nonexistentfunc", str(tmp_path), "--all"])
    assert rc == 1


def test_cli_find_all_build_uses_package_config(tmp_path: Path, capsys) -> None:
    _make_monorepo(tmp_path)
    (tmp_path / "pkg_a" / ".dejafunc.toml").write_text('[index]\nexclude = ["text.py"]\n')
    assert main(["find", "slugify", str(tmp_path), "--all", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert [r["package"] for r in doc["results"]] == ["pkg_b"]
    assert main(["find", "slugify", str(tmp_path), "--all", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == doc


def test_cli_find_all_no_config_applies_to_package_build(tmp_path: Path, capsys) -> None:
    _make_monorepo(tmp_path)
    (tmp_path / "pkg_a" / ".dejafunc.toml").write_text('[index]\nexclude = ["text.py"]\n')
    assert main(["find", "slugify", str(tmp_path), "--all", "--json", "--no-config"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert sorted(r["package"] for r in doc["results"]) == ["pkg_a", "pkg_b"]


@pytest.mark.parametrize("flag", ["--age", "--semantic"])
@pytest.mark.parametrize("roots", [["--all"], ["--root", "."]])
def test_cli_find_rejects_unsupported_merged_options(tmp_path: Path, capsys, flag, roots) -> None:
    assert main(["find", "slugify", str(tmp_path), *roots, flag]) == 2
    assert "cannot be combined" in capsys.readouterr().err
    assert not index_path(tmp_path).exists()


def test_cli_index_rejects_watch_with_multiple_roots(tmp_path: Path, capsys) -> None:
    assert main(["index", str(tmp_path), "--all", "--watch"]) == 2
    assert "--watch cannot be combined" in capsys.readouterr().err
    assert not index_path(tmp_path).exists()
