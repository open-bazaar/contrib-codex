# Codex repository and CI prerequisites

## Design

The first milestone is an independent Codex plugin repository with a working
local-model CI environment. Messages 190feb59-4fd8-5cf7-bc6a-9009960be3fa and
6d91eb9d-775c-5c03-9898-134270e29ab7 establish this scope and shared action reuse.

The distribution follows the [BCN module roadmap](https://github.com/yuchanns/bazaar-compute-node/blob/b1ef183953f146ace34cd6438388b68855eb0041/plans/2026-10-08-plugins/roadmap.md):
Python 3.15, Apache-2.0, an independent uv lock, wheel and sdist. BCN setup/checks
actions and their original scripts are reused at the same fixed revision,
b1ef183953f146ace34cd6438388b68855eb0041.

[OpenDAL's etcd setup](https://github.com/apache/opendal/blob/f749d40a4101fff1090e5b52a3ca56654c16e819/.github/services/etcd/etcd/action.yml)
uses Docker Compose health checks and exports service endpoints. This repository
uses that pattern for CPU llama.cpp, Magpie 0.1.1305 and PostgreSQL 17.6. Each run
owns its Compose project, volumes and ephemeral loopback ports. Cleanup collects
logs and resource statistics before removing the owned services.

[llama.cpp's tool tests](https://github.com/ggml-org/llama.cpp/blob/339f3242c761f16c27e5ffcde1d35dcc31933a55/tools/server/tests/unit/test_tool_call.py)
include real small GGUF models with function, argument and streaming checks. The
initial candidate here is Google's Gemma 4 E4B QAT Q4_0. Its revision, file size
and SHA256 are fixed in ci/model.json; the actual file is 5.15 GB. The CPU server
image is fixed by digest and uses four CPUs, one slot and a 32768-token context.
CI records actual results rather than treating loading estimates as measured
memory usage.

[Magpie's manual](https://github.com/yetone/magpie/blob/v0.1.1305/docs/reference.md)
documents conversion from a Chat Completions provider to Responses and Messages.
The ci provider connects to the local llama.cpp server. Host clients use a fresh
enabled gateway key. Codex receives an isolated home, an explicit Responses
provider and no inherited user credentials.

Codex is installed from npm latest on every run, and its resolved version is
saved with the evidence. Messages 17855ae0-dada-551f-9c1f-e78bba72516c and
422b9f2e-abdf-57e7-b08c-9c3754b84539 explicitly require following the latest Codex.
Model and service versions remain fixed.

Pytest's base directory is in the checkout cache so Codex can create its execution
helpers outside system temp. The [helper setup](https://github.com/openai/codex/blob/2f7d89b1419bf7064346855b0acde23514b1ebc5/codex-rs/arg0/src/lib.rs)
rejects a home under the system temporary directory.

The native sandbox probe reproduced `bwrap: setting up uid map: Permission denied`
on the Ubuntu 24.04 runner. Its [user-namespace restriction](https://documentation.ubuntu.com/release-notes/24.04/#unprivileged-user-namespace-restrictions)
can be disabled for the current boot. Apply that setting only in the disposable
CI job, then require the native probe to succeed before loading the model.
The tool test continues to use Codex's workspace-write sandbox.

The tests exercise Messages SSE and the actual Codex CLI. Messages
04758160-c743-537d-b692-0849a3d1c5e1, ddd29793-3e76-5b62-9421-cca81843c28a
and e450ec40-886a-5eff-9755-e45520a30038 require a simple tool operation to establish
plugin prerequisites. A natural request asks Codex to rename a project in its
settings file. Acceptance checks a successful real command, the requested saved
name and the retained unrelated setting. PostgreSQL remains available for the next
milestone. The run saves events, stderr, resulting files and service evidence.

The Test step follows model logs and Codex event and stderr files during execution.
Prompt processing and tool activity remain visible until the process completes.

Workflow, job and step names follow the short English names used in OpenDAL's
[Python CI](https://github.com/apache/opendal/blob/f749d40a4101fff1090e5b52a3ca56654c16e819/.github/workflows/ci_bindings_python.yml)
and [behavior tests](https://github.com/apache/opendal/blob/f749d40a4101fff1090e5b52a3ca56654c16e819/.github/workflows/test_behavior_binding_python.yml).

## Tasks

### Task 1: Repository and CI prerequisites

- [x] Create open-bazaar/contrib-codex and the distribution scaffold.
- [x] Reuse BCN setup/checks and independent distribution verification.
- [x] Prepare fixed model weights, inference services, Magpie and PostgreSQL.
- [ ] Complete the Messages and Codex file/command flow on GitHub Actions.
- [ ] Record actual results and submit the milestone for review.

### Task 2: Codex Harness

- [ ] Implement codex-builder/codex using the public HarnessBuilder/Harness contracts.
- [ ] Support mcpServer/elicitation/request and item/tool/requestUserInput through
      the actual Channel, including accept/decline/cancel and session/always
      authorization choices. Verify that an approved real MCP tool can continue.
- [ ] Use TestChannel for inbound requests, output observation, approvals, session
      continuation and recovery.
- [ ] Verify independently installed distributions and supported platforms, then
      submit the implementation for review.

Messages 380e605f-01df-5000-902b-ee44683df6ed and
a85043c8-e3d6-5281-a7c0-7573d96d836d establish MCP interaction as new support:
the previous adapter handled command, file and permissions approval only.
The generated Codex 0.159.2 protocol includes the additional requests. The upstream
[elicitation decision](https://github.com/openai/codex/blob/c7234c1174b27d9c55199fba0d546dc27d1b0415/codex-rs/core/src/mcp_tool_call.rs#L2204)
maps Decline to user rejected MCP tool call. The original request for the observed
failure was not captured, so this evidence does not establish a BCN regression.
