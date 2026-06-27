"""Git pre-commit / pre-push redundancy hook (PLAN.md §8 #3).

`deja find` and `deja dupes` are pull-based: you have to remember to run them.
This module makes the "wait, haven't I written this before?" nudge *push-based*
— it warns **while you commit**, the moment a brand-new function strongly
resembles one already in the index.

Two pieces:

* :func:`install_hook` drops a tiny shell stub into ``.git/hooks/pre-commit``
  (or ``pre-push``) that just calls ``deja hook check``. The real logic lives
  here in Python so the installed hook stays trivial and never goes stale.
* :func:`check_staged` reads the **staged** version of each added/modified
  source file (via ``git show :<path>``), extracts its functions, and scores
  each one against the existing index using the same blended similarity
  :mod:`deja.dupes` uses (:func:`deja.dupes.pair_score`). A staged function is
  only ever compared against functions *outside its own file*, so editing a
  function never flags it as a duplicate of itself.

By design the hook **warns but does not block** (PLAN.md §9: "a *hook* that
warns is in backlog; hard gating is not the product"). ``--strict`` is offered
for teams who want to turn the nudge into a gate, but the default exit code is
always ``0``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .dupes import DEFAULT_THRESHOLD, pair_score
from .index import Index, load_index
from .parsers import FunctionRecord, get_parser_for_path

#: Git hooks we know how to install. ``pre-commit`` is the default (earliest,
#: most useful feedback point); ``pre-push`` is offered for slower checks.
SUPPORTED_HOOKS = ("pre-commit", "pre-push")

#: Marker line so we can recognise (and safely overwrite) a hook we wrote, and
#: refuse to clobber an unrelated existing hook unless ``--force`` is given.
_HOOK_MARKER = "# >>> deja-func redundancy hook >>>"

#: Body of the installed hook stub. Kept deliberately tiny: it just shells out
#: to ``deja hook check`` so all real logic lives in (upgradable) Python, not in
#: a frozen copy inside ``.git``. We prefer the ``deja`` console script if it's
#: on ``PATH``, and fall back to the *same* Python interpreter that installed
#: the hook (``{python} -m deja.cli``) so it keeps working in venvs / setups
#: where the entry point isn't on the hook's ``PATH``.
_HOOK_TEMPLATE = """\
#!/usr/bin/env sh
{marker}
# Installed by `deja hook install`. Warns when a newly added function strongly
# resembles one already indexed. Non-blocking by default; remove this file (or
# run `git commit --no-verify`) to skip. Re-run `deja hook install` to update.
if command -v deja >/dev/null 2>&1; then
    exec deja hook check{strict_flag}
else
    exec "{python}" -m deja.cli hook check{strict_flag}
