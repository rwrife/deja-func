"""Rust function & method extraction (extension-dispatch registry, issue #40).

Like the Go and JavaScript parsers, this is a *tree-light*, dependency-free
scanner (PLAN.md §5: stdlib-only, no tree-sitter). We blank comments and string
literals to a code-only view, find every ``fn`` introducer with a regex, and use
balanced-bracket scanning for the argument list, generics, and return type.

Shapes handled:

* free functions — ``fn name(args) -> T {``
* ``pub fn`` / ``pub(crate) fn`` / ``async fn`` / ``const fn`` / ``unsafe fn`` /
  ``extern "C" fn`` and combinations, including generics ``fn foo<T>(...)``
* methods & associated functions inside ``impl`` blocks — the impl *type* is
  folded into the qualified name (e.g. ``Repo.get``) so it reads the way Rust
  programmers refer to it
* trait method signatures inside ``trait`` blocks — both defaulted (``{ ... }``)
  and bodyless declarations (``fn required(&self);``); the trait name is the
  owner in the qualified name

The **doc comment** is the contiguous block of ``///`` (outer) or ``//!``
(inner) line comments immediately above the ``fn`` keyword, with no blank line
between — matching rustdoc convention. Only its first line is stored, in line
with :class:`~deja.parsers.base.FunctionRecord`'s "first line of the docstring"
contract. Because doc comments must be read *before* they are blanked away, we
capture them from the original source (not the blanked view).

Ownership (``impl`` / ``trait`` type) is resolved by scanning the blanked source
for ``impl``/``trait`` headers and their ``{ ... }`` brace spans, then asking
which (innermost) span a given ``fn`` falls inside.

Like every parser here, this returns ``[]`` rather than raising on anything
weird, so one malformed ``.rs`` file never aborts a whole ``deja index`` run.
"""

from __future__ import annotations

import re

from .base import FunctionRecord

_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"

# The `fn` introducer. Qualifiers (pub, async, const, unsafe, extern "…") may
# appear before it; we don't require them in the regex — we just anchor on the
# `fn` keyword and the following name + opening `(` or `<` (generics).
_FUNC = re.compile(
    r"\bfn\s+(?P<name>" + _IDENT + r")\s*(?P<open>[<(])",
)

# `impl [<generics>] [Trait for] Type [<...>] [where ...] {`
# and `trait Name [<...>] [: bounds] {`. We only need the *owner name* and the
# `{` position; generics/bounds are skipped by scanning to the block brace.
_IMPL = re.compile(r"\bimpl\b")
_TRAIT = re.compile(r"\btrait\s+(?P<name>" + _IDENT + r")\b")


def _blank_noncode(src: str) -> str:
    """Replace comments and string/char literals with same-length blanks.

    Newlines are preserved so line numbers stay exact; every other blanked
    character becomes a space. This lets the scanner run over a code-only view
    without tripping on ``fn`` inside a string or ``//`` inside a literal, while
    keeping all byte offsets identical to the original.

    Rust literal kinds handled: ``"..."`` strings (with ``\\`` escapes),
    ``r"..."`` / ``r#"..."#`` raw strings (no escapes, may span lines, with a
    matched number of ``#``), ``'x'`` char literals (lifetimes like ``'a`` are
    left alone), ``//`` line comments and ``/* ... */`` block comments (nested).
    """
    out: list[str] = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        two = src[i : i + 2]

        if two == "//":  # line comment (includes /// and //!)
            j = src.find("\n", i)
            j = n if j == -1 else j
            out.append(" " * (j - i))
            i = j
            continue

        if two == "/*":  # block comment (nested, may span lines)
            depth = 1
            j = i + 2
            while j < n and depth > 0:
                if src[j : j + 2] == "/*":
                    depth += 1
                    j += 2
                elif src[j : j + 2] == "*/":
                    depth -= 1
                    j += 2
                else:
                    j += 1
            out.append("".join(ch if ch == "\n" else " " for ch in src[i:j]))
            i = j
            continue

        # Raw string: r"...", r#"..."#, r##"..."##, ...
        if c == "r" and i + 1 < n and src[i + 1] in '"#':
            k = i + 1
            hashes = 0
            while k < n and src[k] == "#":
                hashes += 1
                k += 1
            if k < n and src[k] == '"':
                closing = '"' + "#" * hashes
                j = src.find(closing, k + 1)
                j = n if j == -1 else j + len(closing)
                out.append("".join(ch if ch == "\n" else " " for ch in src[i:j]))
                i = j
                continue

        if c == '"':  # normal string literal
            j = i + 1
            while j < n and src[j] != '"':
                if src[j] == "\\":
                    j += 2
                    continue
                j += 1
            j = min(j + 1, n)
            out.append("".join(ch if ch == "\n" else " " for ch in src[i:j]))
            i = j
            continue

        if c == "'":  # char literal OR lifetime — disambiguate
            # A char literal is 'x' or '\n' or '\u{..}'; a lifetime is 'name
            # with no closing quote soon after. Only blank true char literals.
            m = re.match(r"'(?:\\.|\\u\{[0-9A-Fa-f]+\}|[^'\\])'", src[i:])
            if m:
                span = m.end()
                out.append(" " * span)
                i += span
                continue
            # else: a lifetime; leave the quote as-is and move on one char.
            out.append(c)
            i += 1
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


