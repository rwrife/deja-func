"""Tests for the Rust parser (issue #40).

Covers free functions (``fn`` / ``pub fn`` / ``async fn`` / generic ``fn``),
associated functions and methods inside ``impl`` blocks (owner folded into the
qualified name), trait method signatures (defaulted and bodyless), doc-comment
capture (``///`` and ``//!``, plus the no-doc case), return-type capture,
registry dispatch by ``.rs`` extension, and the graceful-skip contract (no
crashes, no false positives on ``fn`` inside strings/comments).
"""

from __future__ import annotations

from deja.parsers import RustParser, get_parser_for_path
from deja.parsers.base import FunctionRecord

RUST_SAMPLE = """\
//! Crate-level inner doc.

use std::fmt;

/// Slugify turns a string into a URL-safe slug.
/// Second line of the doc comment.
pub fn slugify(text: &str, sep: char) -> String {
    let decoy = "fn fake_in_string() {}";
    String::new()
}

async fn fetch(url: &str) -> Result<String, Error> {
    Ok(String::new())
}

const fn add<T>(a: T, b: T) -> T {
    a
}

fn no_doc() {}

struct Repo;

impl Repo {
    /// Get fetches a row by id.
    pub fn get(&self, id: u64) -> Option<Row> {
        None
    }

    fn helper() {}
}

impl fmt::Display for Repo {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
        Ok(())
    }
}

trait Store {
    /// Put stores a value.
    fn put(&mut self, k: String, v: String);

    fn size(&self) -> usize {
        0
    }
}

// decoy inside a comment: fn fake_two() {}
fn real_after_decoy() {
    call_something();
}
"""


def _by_qual(records: list[FunctionRecord]) -> dict[str, FunctionRecord]:
    return {r.qualname: r for r in records}


def _parse() -> list[FunctionRecord]:
    return RustParser().parse(RUST_SAMPLE, "src/lib.rs")


def test_registry_dispatches_rs_extension() -> None:
    assert isinstance(get_parser_for_path("src/lib.rs"), RustParser)
    assert isinstance(get_parser_for_path("SRC/MAIN.RS"), RustParser)


def test_all_functions_are_found() -> None:
    quals = {r.qualname for r in _parse()}
    assert quals == {
        "slugify",
        "fetch",
        "add",
        "no_doc",
        "Repo.get",
        "Repo.helper",
        "Repo.fmt",
        "Store.put",
        "Store.size",
        "real_after_decoy",
    }


def test_free_function_signature_and_lang() -> None:
    rec = _by_qual(_parse())["slugify"]
    assert rec.name == "slugify"
    assert rec.lang == "rust"
    assert rec.file == "src/lib.rs"
    assert rec.signature == "(text: &str, sep: char) -> String"


def test_async_and_generic_functions() -> None:
    by = _by_qual(_parse())
    assert by["fetch"].signature == "(url: &str) -> Result<String, Error>"
    # generic type params are skipped; params + return captured.
    assert by["add"].signature == "(a: T, b: T) -> T"


def test_impl_methods_carry_owner_in_qualname() -> None:
    by = _by_qual(_parse())
    assert by["Repo.get"].name == "get"
    assert by["Repo.get"].signature == "(&self, id: u64) -> Option<Row>"
    assert by["Repo.helper"].signature == "()"
    # `impl Trait for Type` folds the *Type* (Repo), not the trait.
    assert by["Repo.fmt"].name == "fmt"


def test_trait_method_signatures() -> None:
    by = _by_qual(_parse())
    # bodyless (required) method
    assert by["Store.put"].signature == "(&mut self, k: String, v: String)"
    # defaulted method
    assert by["Store.size"].signature == "(&self) -> usize"


def test_doc_comments_captured_first_line_only() -> None:
    by = _by_qual(_parse())
    assert by["slugify"].docstring == "Slugify turns a string into a URL-safe slug."
    assert by["Repo.get"].docstring == "Get fetches a row by id."
    assert by["Store.put"].docstring == "Put stores a value."
    assert by["no_doc"].docstring == ""
    assert by["fetch"].docstring == ""


def test_no_false_positives_from_strings_or_comments() -> None:
    quals = {r.qualname for r in _parse()}
    assert "fake_in_string" not in quals
    assert "fake_two" not in quals
    assert "real_after_decoy" in quals


def test_records_sorted_by_line() -> None:
    lines = [r.line for r in _parse()]
    assert lines == sorted(lines)


def test_bad_input_returns_empty_not_raises() -> None:
    # Wildly unbalanced source must not raise — parser returns what it can.
    assert RustParser().parse("fn broken(((", "x.rs") == []
    assert RustParser().parse("", "x.rs") == []


def test_lifetime_not_mistaken_for_char_literal() -> None:
    src = "fn borrow<'a>(x: &'a str) -> &'a str { x }"
    recs = RustParser().parse(src, "x.rs")
    assert len(recs) == 1
    assert recs[0].name == "borrow"
    assert recs[0].signature == "(x: &'a str) -> &'a str"
