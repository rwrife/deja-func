"""Go function & method extraction (extension-dispatch registry, issue #34).

Like the JavaScript parser, this is a *tree-light*, dependency-free scanner
(PLAN.md §5: stdlib-only, no tree-sitter). Go's grammar for what *introduces* a
function is refreshingly regular, so a careful regex over comment/string-blanked
source gets us everything we need: name, receiver (for methods), parameter list,
result signature, doc comment, and ``file:line``.

Shapes handled:

* plain functions — ``func Name(args) ret {`` / ``func Name(args) (a, b) {``
* generic functions — ``func Map[T any](s []T) []T {``
* methods with a receiver — ``func (r *Repo) Get(id string) (*Row, error) {``

For methods the receiver *type* (pointer stars stripped) is folded into the
qualified name (e.g. ``Repo.Get``) so it reads the way Go programmers refer to
it, and the receiver is kept verbatim at the front of the signature so lookups
by shape still see it (``(r *Repo) (id string) (*Row, error)``).

The **doc comment** is the contiguous block of ``//`` line comments (or a single
``/* ... */`` block) immediately above the ``func`` keyword, with no blank line
between — matching Go's own godoc convention. Only its first line is stored, in
line with :class:`~deja.parsers.base.FunctionRecord`'s "first line of the
docstring" contract.

Parameter and result lists are captured by **balanced-paren scanning** on the
blanked source, so nested types (``map[string]func() error``), multi-return
tuples, and struct-literal defaults don't confuse boundary detection.

Like every parser here, this returns ``[]`` rather than raising on anything
weird, so one malformed ``.go`` file never aborts a whole ``deja index`` run.
"""

from __future__ import annotations

import re

from .base import FunctionRecord

_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"

# `func Name(` / `func Name[` (generics) / `func (recv) Name(`.
# The optional receiver group is a balanced-ish `(...)` right after `func`; we
# validate/parse it precisely with paren-scanning below rather than trusting the
# regex for nesting.
_FUNC = re.compile(
    r"\bfunc\b"
    r"(?:\s*(?P<recv>\((?:[^()]*)\)))?"  # optional receiver `(r *T)`
    r"\s*(?P<name>" + _IDENT + r")"
    r"\s*(?P<open>[\[(])",  # `[` for generics, `(` for params
)


def _blank_noncode(src: str) -> str:
    """Replace comments and string/rune literals with same-length blanks.

    Newlines are preserved so line numbers stay exact; every other blanked
    character becomes a space. This lets the func-scanner run over a code-only
    view without tripping on ``func`` inside a string or ``//`` inside a raw
    literal, while keeping all byte offsets identical to the original.

    Go string kinds handled: ``"..."`` interpreted strings (with ``\\`` escapes),
    ``` `...` ``` raw strings (no escapes, may span lines), and ``'x'`` runes.
    """
    out: list[str] = []
    i, n = 0, len(src)
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

        if c == '"':  # interpreted string literal
            j = i + 1
            while j < n and src[j] != '"':
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

        if c == "`":  # raw string literal (no escapes, may span lines)
            j = src.find("`", i + 1)
            j = n if j == -1 else j + 1
            out.append("".join(ch if ch == "\n" else " " for ch in src[i:j]))
            i = j
            continue

        if c == "'":  # rune literal
            j = i + 1
            while j < n and src[j] != "'":
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == "\n":
                    break
                j += 1
            j = min(j + 1, n)
            out.append("".join(ch if ch == "\n" else " " for ch in src[i:j]))
            i = j
            continue

        out.append(c)
        i += 1

    return "".join(out)


def _match_bracket(code: str, open_idx: int, open_ch: str, close_ch: str) -> int:
    """Return the index just past the bracket matching *open_ch* at *open_idx*.

    Operates on comment/string-blanked *code*. Returns ``-1`` if unbalanced.
    """
    depth = 0
    i, n = open_idx, len(code)
    while i < n:
        ch = code[i]
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return -1


def _normalize(raw: str) -> str:
    """Collapse internal whitespace/newlines in a captured span."""
    return re.sub(r"\s+", " ", raw.strip())


def _line_of(src: str, offset: int) -> int:
    """1-based line number of *offset* within *src*."""
    return src.count("\n", 0, offset) + 1


