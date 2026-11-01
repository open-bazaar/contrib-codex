"""Codex specifics for the shared Harness acceptance suite."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import psutil
import tomli_w

KIND = "codex"
PACKAGE = "bazaar-contrib-codex"
BUILDER = "codex-builder"
HOME = "CODEX_HOME"
HOME_DIRECTORY = "codex-home"
API_KEY = "OPENAI_API_KEY"
PRIVATE = ("OPENAI_API_KEY",)
SCENARIOS = (
    "mcp-once",
    "mcp-session",
    "mcp-always",
    "bcc",
    "full-access",
    "recovery",
    "refresh",
    "background",
)
FULL_ACCESS = frozenset({"full-access"})
INSTRUCTION_FILES = ("AGENTS.md",)
MCP_TOOL = "test/get_value"
MCP_APPROVAL = "mcp_tool_approval"
COMMAND_APPROVAL = "command_execution"
BACKGROUND = (
    "as the foreground command in a persistent tool terminal, without a trailing "
    "`&`. Keep that terminal running after this turn."
)


def configure(
    environment: dict[str, str],
    home: Path,
    workspace: Path,
    python: Path,
    model: str,
    scenario: str,
) -> None:
    codex = {
        "model": model,
        "model_context_window": 131072,
        "model_auto_compact_token_limit": 98304,
        "model_provider": "gateway",
        "model_providers": {
            "gateway": {
                "name": "Local test gateway",
                "base_url": environment["AI_GATEWAY_BASE_URL"].rstrip("/") + "/v1",
                "env_key": "OPENAI_API_KEY" if scenario == "mcp-once" else "AI_API_KEY",
                "wire_api": "responses",
            }
        },
        "mcp_servers": {
            "test": {
                "command": str(python),
                "args": ["-m", "bcn_test_channel.mcp"],
                "default_tools_approval_mode": "auto",
                "required": True,
            }
        },
    }
    (home / "config.toml").write_text(tomli_w.dumps(codex))
    rules = home / "rules"
    rules.mkdir(exist_ok=True)
    (rules / "bcc.rules").write_text(
        'prefix_rule(pattern=["bcc"], decision="prompt")\n'
    )


def applications(scenario: str) -> tuple[str, ...]:
    return ()


def bcc_command(call: dict[str, Any]) -> str | None:
    return call["input"] if isinstance(call["input"], str) else None


def mcp_output(call: dict[str, Any]) -> Any:
    return call["output"]["structuredContent"]


def approval_call_id(approval: dict[str, Any]) -> str:
    return approval["details"]["itemId"]


def session_processes(
    parent: psutil.Process, environment: dict[str, str]
) -> list[psutil.Process]:
    return [
        child
        for child in parent.children()
        if "app-server" in child.cmdline()
        and child.environ().get(HOME) == environment[HOME]
    ]
