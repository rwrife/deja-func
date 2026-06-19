# deja-func — PLAN

> *"Wait... haven't I written this before?"*

## 1. Pitch

`deja-func` is a tiny local CLI that indexes every function in your codebase and answers one question fast: **"Have I already written something that does this?"** Search by name, by fuzzy intent ("parse an ISO date"), or by a signature shape, and get back the matching functions with file:line, so you — and your AI coding agents — stop reinventing `slugify` for the fourth time. No server, no cloud, no LLM required for the core; it's grep that understands functions.

## 2. Trend inspiration

While scanning what's hot in mid-2026, one complaint showed up in *every* thread about reviewing AI-generated code:

- **"AI agents frequently disregard code reuse opportunities, resulting in higher levels of redundancy compared to human developers... The agent has no memory of the module you refactored."** — [How to Review an Agent-Generated Pull Request (Medium)](https://medium.com/@ashokgudivada/how-to-review-an-agent-generated-pull-request-08145c261829)
- **"41% of developers who reviewed AI-generated code said the diffs didn't explain *why* the change was made."** — [Version Control with AI](https://brics-econ.org/version-control-with-ai-managing-ai-generated-commits-and-diffs)
- GitHub's own guidance notes agent PRs hide redundancy and tech debt that "looks like clean code." — [Agent pull requests are everywhere. Here's how to review them. (github.blog)](https://github.blog/ai-and-ml/generative-ai/agent-pull-requests-are-everywhere-heres-how-to-review-them/)
- The terminal renaissance continues — small, fast, single-purpose Rust/Go CLIs are winning over heavyweight tooling. — [Terminal Renaissance 2026](https://1337skills.com/blog/2026-03-09-terminal-renaissance-modern-tui-tools-reshaping-developer-workflows/), [Best CLI Tools 2026 (ToolShelf)](https://www.toolshelf.dev/blog/best-cli-tools-2026)
- MCP is everywhere (38k+ servers in the Glama registry), so any dev tool worth shipping should expose itself to agents over MCP too. — [Glama MCP registry](https://glama.ai/mcp/servers)

The thesis: the redundancy problem is best solved *before* code is written, not in PR review. Give the writer (human or agent) an instant "this already exists" lookup.

## 3. Why it's different

- **vs. CodeRabbit / GitLab AI Diff Assist / CodeAnt** — those are heavyweight, cloud, *post-hoc* review SaaS. `deja-func` is local, single-binary, and runs *pre-flight* — answering "does this exist?" before the redundant function is born.
- **vs. ripgrep / grep / ctags / the LSP** — grep finds *text*; ctags/LSP jump to a symbol *you already named*. `deja-func` lets you search by **intent** ("validate an email") and by **signature shape** (`(str) -> bool`) when you *don't* know the name. It ranks semantically similar functions, not just literal matches.
- **vs. Sourcegraph** — enterprise code-search platform. We're the 80/20 single-repo version you can `pipx install` and forget.
- **vs. clone/duplication detectors (jscpd, PMD CPD)** — those flag *copy-pasted blocks* after the fact. We index *named functions* and serve interactive lookup; dedup reporting is a bonus, not the point.
- **vs. our own existing tool-lab repos** — `commit-roast`/`ship-log` work on commits/history; `link-coroner`/`stash-stash` on links/stashes; `schema-seance` on data files. Nothing here indexes the *function inventory* of a repo. New surface entirely.

## 4. MVP scope (v0.1)

The smallest useful thing:

- `deja index` — walk the repo, parse functions, build a local index (`.dejafunc/index.json`).
- `deja find <query>` — fuzzy search the index by function name + docstring; print ranked `name — file:line — signature — one-line summary`.
- Python support first (stdlib `ast` — zero parsing deps), because it's the lingua franca of the AI-coding crowd.
- Respect `.gitignore`; skip `node_modules`, `.venv`, etc.
- Single command, fast (<1s on a few-thousand-function repo), no network.
- A little personality in the output ("🫠 You already wrote this 3 weeks ago:").

## 5. Tech stack

Boring, fast, and frictionless to install:

- **Python 3.10+** — the core audience already has it; stdlib `ast` parses Python with zero deps; `pathlib`/`json` cover the rest.
- **`rapidfuzz`** — fast fuzzy string matching for name/intent search (only real runtime dep; tiny, C-backed).
- **`pathspec`** — proper `.gitignore` matching (small, battle-tested).
- **`pytest`** — tests.
- Packaged with **`pyproject.toml`** so `pipx install deja-func` / `uv tool install` just works; entry point `deja`.

Justification: an idea about *reducing dependency on heavy tooling* shouldn't ship a heavy toolchain. Stdlib `ast` keeps parsing dependency-free and rock-solid; `rapidfuzz` is the one place speed matters.

## 6. Architecture

```
deja/
  cli.py          # argparse entry: index, find, stats, (later: mcp, watch)
  walker.py       # filesystem walk + .gitignore filtering (pathspec)
  parsers/
    base.py       # FunctionRecord dataclass + Parser protocol
    python.py     # ast-based Python function extraction (name, args, signature, docstring, line)
  index.py        # build / load / save .dejafunc/index.json
  search.py       # ranking: fuzzy name + docstring + signature-shape scoring (rapidfuzz)
  render.py       # pretty terminal output + personality
```

Key modules: **parsers** (pluggable per language), **index** (serialize FunctionRecords), **search** (ranking strategy). Adding a language = adding one parser module.

## 7. Milestones

1. **M1 — scaffold + hello-world.** `pyproject.toml`, package skeleton, `deja --version` / `deja hello`, pytest wired, CI (GitHub Actions: lint + test). Ships installable do-nothing CLI.
2. **M2 — Python parser + `deja index`.** Walk repo, ast-extract functions into FunctionRecords, write `.dejafunc/index.json`. Honor `.gitignore` via pathspec.
3. **M3 — `deja find` (fuzzy name + docstring search).** rapidfuzz ranking, pretty `render.py` output with file:line and signature, exit codes for scripting.
4. **M4 — signature-shape & intent search.** Search by arg-count/type hints (`(str)->bool`) and weight docstrings so natural-language intent queries work.
5. **M5 — multi-language: JS/TS parser.** Add a regex/tree-light parser for JS/TS functions + arrow funcs; parser registry dispatch by extension.
6. **M6 — agent integration: MCP server + JSON output.** `deja find --json` and a `deja mcp` stdio server exposing `find_function`, so AI agents query the inventory before writing. Ride the MCP wave.

## 8. Backlog / future features (v0.2+)

1. `deja dupes` — report clusters of near-identical functions (the redundancy report).
2. `--watch` / incremental reindex on file change (debounced).
3. Git pre-commit / pre-push hook: warn when a new function strongly matches an existing one.
4. Optional embedding-based semantic search (local model via `sentence-transformers` or Ollama) behind a flag.
5. More languages: Go, Rust, Ruby, Java (tree-sitter grammars).
6. `deja explain <func>` — show callers/callees and "last touched" git blame age.
7. VS Code / editor command: "Find existing function for selection."
8. Cross-repo / monorepo mode with per-package indexes and a merged view.
9. Stale-function finder: functions never called anywhere in the index (dead code candidates).
10. `deja stats` leaderboard: most-duplicated concepts, biggest functions, "you have 6 date parsers."
11. Config file (`.dejafunc.toml`) for include/exclude globs and ranking weights.
12. Shareable index export so an agent on a fresh clone gets the inventory instantly.

## 9. Out of scope

- Not a full LSP, IDE, or language server — we don't do go-to-def, autocomplete, or type-checking.
- Not a cloud service, web dashboard, or hosted multi-tenant platform.
- Not a PR-review bot or CI gatekeeper that blocks merges (a *hook* that warns is in backlog; hard gating is not the product).
- Not an enterprise code-search competitor (no global index across thousands of repos, no RBAC).
- Not a code *generator* or refactoring tool — we point at the existing function; we don't rewrite for you.
- No mandatory LLM/embedding dependency in the core — semantic search stays optional and behind a flag.
