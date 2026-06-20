"""Language parsers for deja-func.

Each parser turns source files into :class:`~deja.parsers.base.FunctionRecord`
objects. Parsers are pluggable per language (PLAN.md §6); M2 ships Python only.
"""

from __future__ import annotations

from .base import FunctionRecord, Parser
from .python import PythonParser

__all__ = ["FunctionRecord", "Parser", "PythonParser", "get_parser_for_path"]

# Extension → parser instance. Adding a language = adding one entry here.
_PARSERS_BY_EXT: dict[str, Parser] = {
    ".py": PythonParser(),
}


def get_parser_for_path(path: str) -> Parser | None:
    """Return the parser registered for *path*'s extension, or ``None``.

    Dispatch is by file extension (lower-cased) so the walker can ask "do we
    know how to parse this file?" without importing every parser by hand.
    """
    from os.path import splitext

    ext = splitext(path)[1].lower()
    return _PARSERS_BY_EXT.get(ext)
