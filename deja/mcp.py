"""``deja mcp`` — a tiny stdio MCP server so AI agents query the inventory first.

Why hand-rolled instead of the ``mcp`` SDK? PLAN.md is emphatic that the core
stays dependency-light ("an idea about reducing dependency on heavy tooling
shouldn't ship a heavy toolchain", §5). MCP at the wire level is just
**JSON-RPC 2.0 over stdio** with a small, stable method set, so we implement
exactly the slice we need — no extra runtime dependency, and the smoke test is
trivial.

Protocol surface implemented (MCP 2024-11-05):

* ``initialize`` → advertise ``tools`` capability + server info.
* ``notifications/initialized`` → accepted, no reply (it's a notification).
* ``ping`` → ``{}`` (liveness).
* ``tools/list`` → the two tools below.
* ``tools/call`` → dispatch to a tool, return ``content`` blocks.

Tools exposed:

* ``find_function`` — the headline: search the index by name / intent / shape.
  Returns both human-readable text *and* the stable JSON document from
  :mod:`deja.serialize` (as a second text block) so agents can parse it.
* ``index_stats`` — quick inventory summary (counts by language); doubles as the
  "index resource" called for in the M6 acceptance criteria.

Transport framing: newline-delimited JSON (one JSON-RPC message per line). This
is the simplest MCP stdio framing and is what stdio clients that don't require
``Content-Length`` headers expect; it keeps the server hackable and the smoke
test a one-liner.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, BinaryIO

from . import __version__
from .index import build_index, load_index, save_index
from .search import DEFAULT_LIMIT, search
from .serialize import results_to_dict

#: MCP protocol revision we speak.
PROTOCOL_VERSION = "2024-11-05"
#: Advertised server identity.
SERVER_NAME = "deja-func"

# JSON-RPC 2.0 standard error codes we use.
_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_INTERNAL_ERROR = -32603


# --- tool schemas ---------------------------------------------------------

_FIND_FUNCTION_TOOL = {
    "name": "find_function",
    "description": (
        "Search the repo's function index BEFORE writing a new function, to "
        "avoid reinventing one that already exists. Query by name, by natural-"
        "language intent (e.g. 'parse an ISO date'), and/or by signature shape "
        "(e.g. '(str)->bool'). Returns matching functions with file:line, "
        "signature, and a relevance score."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Name or intent to search for. Optional if 'sig' is given.",
            },
            "sig": {
                "type": "string",
                "description": "Signature shape to match, e.g. '(str)->bool' or '(int, int)'.",
            },
            "intent": {
                "type": "boolean",
                "description": "Weight the docstring higher for natural-language queries.",
                "default": False,
            },
            "limit": {
                "type": "integer",
                "description": f"Max matches to return (default {DEFAULT_LIMIT}).",
                "minimum": 1,
            },
        },
    },
}

_INDEX_STATS_TOOL = {
    "name": "index_stats",
    "description": (
        "Summarize the function index for this repo: total functions and a "
        "per-language breakdown. Use it to confirm the index exists and see "
        "what's covered before relying on find_function."
    ),
    "inputSchema": {"type": "object", "properties": {}},
}

TOOLS = [_FIND_FUNCTION_TOOL, _INDEX_STATS_TOOL]


# --- tool implementations -------------------------------------------------


def _load_or_build(root: Path):
    """Load the index for *root*, building (and saving) it on first use."""
    try:
        return load_index(root)
    except FileNotFoundError:
        index = build_index(root)
        save_index(index, root)
        return index


def _tool_find_function(root: Path, args: dict[str, Any]) -> dict[str, Any]:
    """Run a search and return an MCP ``tools/call`` result payload."""
    query = str(args.get("query") or "")
    sig = args.get("sig")
    sig = str(sig) if sig else None
    intent = bool(args.get("intent", False))

    limit = args.get("limit")
    if limit is None:
        limit = DEFAULT_LIMIT
    else:
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            return _tool_error("'limit' must be an integer")
        if limit < 1:
            return _tool_error("'limit' must be >= 1")

    if not query.strip() and not sig:
        return _tool_error("Provide 'query' and/or 'sig'.")

    index = _load_or_build(root)
    results = search(query, index.records, limit=limit, sig=sig, intent=intent)
    doc = results_to_dict(results, query=query, sig=sig, intent=intent)

    summary = _summarize_results(query, sig, results)
    # Two content blocks: a human-friendly summary and the parseable JSON. This
    # lets simple agents read the text and structured ones parse the document.
    return {
        "content": [
            {"type": "text", "text": summary},
            {"type": "text", "text": json.dumps(doc, ensure_ascii=False)},
        ],
        "isError": False,
    }


def _tool_index_stats(root: Path, _args: dict[str, Any]) -> dict[str, Any]:
    """Return a small inventory summary as an MCP result payload."""
    index = _load_or_build(root)
    by_lang: dict[str, int] = {}
    for r in index.records:
        by_lang[r.lang] = by_lang.get(r.lang, 0) + 1
    doc = {
        "schema_version": index.schema_version,
        "tool_version": index.tool_version,
        "count": len(index.records),
        "by_language": dict(sorted(by_lang.items())),
    }
    langs = ", ".join(f"{k}: {v}" for k, v in doc["by_language"].items()) or "none"
    text = f"{len(index.records)} functions indexed ({langs})."
    return {
        "content": [
            {"type": "text", "text": text},
            {"type": "text", "text": json.dumps(doc, ensure_ascii=False)},
        ],
        "isError": False,
    }


_TOOL_DISPATCH: dict[str, Callable[[Path, dict[str, Any]], dict[str, Any]]] = {
    "find_function": _tool_find_function,
    "index_stats": _tool_index_stats,
}


def _summarize_results(query: str, sig: str | None, results) -> str:
    """Plain-text summary of search results for the first content block."""
    label = query.strip() or sig or "that shape"
    if not results:
        return f"No existing function matches {label!r}. Looks new — safe to write it."
    lines = [f"Found {len(results)} existing match(es) for {label!r}:"]
    for s in results:
        r = s.record
        name = r.qualname or r.name
        doc = f" — {r.docstring}" if r.docstring else ""
        lines.append(f"  {name}  {r.file}:{r.line}  {r.signature}  (score {s.score:.0f}){doc}")
    return "\n".join(lines)


def _tool_error(message: str) -> dict[str, Any]:
    """An in-band tool error (``isError`` true), per MCP convention.

    Tool-level failures are reported as a *successful* JSON-RPC response whose
    result carries ``isError: true`` — distinct from protocol errors (bad
    method, malformed JSON), which use JSON-RPC ``error`` objects.
    """
    return {"content": [{"type": "text", "text": message}], "isError": True}


# --- JSON-RPC plumbing ----------------------------------------------------


def _result(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def handle_message(msg: dict[str, Any], root: Path) -> dict[str, Any] | None:
    """Handle one parsed JSON-RPC message; return a response dict or ``None``.

    ``None`` means "no reply" — used for notifications (messages without an
    ``id``), which JSON-RPC forbids responding to.
    """
    req_id = msg.get("id")
    method = msg.get("method")
    is_notification = "id" not in msg

    if msg.get("jsonrpc") != "2.0" or not isinstance(method, str):
        if is_notification:
            return None
        return _error(req_id, _INVALID_REQUEST, "Invalid JSON-RPC 2.0 request.")

    params = msg.get("params") or {}

    if method == "initialize":
        result = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": __version__},
        }
        return None if is_notification else _result(req_id, result)

    if method in ("notifications/initialized", "initialized"):
        # Client handshake completion: a notification, so never reply.
        return None

    if method == "ping":
        return None if is_notification else _result(req_id, {})

    if method == "tools/list":
        return None if is_notification else _result(req_id, {"tools": TOOLS})

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return _error(req_id, _INVALID_PARAMS, "'arguments' must be an object.")
        handler = _TOOL_DISPATCH.get(name)
        if handler is None:
            return _error(req_id, _INVALID_PARAMS, f"Unknown tool: {name!r}")
        try:
            payload = handler(root, arguments)
        except Exception as exc:  # noqa: BLE001 - surface as in-band tool error
            payload = _tool_error(f"Tool {name!r} failed: {exc}")
        return None if is_notification else _result(req_id, payload)

    # Unknown method.
    if is_notification:
        return None
    return _error(req_id, _METHOD_NOT_FOUND, f"Method not found: {method!r}")


def serve(
    root: str | Path = ".",
    *,
    stdin: BinaryIO | None = None,
    stdout: BinaryIO | None = None,
) -> int:
    """Run the stdio MCP loop until EOF. Returns a process exit code.

    Reads newline-delimited JSON-RPC messages from *stdin* and writes responses
    to *stdout*. Streams default to the real ``sys.stdin``/``sys.stdout`` buffers
    so the server speaks raw bytes (UTF-8) and isn't perturbed by text-mode
    newline translation.
    """
    root_path = Path(root)
    in_stream = stdin if stdin is not None else sys.stdin.buffer
    out_stream = stdout if stdout is not None else sys.stdout.buffer

    for raw in in_stream:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            _write(out_stream, _error(None, _PARSE_ERROR, "Parse error: invalid JSON."))
            continue
        if not isinstance(msg, dict):
            _write(out_stream, _error(None, _INVALID_REQUEST, "Expected a JSON object."))
            continue
        response = handle_message(msg, root_path)
        if response is not None:
            _write(out_stream, response)
    return 0


def _write(out_stream: BinaryIO, payload: dict[str, Any]) -> None:
    """Write one JSON-RPC message as a UTF-8 line and flush."""
    data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
    out_stream.write(data)
    out_stream.flush()
