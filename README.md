# bazaar-contrib-codex

Codex harness provider for Bazaar Compute Node.

The first milestone establishes an independent package and reproducible real-model CI.
It runs the actual Codex CLI against a local Gemma model through Magpie. A small
request renames a project in its settings file. The workflow checks an actual
successful command and the saved configuration, and retains inference and execution
evidence. PostgreSQL is also provisioned for the later Harness milestone.

| Component | Fixed input |
| --- | --- |
| Python / uv | BCN shared setup: Python 3.15 / uv 0.13.0 |
| Codex CLI | Latest npm release; actual version saved with each CI run |
| Model | Google Gemma 4 E4B QAT Q4_0, revision and SHA256 in ci/model.json |
| llama.cpp | CPU server image digest in .github/services/compose.yml |
| Magpie | 0.1.1305, image digest |
| PostgreSQL | 17.6-bookworm, image digest |

The model file is 5.15 GB and can be downloaded without an account. The GitHub-hosted
Linux job uses 4 CPU and 16 GB RAM; the inference container is limited to 4 CPU/10 GB, with a
32768-token context. Actual latency, model reliability and resource evidence come from
the workflow run. Successful service startup alone does not establish coding-agent reliability.

## Run the prerequisites locally

Requirements: Linux, Docker Compose, uv and Node.js/npm, with sufficient RAM and disk
for the model and images. All model inference is local; no provider API key is needed.

```sh
uv sync --locked
npm install --prefix .cache/codex --package-lock=false @openai/codex@latest
export PATH="$PWD/.cache/codex/node_modules/.bin:$PATH"
uv run --no-sync python ci/prepare.py
source .cache/environment
uv run --no-sync pytest -m e2e -v
uv run --no-sync python ci/cleanup.py
```

Run cleanup after a failed test as well. Only the generated Compose project and its
volumes are removed; model weights remain cached. Evidence is written to `.artifacts/`.
Each run uses a separate database and Magpie configuration, ephemeral ports, and an
isolated Codex home. The child process receives its local model credential in an isolated environment.

Pytest keeps its isolated files in `.cache/pytest`: Codex refuses to create helper
aliases under the system temporary directory.

The Ubuntu 24.04 CI job enables unprivileged user namespaces on its disposable
runner so Codex's Bubblewrap sandbox can start. A native sandbox probe runs before
model startup and saves its output in `sandbox.log`. Codex uses workspace-write
permissions for the file operation.

## Development

CI reuses the [BCN setup and checks actions](https://github.com/yuchanns/bazaar-compute-node/tree/b1ef183953f146ace34cd6438388b68855eb0041/.github/actions)
and their original scripts from the same fixed revision. Package builds verify both
wheel and sdist installations outside this checkout.

The [implementation plan](plans/2026-10-21-real-model-ci/plan.md) tracks the initial
infrastructure trial and the subsequent Harness implementation with TestChannel
acceptance. The package currently contains the first milestone's scaffolding.