def _result_span(code: str, after_params: int) -> tuple[int, int] | None:
    """Find the span of a Go result signature after the parameter list.

    Returns ``(start, end)`` offsets into *code* covering everything between the
    params and the body ``{`` (or statement end), or ``None`` if empty. Handles
    both a bare single result (``error``) and a parenthesized tuple
    (``(*Row, error)``); paren tuples are captured whole via balanced scanning.
    """
    i, n = after_params, len(code)
    while i < n and code[i] in " \t":
        i += 1
    if i >= n:
        return None
    if code[i] == "{" or code[i] == "\n":
        return None
    start = i
    if code[i] == "(":  # parenthesized result tuple
        end = _match_bracket(code, i, "(", ")")
        return (start, end) if end != -1 else None
    # Bare result(s): read up to the body `{` (depth-aware for `[]`/`map[...]`).
    depth = 0
    while i < n:
        ch = code[i]
        if depth == 0 and ch == "{":
            break
        if depth == 0 and ch == "\n":
            break
        if ch in "[(":
            depth += 1
        elif ch in "])":
            depth -= 1
        i += 1
    return (start, i) if i > start else None


def _receiver_type(recv: str) -> str:
    """Extract the receiver *type name* from a ``(r *Repo)`` / ``(Repo)`` span.

    Strips the optional variable name, leading ``*``, and any generic type
    params, so ``(r *Repo[T])`` → ``Repo``. Returns ``""`` if it can't tell.
    """
    inner = recv.strip()[1:-1].strip()  # drop the surrounding parens
    if not inner:
        return ""
    parts = inner.split()
    type_expr = parts[-1]  # `r *Repo` -> `*Repo`; `*Repo` -> `*Repo`
    type_expr = type_expr.lstrip("*")
    type_expr = type_expr.split("[", 1)[0]  # drop generic params
    return type_expr


class GoParser:
    """Extract functions & methods from Go source (heuristic, dependency-free)."""

    lang = "go"

    def parse(self, source: str, rel_path: str) -> list[FunctionRecord]:
        """Parse *source* into records, returning ``[]`` on any failure."""
        try:
            return self._parse(source, rel_path)
        except Exception:  # pragma: no cover - defensive; never abort an index
            return []

    # -- internals --------------------------------------------------------

    def _parse(self, source: str, rel_path: str) -> list[FunctionRecord]:
        code = _blank_noncode(source)
        records: list[FunctionRecord] = []

        for m in _FUNC.finditer(code):
            name = m.group("name")
            open_ch = m.group("open")
            open_idx = m.start("open")

            # Skip a generic type-param list `[...]` before the real params.
            if open_ch == "[":
                after = _match_bracket(code, open_idx, "[", "]")
                if after == -1:
                    continue
                j = after
                while j < len(code) and code[j] in " \t":
                    j += 1
                if j >= len(code) or code[j] != "(":
                    continue
                paren = j
            else:
                paren = open_idx

            end = _match_bracket(code, paren, "(", ")")
            if end == -1:
                continue

            params = _normalize(source[paren:end])  # includes the ( )
            recv = m.group("recv")
            recv_type = _receiver_type(recv) if recv else ""

            sig_parts = []
            if recv:
                sig_parts.append(_normalize(source[m.start("recv") : m.end("recv")]))
            sig_parts.append(params)
            span = _result_span(code, end)
            if span is not None:
                ret = _normalize(source[span[0] : span[1]])
                if ret:
                    sig_parts.append(ret)
            signature = " ".join(sig_parts)

            qualname = f"{recv_type}.{name}" if recv_type else name
            records.append(
                FunctionRecord(
                    name=name,
                    file=rel_path,
                    line=_line_of(source, m.start()),
                    signature=signature,
                    docstring=self._doc_comment(source, m.start()),
                    lang=self.lang,
                    qualname=qualname,
                )
            )

        records.sort(key=lambda r: (r.line, r.name))
        return records

    @staticmethod
    def _doc_comment(source: str, func_off: int) -> str:
        """First line of the doc comment block directly above *func_off*, if any.

        Walks backwards over the line containing ``func`` to the preceding lines;
        a contiguous run of ``//`` comments (no blank line between them and the
        func) is the doc comment. Returns the first line's text (``//`` and one
        leading space stripped), or ``""``.
        """
        # Start of the func's own line.
        line_start = source.rfind("\n", 0, func_off) + 1
        lines_above: list[str] = []
        pos = line_start
        while pos > 0:
            prev_end = pos - 1  # the '\n' before this line
            prev_start = source.rfind("\n", 0, prev_end) + 1
            text = source[prev_start:prev_end]
            stripped = text.strip()
            if stripped.startswith("//"):
                lines_above.append(stripped[2:].strip())
                pos = prev_start
                continue
            break
        if lines_above:
            # lines_above is bottom-up; the *first* doc line is the last we saw.
            return lines_above[-1]
        return ""
