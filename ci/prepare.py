"""Prepare owned real-model services and export their ephemeral endpoints."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shlex
import subprocess
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    cache = ROOT / ".cache"
    cache.mkdir(exist_ok=True)
    model = json.loads((ROOT / "ci/model.json").read_text())
    directory = cache / "models"
    directory.mkdir(exist_ok=True)
    path = directory / model["file"]
    if not path.exists():
        url = (
            f"https://huggingface.co/{model['repository']}/resolve/"
            f"{model['revision']}/{model['file']}"
        )
        temporary = path.with_suffix(".download")
        print(f"Downloading {model['repository']} at {model['revision']}", flush=True)
        try:
            with (
                urllib.request.urlopen(url) as response,
                temporary.open("wb") as output,
            ):
                while chunk := response.read(8 * 1024 * 1024):
                    output.write(chunk)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    with path.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    if digest != model["sha256"] or path.stat().st_size != model["size"]:
        raise RuntimeError("The cached model does not match ci/model.json")
    print(f"Model verified: {digest}", flush=True)

    environment = {
        "MODEL_DIR": str(directory),
        "COMPOSE_PROJECT_NAME": f"codex-ci-{secrets.token_hex(6)}",
    }
    output = Path(os.environ.get("GITHUB_ENV", cache / "environment"))
    with output.open("a") as handle:
        for key, value in environment.items():
            if "GITHUB_ENV" in os.environ:
                handle.write(f"{key}={value}\n")
            else:
                handle.write(f"export {key}={shlex.quote(value)}\n")
    os.environ.update(environment)
    compose = ["docker", "compose", "-f", str(ROOT / ".github/services/compose.yml")]
    subprocess.run([*compose, "up", "--detach", "--wait"], check=True)
    subprocess.run(
        [
            *compose,
            "exec",
            "-T",
            "magpie",
            "magpie",
            "provider",
            "add",
            "ci",
            "url=http://model:8080/v1",
            "key=local-ci-only",
            "models=gemma-4-e4b",
        ],
        check=True,
        capture_output=True,
    )
    key = f"ci-{secrets.token_urlsafe(32)}"
    subprocess.run(
        [
            *compose,
            "exec",
            "-T",
            "magpie",
            "magpie",
            "gateway-key",
            "add",
            "CI",
            "--key",
            "-",
        ],
        input=f"{key}\n",
        text=True,
        capture_output=True,
        check=True,
    )
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::add-mask::{key}")
    for service, port, name in [
        ("model", "8080", "MODEL_URL"),
        ("magpie", "3425", "MAGPIE_URL"),
        ("postgres", "5432", "PGPORT"),
    ]:
        address = subprocess.check_output(
            [*compose, "port", service, port], text=True
        ).strip()
        environment[name] = (
            address.rsplit(":", 1)[1] if name == "PGPORT" else f"http://{address}/v1"
        )
    environment.update(
        MAGPIE_GATEWAY_KEY=key,
        PGHOST="127.0.0.1",
        PGUSER="codex",
        PGPASSWORD="local-ci-only",
        PGDATABASE="codex",
    )
    # GITHUB_ENV is read by later steps. The local file can be sourced by Bash.
    with output.open("a") as handle:
        for name, value in environment.items():
            if name not in {"MODEL_DIR", "COMPOSE_PROJECT_NAME"}:
                handle.write(f"{name}={value}\n")
    if "GITHUB_ENV" not in os.environ:
        output.write_text(
            "".join(
                f"export {name}={shlex.quote(value)}\n"
                for name, value in environment.items()
            )
        )
        output.chmod(0o600)
    print("Real model, authenticated Magpie and PostgreSQL are ready.", flush=True)


if __name__ == "__main__":
    main()
