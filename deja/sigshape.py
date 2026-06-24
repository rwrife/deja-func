"""Signature-shape parsing & comparison for `deja find --sig` (M4, PLAN.md §7).

M3 searches by *name* and *docstring*. But sometimes you don't know the name —
you just know the **shape**: "something that takes a string and returns a bool".
This module turns both sides of that question into a comparable :class:`SignatureShape`:

* the **query** shape the caller types, e.g. ``(str) -> bool`` or ``(int, int)``; and
* the **stored** shape we reconstruct from a :class:`~deja.parsers.base.FunctionRecord`'s
  ``signature`` string (e.g. ``(text: str, n: int = 0) -> str``).

A shape is deliberately coarse — a list of normalized parameter *type tokens* plus a
return-type token — because the point is fuzzy "does a function of roughly this shape
exist?", not exact type-checking (that's explicitly out of scope, PLAN.md §9). Parameter
*names*, defaults, and decorators are dropped; only the type silhouette survives.

Kept separate from :mod:`deja.search` so the (fiddly) parsing stays pure and unit-testable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Parameter markers that carry no useful *type* signal and are ignored entirely
#: when extracting a shape (``self``/``cls`` receivers, ``*``/``/`` separators).
_IGNORED_PARAMS = frozenset({"self", "cls", "*", "/"})

#: A bare ``*args`` / ``**kwargs`` normalizes to this wildcard token so it can
#: match anything positionally without pretending to know a concrete type.
_VARIADIC = "*"

#: Token used when a parameter or return has no annotation at all.
_ANY = "any"

# Common aliases collapsed so e.g. ``str`` and ``builtins.str`` (or ``typing.List``
# vs ``list``) compare equal. Lowercased, dotted prefixes already stripped.
_TYPE_ALIASES = {
    "boolean": "bool",
    "integer": "int",
    "string": "str",
    "none": "none",
    "nonetype": "none",
    "list": "list",
    "dict": "dict",
    "tuple": "tuple",
    "set": "set",
    "any": _ANY,
    "object": _ANY,
}


@dataclass(frozen=True, slots=True)
class SignatureShape:
    """The coarse type silhouette of a function signature.

    Attributes:
        params: Normalized parameter type tokens, in order. ``self``/``cls`` and
            ``*``/``/`` separators are dropped; unannotated params become
            ``"any"``; ``*args``/``**kwargs`` become ``"*"``.
        returns: Normalized return-type token (``"any"`` when unannotated,
            ``"none"`` for ``None``).
        has_return_hint: Whether the source actually declared a return type
            (lets scoring avoid rewarding an inferred ``"any"`` match).
    """

    params: tuple[str, ...] = field(default_factory=tuple)
    returns: str = _ANY
    has_return_hint: bool = False

    @property
    def arity(self) -> int:
        """Number of positional/keyword params (variadics count as one slot)."""
        return len(self.params)


def _normalize_type(token: str) -> str:
    """Collapse a raw type expression to a coarse comparable token.

    Strips dotted prefixes (``typing.List`` -> ``list``), subscripts
    (``list[int]`` -> ``list``), optional/union noise down to a head token, and
    applies a small alias table. Anything we can't simplify is lowercased as-is.
    """
    t = token.strip()
    if not t:
        return _ANY
    # Drop a subscript: ``Optional[int]`` / ``list[str]`` -> head only.
    t = t.split("[", 1)[0].strip()
    # ``a.b.C`` -> ``C`` (module / package qualifiers are noise for shape).
    if "." in t:
        t = t.rsplit(".", 1)[-1].strip()
    # Strip surrounding quotes from forward-reference annotations ("Foo").
    t = t.strip("\"'")
    key = t.lower()
    return _TYPE_ALIASES.get(key, key or _ANY)


def _split_top_level(s: str) -> list[str]:
    """Split *s* on commas that are not nested inside brackets/parens.

    So ``int, dict[str, int], bool`` -> ``["int", "dict[str, int]", "bool"]``
    rather than naively splitting the dict's inner comma.
    """
    parts: list[str] = []
    depth = 0
    buf: list[str] = []
    for ch in s:
        if ch in "[({":
            depth += 1
        elif ch in "])}":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _param_type(raw: str, *, query: bool) -> str | None:
    """Extract the normalized type token from one raw parameter, or ``None`` to skip.

    Two grammars share this code:

    * **stored** signatures (``query=False``) use ``name: T = default`` — a bare
      ``name`` with no annotation carries no type, so it yields ``"any"``.
    * **query** shapes (``query=True``) are terse: ``(str)`` means "a parameter
      *of type* str", so a colon-less bare token is read as the **type** itself.

    Both forms handle ``name: T`` identically, and ``*args`` / ``**kwargs`` ->
    ``"*"``; ``self``/``cls``/``*``/``/`` are ignored in either mode.
    """
    p = raw.strip()
    if not p or p in _IGNORED_PARAMS:
        return None
    # Variadics collapse to a wildcard regardless of any annotation.
    if p.startswith("*"):
        return _VARIADIC
    # Strip a default value first so ``=`` inside it never confuses the parse.
    if "=" in p:
        p = p.split("=", 1)[0].strip()
    name_part, sep, annotation = p.partition(":")
    if name_part.strip() in _IGNORED_PARAMS:
        return None
    if annotation.strip():
        return _normalize_type(annotation)
    # No annotation: in a query the lone token *is* the type; in a stored
    # signature it's just an unannotated parameter name (type unknown).
    if query and not sep:
        return _normalize_type(name_part)
    return _ANY


def parse_signature(sig: str, *, query: bool = False) -> SignatureShape:
    """Parse a function-signature string into a :class:`SignatureShape`.

    Accepts both the rich form stored on records (``(text: str, n: int = 0) -> str``)
    and the terse form a user types as a query (``(str)->bool``, ``(int, int)``).
    Robust to missing parens, missing return, and extra whitespace; never raises
    on odd input (returns an empty-ish shape instead).

    Args:
        sig: The signature text.
        query: Set true when *sig* is a user-typed query shape, so colon-less
            tokens are read as *types* (``(str)`` -> a ``str`` param) rather than
            as unannotated parameter names.
    """
    if sig is None:
        return SignatureShape()
    s = sig.strip()

    # Pull the return annotation off the end, if present (``-> T``).
    returns = _ANY
    has_return = False
    arrow = s.rfind("->")
    if arrow != -1:
        ret_raw = s[arrow + 2 :].strip()
        s = s[:arrow].strip()
        if ret_raw:
            returns = _normalize_type(ret_raw)
            has_return = True

    # Grab the inside of the parameter parens; tolerate a bare ``str, int`` too.
    m = re.search(r"\((.*)\)", s, re.DOTALL)
    inner = m.group(1) if m else s

    params: list[str] = []
    for raw in _split_top_level(inner):
        t = _param_type(raw, query=query)
        if t is not None:
            params.append(t)

    return SignatureShape(params=tuple(params), returns=returns, has_return_hint=has_return)


def looks_like_signature(query: str) -> bool:
    """Heuristic: does *query* look like a signature shape rather than prose?

    Used so a plain ``deja find`` query isn't accidentally treated as a sig. A
    parenthesized group, or an explicit ``->`` arrow, is the tell.
    """
    q = query.strip()
    if "->" in q:
        return True
    return bool(re.search(r"\(.*\)", q, re.DOTALL))


def _types_match(a: str, b: str) -> float:
    """Per-slot type agreement in ``[0, 1]``.

    Exact token match scores 1.0; a wildcard (``*``) or an unknown ``any`` on
    either side is a soft partial credit (0.5) since it *could* match; otherwise 0.
    """
    if a == b:
        return 1.0
    if _VARIADIC in (a, b) or _ANY in (a, b):
        return 0.5
    return 0.0


def shape_score(query: SignatureShape, candidate: SignatureShape) -> float:
    """Score how well *candidate*'s shape satisfies the *query* shape (0-100).

    Two components, averaged:

    * **arity** — do the parameter counts line up? Exact match is full marks; off
      by a little degrades gracefully (a variadic in the candidate makes any
      higher arity acceptable).
    * **types** — element-wise type agreement over the overlapping params, plus the
      return type when the query specified one.

    A query with no params and no return hint is meaningless as a shape and scores 0.
    """
    if not query.params and not query.has_return_hint:
        return 0.0

    # --- arity component -------------------------------------------------
    q_n, c_n = query.arity, candidate.arity
    candidate_variadic = _VARIADIC in candidate.params
    if q_n == c_n:
        arity_score = 1.0
    elif candidate_variadic and q_n >= c_n - 1:
        # ``*args`` can absorb extra positional args from the query.
        arity_score = 0.9
    else:
        # Linear falloff by the size of the mismatch.
        spread = max(q_n, c_n, 1)
        arity_score = max(0.0, 1.0 - abs(q_n - c_n) / spread)

    # --- type component --------------------------------------------------
    type_scores: list[float] = []
    for i in range(min(q_n, c_n)):
        type_scores.append(_types_match(query.params[i], candidate.params[i]))
    # Unmatched query params (candidate too short, no variadic) count against us.
    if q_n > c_n and not candidate_variadic:
        type_scores.extend(0.0 for _ in range(q_n - c_n))

    if query.has_return_hint:
        type_scores.append(_types_match(query.returns, candidate.returns))

    type_score = sum(type_scores) / len(type_scores) if type_scores else arity_score

    return round((0.5 * arity_score + 0.5 * type_score) * 100.0, 2)
