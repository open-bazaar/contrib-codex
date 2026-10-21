"""Collect service evidence and remove only this run's Compose project."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    if not os.environ.get("COMPOSE_PROJECT_NAME"):
        return
    artifacts = ROOT / ".artifacts"
    artifacts.mkdir(exist_ok=True)
    compose = ["docker", "compose", "-f", str(ROOT / ".github/services/compose.yml")]
    try:
        result = subprocess.run(
            [*compose, "logs", "--no-color"],
            text=True,
            capture_output=True,
            check=False,
        )
        key = os.environ.get("MAGPIE_GATEWAY_KEY")
        logs = result.stdout + result.stderr
        if key:
            logs = logs.replace(key, "[local gateway key]")
        (artifacts / "services.log").write_text(logs)
        ids = subprocess.check_output(
            [*compose, "ps", "--all", "--quiet"], text=True
        ).split()
        if ids:
            with (artifacts / "resources.jsonl").open("w") as handle:
                subprocess.run(
                    ["docker", "stats", "--no-stream", "--format", "{{json .}}", *ids],
                    stdout=handle,
                    check=False,
                )
    finally:
        subprocess.run([*compose, "down", "--volumes", "--remove-orphans"], check=True)


if __name__ == "__main__":
    main()
