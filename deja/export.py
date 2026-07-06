"""Portable index export/import (PLAN.md §8 #12).

The on-disk index (`.dejafunc/index.json`) is deliberately gitignored and
regenerated per-machine, which means an agent on a **fresh clone** has to run a
full ``deja index`` before it can ask "have I written this?" — cold-start
friction exactly when the lookup is most valuable.

``deja export`` / ``deja import`` close that gap with a first-class, portable
artifact a repo can check in (or an agent can hydrate in one command):

- :func:`export_index` serializes the current index to a portable dict that
  *is* the existing :meth:`~deja.index.Index.to_dict` shape plus two extra
  top-level keys — ``exported_at`` (UTC ISO-8601) and an optional ``root_name``
  — so nothing about the record schema changes and old consumers keep working.
- :func:`load_export` parses that document back into an :class:`~deja.index.Index`,
  validating ``schema_version`` up front (forward-compat tone matching
  :meth:`~deja.index.Index.from_dict`): an incompatible version raises
  :class:`ExportError` with a clear message rather than silently importing junk.

Round-tripping is lossless: ``export`` then ``import`` writes byte-identical
records to what a native ``deja index`` would (both go through the same
``Index.to_dict`` record serialization).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .index import SCHEMA_VERSION, Index

#: Default filename ``deja export`` writes when no ``-o FILE`` is given. Chosen
#: to be obvious and safe to check in at a repo root.
DEFAULT_EXPORT_FILE = "deja-index.json"


class ExportError(Exception):
    """Raised when an export document can't be imported (e.g. schema mismatch).

    Mirrors the ``ConfigError`` pattern: the CLI catches it and prints a single
    clean line to stderr, never a traceback.
    """


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string (seconds precision)."""
    # Seconds precision keeps the artifact stable/diffable; timespec avoids a
    # noisy microsecond tail that would churn on every export.
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def export_to_dict(index: Index, *, root_name: str | None = None) -> dict:
    """Return the portable export document for *index*.

    This is exactly :meth:`Index.to_dict` (``schema_version``, ``tool_version``,
    ``count``, ``functions``) with two extra top-level keys layered on:

    - ``exported_at`` — UTC ISO-8601 timestamp of the export.
    - ``root_name`` — an optional human label for where it came from (the
      indexed directory's name); omitted entirely when ``None`` so the shape
      stays minimal.

    The record list is untouched, so a subsequent import reconstructs the same
    records a native index would.
    """
    doc = index.to_dict()
    doc["exported_at"] = _utc_now_iso()
    if root_name:
        doc["root_name"] = root_name
    return doc


def export_index(
    index: Index,
    *,
    root_name: str | None = None,
) -> str:
    """Serialize *index* to the portable export JSON **text** (with trailing NL).

    Kept separate from writing so both the file path and the ``-o -`` stdout path
    emit identical bytes. Uses the same ``indent=2, ensure_ascii=False`` +
    trailing newline convention as :func:`deja.index.save_index`, so a checked-in
    export diffs cleanly.
    """
    doc = export_to_dict(index, root_name=root_name)
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def write_export(
    index: Index,
    out_path: str | os.PathLike[str],
    *,
    root_name: str | None = None,
) -> Path:
    """Write the portable export for *index* to *out_path* and return it."""
    path = Path(out_path)
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(export_index(index, root_name=root_name), encoding="utf-8")
    return path


def load_export(data: dict) -> Index:
    """Rebuild an :class:`~deja.index.Index` from a parsed export document.

    Validates ``schema_version`` before trusting the records: a version newer
    than this build understands (or a non-integer) is rejected with an
    :class:`ExportError` rather than importing a shape we can't read. A missing
    version is treated as the current one (same lenient default as
    :meth:`Index.from_dict`), so hand-written or older-but-compatible files still
    load.

    The extra export-only keys (``exported_at``, ``root_name``) are ignored on
    load — they're provenance, not index content.
    """
    if not isinstance(data, dict):
        raise ExportError("export file is not a JSON object")

    raw_version = data.get("schema_version", SCHEMA_VERSION)
    try:
        version = int(raw_version)
    except (TypeError, ValueError):
        raise ExportError(
            f"unrecognized schema_version {raw_version!r} in export "
            f"(this build understands version {SCHEMA_VERSION})"
        ) from None

    if version > SCHEMA_VERSION:
        raise ExportError(
            f"export uses schema_version {version}, but this build only "
            f"understands up to {SCHEMA_VERSION}. Upgrade deja-func to import it."
        )

    # Reuse the canonical loader so records are parsed identically to a native
    # index (unknown record keys ignored, same field set).
    return Index.from_dict(data)


def read_export(path: str | os.PathLike[str]) -> Index:
    """Read and parse a portable export file into an :class:`~deja.index.Index`.

    Raises:
        FileNotFoundError: If *path* does not exist.
        ExportError: If the file isn't valid JSON, or its ``schema_version`` is
            incompatible.
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"No such export file: {p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ExportError(f"{p} is not valid JSON: {exc}") from None
    return load_export(data)
