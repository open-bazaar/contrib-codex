"""Query Codex metadata without creating an Agent, thread or turn."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from bazaar_contrib_harness import (
    HarnessInspection,
    HarnessMcpServer,
    HarnessMcpTool,
    HarnessModel,
    HarnessSkill,
    McpAuth,
    McpConnection,
    McpFailed,
)

from .approval import decline
from .transport import AppServer, AppServerError, Packet, background, executable

logger = logging.getLogger(__name__)

# Codex's sign-in states as the common ones; one it does not know says nothing
_AUTH = {
    "unsupported": McpAuth.NONE,
    "notLoggedIn": McpAuth.SIGNED_OUT,
    "bearerToken": McpAuth.SIGNED_IN,
    "oAuth": McpAuth.SIGNED_IN,
}
# its connection states, which it gives only within a thread
_CONNECTION = {
    "connected": McpConnection.CONNECTED,
    "starting": McpConnection.CONNECTING,
    "notStarted": McpConnection.CONNECTING,
    "disabled": McpConnection.DISABLED,
}


def _connection(item: dict[str, Any]) -> McpConnection | McpFailed | None:
    """How Codex's link to an MCP server stands: failed when it could not
    list the server's tools or its connection failed, else as its status
    says, when it says."""
    if item.get("toolsError"):
        return McpFailed(reason=str(item["toolsError"]))
    status = item.get("runtimeStatus")
    if status in ("failed", "cancelled"):
        return McpFailed(reason=status)
    return _CONNECTION.get(status) if status else None


async def inspect(workspace: Path, *, timeout: float) -> HarnessInspection:  # noqa: ASYNC109
    try:
        process = await asyncio.create_subprocess_exec(
            await executable(dict(os.environ)),
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as error:
        return HarnessInspection(available=False, error=str(error))
    try:
        stdout, stderr = await process.communicate()
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            background(process.communicate())
    if process.returncode or not stdout.strip():
        return HarnessInspection(
            available=False,
            error=stderr.decode(errors="replace").strip()
            or f"codex --version exited ({process.returncode})",
        )
    version = stdout.decode(errors="replace").strip().split()[-1]
    server: AppServer | None = None
    models: list[HarnessModel] = []
    skills: tuple[HarnessSkill, ...] = ()
    servers: list[HarnessMcpServer] = []

    async def answer(packet: Packet) -> dict[str, Any]:
        logger.warning("No session owns Codex inspection request %s", packet.id)
        return decline(packet)

    try:
        server = await AppServer.start(
            workspace,
            {},
            answer,
            lambda packet: logger.debug(
                "Codex inspection notification: %s", packet.method
            ),
            timeout=timeout,
        )
        await server.initialize()
        cursor = None
        while True:
            page = await server.request("model/list", {"cursor": cursor})
            models.extend(
                HarnessModel(
                    id=item["model"],
                    name=item["displayName"],
                    efforts=tuple(
                        option["reasoningEffort"]
                        for option in item["supportedReasoningEfforts"]
                    ),
                    default_effort=item["defaultReasoningEffort"],
                    default=item["isDefault"],
                )
                for item in page["data"]
                if not item.get("hidden", False)
            )
            cursor = page.get("nextCursor")
            if cursor is None:
                break
        result = await server.request("skills/list", {"cwds": [str(workspace)]})
        skills = tuple(
            HarnessSkill(
                name=skill["name"],
                description=(skill.get("interface") or {}).get("shortDescription")
                or skill.get("shortDescription")
                or skill["description"],
                source={"repo": "workspace", "user": "personal"}.get(
                    skill["scope"], "system"
                ),
            )
            for entry in result["data"]
            for skill in entry["skills"]
            if skill.get("enabled", True)
        )
        # the MCP servers Codex sees, each with its sign-in and its tools
        cursor = None
        while True:
            page = await server.request("mcpServerStatus/list", {"cursor": cursor})
            servers.extend(
                HarnessMcpServer(
                    name=item["name"],
                    connection=_connection(item),
                    auth=_AUTH.get(item["authStatus"]),
                    tools=tuple(
                        HarnessMcpTool(
                            name=tool["name"],
                            description=tool.get("description"),
                            input_schema=tool["inputSchema"],
                        )
                        for tool in item["tools"].values()
                    ),
                )
                for item in page["data"]
            )
            cursor = page.get("nextCursor")
            if cursor is None:
                break
        return HarnessInspection(
            available=True,
            version=version,
            models=tuple(models),
            skills=skills,
            mcp_servers=tuple(servers),
            error="\n".join(
                error["message"]
                for entry in result["data"]
                for error in entry.get("errors", [])
            )
            or None,
        )
    except (AppServerError, OSError) as error:
        return HarnessInspection(
            available=True,
            version=version,
            models=tuple(models),
            skills=skills,
            mcp_servers=tuple(servers),
            error=str(error) or type(error).__name__,
        )
    finally:
        if server is not None:
            server.close(timeout=timeout)
