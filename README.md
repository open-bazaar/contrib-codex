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
