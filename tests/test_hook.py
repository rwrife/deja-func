"""Tests for `deja hook`: install + redundancy check (hook.py), render, serialize, CLI.

The pre-commit/pre-push redundancy warning (PLAN.md §8 #3): warn when a newly
*staged* function strongly resembles one already in the index. Tests drive a
real throwaway git repo (so staged-blob reading is exercised for real) plus pure
unit checks of the scoring/render/serialize layers.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from deja.cli import main
from deja.dupes import DEFAULT_THRESHOLD
from deja.hook import (
    Match,
    check_staged,
    git_repo_root,
    install_hook,
    staged_function_records,
    staged_source_paths,
)
from deja.parsers import FunctionRecord
from deja.render import format_matches
from deja.serialize import matches_to_dict


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


# Reusable fixture source snippets (kept short so lines stay under the limit).
PARSE_ISO = 'def parse_iso_date(s: str):\n    """Parse an ISO 8601 date string."""\n    ...\n'
PARSE_ISO_DUP = (
    "def parse_date_iso(text: str):\n"
    '    """Parse an ISO 8601 date string into a date."""\n'
    "    ...\n"
)
SLUGIFY = 'def slugify(text: str) -> str:\n    """Make a URL-safe slug."""\n    return text\n'
SLUGIFY_EDIT = (
    "def slugify(text: str) -> str:\n"
    '    """Make a URL-safe slug, improved."""\n'
    "    return text.lower()\n"
)
TAX = (
    "def compute_tax(amount: float) -> float:\n"
    '    """Compute sales tax for an order."""\n'
    "    return amount * 0.1\n"
)
SEND_EMAIL = 'def send_email(to: str) -> bool:\n    """Send an email message."""\n    ...\n'
EMAIL_SEND = (
    'def email_send(addr: str) -> bool:\n    """Send an email to a recipient."""\n    ...\n'
)


def _git(repo: Path, *args: str) -> None:
    """Run a git command in *repo*, raising on failure (test plumbing)."""
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A minimal initialized git repo with deterministic identity."""
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "Tester")
    return tmp_path


# -- installation ----------------------------------------------------------


def test_install_writes_executable_pre_commit_hook(repo: Path) -> None:
    target = install_hook(repo)
    assert target == repo / ".git" / "hooks" / "pre-commit"
    assert target.is_file()
    body = target.read_text()
    assert "deja-func redundancy hook" in body
    assert "hook check" in body
    # Executable bit set so git will actually run it.
    assert target.stat().st_mode & 0o111


def test_install_pre_push_variant(repo: Path) -> None:
    target = install_hook(repo, hook="pre-push")
    assert target.name == "pre-push"
    assert target.is_file()


def test_install_strict_bakes_in_flag(repo: Path) -> None:
    target = install_hook(repo, strict=True)
    assert "--strict" in target.read_text()


def test_install_non_strict_has_no_flag(repo: Path) -> None:
    target = install_hook(repo)
    assert "--strict" not in target.read_text()


def test_install_refuses_foreign_hook_without_force(repo: Path) -> None:
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\necho not ours\n")
    with pytest.raises(FileExistsError):
        install_hook(repo)


def test_install_force_overwrites_foreign_hook(repo: Path) -> None:
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\necho not ours\n")
    install_hook(repo, force=True)
    assert "deja-func redundancy hook" in hook.read_text()


def test_install_overwrites_own_hook_idempotently(repo: Path) -> None:
    first = install_hook(repo)
    # Re-installing our own hook needs no --force and stays a single hook (the
    # marker appears exactly twice: the opening >>> and closing <<< fences).
    second = install_hook(repo)
    assert first == second
    assert second.read_text().count("deja-func redundancy hook") == 2


def test_install_unknown_hook_raises() -> None:
    with pytest.raises(ValueError):
        install_hook(".", hook="post-merge")


def test_git_repo_root_outside_repo_raises(tmp_path: Path) -> None:
    # tmp_path here is a bare temp dir with no `git init`.
    with pytest.raises(FileNotFoundError):
        git_repo_root(tmp_path)


# -- staged-change inspection ---------------------------------------------


def _write_and_stage(repo: Path, name: str, body: str) -> None:
    (repo / name).write_text(body)
    _git(repo, "add", name)


def test_staged_source_paths_lists_only_parseable(repo: Path) -> None:
    _write_and_stage(repo, "a.py", "def f():\n    pass\n")
    _write_and_stage(repo, "notes.txt", "hello\n")
    paths = staged_source_paths(repo)
    assert "a.py" in paths
    assert "notes.txt" not in paths


def test_staged_function_records_reads_index_blob(repo: Path) -> None:
    _write_and_stage(
        repo,
        "a.py",
        'def slugify(text: str) -> str:\n    """Make a slug."""\n    return text\n',
    )
    records = staged_function_records(repo)
    names = {r.name for r in records}
    assert "slugify" in names


