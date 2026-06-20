"""Parser base types: the :class:`FunctionRecord` and the :class:`Parser` protocol.

A ``FunctionRecord`` is the unit of the index — one parsed function or method.
Keeping it a plain dataclass (no behaviour) makes JSON (de)serialization in
``deja/index.py`` trivial and keeps parsers dumb data producers.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class FunctionRecord:
    """A single indexed function or method.

    Attributes:
        name: Function/method name (e.g. ``slugify``). For methods this is the
            bare method name; ``qualname`` carries the dotted path.
        file: Repo-relative path to the source file (POSIX separators).
        line: 1-based line number where the ``def`` appears.
        signature: Human-readable signature, e.g. ``(text: str) -> str``.
        docstring: First line of the docstring, or ``""`` if none.
        lang: Source language identifier (e.g. ``python``).
        qualname: Dotted qualified name (e.g. ``Foo.method``); falls back to
            ``name`` for module-level functions.
    """

    name: str
    file: str
    line: int
    signature: str
    docstring: str
    lang: str
    qualname: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict (used by the index writer)."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FunctionRecord:
        """Rebuild a record from a dict produced by :meth:`to_dict`.

        Unknown keys are ignored so older/newer index files load gracefully.
        """
        fields = {"name", "file", "line", "signature", "docstring", "lang", "qualname"}
        return cls(**{k: v for k, v in data.items() if k in fields})


@runtime_checkable
class Parser(Protocol):
    """Protocol every language parser implements.

    A parser is stateless: give it source text and its path, get records back.
    """

    #: Language identifier stored on each record (e.g. ``python``).
    lang: str

    def parse(self, source: str, rel_path: str) -> list[FunctionRecord]:
        """Extract function records from *source*.

        Args:
            source: Full text of the file.
            rel_path: Repo-relative path (stored on each record).

        Returns:
            A list of records. On unparseable input, return ``[]`` rather than
            raising — one bad file shouldn't abort a whole index run.
        """
        ...
