"""Tests for the Go parser (issue #34).

Covers plain functions, methods with pointer/value receivers, generic
functions, doc-comment capture (and the no-doc case), result signatures
(single, tuple, none), registry dispatch by ``.go`` extension, and the
graceful-skip contract (no crashes, no false positives on calls/keywords/
strings/comments).
"""

from __future__ import annotations

from deja.parsers import GoParser, get_parser_for_path
from deja.parsers.base import FunctionRecord

GO_SAMPLE = """\
package main

import "fmt"

// Slugify turns a string into a URL-safe slug.
// Second line of the doc comment.
func Slugify(text string, sep string) string {
	return text
}

func add(a, b int) int {
	return a + b
}

// Get fetches a row by id.
func (r *Repo) Get(id string) (*Row, error) {
	return nil, nil
}

func (r Repo) Count() int {
	return 0
}

// Map applies f to every element.
func Map[T any](s []T, f func(T) T) []T {
	return s
}

func noDoc() {}

// decoy inside a string must be ignored: "func FakeOne() {}"
// and a comment: func FakeTwo() {}
func realAfterDecoys() {
	callSomething()
	if ready {
		doThing()
	}
}
"""


def _by_name(records: list[FunctionRecord]) -> dict[str, FunctionRecord]:
    return {r.name: r for r in records}


def test_dispatch_by_extension() -> None:
    assert isinstance(get_parser_for_path("main.go"), GoParser)
    assert isinstance(get_parser_for_path("PKG/Thing.GO"), GoParser)


def test_plain_function() -> None:
    recs = _by_name(GoParser().parse(GO_SAMPLE, "main.go"))
    assert "add" in recs
    add = recs["add"]
    assert add.lang == "go"
    assert add.file == "main.go"
    assert add.qualname == "add"
    assert add.signature == "(a, b int) int"


def test_doc_comment_first_line() -> None:
    recs = _by_name(GoParser().parse(GO_SAMPLE, "main.go"))
    assert recs["Slugify"].docstring == "Slugify turns a string into a URL-safe slug."
    assert recs["Slugify"].signature == "(text string, sep string) string"


def test_no_doc_is_empty() -> None:
    recs = _by_name(GoParser().parse(GO_SAMPLE, "main.go"))
    assert recs["noDoc"].docstring == ""


def test_pointer_receiver_method() -> None:
    recs = _by_name(GoParser().parse(GO_SAMPLE, "main.go"))
    get = recs["Get"]
    assert get.qualname == "Repo.Get"
    assert get.docstring == "Get fetches a row by id."
    assert "(r *Repo)" in get.signature
    assert "(*Row, error)" in get.signature


def test_value_receiver_method() -> None:
    recs = _by_name(GoParser().parse(GO_SAMPLE, "main.go"))
    count = recs["Count"]
    assert count.qualname == "Repo.Count"
    assert count.signature == "(r Repo) () int"


def test_generic_function() -> None:
    recs = _by_name(GoParser().parse(GO_SAMPLE, "main.go"))
    mp = recs["Map"]
    assert mp.qualname == "Map"
    assert mp.signature == "(s []T, f func(T) T) []T"
    assert mp.docstring == "Map applies f to every element."


def test_no_false_positives_from_strings_or_calls() -> None:
    names = {r.name for r in GoParser().parse(GO_SAMPLE, "main.go")}
    assert "FakeOne" not in names
    assert "FakeTwo" not in names
    assert "callSomething" not in names
    assert "doThing" not in names
    assert "realAfterDecoys" in names


def test_bad_source_returns_empty_not_raises() -> None:
    # Unterminated everything; must not raise.
    assert GoParser().parse('func broken( "unterminated', "x.go") == []
    assert GoParser().parse("", "x.go") == []


def test_line_numbers() -> None:
    recs = _by_name(GoParser().parse(GO_SAMPLE, "main.go"))
    # Slugify's `func` is on line 7 (doc comments start line 5).
    assert recs["Slugify"].line == 7