# -- check_staged ----------------------------------------------------------


def _index_with(repo: Path, *files: tuple[str, str]) -> None:
    """Write *files* into *repo*, commit them, and build the deja index.

    Builds the index via the library API (not ``main(["index"])``) so no
    progress line is printed into a test's captured stdout — important for the
    ``--json`` tests that parse the whole buffer.
    """
    from deja.index import build_index, save_index

    for name, body in files:
        (repo / name).write_text(body)
        _git(repo, "add", name)
    _git(repo, "commit", "-m", "seed")
    save_index(build_index(repo), repo)


def test_check_staged_flags_near_duplicate(repo: Path) -> None:
    _index_with(repo, ("util.py", PARSE_ISO))
    _write_and_stage(repo, "dates.py", PARSE_ISO_DUP)
    matches = check_staged(repo)
    assert len(matches) == 1
    m = matches[0]
    assert m.staged.name == "parse_date_iso"
    assert m.existing.name == "parse_iso_date"
    assert m.existing.file == "util.py"
    assert m.score >= DEFAULT_THRESHOLD


def test_check_staged_ignores_same_file_self_match(repo: Path) -> None:
    # The staged file edits a function that already exists *in that same file*;
    # it must not be reported as a duplicate of itself.
    _index_with(repo, ("util.py", SLUGIFY))
    _write_and_stage(repo, "util.py", SLUGIFY_EDIT)
    assert check_staged(repo) == []


def test_check_staged_unrelated_function_not_flagged(repo: Path) -> None:
    _index_with(repo, ("util.py", SLUGIFY))
    _write_and_stage(repo, "tax.py", TAX)
    assert check_staged(repo) == []


def test_check_staged_no_index_returns_empty(repo: Path) -> None:
    # Committed + staged code but no `deja index` ever run.
    _write_and_stage(repo, "a.py", 'def slugify(s):\n    """slug"""\n    return s\n')
    assert check_staged(repo) == []


def test_check_staged_threshold_controls_sensitivity(repo: Path) -> None:
    _index_with(repo, ("util.py", SEND_EMAIL))
    _write_and_stage(repo, "mailer.py", EMAIL_SEND)
    # A very high threshold suppresses the (real but imperfect) match.
    assert check_staged(repo, threshold=99.9) == []
    # A permissive threshold surfaces it.
    assert len(check_staged(repo, threshold=50.0)) == 1


# -- rendering -------------------------------------------------------------


def test_format_matches_no_color_shows_both_locations() -> None:
    matches = [
        Match(
            staged=_rec("parse_date_iso", file="dates.py", line=1, sig="(t: str)"),
            existing=_rec(
                "parse_iso_date",
                file="util.py",
                line=9,
                sig="(s: str)",
                doc="Parse a date.",
            ),
            score=92.0,
        )
    ]
    out = format_matches(matches, color=False)
    assert "parse_date_iso" in out
    assert "dates.py:1" in out
    assert "util.py:9" in out
    assert "92%" in out
    assert "\033[" not in out  # no ANSI when color is off


def test_format_matches_empty_is_friendly() -> None:
    out = format_matches([], color=False)
    assert out
    assert "carry on" in out.lower()


def test_format_matches_warn_only_mentions_proceeds() -> None:
    matches = [Match(staged=_rec("a"), existing=_rec("b", file="x.py"), score=88.0)]
    out = format_matches(matches, strict=False, color=False)
    assert "warning only" in out.lower()


def test_format_matches_strict_says_blocking() -> None:
    matches = [Match(staged=_rec("a"), existing=_rec("b", file="x.py"), score=88.0)]
    out = format_matches(matches, strict=True, color=False)
    assert "blocking" in out.lower()
    assert "warning only" not in out.lower()


# -- serialization ---------------------------------------------------------


def test_matches_to_dict_stable_shape() -> None:
    matches = [
        Match(
            staged=_rec("parse_date_iso", file="dates.py", line=1, sig="(t: str)"),
            existing=_rec("parse_iso_date", file="util.py", line=9, sig="(s: str)"),
            score=92.0,
        )
    ]
    doc = matches_to_dict(matches, threshold=DEFAULT_THRESHOLD, strict=True)
    assert doc["schema_version"] == 1
    assert doc["threshold"] == DEFAULT_THRESHOLD
    assert doc["strict"] is True
    assert doc["count"] == 1
    entry = doc["matches"][0]
    assert entry["score"] == 92.0
    # Bare record shape on both sides (no search-only score/breakdown).
    expected_keys = {"name", "qualname", "file", "line", "signature", "docstring", "lang"}
    assert set(entry["staged"]) == expected_keys
    assert set(entry["existing"]) == expected_keys


def test_matches_to_dict_empty() -> None:
    doc = matches_to_dict([], threshold=80.0)
    assert doc["count"] == 0
    assert doc["matches"] == []
    assert doc["strict"] is False


