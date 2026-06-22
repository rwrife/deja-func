"""Tests for signature-shape parsing & scoring (deja/sigshape.py, M4)."""

from __future__ import annotations

import pytest

from deja.sigshape import (
    SignatureShape,
    looks_like_signature,
    parse_signature,
    shape_score,
)

# -- parse_signature -------------------------------------------------------


def test_parse_terse_query_shape() -> None:
    shape = parse_signature("(str)->bool", query=True)
    assert shape.params == ("str",)
    assert shape.returns == "bool"
    assert shape.has_return_hint is True
    assert shape.arity == 1


def test_parse_rich_stored_signature_drops_names_and_defaults() -> None:
    shape = parse_signature("(text: str, n: int = 0) -> str")
    assert shape.params == ("str", "int")
    assert shape.returns == "str"
    assert shape.arity == 2


def test_parse_unannotated_params_become_any() -> None:
    # Stored-signature semantics: a bare name with no annotation is type-unknown.
    shape = parse_signature("(a, b)")
    assert shape.params == ("any", "any")
    assert shape.has_return_hint is False
    assert shape.returns == "any"


def test_parse_query_bare_tokens_are_types() -> None:
    # Query semantics: terse bare tokens ARE the types.
    shape = parse_signature("(str, int)", query=True)
    assert shape.params == ("str", "int")


def test_parse_drops_self_and_cls() -> None:
    assert parse_signature("(self, x: int)").params == ("int",)
    assert parse_signature("(cls, x: int)").params == ("int",)


def test_parse_variadics_become_wildcard() -> None:
    shape = parse_signature("(*args, **kwargs)")
    assert shape.params == ("*", "*")


def test_parse_nested_brackets_not_split_on_inner_comma() -> None:
    shape = parse_signature("(d: dict[str, int], items: list[str]) -> None")
    # dict[str, int] is ONE param (head token 'dict'), not two.
    assert shape.params == ("dict", "list")
    assert shape.returns == "none"


def test_parse_normalizes_dotted_and_aliased_types() -> None:
    shape = parse_signature("(s: typing.List[int], flag: builtins.bool) -> NoneType")
    assert shape.params == ("list", "bool")
    assert shape.returns == "none"


def test_parse_bare_comma_list_without_parens() -> None:
    # Tolerate a user typing just "str, int" (query mode -> types).
    shape = parse_signature("str, int", query=True)
    assert shape.params == ("str", "int")


def test_parse_optional_subscript_collapses_to_head() -> None:
    shape = parse_signature("(x: Optional[int]) -> bool")
    assert shape.params == ("optional",)
    assert shape.returns == "bool"


def test_parse_empty_is_empty_shape() -> None:
    shape = parse_signature("()")
    assert shape.params == ()
    assert shape.has_return_hint is False


def test_parse_none_input_does_not_raise() -> None:
    assert parse_signature(None).params == ()


# -- looks_like_signature --------------------------------------------------


@pytest.mark.parametrize(
    "q",
    ["(str)->bool", "(int, int)", "() -> None", "validate(str)"],
)
def test_looks_like_signature_true(q: str) -> None:
    assert looks_like_signature(q) is True


@pytest.mark.parametrize("q", ["slugify", "parse an iso date", "url safe slug"])
def test_looks_like_signature_false(q: str) -> None:
    assert looks_like_signature(q) is False


# -- shape_score -----------------------------------------------------------


def test_shape_score_exact_match_is_high() -> None:
    q = parse_signature("(str)->bool", query=True)
    c = parse_signature("(text: str) -> bool")
    assert shape_score(q, c) == 100.0


def test_shape_score_wrong_arity_lower_than_exact() -> None:
    q = parse_signature("(str)->bool", query=True)
    exact = shape_score(q, parse_signature("(s: str) -> bool"))
    one_extra = shape_score(q, parse_signature("(s: str, n: int) -> bool"))
    assert one_extra < exact


def test_shape_score_wrong_types_lower_than_right() -> None:
    q = parse_signature("(int, int)", query=True)
    right = shape_score(q, parse_signature("(a: int, b: int)"))
    wrong = shape_score(q, parse_signature("(a: str, b: str)"))
    assert wrong < right


def test_shape_score_variadic_absorbs_extra_args() -> None:
    q = parse_signature("(str, str, str)", query=True)
    variadic = shape_score(q, parse_signature("(*args)"))
    # A variadic candidate should score better than a rigid 1-arg mismatch.
    rigid = shape_score(q, parse_signature("(x: int)"))
    assert variadic > rigid


def test_shape_score_return_hint_matters() -> None:
    q = parse_signature("(str)->bool", query=True)
    matches_return = shape_score(q, parse_signature("(s: str) -> bool"))
    wrong_return = shape_score(q, parse_signature("(s: str) -> str"))
    assert wrong_return < matches_return


def test_shape_score_empty_query_is_zero() -> None:
    # No params and no return hint = nothing to match on.
    assert shape_score(SignatureShape(), parse_signature("(s: str) -> bool")) == 0.0


def test_shape_score_in_range() -> None:
    q = parse_signature("(str, int) -> bool", query=True)
    s = shape_score(q, parse_signature("(a: str, b: int) -> bool"))
    assert 0.0 <= s <= 100.0
