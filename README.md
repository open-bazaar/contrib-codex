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

## Acceptance

CI installs published BCN with the built provider and a real TCP TestChannel in
an isolated environment. A single agent receives a natural request for a five-second
reminder. Reading and replying to messages also exercise bcc. Native command approval,
steering, continuation, app-server recovery, instruction refresh and shutdown exercise
the Harness interfaces in that conversation. Acceptance checks interface outcomes
and effective bcc execution; reply wording and tool order are unrestricted. The
artifact retains actual replies, public audit events, tool results and owned-process
cleanup results.

The same conversation uses a local MCP server with one `save_note` tool. It
replaces a real note file and returns its path and text. Acceptance verifies the
actual MCP call and saved content for ONCE, SESSION and ALWAYS approvals. A
follow-up checks that SESSION avoids another prompt in that session; instruction
refresh creates a new session and checks that ALWAYS remains effective there.

After the application exits, a native Harness turn asks the same local model to
start a temporary loopback preview server. The probe observes actual background
terminals after that turn completes and reconciles its completed state. A natural
follow-up in the same session then checks active reconciliation, stream closure
and Codex's persisted interruption state before stopping the session and checking
owned processes and pending requests. This native session uses the `never`
approval policy; channel approval behavior is checked by the application conversation.
