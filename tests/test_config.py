"""Tests for optional project config (`.dejafunc.toml`, issue #25 / PLAN.md §8 #11).

Covers discovery (ancestor walk), the `[index]` exclude/include glob layering in
the walker, `[search].weights` reordering `deja find`, CLI-flag > config > default
precedence (via `--no-config`), and the missing / partial / malformed paths that
must never crash or change today's behavior.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from deja.cli import main
from deja.config import Config, ConfigError, find_config_file, load_config
from deja.parsers import FunctionRecord
from deja.search import search
from deja.walker import iter_source_files

# -- discovery -------------------------------------------------------------


def test_missing_config_yields_inert_default(tmp_path: Path) -> None:
    cfg = load_config(tmp_path)
    assert cfg.is_default
    assert cfg.exclude == () and cfg.include == ()
    assert cfg.source is None


def test_find_config_walks_up_from_subdir(tmp_path: Path) -> None:
    (tmp_path / ".dejafunc.toml").write_text("[index]\nexclude = []\n")
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    found = find_config_file(deep)
    assert found == (tmp_path / ".dejafunc.toml").resolve()
    # And loading from the subdir picks up that same root config.
    assert load_config(deep).source == found


def test_nearest_config_wins(tmp_path: Path) -> None:
    (tmp_path / ".dejafunc.toml").write_text('[index]\nexclude = ["root/**"]\n')
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / ".dejafunc.toml").write_text('[index]\nexclude = ["sub/**"]\n')
    cfg = load_config(sub)
    assert cfg.exclude == ("sub/**",)


# -- [index] exclude / include in the walker -------------------------------


def _make_tree(root: Path) -> None:
    (root / "pkg").mkdir()
    (root / "pkg" / "a.py").write_text("def a(): pass\n")
    (root / "pkg" / "keep.py").write_text("def keep(): pass\n")
    (root / "build").mkdir()
    (root / "build" / "gen.py").write_text("def gen(): pass\n")


def test_config_exclude_drops_file(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    # Without config, pkg/a.py is indexed.
    assert "pkg/a.py" in set(iter_source_files(tmp_path))
    # A configured exclude drops it, layered on the existing rules.
    found = set(iter_source_files(tmp_path, exclude=("pkg/a.py",)))
    assert "pkg/a.py" not in found
    assert "pkg/keep.py" in found


def test_config_exclude_glob_prunes_directory(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    # 'build' isn't gitignored here, only in ALWAYS_SKIP_DIRS by name; use a
    # differently named dir to prove *config* globs prune subtrees.
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor" / "dep.py").write_text("def dep(): pass\n")
    found = set(iter_source_files(tmp_path, exclude=("vendor/**",)))
    assert not any(p.startswith("vendor/") for p in found)
    assert "pkg/a.py" in found


def test_config_include_readmits_excluded_file(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "tool.py").write_text("def tool(): pass\n")
    # Exclude everything under scripts, but re-include one file: include wins
    # over the *config* exclude.
    found = set(
        iter_source_files(
            tmp_path,
            exclude=("scripts/**",),
            include=("scripts/tool.py",),
        )
    )
    assert "scripts/tool.py" in found


def test_config_include_never_overrides_gitignore(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    (tmp_path / "secret.py").write_text("def secret(): pass\n")
    (tmp_path / ".gitignore").write_text("secret.py\n")
    # Even an explicit include glob can't resurrect a gitignored file: safety
    # rules stay authoritative.
    found = set(iter_source_files(tmp_path, include=("secret.py",)))
    assert "secret.py" not in found


def test_absent_config_globs_are_byte_for_byte_identical(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    base = set(iter_source_files(tmp_path))
    same = set(iter_source_files(tmp_path, exclude=(), include=()))
    assert base == same


# -- [search].weights reorder find -----------------------------------------


def _rec(name: str, *, doc: str = "", file: str = "m.py", line: int = 1):
    return FunctionRecord(
        name=name,
        file=file,
        line=line,
        signature="()",
        docstring=doc,
        lang="python",
        qualname=name,
    )


def test_weights_reorder_results(tmp_path: Path) -> None:
    # Two records: one matches on *name*, one matches on *docstring*.
    name_hit = _rec("parse_config", doc="Unrelated summary.", line=1)
    doc_hit = _rec("zzz", doc="parse the config file into a dict", line=2)
    records = [name_hit, doc_hit]

    # Default weights: the strong name match should lead.
    default = search("parse config", records)
    assert default[0].record.name == "parse_config"

    # Crank the docstring weight far above the name weight → the doc match wins.
    cfg = Config(weights=type(Config().weights)(name=0.1, docstring=3.0))
    tuned = search("parse config", records, weights=cfg.weights)
    assert tuned[0].record.name == "zzz"


def test_partial_weights_leave_others_default() -> None:
    records = [_rec("slugify", line=1), _rec("compute", line=2)]
    # Only override the signature weight; name matching is untouched, so the
    # obvious name hit still wins.
    weights = type(Config().weights)(signature=0.5)
    results = search("slugify", records, weights=weights)
    assert results[0].record.name == "slugify"


# -- precedence: CLI flag (--no-config) > config > default -----------------


def _write_pkg(root: Path) -> None:
    (root / "pkg").mkdir()
    (root / "pkg" / "a.py").write_text("def alpha(): pass\n")
    (root / "pkg" / "b.py").write_text("def beta(): pass\n")


def test_no_config_flag_overrides_config_exclude(tmp_path: Path, capsys) -> None:
    _write_pkg(tmp_path)
    (tmp_path / ".dejafunc.toml").write_text('[index]\nexclude = ["pkg/a.py"]\n')

    # With config honored, a.py is excluded → indexing it away means `find alpha`
    # returns nothing (exit 1).
    rc = main(["index", str(tmp_path)])
    assert rc == 0
    capsys.readouterr()
    rc_find = main(["find", "alpha", str(tmp_path)])
    assert rc_find == 1  # alpha was excluded by config

    # --no-config on the *index* build ignores the exclude → alpha is indexed.
    rc = main(["index", "--no-config", str(tmp_path)])
    assert rc == 0
    capsys.readouterr()
    rc_find = main(["find", "alpha", str(tmp_path), "--no-config"])
    assert rc_find == 0  # CLI flag beat the config


# -- robustness: missing / empty / partial / malformed ---------------------


def test_empty_config_is_default(tmp_path: Path) -> None:
    (tmp_path / ".dejafunc.toml").write_text("")
    assert load_config(tmp_path).is_default


def test_partial_config_only_sets_named_keys(tmp_path: Path) -> None:
    (tmp_path / ".dejafunc.toml").write_text('[index]\nexclude = ["x/**"]\n')
    cfg = load_config(tmp_path)
    assert cfg.exclude == ("x/**",)
    assert cfg.include == ()
    assert cfg.weights.name is None


def test_unknown_keys_are_ignored(tmp_path: Path) -> None:
    (tmp_path / ".dejafunc.toml").write_text(
        "[index]\nexclude = []\nfuture_key = 42\n\n[brand_new_table]\nk = 1\n"
    )
    # Forward-compat: unknown table/keys don't raise.
    cfg = load_config(tmp_path)
    assert cfg.is_default


def test_malformed_toml_raises_config_error(tmp_path: Path) -> None:
    (tmp_path / ".dejafunc.toml").write_text("[index\nexclude = ")
    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_wrong_typed_exclude_raises(tmp_path: Path) -> None:
    (tmp_path / ".dejafunc.toml").write_text('[index]\nexclude = "not-a-list"\n')
    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_wrong_typed_weight_raises(tmp_path: Path) -> None:
    (tmp_path / ".dejafunc.toml").write_text("[search]\nweights = { name = true }\n")
    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_negative_weight_raises(tmp_path: Path) -> None:
    (tmp_path / ".dejafunc.toml").write_text("[search]\nweights = { name = -1.0 }\n")
    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_malformed_config_does_not_crash_cli(tmp_path: Path, capsys) -> None:
    _write_pkg(tmp_path)
    (tmp_path / ".dejafunc.toml").write_text("this is = not valid = toml")
    # A broken config must not crash `deja index`; it prints a note and proceeds
    # with defaults (so alpha/beta still get indexed).
    rc = main(["index", str(tmp_path)])
    assert rc == 0
    err = capsys.readouterr().err
    assert "deja:" in err  # a clear, non-crashing note was surfaced
    rc_find = main(["find", "alpha", str(tmp_path)])
    assert rc_find == 0
