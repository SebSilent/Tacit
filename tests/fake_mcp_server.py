"""A tiny MCP server over stdio, used by the test-suite.

Speaks the same newline-delimited JSON-RPC 2.0 that real MCP servers do, so the
tests exercise Tacit's real client rather than a mock.
"""

import json
import sys

PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {"name": "echo", "description": "Echo the given text back.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}},
                     "required": ["text"]}},
    {"name": "add_numbers", "description": "Add two integers and return the sum.",
     "inputSchema": {"type": "object",
                     "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                     "required": ["a", "b"]}},
    {"name": "delete_everything",
     "description": "Delete every stored record permanently. Destructive.",
     "inputSchema": {"type": "object", "properties": {}}},
]


def send(payload):
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def handle(name, arguments):
    if name == "echo":
        return f"echo: {arguments.get('text', '')}"
    if name == "add_numbers":
        return str(int(arguments.get("a", 0)) + int(arguments.get("b", 0)))
    if name == "delete_everything":
        return "deleted (pretend)"
    raise ValueError(f"unknown tool {name}")


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method")
        rid = msg.get("id")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-mcp", "version": "1.0"}}})
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            params = msg.get("params") or {}
            try:
                text = handle(params.get("name"), params.get("arguments") or {})
                result = {"content": [{"type": "text", "text": text}]}
            except Exception as exc:  # noqa: BLE001
                result = {"content": [{"type": "text", "text": f"error: {exc}"}],
                          "isError": True}
            send({"jsonrpc": "2.0", "id": rid, "result": result})
        elif rid is not None:
            send({"jsonrpc": "2.0", "id": rid,
                  "error": {"code": -32601, "message": f"method not found: {method}"}})


if __name__ == "__main__":
    main()
