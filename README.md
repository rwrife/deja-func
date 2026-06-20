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
`.dejafunc/index.json`. `deja find` is next.

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

## Quickstart (planned)

```bash
pipx install deja-func        # or: uv tool install deja-func
deja index                    # build .dejafunc/index.json from this repo
deja find "validate email"    # → existing functions that already do it (M3)
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
- **M3** `deja find` fuzzy search
- **M4** signature-shape & intent search
- **M5** JS/TS support
- **M6** MCP server + JSON output for agents

Full plan, backlog, and what's explicitly out of scope: [`PLAN.md`](./PLAN.md).

## License

MIT
