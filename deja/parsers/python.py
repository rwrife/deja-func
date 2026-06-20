"""Python function extraction via the standard-library :mod:`ast`.

Zero parsing dependencies (PLAN.md §5): we compile the source to an AST and walk
it, emitting a :class:`~deja.parsers.base.FunctionRecord` for every ``def`` /
``async def`` — module-level functions *and* methods nested in classes.

Signatures are reconstructed from the AST (args, type hints, defaults, ``*args``
/ ``**kwargs``, and the return annotation) using :func:`ast.unparse`, so they
read the way a human wrote them, e.g. ``(text: str, *, sep: str = "-") -> str``.
"""

from __future__ import annotations

import ast

from .base import FunctionRecord

_FUNC_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)


def _annotation(node: ast.expr | None) -> str:
    """Render a type annotation back to source, or ``""`` if absent."""
    if node is None:
        return ""
    try:
        return ast.unparse(node)
    except Exception:  # pragma: no cover - unparse is robust on valid ASTs
        return ""


def _default(node: ast.expr) -> str:
    """Render a default value expression back to source."""
    try:
        return ast.unparse(node)
    except Exception:  # pragma: no cover
        return "..."


def _format_arg(arg: ast.arg, default: ast.expr | None) -> str:
    """Render a single parameter, e.g. ``text: str = "-"``."""
    out = arg.arg
    ann = _annotation(arg.annotation)
    if ann:
        out += f": {ann}"
    if default is not None:
        # PEP 8: no spaces around = for un-annotated args, spaces if annotated.
        out += f" = {_default(default)}" if ann else f"={_default(default)}"
    return out


def _build_signature(func: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Reconstruct a readable ``(params) -> return`` signature from the AST."""
    a = func.args
    parts: list[str] = []

    # Positional-only args (PEP 570) and their trailing "/" marker.
    posonly = list(getattr(a, "posonlyargs", []))
    positional = posonly + list(a.args)

    # Defaults align to the *end* of posonly+args combined.
    num_defaults = len(a.defaults)
    first_default = len(positional) - num_defaults
    for i, arg in enumerate(positional):
        default = a.defaults[i - first_default] if i >= first_default else None
        parts.append(_format_arg(arg, default))
        if posonly and i == len(posonly) - 1:
            parts.append("/")

    # *args  (or a bare "*" if there are keyword-only args without *args).
    if a.vararg is not None:
        parts.append("*" + _format_arg(a.vararg, None))
    elif a.kwonlyargs:
        parts.append("*")

    # Keyword-only args, each with its own (possibly None) default. The AST
    # guarantees these two lists are the same length, so strict zip is safe.
    for arg, default in zip(a.kwonlyargs, a.kw_defaults, strict=True):
        parts.append(_format_arg(arg, default))

    # **kwargs
    if a.kwarg is not None:
        parts.append("**" + _format_arg(a.kwarg, None))

    sig = "(" + ", ".join(parts) + ")"
    ret = _annotation(func.returns)
    if ret:
        sig += f" -> {ret}"
    return sig


def _first_doc_line(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """First non-empty line of the docstring, or ``""``."""
    doc = ast.get_docstring(node)
    if not doc:
        return ""
    for line in doc.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


class PythonParser:
    """Extracts functions and methods from Python source via :mod:`ast`."""

    lang = "python"

    def parse(self, source: str, rel_path: str) -> list[FunctionRecord]:
        """Parse *source* into function records.

        Returns ``[]`` for files that fail to compile (syntax errors), so a
        single bad file never aborts a whole ``deja index`` run.
        """
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return []

        records: list[FunctionRecord] = []
        self._walk(tree, rel_path, prefix=(), records=records)
        return records

    def _walk(
        self,
        node: ast.AST,
        rel_path: str,
        prefix: tuple[str, ...],
        records: list[FunctionRecord],
    ) -> None:
        """Recurse, building dotted qualnames through classes and nested defs."""
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _FUNC_NODES):
                qual = ".".join((*prefix, child.name))
                records.append(
                    FunctionRecord(
                        name=child.name,
                        file=rel_path,
                        line=child.lineno,
                        signature=_build_signature(child),
                        docstring=_first_doc_line(child),
                        lang=self.lang,
                        qualname=qual,
                    )
                )
                # Recurse into the function body for nested defs/closures.
                self._walk(child, rel_path, (*prefix, child.name), records)
            elif isinstance(child, ast.ClassDef):
                self._walk(child, rel_path, (*prefix, child.name), records)
            else:
                self._walk(child, rel_path, prefix, records)
