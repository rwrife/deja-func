"""Optional project configuration via ``.dejafunc.toml`` (PLAN.md §8 #11).

As ``deja-func`` grows commands (``index``, ``find``, ``dupes``, ``hook``,
``--watch``, ``stats``, ``stale`` …), repeat-flags pile up. A small, *optional*
``.dejafunc.toml`` at the repo root lets a project pin its include/exclude globs
and ranking weights once instead of re-passing flags on every invocation.

Design goals (issue #25):

* **Absent file ⇒ today's behavior, byte-for-byte.** An empty, missing, or
  partial config never changes existing output.
* **Precedence is CLI flag > config > built-in default.** Config only fills
  options the caller left unset; an explicit flag always wins. (Callers own the
  "was a flag passed?" decision; this module just supplies config values.)
* **Forward-compatible.** Unknown tables/keys are ignored rather than fatal, in
  the same spirit as :meth:`FunctionRecord.from_dict`.
* **No new *core-behavior* dependency.** Parsing uses the stdlib ``tomllib``
  (Python 3.11+). On 3.10, where ``tomllib`` is absent, we fall back to the
  tiny ``tomli`` backport (tomllib's own upstream, pinned in ``pyproject.toml``
  for ``python_version < '3.11'``) so config parses identically everywhere.

Supported keys (v1 — intentionally tight)::

    [index]
    exclude = ["**/build/**", "vendor/**"]   # extra globs to skip
    include = ["scripts/*.py"]               # extra globs to force-in

    [search]
    weights = { name = 1.0, docstring = 0.6, signature = 0.4 }

``[index].exclude`` / ``[index].include`` are layered on top of the existing
``.gitignore`` / :data:`~deja.walker.ALWAYS_SKIP_DIRS` logic in ``walker.py``.
``[search].weights`` feed the existing ``search.py`` ranking so a project can
tune name-vs-docstring-vs-signature emphasis.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

try:  # tomllib is stdlib on 3.11+; on 3.10 use the tomli backport (see docstring).
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised only on py3.10
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:  # pragma: no cover - neither present (unpinned env)
        tomllib = None  # type: ignore[assignment]

#: Filename we look for, walking up from the search root (like git's ``.git``).
CONFIG_FILENAME = ".dejafunc.toml"


class ConfigError(Exception):
    """Raised when a located ``.dejafunc.toml`` cannot be parsed.

    Only malformed TOML (or a wrong-typed value we cannot coerce) raises. A
    *missing* file is never an error — it simply yields an empty
    :class:`Config`. Callers turn this into a clear, non-crashing CLI message.
    """


@dataclass(frozen=True, slots=True)
class SearchWeights:
    """Ranking weights for ``deja find`` (see :mod:`deja.search`).

    ``None`` means "unset — use the search default", so a partial ``weights``
    table only overrides the components it names. Values are non-negative
    multipliers on the 0-100 per-signal scores.
    """

    name: float | None = None
    docstring: float | None = None
    signature: float | None = None


@dataclass(frozen=True, slots=True)
class Config:
    """Parsed ``.dejafunc.toml`` with safe, behavior-preserving defaults.

    An all-defaults instance (the ``Config()`` you get for a missing file) is
    inert: empty glob lists and all-``None`` weights, so wiring it in never
    changes today's behavior.
    """

    #: Extra exclude globs, layered on ``.gitignore`` / ``ALWAYS_SKIP_DIRS``.
    exclude: tuple[str, ...] = ()
    #: Extra include globs that force a match back in despite an exclude.
    include: tuple[str, ...] = ()
    #: Optional ranking-weight overrides for ``deja find``.
    weights: SearchWeights = field(default_factory=SearchWeights)
    #: Absolute path of the file this config came from (``None`` if defaults).
    source: Path | None = None

    @property
    def is_default(self) -> bool:
        """True when nothing meaningful was configured (inert config)."""
        return not self.exclude and not self.include and self.weights == SearchWeights()


def find_config_file(start: str | Path) -> Path | None:
    """Return the nearest ``.dejafunc.toml`` at/above *start*, or ``None``.

    Walks up from *start* (a file or directory) to the filesystem root, the way
    git discovers ``.git``. The first ``.dejafunc.toml`` found wins, so a repo
    root config applies to any subdirectory you run ``deja`` from.
    """
    p = Path(start).resolve()
    # Search *start* itself when it's a directory, then every ancestor.
    for directory in (p, *p.parents) if p.is_dir() else p.parents:
        candidate = directory / CONFIG_FILENAME
        if candidate.is_file():
            return candidate
    return None


def _coerce_str_list(value: object, *, where: str) -> tuple[str, ...]:
    """Coerce a TOML value into a tuple of strings (globs), or raise."""
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{where} must be a list of strings")
    return tuple(value)


def _coerce_weight(value: object, *, where: str) -> float | None:
    """Coerce a single weight to a non-negative float, or raise. ``None`` skips."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        # bool is an int subclass; reject it explicitly so `name = true` errors.
        raise ConfigError(f"{where} must be a number")
    weight = float(value)
    if weight < 0:
        raise ConfigError(f"{where} must be >= 0")
    return weight


