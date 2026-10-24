"""Query Codex metadata without creating an Agent, thread or turn."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from bazaar_contrib_harness import HarnessInspection, HarnessModel, HarnessSkill

from .approval import decline
from .transport import AppServer, AppServerError, Packet

logger = logging.getLogger(__name__)


async def inspect(workspace: Path, *, timeout: float) -> HarnessInspection:  # noqa: ASYNC109
    try:
        process = await asyncio.create_subprocess_exec(
            "codex",
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as error:
        return HarnessInspection(available=False, error=str(error))
    try:
        async with asyncio.timeout(timeout):
            stdout, stderr = await process.communicate()
    except TimeoutError as error:
        return HarnessInspection(
            available=False, error=str(error) or type(error).__name__
        )
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
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
        )
        await server.initialize(timeout=timeout)
        cursor = None
        while True:
            page = await server.request(
                "model/list", {"cursor": cursor}, timeout=timeout
            )
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
        result = await server.request(
            "skills/list", {"cwds": [str(workspace)]}, timeout=timeout
        )
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
        return HarnessInspection(
            available=True,
            version=version,
            models=tuple(models),
            skills=skills,
            error="\n".join(
                error["message"]
                for entry in result["data"]
                for error in entry.get("errors", [])
            )
            or None,
        )
    except (AppServerError, OSError, TimeoutError) as error:
        return HarnessInspection(
            available=True,
            version=version,
            models=tuple(models),
            skills=skills,
            error=str(error) or type(error).__name__,
        )
    finally:
        if server is not None:
            await server.aclose(timeout=timeout)
