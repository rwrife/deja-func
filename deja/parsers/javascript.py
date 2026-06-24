"""JavaScript / TypeScript function extraction (M5).

PLAN.md §5/§6 calls for a *tree-light* parser: no parsing dependencies, just a
careful scan over the source. A full JS/TS grammar (Babel, tree-sitter) would
contradict the dependency-light ethos, and we only need *what a function is
called, where it lives, and its parameter list* — not a real AST.

So this parser tokenizes lightly: it strips comments and string/template
literals (replacing them with same-length blanks so byte offsets and line
numbers stay correct), then walks the text looking for the handful of shapes
that introduce a named function:

* ``function foo(...)`` / ``async function foo(...)`` / ``function* gen(...)``
* arrow functions bound to a name: ``const foo = (...) => ...`` (also
  ``let`` / ``var``, optionally ``async``, and the bare-param form
  ``const f = x => x``)
* class / object methods: ``foo(...) {`` and ``async foo(...) {``,
  ``get x() {}``, ``set x(v) {}``, ``*gen() {}``

Parameter lists are captured by **balanced-paren scanning** (not a flat regex),
so nested calls, generics (``Map<string, number>``), object/array destructuring
with defaults, and arrow defaults inside params all survive intact. TypeScript
param annotations and return types are kept verbatim in the signature when
present, e.g. ``(text: string, n = 0): boolean``.

This is deliberately heuristic. It favours *recall with low false-positives* on
real-world code over grammar-perfect precision, and — like every parser here —
returns ``[]`` rather than raising if anything goes sideways, so one weird file
never aborts a whole ``deja index`` run.
"""

from __future__ import annotations

import re

from .base import FunctionRecord

# Reserved words that can appear right before a ``(`` (in control flow, etc.)
# and must never be mistaken for a method/function name.
_KEYWORDS = frozenset(
    {
        "if",
        "for",
        "while",
        "switch",
        "catch",
        "return",
        "function",
        "await",
        "typeof",
        "delete",
        "void",
        "in",
        "of",
        "new",
        "do",
        "else",
        "yield",
        "case",
        "with",
        "super",
        "this",
        "instanceof",
    }
)

_IDENT = r"[A-Za-z_$][\w$]*"

# `function foo(` / `async function foo(` / `function* gen(` / `async function* g(`
_FUNC_DECL = re.compile(
    r"\b(?:async\s+)?function\s*(?P<star>\*)?\s*(?P<name>" + _IDENT + r")\s*(?P<lt>[<(])",
)

# `const foo = ( ... ) =>`  /  `let foo = async ( ... ) =>`  /  `const f = x =>`
# Name + assignment + (optional async) + start of params (either `(`/`<` for a
# parenthesized/generic arrow, or a bare identifier for `x => ...`).
_ARROW_ASSIGN = re.compile(
    r"\b(?:const|let|var)\s+(?P<name>" + _IDENT + r")\s*"
    r"(?::\s*[^=;]+?)?"  # optional TS type on the binding, e.g. `: Foo =`
    r"=\s*(?:async\s+)?(?P<start>[(<]|" + _IDENT + r"\s*=>)",
)

# Method-ish: `name( ... ) {`  with optional leading async/get/set/* modifiers.
# We validate the *preceding* token separately to avoid matching calls.
_METHOD = re.compile(
    r"(?P<mods>(?:(?:public|private|protected|static|readonly|abstract|async|get|set)\s+)*)"
    r"(?P<star>\*\s*)?(?P<name>" + _IDENT + r")\s*(?P<lt>[<(])",
)


def _blank_noncode(src: str) -> str:
    """Replace comments and string/template literals with same-length blanks.

    Newlines are preserved so line numbers are unaffected; every other blanked
    character becomes a space. This lets the downstream regexes run over a
    "code-only" view without tripping on ``//`` inside a string or a ``{`` in a
    template literal, while keeping all offsets identical to the original.
    """
    out: list[str] = []
    i, n = 0, len(src)
    # State machine over the four "non-code" regions.
    while i < n:
        c = src[i]
        two = src[i : i + 2]

        if two == "//":  # line comment
            j = src.find("\n", i)
            j = n if j == -1 else j
            out.append(" " * (j - i))
            i = j
            continue

        if two == "/*":  # block comment (may span lines)
            j = src.find("*/", i + 2)
            j = n if j == -1 else j + 2
            out.append("".join(ch if ch == "\n" else " " for ch in src[i:j]))
            i = j
            continue

        if c in "\"'":  # single/double-quoted string
            j = i + 1
            while j < n and src[j] != c:
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == "\n":  # unterminated; bail at the newline
                    break
                j += 1
            j = min(j + 1, n)
            out.append("".join(ch if ch == "\n" else " " for ch in src[i:j]))
            i = j
            continue

        if c == "`":  # template literal (can span lines, can nest ${...})
            j = i + 1
            depth = 0
            while j < n:
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == "`" and depth == 0:
                    j += 1
                    break
                if src[j : j + 2] == "${":
                    depth += 1
                    j += 2
                    continue
                if src[j] == "}" and depth > 0:
                    depth -= 1
                j += 1
            out.append("".join(ch if ch == "\n" else " " for ch in src[i:j]))
            i = j
            continue

        out.append(c)
        i += 1

    return "".join(out)


