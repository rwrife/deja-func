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
**M6 shipped:** machine-readable output (`deja find --json`) and `deja mcp`, a
zero-dependency **stdio MCP server** exposing a `find_function` tool, so AI
agents can query the inventory *before* writing code. See
[Use with AI agents](#use-with-ai-agents).
**`deja dupes` shipped:** the **redundancy report** — cluster near-identical
functions across the repo so you can finally see "you have 6 date parsers". See
[Find redundant functions](#find-redundant-functions-deja-dupes).
**`deja hook` shipped:** a git **pre-commit / pre-push hook** that warns the
moment a newly *staged* function strongly resembles existing code — catching
redundancy *before* it lands, not in review. See
[Warn on duplicates as you commit](#warn-on-duplicates-as-you-commit-deja-hook).
**Semantic search shipped (optional):** `deja find --semantic` ranks by
*meaning* using local embeddings (sentence-transformers or a running Ollama),
so intent queries land even when they share no words with your code. It's fully
optional — off by default, no heavy import on the core path, and it falls back
to fuzzy search with a clear message if no backend is installed. See
[Semantic search](#semantic-search-deja-find---semantic).
**`deja index --watch` shipped:** keep the index fresh automatically — it
incrementally reparses **only the files you touch** (debounced) as you work,
honoring the same `.gitignore`/exclude rules, and stops cleanly on Ctrl-C with a
summary. See [Keep the index fresh](#keep-the-index-fresh-deja-index---watch).
**`deja stats` shipped:** an inventory **leaderboard** — totals, language
breakdown, the most-duplicated function names, and the biggest files by function
count, in one skimmable view (the *"you have 6 date parsers"* readout at a
glance). See [See your inventory at a glance](#see-your-inventory-at-a-glance-deja-stats).
**`deja stale` shipped:** the flip side of `find` — a **dead-code candidate**
finder that lists indexed functions whose names are never referenced anywhere
else in the tree. It's a fast, conservative string-level heuristic (not a call
graph), so results are *candidates* to review, not proof. See
[Find dead-code candidates](#find-dead-code-candidates-deja-stale).

## Install (from source)

```bash
pipx install .                # or: uv tool install .
deja --version                # deja-func 0.1.0
deja hello                    # 🫠 say hi
```

Want **semantic search** (`deja find --semantic`)? Install the optional extra to
pull in the local embedding model (or point it at a running Ollama instead — see
[Semantic search](#semantic-search-deja-find---semantic)):

```bash
pipx install '.[semantic]'    # adds sentence-transformers for --semantic
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

### Keep the index fresh (`deja index --watch`)

Don't re-run `deja index` by hand every time you save. `--watch` builds the index
once, then stays running and **incrementally reindexes only the files that
change** — created, modified, or deleted — never re-walking the whole tree:

```bash
deja index --watch                 # watch the current repo
deja index path/to/project --watch # watch a specific directory
# → 👁️  Watching . for changes (poll 1s, debounce 0.4s). Press Ctrl-C to stop.
#   🔄 reindex: +1 new → 413 functions (+3/-0)
#   🔄 reindex: ~1 changed → 414 functions (+2/-1)
#   🔄 reindex: -1 gone → 412 functions (+0/-2)
#   👋 Stopped watching. 3 reindex pass(es), 3 file event(s), net +1 function(s) over 92s.
```

Details:

- **Touched-files only.** Each change reparses just that file and merges it into
  the existing index, so a one-line edit doesn't cost a full rescan.
- **Same rules as a full index.** It reuses the walker, so `.gitignore` and the
  skip-dirs (`.venv`, `node_modules`, …) apply identically — a watched edit can
  never sneak a vendored file into the index.
- **Debounced.** A burst of saves (or a `git checkout`, or an editor's
  atomic-rename) collapses into a single reindex once the filesystem settles.
- **Dependency-free.** It polls file mtimes/sizes with the stdlib (no
  `watchdog`/inotify dep). Tune it with `--interval` (poll seconds, default `1`)
  and `--debounce` (quiet-window seconds, default `0.4`).
- **Clean shutdown.** Ctrl-C stops the loop and prints a summary of the session.

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

### Semantic search (`deja find --semantic`)

Fuzzy search matches *words*. Sometimes you know what you want but not what you
called it — your query and the function share **meaning** but no tokens. `--semantic`
ranks by embedding similarity so "turn a blob of HTML into clean text" finds
`strip_markup` even with a terse docstring:

```bash
deja find "convert html into readable text" --semantic
# 🧪 semantic search using sentence-transformers:all-MiniLM-L6-v2
# 🫠 You already wrote this:
#     strip_markup — render.py:42 — (s: str) -> str
#         Convert HTML into plain text.
```

It's **opt-in and dependency-light by design** (PLAN.md §9: no mandatory LLM in
the core):

- **Off by default — zero cost when unused.** The embedding code (and its heavy
  deps) is only imported when you pass `--semantic`; plain `deja find` never
  touches it.
- **Two backends, auto-detected.** Install the extra for a local
  sentence-transformers model, *or* run [Ollama](https://ollama.com) and deja
  will talk to it — no Python ML deps needed:

  ```bash
  pipx install '.[semantic]'                 # local sentence-transformers, or…
  DEJA_EMBED_BACKEND=ollama deja find "…" --semantic   # use a running Ollama
  ```

- **Cached + incremental.** Embeddings live in `.dejafunc/embeddings.json`, keyed
  by a content fingerprint of each function. Re-running only embeds the functions
  that actually **changed**; everything else is reused.
- **Graceful fallback.** If no backend is installed/reachable, deja prints a clear
  one-liner and **falls back to fuzzy search** instead of erroring — so
  `--semantic` is always safe to leave in a script.

Knobs (all optional environment variables):

```bash
DEJA_EMBED_BACKEND=sentence-transformers   # force a backend (or: ollama)
DEJA_EMBED_MODEL=all-MiniLM-L6-v2          # pick the embedding model
OLLAMA_HOST=http://localhost:11434         # where the Ollama daemon lives
```

### Find redundant functions (`deja dupes`)

`deja find` answers "have I written *this* before?" for a query you type.
`deja dupes` flips it around and asks the question of the repo against *itself*:
**which functions are near-duplicates of each other?** It's the redundancy
report — the "you have 6 date parsers" view.

```bash
deja dupes                    # scan this repo for near-duplicate clusters
deja dupes path/to/project    # ...in a specific directory
# → ♻️ Found 1 cluster of near-duplicate functions:
#     ×3 · ~95% similar
#       parse_iso_date — dates.py:1 — (s: str)
#           Parse an ISO 8601 date string into a date.
#       parse_date_iso — dates.py:5 — (text: str)
#           Parse an ISO 8601 date string into a date object.
#       parse_iso — dates.py:9 — (value: str)
#           Parse an ISO date string.
```

Each pair of functions is scored by blending the same signals `find` trusts —
fuzzy **name**, fuzzy **docstring**, and **signature shape** — then functions are
grouped by **complete-linkage**: a function joins a cluster only if it's similar
to *every* member, so each cluster stays internally coherent (no "bridge"
functions chaining unrelated code into one blob). Clusters are printed
**largest first**; lone functions with no twin are omitted. Tune sensitivity with
`--threshold` (0–100, default `75`; lower = looser, bigger clusters), cap output
with `-n/--limit`, and add `--json` for tooling:

```bash
deja dupes --threshold 85     # only flag very close twins
deja dupes -n 5               # show the 5 biggest clusters
deja dupes --json | jq '.clusters[0].members[].file'
```

It auto-builds the index on first run, and exits `0` when any redundancy is
found / `1` when the inventory is clean — handy as a soft CI signal.

### See your inventory at a glance (`deja stats`)

`find` and `dupes` are lookups; `deja stats` is the **bird's-eye view**. It
aggregates the existing index into a single skimmable leaderboard — no new
parsing, no new dependencies — so you can size up a codebase's function
inventory in one glance:

```bash
deja stats                    # summarize this repo
deja stats path/to/project    # ...a specific directory
# → 🧠 180 functions across 24 files.
#     python: 142 · javascript: 38
#       Most duplicated names
#         parse (×6)
#         slugify (×4)
#       Biggest files by function count
#         src/text.py (×19)
#     😬 "parse" shows up 6 times — sure you need all of them?
```

It reports **totals** (functions + files), a full **language breakdown**, the
most-repeated **bare function names** (the duplication leaderboard — only names
that actually repeat show up), and the **biggest files by function count**. Cap
each leaderboard with `-t/--top` (default `10`), and add `--json` for tooling:

```bash
deja stats --top 5            # shorter leaderboards
deja stats --json | jq '.top_names[0]'
```

Like the other commands it auto-builds the index on first run, and exits `0`
whenever there's something to report / `1` on an empty inventory.

### Find dead-code candidates (`deja stale`)

If `deja find` answers *"have I written this before?"*, `deja stale` answers the
complement: *"is this still used at all?"* It reuses the same inventory and file
walk, then flags functions whose names never appear **outside their own
definition** anywhere in the indexed tree — strong dead-code candidates to prune:

```bash
deja stale                    # scan this repo for unused functions
deja stale path/to/project    # ...a specific directory
# → 🪦 2 dead-code candidates — names never referenced elsewhere:
#     old_helper — src/util.py:42 — (x: int) -> int
#     legacy_parse — src/text.py:88 — (s: str) -> Date
#   🧪 Heuristic: string-level scan, not a call graph — reflection or dynamic
#      dispatch can hide real uses. Review before deleting.
```

**These are candidates, not proof.** The scan is deliberately *string-level* —
it matches whole-word name references (`parse` never matches inside `parseAll`)
over exactly the files `deja index` parsed. It does **not** follow a real call
graph (that's an LSP, out of scope), so dynamic dispatch, reflection,
string-keyed registries, and cross-language calls can hide a genuine use. Treat
the list as a review queue, not a delete list.

Common entry points are ignored by default so they aren't falsely flagged
(`__*__` dunders, `main`, `test_*`, `setUp`/`tearDown` and friends). Add your
own patterns, filter by language, or emit JSON:

```bash
deja stale --ignore "cli_*" --ignore "handle_*"   # never flag these names
deja stale --lang python                          # only report Python functions
deja stale --json | jq '.candidates[].function.name'
```

`--lang` narrows only *which functions are reported* — the reference scan always
covers every file, so a helper called only from another language isn't flagged.
Like the other commands it auto-builds the index on first run, and exits `0`
when any candidate is found / `1` when nothing is stale (scriptable / CI-
friendly).

### Warn on duplicates as you commit (`deja hook`)

`deja dupes` and `deja find` are pull-based — you have to remember to run them.
`deja hook` makes the nudge **push-based**: a git hook that fires while you
commit and warns when a brand-new function strongly resembles one already in the
index. Redundancy gets caught *before* it's written into history, not in PR
review.

```bash
deja index                    # build the inventory to compare against
deja hook install             # write a .git/hooks/pre-commit hook
# → 🫠 Installed pre-commit redundancy hook → .git/hooks/pre-commit  [warn-only]
```

Now every `git commit` checks the functions you're about to add:

```text
🫠 1 staged function already exist(s) (heads-up):
  parse_date_iso — dates.py:1 — (text: str)
      ~98% similar to existing:
      parse_iso_date — util.py:1 — (s: str)
          Parse an ISO 8601 date string into a date object.
  (warning only — commit proceeds; run with --strict to block)
```

By design it **warns but doesn't block** (PLAN.md §9: a hook that warns is in
scope; hard gating isn't the product). A staged function is only ever compared
against functions in *other* files, so editing an existing function never flags
it as its own duplicate.

```bash
deja hook install --strict    # turn the nudge into a gate (fails the commit)
deja hook install --pre-push  # run on push instead of commit
deja hook install --force     # overwrite a pre-existing hook
deja hook check               # run the check manually (what the hook calls)
deja hook check -t 85         # only warn on very close matches
deja hook check --json        # structured output for tooling
```

The installed hook is a tiny stub that just calls `deja hook check`, so upgrades
to deja take effect immediately (nothing stale is frozen into `.git`). Skip it
for one commit with `git commit --no-verify`. If you haven't run `deja index`
yet, the hook stays quiet and reminds you to build the inventory — it never
breaks a commit on its own.

## Use with AI agents

The whole point of `deja-func` is to stop redundant code *before it's written* —
so it speaks two machine-friendly dialects: structured **JSON** for scripts, and
**MCP** for coding agents.

### `deja find --json` (stable schema)

Add `--json` to any `find` and you get a stable, documented document instead of
the pretty output — no ANSI, no emoji, easy to pipe into `jq`:

```bash
deja find slugify --json | jq '.results[0]'
```

```json
{
  "schema_version": 1,
  "query": "slugify",
  "sig": null,
  "intent": false,
  "count": 1,
  "results": [
    {
      "name": "slugify",
      "qualname": "text.slugify",
      "file": "src/text.py",
      "line": 42,
      "signature": "(value: str) -> str",
      "docstring": "Turn a string into a URL-safe slug.",
      "lang": "python",
      "score": 88.0,
      "breakdown": { "name": 88.0, "doc": 60.0, "sig": null }
    }
  ]
}
```

Exit code is still `0` when there's at least one match and non-zero when there
are none, so `--json` stays scriptable.

### `deja mcp` (Model Context Protocol server)

`deja mcp` runs a **stdio MCP server** so an agent can look up existing functions
mid-task. It's dependency-free (plain JSON-RPC 2.0 over stdio — no heavy SDK) and
exposes two tools:

- **`find_function`** — search by `query` (name/intent), `sig` (shape like
  `(str)->bool`), and/or `intent`. Returns a human summary *and* the same JSON
  document as `--json`.
- **`index_stats`** — totals + per-language counts, to confirm the index exists.

It indexes the target repo on first use, so there's nothing to pre-build:

```bash
deja mcp                      # serve the current directory over stdio
deja mcp path/to/project      # serve a specific repo
```

#### Register with a coding agent

Most MCP clients take a server command + args. Point them at `deja mcp` with the
repo you want indexed (use an absolute path, or `.` if the client launches in the
project root).

**Claude Code:**

```bash
claude mcp add deja-func -- deja mcp /abs/path/to/your/repo
```

**Cursor / generic `mcp.json`** (e.g. `.cursor/mcp.json` or your client's config):

```json
{
  "mcpServers": {
    "deja-func": {
      "command": "deja",
      "args": ["mcp", "/abs/path/to/your/repo"]
    }
  }
}
```

**OpenClaw** (`~/.openclaw` MCP config, same shape):

```json
{
  "mcpServers": {
    "deja-func": {
      "command": "deja",
      "args": ["mcp", "."]
    }
  }
}
```

Then tell the agent, in its system/instructions, to **call `find_function`
before writing a new utility** — e.g. *"Before adding a helper, query the
`deja-func` MCP `find_function` tool to check it doesn't already exist."*

> Transport note: framing is newline-delimited JSON-RPC over stdio (one message
> per line), the simplest MCP stdio framing. If your client requires
> `Content-Length` headers, open an issue — it's a small addition.

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
- **M6** MCP server + JSON output for agents ✅

Full plan, backlog, and what's explicitly out of scope: [`PLAN.md`](./PLAN.md).

## License

MIT
