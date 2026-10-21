"""Exercise a real local model through Magpie and the actual Codex CLI."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.e2e
ROOT = Path(__file__).resolve().parents[1]


def test_messages_stream() -> None:
    events = []
    with httpx.stream(
        "POST",
        f"{os.environ['MAGPIE_URL']}/messages",
        headers={
            "x-api-key": os.environ["MAGPIE_GATEWAY_KEY"],
            "anthropic-version": "2023-06-01",
        },
        json={
            "model": "ci/gemma-4-e4b",
            "max_tokens": 64,
            "stream": True,
            "messages": [
                {"role": "user", "content": "Greet the reader in one sentence."}
            ],
        },
        timeout=None,
        trust_env=False,
    ) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line.removeprefix("data: ")))
    artifacts = ROOT / ".artifacts"
    artifacts.mkdir(exist_ok=True)
    (artifacts / "messages.json").write_text(json.dumps(events, indent=2))
    assert any(event.get("type") == "message_stop" for event in events)
    assert any(
        event.get("delta", {}).get("text", "").strip()
        for event in events
        if event.get("type") == "content_block_delta"
    )


def test_codex_tools(tmp_path: Path) -> None:
    project = tmp_path / "project"
    shutil.copytree(ROOT / "tests/fixtures/project", project)
    home = tmp_path / "home"
    home.mkdir()
    (home / ".codex").mkdir()
    artifacts = ROOT / ".artifacts"
    artifacts.mkdir(exist_ok=True)

    environment = {
        "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
        "HOME": str(home),
        "CODEX_HOME": str(home / ".codex"),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "SHELL": "/bin/bash",
        "LANG": "C.UTF-8",
        "MAGPIE_GATEWAY_KEY": os.environ["MAGPIE_GATEWAY_KEY"],
    }
    started = time.monotonic()
    with (
        (artifacts / "codex.jsonl").open("w") as output,
        (artifacts / "codex.stderr").open("w") as errors,
    ):
        result = subprocess.run(
            [
                "codex",
                "exec",
                "--ignore-user-config",
                "--ignore-rules",
                "--skip-git-repo-check",
                "--ephemeral",
                "--json",
                "--sandbox",
                "workspace-write",
                "-c",
                'approval_policy="never"',
                "-c",
                "sandbox_workspace_write.network_access=true",
                "-c",
                'model="ci/gemma-4-e4b"',
                "-c",
                'model_provider="magpie"',
                "-c",
                "model_context_window=32768",
                "-c",
                "model_auto_compact_token_limit=28672",
                "-c",
                'model_providers.magpie.name="Magpie"',
                "-c",
                f"model_providers.magpie.base_url={json.dumps(os.environ['MAGPIE_URL'])}",
                "-c",
                'model_providers.magpie.env_key="MAGPIE_GATEWAY_KEY"',
                "-c",
                'model_providers.magpie.wire_api="responses"',
                "-c",
                "model_providers.magpie.requires_openai_auth=false",
                "-c",
                "model_providers.magpie.supports_websockets=false",
                (
                    "Please rename this project to Codex tool check. "
                    "Its settings are in settings.json."
                ),
            ],
            cwd=project,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=errors,
            check=False,
        )
    shutil.copytree(project, artifacts / "project", dirs_exist_ok=True)
    events = [
        json.loads(line)
        for line in (artifacts / "codex.jsonl").read_text().splitlines()
    ]
    settings = json.loads((project / "settings.json").read_text())
    (artifacts / "result.json").write_text(
        json.dumps(
            {
                "duration_seconds": time.monotonic() - started,
                "returncode": result.returncode,
                "settings": settings,
                "model": json.loads((ROOT / "ci/model.json").read_text()),
            },
            indent=2,
        )
    )
    assert result.returncode == 0, (artifacts / "codex.stderr").read_text()
    assert any(
        event.get("type") == "item.completed"
        and event.get("item", {}).get("type") == "command_execution"
        and event["item"].get("exit_code") == 0
        for event in events
    ), "Codex must execute a successful real command"
    assert settings["project_name"] == "Codex tool check"
    assert settings["theme"] == "dark"
