"""Build, save, and load the function index (`.dejafunc/index.json`).

The index is a small JSON document: a schema version, a tiny bit of metadata,
and the list of :class:`~deja.parsers.base.FunctionRecord` dicts. Keeping it
plain JSON (no binary format) means it's diffable, inspectable, and trivial for
agents to read (PLAN.md §6/§8).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__
from .parsers import FunctionRecord, get_parser_for_path
from .walker import iter_source_files

#: Directory (under the indexed root) holding the index file.
INDEX_DIR = ".dejafunc"
#: Index filename within :data:`INDEX_DIR`.
INDEX_FILE = "index.json"
#: Bump when the on-disk shape changes incompatibly.
SCHEMA_VERSION = 1


@dataclass
class Index:
    """In-memory function index plus the metadata we persist alongside it."""

    records: list[FunctionRecord] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION
    tool_version: str = __version__

    def __len__(self) -> int:
        return len(self.records)

    # -- (de)serialization ------------------------------------------------

    def to_dict(self) -> dict:
        """Return the JSON-serializable representation of the index."""
        return {
            "schema_version": self.schema_version,
            "tool_version": self.tool_version,
            "count": len(self.records),
            "functions": [r.to_dict() for r in self.records],
        }

    @classmethod
    def from_dict(cls, data: dict) -> Index:
        """Rebuild an index from parsed JSON produced by :meth:`to_dict`."""
        records = [FunctionRecord.from_dict(d) for d in data.get("functions", [])]
        return cls(
            records=records,
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
            tool_version=str(data.get("tool_version", "")),
        )


def index_path(root: str | os.PathLike[str]) -> Path:
    """Return the path to the index file for *root*."""
    return Path(root) / INDEX_DIR / INDEX_FILE


def build_index(root: str | os.PathLike[str]) -> Index:
    """Walk *root*, parse every supported file, and return an :class:`Index`.

    Files that fail to read or parse are skipped silently (a parser returns
    ``[]`` on syntax errors), so one bad file never aborts the whole run.
    """
    root_path = Path(root)
    records: list[FunctionRecord] = []

    for rel_path in iter_source_files(root_path):
        parser = get_parser_for_path(rel_path)
        if parser is None:  # pragma: no cover - walker already filtered these
            continue
        try:
            source = (root_path / rel_path).read_text(encoding="utf-8", errors="replace")
        except OSError:  # pragma: no cover - unreadable file
            continue
        records.extend(parser.parse(source, rel_path))

    # Stable ordering: by file, then line. Deterministic output is friendlier
    # for diffs and tests.
    records.sort(key=lambda r: (r.file, r.line))
    return Index(records=records)


def save_index(index: Index, root: str | os.PathLike[str]) -> Path:
    """Write *index* to ``<root>/.dejafunc/index.json`` and return its path."""
    path = index_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(index.to_dict(), indent=2, ensure_ascii=False)
    path.write_text(payload + "\n", encoding="utf-8")
    return path


def load_index(root: str | os.PathLike[str]) -> Index:
    """Load and return the index for *root*.

    Raises:
        FileNotFoundError: If no index exists yet (run ``deja index`` first).
    """
    path = index_path(root)
    if not path.is_file():
        raise FileNotFoundError(f"No index found at {path}. Run `deja index` first.")
    data = json.loads(path.read_text(encoding="utf-8"))
    return Index.from_dict(data)
