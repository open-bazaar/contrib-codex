"""Prepare the real local workspace application without touching the host node."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from importlib.metadata import distributions as installed_distributions
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

import tomli_w

root = Path(sys.argv[1]).resolve()
root.mkdir(exist_ok=True)
python = Path(sys.executable)
model = sys.argv[2]
distributions = Path(sys.argv[3]).resolve()
agent_root = Path(sys.argv[4]).resolve()
scenario = sys.argv[5]
environment = {
    name: value
    for name, value in os.environ.items()
    if not name.startswith(("BCN_", "BCC_", "CODEX_", "OPENAI_", "MYSCRM_", "APOLLO_"))
}
endpoint = (
    rf"\\.\pipe\bccj-{uuid4()}"
    if os.name == "nt"
    else "/tmp/bccj-" + str(uuid4()) + ".sock"
)
environment.update(
    BCN_HOME=str(root / "home"),
    CODEX_HOME=str(agent_root / "codex-home"),
    PATH=str(python.parent) + os.pathsep + os.environ["PATH"],
)
(root / "environment.private.json").write_text(
    json.dumps(
        {
            name: environment[name]
            for name in (
                "BCN_HOME",
                "CODEX_HOME",
                "PATH",
            )
        }
    )
)
(root / "environment.private.json").chmod(0o600)
home = Path(environment["CODEX_HOME"])
home.mkdir(parents=True, exist_ok=True)
workspace = agent_root / "workspace"
workspace.mkdir(parents=True, exist_ok=True)
agents = [
    {
        "id": "assistant",
        "name": "Assistant",
        "workspace": str(workspace),
        "mode": "session",
        "channel": [{"kind": "test-channel", "audit_file": str(root / "audit.jsonl")}],
        "harness": [{"kind": "codex", "model": model}],
    }
]
if scenario == "full-access":
    agents[0]["session"] = {"config": {"sandbox": {"policy": "danger-full-access"}}}
codex = {
    "model": model,
    "model_context_window": 131072,
    "model_auto_compact_token_limit": 98304,
    "model_provider": "gateway",
    "model_providers": {
        "gateway": {
            "name": "Local test gateway",
            "base_url": environment["AI_GATEWAY_BASE_URL"].rstrip("/") + "/v1",
            "env_key": "AI_API_KEY",
            "wire_api": "responses",
        }
    },
    "mcp_servers": {
        "test": {
            "command": str(python),
            "args": ["-m", "bcn_codex_test_channel.mcp"],
            "default_tools_approval_mode": "auto",
            "required": True,
        }
    },
}
(home / "config.toml").write_text(tomli_w.dumps(codex))
rules = home / "rules"
rules.mkdir(exist_ok=True)
(rules / "bcc.rules").write_text('prefix_rule(pattern=["bcc"], decision="prompt")\n')
config = root / "node.toml"
command = [str(python), "-m", "bazaar_compute_node"]
print("Bootstrap installed application", config, flush=True)
result = subprocess.run(
    [*command, "--configer-options", str(config), "--help"],
    env=environment,
    check=False,
    capture_output=True,
    text=True,
)
(root / "bootstrap.log").write_text(result.stdout + result.stderr)
print(result.stdout + result.stderr, end="", flush=True)
if result.returncode:
    raise RuntimeError("Application bootstrap failed; see bootstrap.log")
document = tomllib.loads(config.read_text())
for package in document["contribs"]:
    for entry in package["enabled"]:
        if entry["entry"] == "agent":
            entry["config"]["agents"] = agents
        elif entry["entry"] in {"sidecar-unix", "sidecar-windows"}:
            entry["config"] = {"endpoint": endpoint}
document["contribs"] = [
    package
    for package in document["contribs"]
    if package["package"] not in {"bcn-codex-test-channel", "bazaar-contrib-codex"}
]
for package, entry in (
    ("bcn-codex-test-channel", "test-channel-builder"),
    ("bazaar-contrib-codex", "codex-builder"),
):
    (wheel,) = distributions.glob(package.replace("-", "_") + "-*.whl")
    document["contribs"].append(
        {
            "package": package + " @ " + wheel.as_uri(),
            "enabled": [{"entry": entry}],
        }
    )
config.write_text(tomli_w.dumps(document))
print("Activate provider and test channel", config, flush=True)
with (root / "activation.log").open("w") as log:
    result = subprocess.run(
        [*command, "--configer-options", str(config), "--help"],
        env=environment,
        check=False,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
print((root / "activation.log").read_text(), end="", flush=True)
result.check_returncode()
os.environ.update(environment)
print(
    json.dumps(
        {
            "prepared": str(root),
            "model": model,
            "endpoint": endpoint,
            "bcn_version": version("bazaar-compute-node"),
            "agent_id": "assistant",
            "workspace": str(workspace),
            "codex_home": str(home),
            "bcn_packages": {
                package.metadata["Name"]: package.version
                for package in installed_distributions()
                if package.metadata["Name"].startswith("bazaar-")
            },
        }
    ),
    flush=True,
)
