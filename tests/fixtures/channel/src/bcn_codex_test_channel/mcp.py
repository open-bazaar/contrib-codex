"""A minimal MCP tool for real invocation and approval checks."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from mcp.server import MCPServer

root = Path(sys.argv[1]).resolve()
server = MCPServer("test")


@server.tool()
async def get_value(key: str) -> dict[str, str]:
    """Return the fixed value MCP_OK for a test key."""
    result = {"key": key, "value": "MCP_OK"}
    record = {"arguments": {"key": key}, "result": result}

    def retain_call():
        with (root / "mcp-requests.jsonl").open("a") as log:
            log.write(json.dumps(record) + "\n")

    await asyncio.to_thread(retain_call)
    return result


if __name__ == "__main__":
    server.run()