def _parse_data(data: dict, *, source: Path | None) -> Config:
    """Build a :class:`Config` from an already-parsed TOML mapping.

    Unknown tables and keys are ignored (forward-compat). Only recognized keys
    with the wrong *type* raise :class:`ConfigError`.
    """
    index_tbl = data.get("index") or {}
    if not isinstance(index_tbl, dict):
        raise ConfigError("[index] must be a table")
    exclude = _coerce_str_list(index_tbl.get("exclude"), where="[index].exclude")
    include = _coerce_str_list(index_tbl.get("include"), where="[index].include")

    search_tbl = data.get("search") or {}
    if not isinstance(search_tbl, dict):
        raise ConfigError("[search] must be a table")
    weights_tbl = search_tbl.get("weights") or {}
    if not isinstance(weights_tbl, dict):
        raise ConfigError("[search].weights must be a table")
    weights = SearchWeights(
        name=_coerce_weight(weights_tbl.get("name"), where="[search].weights.name"),
        docstring=_coerce_weight(weights_tbl.get("docstring"), where="[search].weights.docstring"),
        signature=_coerce_weight(weights_tbl.get("signature"), where="[search].weights.signature"),
    )

    return Config(exclude=exclude, include=include, weights=weights, source=source)


def load_config(start: str | Path) -> Config:
    """Discover and parse ``.dejafunc.toml`` at/above *start*.

    Returns an inert default :class:`Config` when no file is found or when the
    file is empty. Raises :class:`ConfigError` only on genuinely malformed TOML
    or wrong-typed keys. TOML parsing uses stdlib ``tomllib`` (3.11+) or the
    pinned ``tomli`` backport (3.10), so behavior is identical across supported
    Pythons.

    Args:
        start: File or directory to begin the ancestor search from (typically
            the ``--root`` / ``path`` a command was given).

    Returns:
        A :class:`Config`. Its ``source`` is set when it came from a file.
    """
    path = find_config_file(start)
    if path is None:
        return Config()

    if tomllib is None:  # pragma: no cover - only if the tomli backport is missing
        # Defensive: py3.10 pins `tomli` in pyproject, so this branch is
        # unreachable in a correctly-installed env. If someone strips the
        # backport, don't hard-crash the command -- surface a clear note and
        # fall back to defaults (caller downgrades ConfigError to a one-liner).
        raise ConfigError(
            f"found {path.name} but no TOML parser is available "
            f"(install 'tomli' on Python < 3.11); ignoring config"
        )

    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"malformed {path.name}: {exc}") from exc
    except OSError as exc:  # pragma: no cover - unreadable despite is_file()
        raise ConfigError(f"could not read {path}: {exc}") from exc

    return _parse_data(data, source=path)