def _match_paren(code: str, open_idx: int) -> int:
    """Return the index just past the ``)`` matching the ``(`` at *open_idx*.

    Operates on comment/string-blanked *code*, so quotes/braces inside the
    params have already been neutralised. Returns ``-1`` if unbalanced.
    """
    depth = 0
    i, n = open_idx, len(code)
    while i < n:
        ch = code[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return -1


def _skip_generic(code: str, lt_idx: int) -> int:
    """Skip a balanced ``<...>`` type-parameter list; return index of next char.

    Used when a declaration has TS generics before its params, e.g.
    ``function map<T, U>(...)``. Returns ``-1`` if it doesn't cleanly balance
    (so the caller can give up rather than guess).
    """
    depth = 0
    i, n = lt_idx, len(code)
    while i < n:
        ch = code[i]
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
            if depth == 0:
                return i + 1
        elif ch in ";{}":  # clearly not a generic list (e.g. a comparison)
            return -1
        i += 1
    return -1


def _return_type_span(code: str, after_params: int) -> tuple[int, int] | None:
    """Find the span of a TS return-type annotation following the param list.

    Looks for ``): <type>`` up to the function body ``{`` / arrow ``=>`` /
    statement end. Returns ``(start, end)`` offsets into *code* (excluding the
    leading colon) or ``None`` if there's no annotation. Operates on blanked
    *code* for boundary safety; the caller slices the original source for text.
    """
    i, n = after_params, len(code)
    while i < n and code[i] in " \t":
        i += 1
    if i >= n or code[i] != ":":
        return None
    i += 1
    start = i
    depth = 0  # track <...>/(...)/[...] so a `{` inside a type isn't the body
    while i < n:
        ch = code[i]
        # A depth-0 `{` is the function body, not part of the type — stop first.
        if depth == 0 and (ch == "{" or ch in ";\n" or code[i : i + 2] == "=>"):
            break
        if ch in "<([{":
            depth += 1
        elif ch in ">)]}":
            if depth == 0:
                break
            depth -= 1
        i += 1
    return (start, i)


def _normalize_params(raw: str) -> str:
    """Collapse internal whitespace/newlines in a captured param list."""
    return re.sub(r"\s+", " ", raw.strip())


def _line_of(src: str, offset: int) -> int:
    """1-based line number of *offset* within *src*."""
    return src.count("\n", 0, offset) + 1


# Tokens that, when they directly precede a ``name(`` method-ish match, mean it
# is *not* a definition (it's a call, a control-flow head, etc.).
_NON_METHOD_PREV = frozenset({")", "]", ".", "=", ">", "+", "-", "*", "/", "%", "&", "|", "?", ","})


class JavaScriptParser:
    """Extract functions/methods/arrows from JS & TS source (heuristic, dep-free).

    A single parser instance handles ``.js``, ``.jsx``, ``.ts`` and ``.tsx`` —
    the JSX/TSX bits live in expression position and don't introduce function
    names we'd miss, and TS annotations are captured verbatim in signatures.
    """

    lang = "javascript"

    def parse(self, source: str, rel_path: str) -> list[FunctionRecord]:
        """Parse *source* into records, returning ``[]`` on any failure."""
        try:
            return self._parse(source, rel_path)
        except Exception:  # pragma: no cover - defensive; never abort an index
            return []

    # -- internals --------------------------------------------------------

    def _parse(self, source: str, rel_path: str) -> list[FunctionRecord]:
        code = _blank_noncode(source)
        # Offset -> record, so two patterns hitting the same function (e.g. an
        # arrow that also looks method-ish) collapse to one. Keyed by the name
        # start offset.
        found: dict[int, FunctionRecord] = {}

        self._scan_function_decls(source, code, rel_path, found)
        self._scan_arrow_assignments(source, code, rel_path, found)
        self._scan_methods(source, code, rel_path, found)

        records = list(found.values())
        records.sort(key=lambda r: (r.line, r.name))
        return records

    def _signature(self, source: str, code: str, open_paren: int) -> tuple[str, int] | None:
        """Build a ``(params)`` (+ optional ``: ret``) signature from *open_paren*.

        Boundaries are found on the blanked *code* (so quotes/braces inside the
        params can't confuse paren-matching), but the visible text is sliced
        from the original *source* — so string/template defaults like ``= "-"``
        survive verbatim. Returns ``(signature, end_offset)`` or ``None`` if the
        params don't balance.
        """
        end = _match_paren(code, open_paren)
        if end == -1:
            return None
        params = _normalize_params(source[open_paren + 1 : end - 1])
        sig = f"({params})"
        span = _return_type_span(code, end)
        if span is not None:
            ret = _normalize_params(source[span[0] : span[1]])
            if ret:
                sig += f": {ret}"
        return sig, end

    def _resolve_params_start(self, code: str, lt_or_paren: int) -> int:
        """Given the index of ``<`` or ``(`` after a name, return the ``(`` index.

        Skips a leading TS generic list if present. Returns ``-1`` if it can't
        find a clean param-opening paren.
        """
        if code[lt_or_paren] == "(":
            return lt_or_paren
        after = _skip_generic(code, lt_or_paren)
        if after == -1:
            return -1
        # Whitespace then the real param paren.
        j = after
        while j < len(code) and code[j] in " \t\n":
            j += 1
        return j if j < len(code) and code[j] == "(" else -1

    def _scan_function_decls(self, source, code, rel_path, found) -> None:
        for m in _FUNC_DECL.finditer(code):
            name = m.group("name")
            paren = self._resolve_params_start(code, m.start("lt"))
            if paren == -1:
                continue
            built = self._signature(source, code, paren)
            if built is None:
                continue
            sig, _ = built
            off = m.start("name")
            found[off] = FunctionRecord(
                name=name,
                file=rel_path,
                line=_line_of(source, off),
                signature=sig,
                docstring="",
                lang=self.lang,
                qualname=name,
            )

    def _scan_arrow_assignments(self, source, code, rel_path, found) -> None:
        for m in _ARROW_ASSIGN.finditer(code):
            name = m.group("name")
            start = m.start("start")
            off = m.start("name")
            if code[start] in "(<":
                paren = self._resolve_params_start(code, start)
                if paren == -1:
                    continue
                built = self._signature(source, code, paren)
                if built is None:
                    continue
                sig = built[0]
            else:
                # Bare single param: `const f = x => ...` (no parens in source).
                bare = re.match(_IDENT, code[start:])
                if not bare:
                    continue
                sig = f"({bare.group(0)})"
            found[off] = FunctionRecord(
                name=name,
                file=rel_path,
                line=_line_of(source, off),
                signature=sig,
                docstring="",
                lang=self.lang,
                qualname=name,
            )

    def _scan_methods(self, source, code, rel_path, found) -> None:
        for m in _METHOD.finditer(code):
            name = m.group("name")
            off = m.start("name")
            if off in found:  # already captured as a decl/arrow
                continue
            if name in _KEYWORDS:
                continue
            # Reject if this is really a call/expression: inspect the char just
            # before the (possibly modifier-led) match.
            prev = self._prev_significant(code, m.start("mods") if m.group("mods") else off)
            mods = m.group("mods") or ""
            # With no modifiers and a "expression-ish" preceding char, it's a call.
            if not mods and not m.group("star") and prev in _NON_METHOD_PREV:
                continue
            # `function`-led matches are handled by the decl scanner; skip here.
            if prev_word_is(code, off, "function"):
                continue
            paren = self._resolve_params_start(code, m.start("lt"))
            if paren == -1:
                continue
            built = self._signature(source, code, paren)
            if built is None:
                continue
            sig, end = built
            # Must be a real method body `{` (after an optional return type),
            # not e.g. `foo()` used as a call/statement.
            if not self._opens_block(code, end):
                continue
            found[off] = FunctionRecord(
                name=name,
                file=rel_path,
                line=_line_of(source, off),
                signature=sig,
                docstring="",
                lang=self.lang,
                qualname=name,
            )

    @staticmethod
    def _prev_significant(code: str, idx: int) -> str:
        """Nearest non-whitespace char before *idx* (or ``""`` at start)."""
        j = idx - 1
        while j >= 0 and code[j] in " \t\n":
            j -= 1
        return code[j] if j >= 0 else ""

    @staticmethod
    def _opens_block(code: str, end: int) -> bool:
        """True if the first significant char at/after *end* (skipping a TS
        return type) is ``{`` — i.e. a method body follows the param list."""
        i, n = end, len(code)
        while i < n and code[i] in " \t\n":
            i += 1
        if i < n and code[i] == ":":  # skip `: ReturnType` before the body
            i += 1
            depth = 0
            while i < n:
                ch = code[i]
                if ch in "<([{" and not (ch == "{" and depth == 0):
                    depth += 1
                elif ch in ">)]}":
                    if depth == 0:
                        break
                    depth -= 1
                elif depth == 0 and ch == "{":
                    break
                elif depth == 0 and ch in ";\n=":
                    break
                i += 1
            while i < n and code[i] in " \t\n":
                i += 1
        return i < n and code[i] == "{"


def prev_word_is(code: str, name_off: int, word: str) -> bool:
    """True if the identifier word immediately before *name_off* equals *word*."""
    j = name_off - 1
    while j >= 0 and code[j] in " \t\n*":
        j -= 1
    end = j + 1
    while j >= 0 and (code[j].isalnum() or code[j] in "_$"):
        j -= 1
    return code[j + 1 : end] == word