fi
# <<< deja-func redundancy hook <<<
"""


@dataclass(frozen=True, slots=True)
class Match:
    """One staged function that strongly resembles an existing indexed function.

    Attributes:
        staged: The function as written in the staged change.
        existing: The pre-existing indexed function it resembles most.
        score: Blended similarity (0-100) between the two.
    """

    staged: FunctionRecord
    existing: FunctionRecord
    score: float


# -- installation ----------------------------------------------------------


def git_repo_root(start: str | os.PathLike[str] = ".") -> Path:
    """Return the working-tree root of the git repo containing *start*.

    Raises:
        FileNotFoundError: If *start* is not inside a git working tree (or git
            is unavailable).
    """
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(start),
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:  # pragma: no cover - env-dependent
        raise FileNotFoundError("not inside a git working tree") from exc
    root = out.stdout.strip()
    if not root:  # pragma: no cover - defensive
        raise FileNotFoundError("not inside a git working tree")
    return Path(root)


def hooks_dir(repo_root: str | os.PathLike[str]) -> Path:
    """Return the hooks directory for *repo_root*, honoring ``core.hooksPath``.

    Most repos use ``.git/hooks``; this respects a custom ``core.hooksPath`` so
    we install where git will actually look.
    """
    root = Path(repo_root)
    try:
        out = subprocess.run(
            ["git", "config", "--get", "core.hooksPath"],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=False,
        )
        configured = out.stdout.strip()
    except OSError:  # pragma: no cover - env-dependent
        configured = ""
    if configured:
        path = Path(configured)
        return path if path.is_absolute() else (root / path)
    return root / ".git" / "hooks"


def install_hook(
    start: str | os.PathLike[str] = ".",
    *,
    hook: str = "pre-commit",
    strict: bool = False,
    force: bool = False,
) -> Path:
    """Install the redundancy hook into the repo containing *start*.

    Args:
        start: Any path inside the target git working tree.
        hook: Which hook to install (``pre-commit`` or ``pre-push``).
        strict: Bake ``--strict`` into the stub so the hook *fails* the commit on
            a strong match instead of merely warning.
        force: Overwrite an existing hook even if we didn't write it.

    Returns:
        The path to the installed hook file.

    Raises:
        ValueError: If *hook* isn't one of :data:`SUPPORTED_HOOKS`.
        FileExistsError: If a foreign hook already exists and *force* is False.
        FileNotFoundError: If *start* isn't inside a git working tree.
    """
    if hook not in SUPPORTED_HOOKS:
        raise ValueError(f"unsupported hook {hook!r}; choose one of {', '.join(SUPPORTED_HOOKS)}")

    root = git_repo_root(start)
    hooks = hooks_dir(root)
    hooks.mkdir(parents=True, exist_ok=True)
    target = hooks / hook

    if target.exists() and not force:
        existing = target.read_text(encoding="utf-8", errors="replace")
        if _HOOK_MARKER not in existing:
            raise FileExistsError(
                f"{target} already exists and wasn't written by deja (use --force to overwrite)"
            )

    strict_flag = " --strict" if strict else ""
    target.write_text(
        _HOOK_TEMPLATE.format(
            marker=_HOOK_MARKER,
            strict_flag=strict_flag,
            python=sys.executable or "python3",
        ),
        encoding="utf-8",
    )
    # rwxr-xr-x so git will execute it.
    target.chmod(0o755)
    return target


# -- staged-change inspection ---------------------------------------------


def _git_lines(args: list[str], cwd: Path) -> list[str]:
    """Run a git command and return non-empty stdout lines (``[]`` on failure)."""
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):  # pragma: no cover - env-dependent
        return []
    return [line for line in out.stdout.splitlines() if line.strip()]


def staged_source_paths(repo_root: Path) -> list[str]:
    """Return repo-relative paths of staged, still-present source files.

    Only added/copied/modified/renamed entries are considered (deletions can't
    introduce a new duplicate), and only files we have a parser for.
    """
    # --diff-filter excludes deletions (D); -z would need byte handling, so we
    # use newline output and accept that exotic paths are rare in practice.
    paths = _git_lines(
        ["diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        repo_root,
    )
    out: list[str] = []
    for p in paths:
        if get_parser_for_path(p) is not None:
            out.append(p)
    return out


def _staged_blob(repo_root: Path, rel_path: str) -> str | None:
    """Return the staged (index) contents of *rel_path*, or ``None`` if absent."""
    try:
        proc = subprocess.run(
            ["git", "show", f":{rel_path}"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):  # pragma: no cover - env-dependent
        return None
    return proc.stdout


def staged_function_records(repo_root: Path) -> list[FunctionRecord]:
    """Extract :class:`FunctionRecord`s from the *staged* version of changed files.

    Reads each staged file from the index (not the working tree), so it reflects
    exactly what is about to be committed.
    """
    records: list[FunctionRecord] = []
    for rel_path in staged_source_paths(repo_root):
        parser = get_parser_for_path(rel_path)
        if parser is None:  # pragma: no cover - filtered above
            continue
        source = _staged_blob(repo_root, rel_path)
        if not source:
            continue
        records.extend(parser.parse(source, rel_path))
    return records


def check_staged(
    start: str | os.PathLike[str] = ".",
    *,
    threshold: float = DEFAULT_THRESHOLD,
    index: Index | None = None,
) -> list[Match]:
    """Find staged functions that strongly resemble already-indexed functions.

    For every function in the staged changes, the best-scoring existing function
    from a *different file* is found; pairs at/above *threshold* are returned as
    :class:`Match` objects, strongest first.

    Args:
        start: Any path inside the target git working tree.
        threshold: Minimum blended similarity (0-100) to warn on.
        index: Pre-loaded index to compare against; if ``None``, the repo's
            ``.dejafunc/index.json`` is loaded (missing index → no matches).

    Returns:
        Matches sorted by score descending, then by staged ``(file, line)`` for
        deterministic output. Empty when nothing resembles existing code (or no
        index exists yet).
    """
    repo_root = git_repo_root(start)

    if index is None:
        try:
            index = load_index(repo_root)
        except FileNotFoundError:
            # No index to compare against yet: nothing to warn about. The hook
            # message (see CLI) nudges the user to run `deja index`.
            return []

    existing = index.records
    if not existing:
        return []

    matches: list[Match] = []
    for staged in staged_function_records(repo_root):
        best: FunctionRecord | None = None
        best_score = 0.0
        for other in existing:
            # Never compare a staged function against its own file's records:
            # editing an existing function shouldn't flag it as its own dupe.
            if other.file == staged.file:
                continue
            score = pair_score(staged, other)
            if score > best_score:
                best_score = score
                best = other
        if best is not None and best_score >= threshold:
            matches.append(Match(staged=staged, existing=best, score=best_score))

    matches.sort(key=lambda m: (-m.score, m.staged.file, m.staged.line))
    return matches