# -- CLI -------------------------------------------------------------------


def test_cli_hook_install_creates_hook(repo: Path, capsys: pytest.CaptureFixture) -> None:
    rc = main(["hook", "install", str(repo)])
    out = capsys.readouterr().out
    assert rc == 0
    assert (repo / ".git" / "hooks" / "pre-commit").is_file()
    assert "redundancy hook" in out


def test_cli_hook_install_pre_push(repo: Path, capsys: pytest.CaptureFixture) -> None:
    rc = main(["hook", "install", str(repo), "--pre-push"])
    capsys.readouterr()
    assert rc == 0
    assert (repo / ".git" / "hooks" / "pre-push").is_file()


def test_cli_hook_install_outside_repo_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    rc = main(["hook", "install", str(tmp_path)])
    err = capsys.readouterr().err
    assert rc == 2
    assert "not inside a git repository" in err


def test_cli_hook_install_foreign_hook_exits_2(repo: Path, capsys: pytest.CaptureFixture) -> None:
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\necho mine\n")
    rc = main(["hook", "install", str(repo)])
    err = capsys.readouterr().err
    assert rc == 2
    assert "already exists" in err


def test_cli_hook_check_warns_but_exits_zero(repo: Path, capsys: pytest.CaptureFixture) -> None:
    _index_with(repo, ("util.py", PARSE_ISO))
    _write_and_stage(repo, "dates.py", PARSE_ISO_DUP)
    rc = main(["hook", "check", str(repo)])
    out = capsys.readouterr().out
    assert rc == 0  # warn-only never blocks
    assert "parse_date_iso" in out
    assert "util.py:" in out


def test_cli_hook_check_strict_exits_one_on_match(
    repo: Path, capsys: pytest.CaptureFixture
) -> None:
    _index_with(repo, ("util.py", PARSE_ISO))
    _write_and_stage(repo, "dates.py", PARSE_ISO_DUP)
    rc = main(["hook", "check", str(repo), "--strict"])
    capsys.readouterr()
    assert rc == 1  # strict turns the nudge into a gate


def test_cli_hook_check_clean_exits_zero(repo: Path, capsys: pytest.CaptureFixture) -> None:
    _index_with(repo, ("util.py", SLUGIFY))
    _write_and_stage(repo, "tax.py", TAX)
    rc = main(["hook", "check", str(repo)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "carry on" in out.lower()


def test_cli_hook_check_no_index_nudges_exits_zero(
    repo: Path, capsys: pytest.CaptureFixture
) -> None:
    _write_and_stage(repo, "a.py", 'def f(s):\n    """x"""\n    return s\n')
    rc = main(["hook", "check", str(repo)])
    err = capsys.readouterr().err
    assert rc == 0
    assert "no index yet" in err.lower()


def test_cli_hook_check_json_output(repo: Path, capsys: pytest.CaptureFixture) -> None:
    _index_with(repo, ("util.py", PARSE_ISO))
    _write_and_stage(repo, "dates.py", PARSE_ISO_DUP)
    rc = main(["hook", "check", str(repo), "--json"])
    out = capsys.readouterr().out
    assert rc == 0  # json defaults to warn-only exit semantics
    doc = json.loads(out)
    assert doc["schema_version"] == 1
    assert doc["count"] == 1
    assert doc["matches"][0]["staged"]["name"] == "parse_date_iso"


def test_cli_hook_check_json_strict_exits_one(repo: Path, capsys: pytest.CaptureFixture) -> None:
    _index_with(repo, ("util.py", PARSE_ISO))
    _write_and_stage(repo, "dates.py", PARSE_ISO_DUP)
    rc = main(["hook", "check", str(repo), "--strict", "--json"])
    out = capsys.readouterr().out
    assert rc == 1
    assert json.loads(out)["strict"] is True


def test_cli_hook_bad_threshold_exits_2(repo: Path, capsys: pytest.CaptureFixture) -> None:
    rc = main(["hook", "check", str(repo), "--threshold", "150"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "threshold" in err.lower()


def test_cli_hook_bare_prints_help(capsys: pytest.CaptureFixture) -> None:
    rc = main(["hook"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "install" in out
    assert "check" in out


def test_cli_hook_end_to_end_real_commit(repo: Path) -> None:
    """The installed pre-commit hook fires on a real `git commit` and warns.

    Warn-only mode must let the commit through (exit 0) while still printing the
    redundancy notice, exercising the full stub → `deja hook check` path.
    """
    _index_with(repo, ("util.py", PARSE_ISO))
    install_hook(repo)
    (repo / "dates.py").write_text(PARSE_ISO_DUP)
    _git(repo, "add", "dates.py")
    result = subprocess.run(
        ["git", "commit", "-m", "add dates"],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    # Commit succeeds (warn-only) and the hook's warning reached the user.
    assert result.returncode == 0
    combined = result.stdout + result.stderr
    assert "parse_date_iso" in combined
