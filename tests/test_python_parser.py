"""Tests for the Python parser (M2)."""

from __future__ import annotations

from deja.parsers import PythonParser, get_parser_for_path
from deja.parsers.base import FunctionRecord

SAMPLE = '''
"""Module docstring (not a function)."""


def slugify(text: str, sep: str = "-") -> str:
    """Turn text into a URL slug."""
    return text.lower().replace(" ", sep)


async def fetch(url: str) -> bytes:
    """Fetch a URL asynchronously."""
    return b""


def variadic(*args, **kwargs):
    pass


def kw_only(a, *, b: int = 3, c=4):
    """Has keyword-only params."""
    return a


class Greeter:
    def __init__(self, name: str) -> None:
        self.name = name

    def greet(self) -> str:
        """Say hi."""
        return f"hi {self.name}"

    @staticmethod
    def shout(msg: str) -> str:
        def _inner() -> str:
            return msg.upper()

        return _inner()
'''


def _parse(src: str = SAMPLE) -> list[FunctionRecord]:
    return PythonParser().parse(src, "sample.py")


def test_extracts_all_functions_and_methods() -> None:
    names = {r.name for r in _parse()}
    # Module funcs, methods, the static method, and the nested closure.
    assert {
        "slugify",
        "fetch",
        "variadic",
        "kw_only",
        "__init__",
        "greet",
        "shout",
        "_inner",
    } <= names


def test_signature_with_type_hints_and_default() -> None:
    rec = next(r for r in _parse() if r.name == "slugify")
    # ast.unparse normalizes string literals to single quotes.
    assert rec.signature == "(text: str, sep: str = '-') -> str"
    assert rec.docstring == "Turn text into a URL slug."
    assert rec.lang == "python"
    assert rec.file == "sample.py"
    assert rec.line > 0


def test_async_function_is_captured() -> None:
    rec = next(r for r in _parse() if r.name == "fetch")
    assert rec.signature == "(url: str) -> bytes"


def test_varargs_and_kwargs_signature() -> None:
    rec = next(r for r in _parse() if r.name == "variadic")
    assert rec.signature == "(*args, **kwargs)"


def test_keyword_only_signature() -> None:
    rec = next(r for r in _parse() if r.name == "kw_only")
    assert rec.signature == "(a, *, b: int = 3, c=4)"


def test_method_qualname_is_dotted() -> None:
    greet = next(r for r in _parse() if r.name == "greet")
    assert greet.qualname == "Greeter.greet"
    inner = next(r for r in _parse() if r.name == "_inner")
    assert inner.qualname == "Greeter.shout._inner"


def test_module_function_qualname_equals_name() -> None:
    rec = next(r for r in _parse() if r.name == "slugify")
    assert rec.qualname == "slugify"


def test_missing_docstring_is_empty_string() -> None:
    rec = next(r for r in _parse() if r.name == "variadic")
    assert rec.docstring == ""


def test_syntax_error_yields_no_records() -> None:
    assert PythonParser().parse("def broken(:\n", "bad.py") == []


def test_registry_dispatches_python_by_extension() -> None:
    assert isinstance(get_parser_for_path("foo/bar.py"), PythonParser)
    assert get_parser_for_path("foo/bar.PY") is not None  # case-insensitive
    assert get_parser_for_path("foo/bar.txt") is None
