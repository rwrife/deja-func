"""Tests for the JavaScript / TypeScript parser (M5).

Covers function declarations, arrow functions bound to consts, class & object
methods, TypeScript annotations, registry dispatch by extension, and the
graceful-skip contract (no crashes, no false positives on calls/keywords).
"""

from __future__ import annotations

from deja.parsers import JavaScriptParser, get_parser_for_path
from deja.parsers.base import FunctionRecord

JS_SAMPLE = """
// a line comment with function decoy(): not real
import { thing } from "./mod";

function slugify(text, sep = "-") {
  return text.toLowerCase().split(" ").join(sep);
}

async function fetchUser(id) {
  return await db.get(id);
}

function* counter(start) {
  yield start;
}

const add = (a, b) => a + b;

const double = x => x * 2;

const greet = async (name) => {
  return `hi ${name}`;
};

// A call that must NOT be picked up as a definition:
slugify("Hello World");
if (ready) { doThing(); }

const obj = {
  load(path) {
    return read(path);
  },
  async save(path, data) {
    return write(path, data);
  },
};

class Greeter {
  constructor(name) {
    this.name = name;
  }

  greet() {
    return `hi ${this.name}`;
  }

  static shout(msg) {
    return msg.toUpperCase();
  }

  get label() {
    return this.name;
  }
}
"""

TS_SAMPLE = """
interface User { id: number; name: string; }

export function validate(email: string): boolean {
  return email.includes("@");
}

export const toUser = (id: number, name: string): User => ({ id, name });

function identity<T>(value: T): T {
  return value;
}

class Repo<T> {
  private items: T[] = [];

  add(item: T): void {
    this.items.push(item);
  }

  async findAll(): Promise<T[]> {
    return this.items;
  }
}

// Decoy: a generic comparison, not a function.
const cmp = a < b && c > d;
"""


def _parse(src: str, path: str) -> list[FunctionRecord]:
    return JavaScriptParser().parse(src, path)


# -- registry --------------------------------------------------------------


def test_registry_dispatches_js_family_extensions() -> None:
    for ext in (".js", ".jsx", ".ts", ".tsx"):
        parser = get_parser_for_path(f"foo{ext}")
        assert parser is not None
        assert parser.lang == "javascript"


def test_registry_ignores_unknown_extension() -> None:
    assert get_parser_for_path("foo.rb") is None


# -- JavaScript ------------------------------------------------------------


def test_extracts_function_declarations() -> None:
    names = {r.name for r in _parse(JS_SAMPLE, "sample.js")}
    assert {"slugify", "fetchUser", "counter"} <= names


def test_function_signature_with_default() -> None:
    rec = next(r for r in _parse(JS_SAMPLE, "sample.js") if r.name == "slugify")
    assert rec.signature == '(text, sep = "-")'
    assert rec.lang == "javascript"
    assert rec.file == "sample.js"
    assert rec.line > 0


def test_async_and_generator_functions() -> None:
    recs = {r.name: r for r in _parse(JS_SAMPLE, "sample.js")}
    assert recs["fetchUser"].signature == "(id)"
    assert recs["counter"].signature == "(start)"


def test_arrow_const_parenthesized() -> None:
    rec = next(r for r in _parse(JS_SAMPLE, "sample.js") if r.name == "add")
    assert rec.signature == "(a, b)"


def test_arrow_const_bare_param() -> None:
    rec = next(r for r in _parse(JS_SAMPLE, "sample.js") if r.name == "double")
    assert rec.signature == "(x)"


def test_arrow_const_async() -> None:
    rec = next(r for r in _parse(JS_SAMPLE, "sample.js") if r.name == "greet")
    assert rec.signature == "(name)"


def test_object_methods_extracted() -> None:
    recs = {r.name: r for r in _parse(JS_SAMPLE, "sample.js")}
    assert recs["load"].signature == "(path)"
    assert recs["save"].signature == "(path, data)"


def test_class_methods_and_accessors() -> None:
    names = {r.name for r in _parse(JS_SAMPLE, "sample.js")}
    assert {"constructor", "greet", "shout", "label"} <= names


def test_calls_and_keywords_are_not_functions() -> None:
    names = {r.name for r in _parse(JS_SAMPLE, "sample.js")}
    # The bare `slugify("Hello World")` call and the `if (...)` must not appear
    # as separate definitions; `if`/`doThing` calls are never records.
    assert "if" not in names
    assert "doThing" not in names
    # `decoy` only appears inside a comment, so it must be invisible.
    assert "decoy" not in names


def test_comment_decoy_ignored() -> None:
    src = "// function ghost() {}\nconst real = () => 1;\n"
    names = {r.name for r in _parse(src, "x.js")}
    assert names == {"real"}


def test_string_with_braces_does_not_break_scan() -> None:
    src = 'const f = (s) => { return "a) { fake"; };\nfunction g(x) { return x; }\n'
    recs = {r.name: r for r in _parse(src, "x.js")}
    assert recs["f"].signature == "(s)"
    assert recs["g"].signature == "(x)"


# -- TypeScript ------------------------------------------------------------


def test_ts_function_with_annotations_and_return_type() -> None:
    rec = next(r for r in _parse(TS_SAMPLE, "sample.ts") if r.name == "validate")
    assert rec.signature == "(email: string): boolean"


def test_ts_arrow_with_return_type() -> None:
    rec = next(r for r in _parse(TS_SAMPLE, "sample.ts") if r.name == "toUser")
    assert rec.signature == "(id: number, name: string): User"


def test_ts_generic_function() -> None:
    rec = next(r for r in _parse(TS_SAMPLE, "sample.ts") if r.name == "identity")
    # Generic <T> before params is skipped; params + return type are kept.
    assert rec.signature == "(value: T): T"


def test_ts_class_methods_with_types() -> None:
    recs = {r.name: r for r in _parse(TS_SAMPLE, "sample.ts")}
    assert recs["add"].signature == "(item: T): void"
    assert recs["findAll"].signature == "(): Promise<T[]>"


def test_ts_generic_comparison_not_a_function() -> None:
    names = {r.name for r in _parse(TS_SAMPLE, "sample.ts")}
    assert "cmp" not in names  # `const cmp = a < b && c > d` is not a function


# -- robustness ------------------------------------------------------------


def test_empty_and_garbage_sources_return_list() -> None:
    assert _parse("", "x.js") == []
    assert _parse("@@@ not real ((( js", "x.js") == []


def test_records_are_sorted_by_line() -> None:
    recs = _parse(JS_SAMPLE, "sample.js")
    lines = [r.line for r in recs]
    assert lines == sorted(lines)
