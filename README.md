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

🚧 Early days — see [`PLAN.md`](./PLAN.md) for the roadmap. v0.1 targets Python.

**M1 shipped:** the CLI installs and runs (`deja --version`, `deja hello`).
**M2 shipped:** `deja index` walks the repo, parses every Python function/method
with a stdlib `ast` parser (honoring `.gitignore`), and writes a JSON index to
`.dejafunc/index.json`.
**M3 shipped:** `deja find <query>` fuzzy-searches that index by name + docstring
and prints ranked matches with `file:line`.
**M4 shipped:** `deja find` now also searches by **signature shape**
(`--sig "(str)->bool"`), weights docstrings for natural-language **intent**
queries (`--intent`), and can **explain** why each result matched (`--explain`).

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

`deja index` recursively scans for supported source files (Python today),
extracts each function/method's name, signature, docstring, and `file:line`, and
saves them to a small, diffable JSON index. It respects your `.gitignore` and
skips noise dirs (`.venv`, `node_modules`, `__pycache__`, …).

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
- **M5** JS/TS support
- **M6** MCP server + JSON output for agents

Full plan, backlog, and what's explicitly out of scope: [`PLAN.md`](./PLAN.md).

## License

MIT
