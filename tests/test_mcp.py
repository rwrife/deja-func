"""Tests for the stdio MCP server (mcp.py, M6).

Covers the JSON-RPC dispatch (`handle_message`) unit-by-unit and a real
end-to-end smoke test that drives `serve()` over in-memory byte streams, plus a
subprocess check that `deja mcp` boots and answers a `find_function` call —
exactly the acceptance criterion in issue #6.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from deja.index import build_index, save_index
from deja.mcp import (
    PROTOCOL_VERSION,
    TOOLS,
    handle_message,
    serve,
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A tiny indexed repo to serve from."""
    (tmp_path / "text.py").write_text(
        '''
def slugify(value: str) -> str:
    """Turn a string into a URL-safe slug."""
    return value.lower().replace(" ", "-")


def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b
'''.lstrip(),
        encoding="utf-8",
    )
    save_index(build_index(tmp_path), tmp_path)
    return tmp_path


# -- handle_message dispatch ----------------------------------------------


def test_initialize_advertises_tools_capability(repo: Path) -> None:
    resp = handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, repo)
    assert resp is not None
    assert resp["id"] == 1
    result = resp["result"]
    assert result["protocolVersion"] == PROTOCOL_VERSION
    assert "tools" in result["capabilities"]
    assert result["serverInfo"]["name"] == "deja-func"


def test_initialized_notification_gets_no_reply(repo: Path) -> None:
    resp = handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}, repo)
    assert resp is None


def test_request_without_id_is_notification(repo: Path) -> None:
    # A 'ping' with no id is a notification -> no response.
    assert handle_message({"jsonrpc": "2.0", "method": "ping"}, repo) is None


def test_ping_with_id_returns_empty_result(repo: Path) -> None:
    resp = handle_message({"jsonrpc": "2.0", "id": 9, "method": "ping"}, repo)
    assert resp == {"jsonrpc": "2.0", "id": 9, "result": {}}


def test_tools_list_returns_both_tools(repo: Path) -> None:
    resp = handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, repo)
    names = {t["name"] for t in resp["result"]["tools"]}
    assert names == {"find_function", "index_stats"}
    # Schema is what we advertise verbatim.
    assert resp["result"]["tools"] == TOOLS


def test_every_tool_has_an_input_schema() -> None:
    for tool in TOOLS:
        assert tool["inputSchema"]["type"] == "object"
        assert "description" in tool


def test_find_function_returns_match_and_json_block(repo: Path) -> None:
    resp = handle_message(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "find_function", "arguments": {"query": "slugify"}},
        },
        repo,
    )
    result = resp["result"]
    assert result["isError"] is False
    blocks = result["content"]
    # First block human text; second block is the parseable JSON document.
    assert any("slugify" in b["text"] for b in blocks)
    doc = json.loads(blocks[1]["text"])
    assert doc["count"] >= 1
    assert doc["results"][0]["name"] == "slugify"


def test_find_function_by_signature_shape(repo: Path) -> None:
    resp = handle_message(
        {
            "jsonrpc": "2.0",
            "id": 31,
            "method": "tools/call",
            "params": {"name": "find_function", "arguments": {"sig": "(int, int)->int"}},
        },
        repo,
    )
    doc = json.loads(resp["result"]["content"][1]["text"])
    assert any(r["name"] == "add" for r in doc["results"])


def test_find_function_no_args_is_in_band_error(repo: Path) -> None:
    resp = handle_message(
        {
            "jsonrpc": "2.0",
            "id": 32,
            "method": "tools/call",
            "params": {"name": "find_function", "arguments": {}},
        },
        repo,
    )
    # Tool-level failure: successful JSON-RPC response, isError true.
    assert "error" not in resp
    assert resp["result"]["isError"] is True


def test_find_function_bad_limit_is_in_band_error(repo: Path) -> None:
    resp = handle_message(
        {
            "jsonrpc": "2.0",
            "id": 33,
            "method": "tools/call",
            "params": {
                "name": "find_function",
                "arguments": {"query": "x", "limit": "lots"},
            },
        },
        repo,
    )
    assert resp["result"]["isError"] is True


def test_index_stats_reports_counts(repo: Path) -> None:
    resp = handle_message(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "index_stats", "arguments": {}},
        },
        repo,
    )
    doc = json.loads(resp["result"]["content"][1]["text"])
    assert doc["count"] == 2
    assert doc["by_language"] == {"python": 2}


def test_unknown_tool_is_invalid_params(repo: Path) -> None:
    resp = handle_message(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "nope", "arguments": {}},
        },
        repo,
    )
    assert resp["error"]["code"] == -32602


def test_unknown_method_is_method_not_found(repo: Path) -> None:
    resp = handle_message({"jsonrpc": "2.0", "id": 6, "method": "frobnicate"}, repo)
    assert resp["error"]["code"] == -32601


def test_bad_jsonrpc_version_is_invalid_request(repo: Path) -> None:
    resp = handle_message({"jsonrpc": "1.0", "id": 7, "method": "ping"}, repo)
    assert resp["error"]["code"] == -32600


# -- end-to-end serve() over byte streams ----------------------------------


def _drive(repo: Path, messages: list[dict]) -> list[dict]:
    """Feed *messages* through serve() and return the parsed JSON responses."""
    raw_in = "".join(json.dumps(m) + "\n" for m in messages).encode("utf-8")
    stdin = io.BytesIO(raw_in)
    stdout = io.BytesIO()
    code = serve(repo, stdin=stdin, stdout=stdout)
    assert code == 0
    out = stdout.getvalue().decode("utf-8")
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def test_serve_full_handshake_and_call(repo: Path) -> None:
    responses = _drive(
        repo,
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "find_function", "arguments": {"query": "slugify"}},
            },
        ],
    )
    # 3 responses (the notification produced none).
    assert [r.get("id") for r in responses] == [1, 2, 3]
    doc = json.loads(responses[2]["result"]["content"][1]["text"])
    assert doc["results"][0]["name"] == "slugify"


def test_serve_skips_blank_lines_and_reports_parse_errors(repo: Path) -> None:
    raw_in = (
        b"\n   \nnot json\n"
        + json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode("utf-8")
        + b"\n"
    )
    stdout = io.BytesIO()
    serve(repo, stdin=io.BytesIO(raw_in), stdout=stdout)
    lines = [json.loads(line) for line in stdout.getvalue().decode().splitlines() if line.strip()]
    # One parse error (id null) + one ping result.
    assert lines[0]["error"]["code"] == -32700
    assert lines[1] == {"jsonrpc": "2.0", "id": 1, "result": {}}


# -- subprocess: the real `deja mcp` entry point ---------------------------


def test_deja_mcp_subprocess_answers_find(repo: Path) -> None:
    """`deja mcp` boots as a process and answers a find_function call (issue #6)."""
    messages = (
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        + "\n"
        + json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "find_function", "arguments": {"query": "slugify"}},
            }
        )
        + "\n"
    )
    result = subprocess.run(
        [sys.executable, "-m", "deja.cli", "mcp", str(repo)],
        input=messages,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    lines = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert lines[0]["result"]["serverInfo"]["name"] == "deja-func"
    call = lines[1]["result"]
    assert call["isError"] is False
    assert any("slugify" in b["text"] for b in call["content"])
