# bazaar-contrib-codex

Codex Harness provider for [Bazaar Compute Node](https://github.com/yuchanns/bazaar-compute-node).

## Setup

Install and configure the Codex CLI, then install BCN and this provider:

```sh
uv tool install bazaar-compute-node
bcn contrib add codex --enable
```

Add an agent using an installed Channel provider:

```sh
bcn agent add --name Editor --channel CHANNEL_KIND --harness codex
bcn run
```

## Configuration

Optional fields in the agent's `codex` Harness configuration:

- `model`: Codex model; defaults to the Codex configuration.
- `effort`: Reasoning effort; defaults to the Codex configuration.

## Release

Run the **Release** GitHub Actions workflow with a version such as `0.3.0rc1`.
Stable releases run from `main`. The workflow checks the fixed release commit,
builds a wheel and sdist, verifies their installation on Linux, macOS and Windows,
publishes the same files to PyPI, and verifies a clean PyPI installation before
pushing the annotated `vVERSION` tag.

PyPI Trusted Publishing uses repository `open-bazaar/contrib-codex`, workflow
`release.yml`, and GitHub environment `pypi` for project `bazaar-contrib-codex`.
