# deja-func 🫠

> *"Wait... haven't I written this before?"*

A tiny local CLI that indexes every function in your codebase and answers one question fast:
**"Have I already written something that does this?"**

Search by name, by fuzzy intent (`"parse an ISO date"`), or by signature shape — and get back the
matching functions with `file:line`, so you (and your AI coding agents) stop reinventing `slugify`
for the fourth time. No server, no cloud, no LLM required. It's grep that understands functions.

## Why

AI coding agents are notorious for ignoring code that already exists and writing redundant
near-duplicates — "the agent has no memory of the module you refactored." `deja-func` is the
**pre-flight** check: ask *before* you write, not in PR review.

## Status

🚧 Early days — see [`PLAN.md`](./PLAN.md) for the roadmap. v0.1 targets Python,
with JavaScript/TypeScript now indexed too.

**M1 shipped:** the CLI installs and runs (`deja --version`, `deja hello`).
**M2 shipped:** `deja index` walks the repo, parses every Python function/method
with a stdlib `ast` parser (honoring `.gitignore`), and writes a JSON index to
`.dejafunc/index.json`.
**M3 shipped:** `deja find <query>` fuzzy-searches that index by name + docstring
and prints ranked matches with `file:line`.
**M4 shipped:** `deja find` now also searches by **signature shape**
(`--sig "(str)->bool"`), weights docstrings for natural-language **intent**
queries (`--intent`), and can **explain** why each result matched (`--explain`).
**M5 shipped:** indexing is now **multi-language** — a tree-light, dependency-free
parser picks up **JavaScript/TypeScript** (`.js`, `.jsx`, `.ts`, `.tsx`)
functions, arrow consts, and class/object methods alongside Python.

## Install (from source)

```bash
pipx install .                # or: uv tool install .
deja --version                # deja-func 0.1.0
deja hello                    # 🫠 say hi
```

## Usage

```bash
deja index                    # walk this repo, write .dejafunc/index.json
deja index path/to/project    # index a specific directory
# → 🧠 Indexed 412 functions → .dejafunc/index.json
```

`deja index` recursively scans for supported source files (Python and
JavaScript/TypeScript today), extracts each function/method's name, signature,
docstring, and `file:line`, and saves them to a small, diffable JSON index. It
respects your `.gitignore` and skips noise dirs (`.venv`, `node_modules`,
`__pycache__`, …).

### Multi-language: JavaScript & TypeScript (M5)

Indexing isn't Python-only. `.js`, `.jsx`, `.ts`, and `.tsx` files are parsed by
a small, **dependency-free** scanner (no Babel/tree-sitter) that recognizes:

- `function foo(...)`, `async function`, and generators (`function* gen`)
- arrow functions bound to a name: `const add = (a, b) => ...` (incl. `x => x`)
- class & object methods, plus `get`/`set` accessors

TypeScript parameter annotations and return types are preserved in the
signature, e.g. `(email: string): boolean`. Adding a language is one parser
module + one registry entry, so more are easy to slot in later. The scan is
best-effort: anything it can't make sense of in a file is skipped rather than
raising, so one odd file can never abort the whole index.

```bash
deja find "validate email"    # fuzzy-search the index by name + docstring
deja find slugify -n 5        # cap results (default: 10)
deja find "parse iso date" path/to/project
# → 🫠 You already wrote this:
#     parse_iso_date — dates.py:42 — (s: str) -> datetime
#         Parse an ISO-8601 timestamp into a datetime.
```

`deja find` ranks functions with `rapidfuzz` over both the **name/qualname**
(catches "I'm about to write `slugify`") and the **docstring** (catches intent
queries like `"parse an ISO date"`). It auto-builds the index on first run if one
doesn't exist yet, and exits `0` when matches are found / non-zero when none are —
so it's easy to script.

### Search by signature shape, intent, or both (M4)

Don't know the name? Search by **shape** — argument types and return type:

```bash
deja find --sig "(str)->bool"          # functions taking a str, returning a bool
deja find --sig "(int, int)"           # any two-int function (return type ignored)
deja find --sig "(str)->bool" ./proj   # ...in a specific project
# → 🫠 You already wrote this:
#     is_valid — validate.py:7 — (s: str) -> bool
```

Shapes are matched coarsely (arg count + normalized type tokens + return type),
so `(str)` matches `(text: str)`, `list[int]` collapses to `list`, `*args`
becomes a wildcard, and `self`/`cls` are ignored. It's "roughly this shape?", not
type-checking.

Weight the **docstring** higher for plain-English intent queries:

```bash
deja find "turn a title into a url slug" --intent
```

**Blend** text and shape, and ask *why* each result matched:

```bash
deja find "validate" --sig "(str)->bool" --explain
# → 🫠 You already wrote this:
#     validate_email — util.py:1 — (addr: str) -> bool
#         Validate an email address and return True if it looks valid.
#         score 100  (name 100 · sig 100 · doc 88)
```

## Develop

```bash
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"
ruff check . && ruff format --check .
pytest -q
```

## Roadmap (short)

- **M1** scaffold + hello-world
- **M2** Python parser + `deja index`
- **M3** `deja find` fuzzy search ✅
- **M4** signature-shape & intent search ✅
- **M5** JS/TS support ✅
- **M6** MCP server + JSON output for agents

Full plan, backlog, and what's explicitly out of scope: [`PLAN.md`](./PLAN.md).

## License

MIT