def _return_span(code: str, after_params: int) -> tuple[int, int] | None:
    """Find the span of a ``-> T`` return type after the parameter list.

    Returns ``(start, end)`` offsets into *code* covering everything between the
    ``->`` and the body ``{`` / ``where`` / statement end (``;``), or ``None``
    if there is no return arrow. Depth-aware over ``<>``, ``()``, ``[]`` so
    generic and tuple return types are captured whole.
    """
    i, n = after_params, len(code)
    while i < n and code[i] in " \t\r\n":
        i += 1
    if code[i : i + 2] != "->":
        return None
    i += 2
    while i < n and code[i] in " \t\r\n":
        i += 1
    start = i
    depth = 0
    while i < n:
        ch = code[i]
        if depth == 0 and ch in "{;":
            break
        # stop at a `where` clause boundary at depth 0
        if depth == 0 and code[i : i + 5] == "where" and (i + 5 >= n or not code[i + 5].isalnum()):
            break
        if ch in "<([":
            depth += 1
        elif ch in ">)]":
            if depth > 0:
                depth -= 1
        i += 1
    end = i
    while end > start and code[end - 1] in " \t\r\n":
        end -= 1
    return (start, end) if end > start else None


def _impl_owner(code: str, header_end: int) -> str:
    """Extract the owner type name from an ``impl`` header ending at *header_end*.

    *header_end* is the offset of the block-opening ``{``. Handles:
    ``impl Type``, ``impl<T> Type<T>``, ``impl Trait for Type``,
    ``impl<T> Trait<T> for Type<T> where ...``. Returns the *Type* (the token
    after ``for`` when present, else the first type after any generics), with
    generic params and path prefixes stripped. Returns ``""`` if unclear.
    """
    # header text between `impl` and `{`
    # find the LAST `impl` before header_end that owns this brace
    last = None
    for mm in _IMPL.finditer(code, 0, header_end):
        last = mm
    if last is None:
        return ""
    header = code[last.end() : header_end]
    # `for` splits `impl Trait for Type`
    if re.search(r"\bfor\b", header):
        header = re.split(r"\bfor\b", header, maxsplit=1)[1]
    # drop a leading generic param list `<...>`
    header = header.strip()
    if header.startswith("<"):
        end = _match_bracket(header, 0, "<", ">")
        if end != -1:
            header = header[end:].strip()
    # first identifier-ish token (strip path prefix and generics)
    m2 = re.search(r"([A-Za-z_][A-Za-z0-9_:]*)", header)
    if not m2:
        return ""
    name = m2.group(1)
    name = name.split("::")[-1]  # strip module path
    return name


class _Block:
    """An ``impl``/``trait`` owner block: name + [start, end) brace span."""

    __slots__ = ("name", "start", "end")

    def __init__(self, name: str, start: int, end: int) -> None:
        self.name = name
        self.start = start
        self.end = end


def _owner_blocks(code: str) -> list[_Block]:
    """Collect all ``impl`` and ``trait`` owner blocks from blanked *code*.

    Each block records the owner name and the ``[open_brace, close_brace)`` span
    so a ``fn`` offset can be mapped to its innermost owner.
    """
    blocks: list[_Block] = []

    for m in _IMPL.finditer(code):
        # Find the block-opening `{` after the header (skip `<...>` generics and
        # bounds by scanning to the first top-level `{`).
        brace = _first_top_brace(code, m.end())
        if brace == -1:
            continue
        end = _match_bracket(code, brace, "{", "}")
        if end == -1:
            continue
        name = _impl_owner(code, brace)
        if name:
            blocks.append(_Block(name, brace, end))

    for m in _TRAIT.finditer(code):
        brace = _first_top_brace(code, m.end())
        if brace == -1:
            continue
        end = _match_bracket(code, brace, "{", "}")
        if end == -1:
            continue
        blocks.append(_Block(m.group("name"), brace, end))

    return blocks


def _first_top_brace(code: str, start: int) -> int:
    """Offset of the first ``{`` at generic/paren depth 0 at/after *start*.

    Skips ``<...>``, ``(...)``, ``[...]`` so generics and where-clause bounds
    don't cause us to grab a nested brace (there shouldn't be one before the
    block, but be safe). Stops at ``;`` (a bodyless trait fn / decl).
    """
    depth = 0
    i, n = start, len(code)
    while i < n:
        ch = code[i]
        if depth == 0 and ch == "{":
            return i
        if depth == 0 and ch == ";":
            return -1
        if ch in "<([":
            depth += 1
        elif ch in ">)]":
            if depth > 0:
                depth -= 1
        i += 1
    return -1


def _owner_of(blocks: list[_Block], offset: int) -> str:
    """Innermost owner name whose brace span contains *offset*, or ``""``."""
    best: _Block | None = None
    for b in blocks:
        if b.start < offset < b.end:
            if best is None or b.start > best.start:  # innermost = latest start
                best = b
    return best.name if best else ""


class RustParser:
    """Extract functions & methods from Rust source (heuristic, dependency-free)."""

    lang = "rust"

    def parse(self, source: str, rel_path: str) -> list[FunctionRecord]:
        """Parse *source* into records, returning ``[]`` on any failure."""
        try:
            return self._parse(source, rel_path)
        except Exception:  # pragma: no cover - defensive; never abort an index
            return []

    # -- internals --------------------------------------------------------

    def _parse(self, source: str, rel_path: str) -> list[FunctionRecord]:
        code = _blank_noncode(source)
        blocks = _owner_blocks(code)
        records: list[FunctionRecord] = []

        for m in _FUNC.finditer(code):
            name = m.group("name")
            open_ch = m.group("open")
            open_idx = m.start("open")

            # Skip a generic type-param list `<...>` before the real params.
            if open_ch == "<":
                after = _match_bracket(code, open_idx, "<", ">")
                if after == -1:
                    continue
                j = after
                while j < len(code) and code[j] in " \t\r\n":
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
            sig_parts = [params]
            span = _return_span(code, end)
            if span is not None:
                ret = _normalize(source[span[0] : span[1]])
                if ret:
                    sig_parts.append(f"-> {ret}")
            signature = " ".join(sig_parts)

            owner = _owner_of(blocks, m.start())
            qualname = f"{owner}.{name}" if owner else name

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
    def _doc_comment(source: str, fn_off: int) -> str:
        """First line of the ``///`` / ``//!`` doc block directly above *fn_off*.

        Walks backwards over the line containing ``fn`` to the preceding lines; a
        contiguous run of ``///`` or ``//!`` comments (no blank line between them
        and the fn) is the doc block. Attribute lines (``#[...]``) between the
        doc block and the ``fn`` are skipped so ``/// doc`` above ``#[inline]``
        still attaches. Returns the first doc line's text (marker + one leading
        space stripped), or ``""``.
        """
        line_start = source.rfind("\n", 0, fn_off) + 1
        doc_lines: list[str] = []
        pos = line_start
        seen_doc = False
        while pos > 0:
            prev_end = pos - 1  # the '\n' before this line
            prev_start = source.rfind("\n", 0, prev_end) + 1
            text = source[prev_start:prev_end]
            stripped = text.strip()
            if stripped.startswith("///") or stripped.startswith("//!"):
                doc_lines.append(stripped[3:].strip())
                seen_doc = True
                pos = prev_start
                continue
            # allow attributes between the doc block and the fn (only before we
            # have started collecting doc lines)
            if not seen_doc and (stripped.startswith("#[") or stripped.startswith("#![")):
                pos = prev_start
                continue
            break
        if doc_lines:
            # doc_lines is bottom-up; the *first* doc line is the last we saw.
            return doc_lines[-1]
        return ""
