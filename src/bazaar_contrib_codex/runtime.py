"""Implement the public Harness with session-owned Codex app-server processes."""

# Provider budgets come from the public Harness contract.
# ruff: noqa: ASYNC109

from __future__ import annotations

import asyncio
import logging
from math import ceil
from pathlib import Path
from typing import Any

from bazaar_contrib_audit import Audit, AuditContext
from bazaar_contrib_harness import (
    ApprovalHandler,
    ContentDelta,
    ContentDeltaKind,
    ContextCompactionCompleted,
    ContextCompactionStarted,
    FileChangeEntry,
    HarnessEventPayload,
    HarnessExpire,
    HarnessFailure,
    HarnessFailureReason,
    HarnessInput,
    HarnessOutput,
    HarnessSession,
    HarnessSessionReconciliation,
    HarnessSessionUnavailable,
    HarnessTurn,
    ProviderCallResult,
    ProviderCallStatus,
    SandboxPolicy,
    SessionInput,
    TokenUsage,
    ToolCall,
    ToolCallCompleted,
    ToolCallDeltaKind,
    ToolCallFailed,
    ToolCallInteraction,
    ToolCallPatchUpdated,
    ToolCallStarted,
    ToolCallTextDelta,
    TurnCompleted,
    TurnFailed,
    TurnStarted,
    UsageUpdated,
)
from bazaar_contrib_timer import Timer
from bazaar_contrib_timer import timeout as timer_timeout
from pydantic import BaseModel, JsonValue, TypeAdapter, ValidationError

from .approval import decline, interact
from .source import SourceAudit
from .transport import AppServer, AppServerError, AppServerUnavailable, Packet, failure

logger = logging.getLogger(__name__)

SANDBOX_POLICIES = {
    SandboxPolicy.WORKSPACE_WRITE: "workspace-write",
    SandboxPolicy.DANGER_FULL_ACCESS: "danger-full-access",
}

_TOOL_TYPE_NAMES = {
    "commandExecution": "command",
    "fileChange": "file_change",
    "mcpToolCall": "mcp_tool",
    "dynamicToolCall": "dynamic_tool",
    "collabAgentToolCall": "agent",
    "subAgentActivity": "agent",
    "webSearch": "web_search",
    "imageView": "image_view",
    "imageGeneration": "image_generation",
    "sleep": "sleep",
    "functionCallOutput": "function",
}

_JSON_VALUE_ADAPTER = TypeAdapter[JsonValue](JsonValue)


class FsWatchResponse(BaseModel):
    path: Path


class BackgroundTerminalsResponse(BaseModel):
    data: list[dict[str, Any]]


def turn_failure(
    turn: dict[str, Any], *, needs_reconciliation: bool
) -> HarnessFailure | None:
    """Classify the same native terminal state in notifications and history."""

    error = turn.get("error") or {}
    match turn["status"]:
        case "completed":
            return None
        case "failed":
            reason = HarnessFailureReason.EXECUTION
            message = error.get("message") or "provider_failed"
        case _:
            reason = HarnessFailureReason.PROTOCOL
            message = f"Unsupported Codex turn status: {turn['status']}"
    return HarnessFailure(
        reason,
        message,
        needs_reconciliation=needs_reconciliation
        and reason is HarnessFailureReason.PROTOCOL,
        code=error.get("code"),
    )


class TurnStream:
    """Translate source events while the transport independently routes interactions."""

    def __init__(
        self,
        connection: Connection,
        turn: HarnessTurn,
        approval: ApprovalHandler,
        timeout: float,
    ):
        self.connection = connection
        self.turn = turn
        self.approval = approval
        self.timeout = timeout
        self.provider_turn_id: str | None = None
        self.finished = False
        self.initial_error: Exception | None = None

    def __aiter__(self) -> TurnStream:
        return self

    def output(self, payload: HarnessEventPayload, packet: Packet) -> HarnessOutput:
        return HarnessOutput(
            session_id=self.turn.session_id,
            turn_id=self.turn.id,
            payload=payload,
            provider="codex",
            provider_event=packet.method,
            provider_session_id=self.connection.session.provider_thread_id,
            provider_turn_id=self.provider_turn_id,
            occurred_at_ms=(
                packet.params.get("completedAtMs")
                or packet.params.get("startedAtMs")
                or (packet.model_extra or {}).get("emittedAtMs")
            ),
            raw=packet.model_dump(mode="json", exclude_none=True),
        )

    async def __anext__(self) -> HarnessOutput:
        if self.finished:
            raise StopAsyncIteration
        try:
            if self.initial_error is not None:
                error = self.initial_error
                self.finished = True
                return HarnessOutput(
                    session_id=self.turn.session_id,
                    turn_id=self.turn.id,
                    payload=TurnFailed(
                        event_name="codex.turn.start.unknown",
                        failure=failure(error, needs_reconciliation=True),
                    ),
                    provider="codex",
                    provider_event="turn/start",
                    provider_session_id=self.connection.session.provider_thread_id,
                )
            while True:
                packet = await self.connection.server.events.get()
                if isinstance(packet, Exception):
                    raise packet
                params = packet.params
                if params.get("threadId") != self.connection.session.provider_thread_id:
                    continue
                provider_turn_id = params.get("turnId") or (
                    params.get("turn") or {}
                ).get("id")
                if provider_turn_id is not None:
                    if (
                        self.provider_turn_id is not None
                        and provider_turn_id != self.provider_turn_id
                    ):
                        continue
                    self.provider_turn_id = provider_turn_id
                    self.connection.owner.turns[self.turn.session_id] = (
                        self.turn.id,
                        provider_turn_id,
                    )
                match packet.method:
                    case "turn/started":
                        return self.output(
                            TurnStarted(event_name="codex.turn.started"), packet
                        )
                    case "turn/completed":
                        self.finished = True
                        terminal = turn_failure(
                            params["turn"], needs_reconciliation=True
                        )
                        metadata = {
                            "status": params["turn"]["status"],
                            "error": params["turn"].get("error"),
                        }
                        return self.output(
                            TurnCompleted(
                                event_name="codex.turn.completed", metadata=metadata
                            )
                            if terminal is None
                            else TurnFailed(
                                event_name="codex.turn.failed",
                                failure=terminal,
                                metadata=metadata,
                            ),
                            packet,
                        )
                    case (
                        "item/agentMessage/delta"
                        | "item/plan/delta"
                        | "item/reasoning/summaryTextDelta"
                        | "item/reasoning/textDelta"
                    ):
                        kind, index = {
                            "item/agentMessage/delta": (
                                ContentDeltaKind.AGENT_MESSAGE,
                                None,
                            ),
                            "item/plan/delta": (ContentDeltaKind.PLAN, None),
                            "item/reasoning/summaryTextDelta": (
                                ContentDeltaKind.REASONING_SUMMARY,
                                params.get("summaryIndex"),
                            ),
                            "item/reasoning/textDelta": (
                                ContentDeltaKind.REASONING_TEXT,
                                params.get("contentIndex"),
                            ),
                        }[packet.method]
                        return self.output(
                            ContentDelta(
                                kind=kind,
                                text=params["delta"],
                                index=index
                                if isinstance(index, int)
                                and not isinstance(index, bool)
                                else None,
                            ),
                            packet,
                        )
                    case "item/started" | "item/completed":
                        item = params["item"]
                        if item["type"] in {
                            "userMessage",
                            "hookPrompt",
                            "agentMessage",
                            "plan",
                            "reasoning",
                            "enteredReviewMode",
                            "exitedReviewMode",
                        }:
                            continue
                        if not isinstance(item.get("id"), str) or not item["id"]:
                            continue
                        timestamp = (
                            "startedAtMs"
                            if packet.method == "item/started"
                            else "completedAtMs"
                        )
                        if not isinstance(params.get(timestamp), int) or isinstance(
                            params[timestamp], bool
                        ):
                            raise AppServerError(
                                f"item lifecycle requires {timestamp}",
                                reason=HarnessFailureReason.PROTOCOL,
                            )
                        if item["type"] == "contextCompaction":
                            return self.output(
                                ContextCompactionStarted(compaction_id=item["id"])
                                if packet.method == "item/started"
                                else ContextCompactionCompleted(
                                    compaction_id=item["id"]
                                ),
                                packet,
                            )
                        name = item.get("tool") or item.get("name")
                        if item.get("server") and item.get("tool"):
                            name = f"{item['server']}/{item['tool']}"
                        if not name and item.get("command"):
                            parts = item["command"].strip().split(maxsplit=1)
                            name = parts[0] if parts else None
                        name = name or _TOOL_TYPE_NAMES.get(item["type"], "tool")
                        started = packet.method == "item/started"
                        fields = (
                            ("arguments", "input", "command")
                            if started
                            else ("result", "output", "contentItems")
                        )
                        field = next((field for field in fields if field in item), None)
                        try:
                            value = (
                                None
                                if field is None
                                else _JSON_VALUE_ADAPTER.validate_python(
                                    item[field], strict=True
                                )
                            )
                        except ValidationError as error:
                            raise AppServerError(
                                f"item lifecycle {field} must be a JSON value",
                                reason=HarnessFailureReason.PROTOCOL,
                            ) from error
                        call = ToolCall(
                            call_id=item["id"],
                            name=name,
                            input=value if started else None,
                            output=None if started else value,
                        )
                        error = item.get("error")
                        if started:
                            payload = ToolCallStarted(call=call)
                        elif (
                            item.get("status") in {"failed", "declined", "errored"}
                            or error is not None
                        ):
                            payload = ToolCallFailed(
                                call=call,
                                error_message=(
                                    error.get("message")
                                    if isinstance(error, dict)
                                    else error
                                )
                                if isinstance(error, (dict, str))
                                else None,
                            )
                        else:
                            payload = ToolCallCompleted(call=call)
                        return self.output(payload, packet)
                    case "item/fileChange/patchUpdated":
                        for change in params["changes"]:
                            if (
                                not isinstance(change["path"], str)
                                or not change["path"]
                                or not isinstance(change["diff"], str)
                                or change["kind"]["type"]
                                not in {"add", "delete", "update"}
                            ):
                                raise AppServerError(
                                    "Invalid Codex file change patch",
                                    reason=HarnessFailureReason.PROTOCOL,
                                )
                        return self.output(
                            ToolCallPatchUpdated(
                                call_id=params["itemId"],
                                changes=tuple(
                                    FileChangeEntry(
                                        path=change["path"],
                                        kind=change["kind"]["type"],
                                        patch=change["diff"],
                                    )
                                    for change in params["changes"]
                                ),
                            ),
                            packet,
                        )
                    case "item/commandExecution/terminalInteraction":
                        return self.output(
                            ToolCallInteraction(
                                call_id=params["itemId"],
                                stdin=params["stdin"],
                                process_id=params.get("processId"),
                            ),
                            packet,
                        )
                    case "thread/tokenUsage/updated":
                        usage = params["tokenUsage"]
                        tokens = {
                            key: TokenUsage(
                                input_tokens=max(
                                    0, value["inputTokens"] - value["cachedInputTokens"]
                                ),
                                cached_input_tokens=value["cachedInputTokens"],
                                cache_write_input_tokens=value.get(
                                    "cacheWriteInputTokens"
                                ),
                                output_tokens=value["outputTokens"],
                                reasoning_output_tokens=value.get(
                                    "reasoningOutputTokens"
                                ),
                                total_tokens=value["totalTokens"],
                            )
                            for key in ("total", "last")
                            for value in (usage[key],)
                        }
                        return self.output(
                            UsageUpdated(
                                total=tokens["total"],
                                last=tokens["last"],
                                model_context_window=usage.get("modelContextWindow"),
                            ),
                            packet,
                        )
                    case "error":
                        if not isinstance(params["willRetry"], bool):
                            raise AppServerError(
                                "params.willRetry must be a bool",
                                reason=HarnessFailureReason.PROTOCOL,
                            )
                        metadata: dict[str, JsonValue] = {
                            "will_retry": params["willRetry"]
                        }
                        info = params["error"].get("codexErrorInfo")
                        if isinstance(info, dict):
                            if len(info) != 1:
                                raise AppServerError(
                                    "codexErrorInfo must contain one error type",
                                    reason=HarnessFailureReason.PROTOCOL,
                                )
                            info = next(iter(info))
                        if info is not None:
                            metadata["provider_error_type"] = info
                        return self.output(
                            TurnStarted(
                                event_name="codex.turn.error", metadata=metadata
                            ),
                            packet,
                        )
                    case _:
                        if packet.method and packet.method.startswith("item/"):
                            suffix = packet.method.rsplit("/", 1)[-1]
                            if suffix in {"progress", "outputDelta"}:
                                return self.output(
                                    ToolCallTextDelta(
                                        call_id=params["itemId"],
                                        kind=ToolCallDeltaKind.PROGRESS
                                        if suffix == "progress"
                                        else ToolCallDeltaKind.OUTPUT,
                                        text=params["message"]
                                        if suffix == "progress"
                                        else params["delta"],
                                    ),
                                    packet,
                                )
                        continue
        except Exception as error:  # noqa: BLE001 -- Report native stream failures.
            self.finished = True
            protocol = isinstance(error, (KeyError, TypeError, ValueError)) or (
                isinstance(error, AppServerError)
                and not isinstance(error, AppServerUnavailable)
            )
            if isinstance(error, (KeyError, TypeError, ValueError)):
                error = AppServerError(str(error), reason=HarnessFailureReason.PROTOCOL)
            return HarnessOutput(
                session_id=self.turn.session_id,
                turn_id=self.turn.id,
                payload=TurnFailed(
                    event_name="codex.turn.protocol.unknown"
                    if protocol
                    else "codex.turn.transport.unknown",
                    failure=failure(error, needs_reconciliation=True),
                ),
                provider="codex",
                provider_session_id=self.connection.session.provider_thread_id,
                provider_turn_id=self.provider_turn_id,
            )

    async def aclose(self):
        try:
            if (
                not self.finished
                and self.provider_turn_id is not None
                and self.connection.server.process.returncode is None
            ):
                async with timer_timeout(
                    self.connection.owner.timer, ceil(self.timeout * 1000)
                ):
                    await self.connection.server.request(
                        "turn/interrupt",
                        {
                            "threadId": self.connection.session.provider_thread_id,
                            "turnId": self.provider_turn_id,
                        },
                    )
        finally:
            self.finished = True


class Connection:
    server: AppServer

    def __init__(self, owner: CodexHarness, request: SessionInput):
        self.owner = owner
        self.request = request
        self.session = HarnessSession(id=request.id)
        self.stream: TurnStream | None = None
        self.watch_targets: dict[str, Path] = {}
        self.home: Path | None = None

    async def open(self, *, timeout: float):
        for attempt in range(2):
            self.source = SourceAudit(
                self.owner.audit,
                AuditContext(
                    agent_id=self.owner.input.agent_id,
                    harness="codex",
                    session_id=self.request.id,
                    provider_session_id=self.session.provider_thread_id,
                ),
            )
            async with timer_timeout(
                self.owner.timer, ceil(self.owner.input.startup_timeout * 1000)
            ):
                self.server = await AppServer.start(
                    self.request.workspace,
                    self.request.environment,
                    self.answer,
                    self.notify,
                    timer=self.owner.timer,
                    timeout=timeout,
                    source=self.source,
                )
            try:
                async with timer_timeout(
                    self.owner.timer, ceil(self.owner.input.startup_timeout * 1000)
                ):
                    result = await self.server.initialize()
                self.home = Path(result["codexHome"])
                await self.watch()
                return
            except BaseException as error:
                if hasattr(self, "server"):
                    self.owner.close(self, timeout=timeout)
                if not isinstance(error, TimeoutError) or attempt == 1:
                    raise
                logger.warning(
                    "Retrying Codex initialization for session %s after a timeout",
                    self.session.id,
                )

    async def answer(self, packet: Packet) -> dict[str, Any]:
        stream = self.stream
        if stream is None or stream.finished:
            logger.warning("No active turn owns Codex request %s", packet.id)
            return decline(packet)
        if packet.params.get("threadId") != self.session.provider_thread_id:
            logger.warning("Codex request %s refers to a different thread", packet.id)
            return decline(packet)
        provider_turn_id = packet.params.get("turnId")
        if (
            provider_turn_id is not None
            and stream.provider_turn_id is not None
            and provider_turn_id != stream.provider_turn_id
        ):
            logger.warning("Codex request %s refers to a different turn", packet.id)
            return decline(packet)
        try:
            return await interact(
                packet,
                session_id=self.session.id,
                turn_id=stream.turn.id,
                handler=stream.approval,
                timeout=stream.timeout,
            )
        except Exception:
            logger.exception("Could not handle Codex request %s", packet.id)
            return decline(packet)

    def notify(self, packet: Packet):
        if packet.method == "skills/changed" or (
            packet.method == "fs/changed"
            and self.watch_targets.get(packet.params["watchId"])
            in {Path(path) for path in packet.params["changedPaths"]}
        ):
            self.owner.events.put_nowait(HarnessExpire(session_id=self.session.id))

    async def watch(self):
        assert self.home is not None
        self.watch_targets = {
            "workspace": self.request.workspace / "AGENTS.md",
            "codex-home": self.home / "AGENTS.md",
        }
        for id, path in self.watch_targets.items():
            async with timer_timeout(
                self.owner.timer, ceil(self.owner.input.startup_timeout * 1000)
            ):
                result = FsWatchResponse.model_validate(
                    await self.server.request(
                        "fs/watch",
                        {
                            "watchId": id,
                            "path": str(path),
                        },
                    )
                )
            if result.path != path:
                raise AppServerError(
                    "fs/watch returned a different path",
                    reason=HarnessFailureReason.PROTOCOL,
                )


class CodexHarness:
    def __init__(
        self,
        input: HarnessInput,
        timer: Timer,
        audit: Audit,
        model: str | None,
        effort: str | None,
    ):
        self.input = input
        self.timer = timer
        self.audit = audit
        self.model = model
        self.effort = effort
        self.connections: dict[str, Connection] = {}
        self.sessions: dict[str, SessionInput] = {}
        self.turns: dict[str, tuple[str, str]] = {}
        self.events: asyncio.Queue[HarnessExpire] = asyncio.Queue()
        self.closing: set[asyncio.Task[None]] = set()

    def close(self, connection: Connection, *, timeout: float) -> asyncio.Task[None]:
        task = connection.server.close(timeout=timeout)
        self.closing.add(task)
        task.add_done_callback(self.closing.discard)
        return task

    async def receive_event(self) -> HarnessExpire:
        return await self.events.get()

    async def start_session(
        self,
        request: SessionInput,
        *,
        timeout: float,
    ) -> ProviderCallResult[HarnessSession]:
        self.sessions[request.id] = request
        previous = self.connections.pop(request.id, None)
        if previous is not None:
            self.close(previous, timeout=timeout)
        connection = Connection(self, request)
        try:
            await connection.open(timeout=timeout)
            async with timer_timeout(self.timer, ceil(timeout * 1000)):
                result = await connection.server.request(
                    "thread/start",
                    {
                        "cwd": str(request.workspace),
                        "model": self.model,
                        "developerInstructions": "\n\n".join(request.instructions),
                        "approvalPolicy": "on-request",
                        "sandbox": SANDBOX_POLICIES[request.sandbox_policy],
                    },
                )
                connection.session = HarnessSession(
                    id=request.id, provider_thread_id=result["thread"]["id"]
                )
            self.connections[request.id] = connection
            return ProviderCallResult(
                status=ProviderCallStatus.CONFIRMED, value=connection.session
            )
        except BaseException as error:
            if hasattr(connection, "server"):
                self.close(connection, timeout=timeout)
            if isinstance(
                error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)
            ):
                raise
            return ProviderCallResult(
                status=ProviderCallStatus.FAILED,
                failure=failure(
                    error,
                    needs_reconciliation=isinstance(
                        error, (AppServerUnavailable, TimeoutError)
                    ),
                ),
            )

    async def start_turn(
        self,
        session: HarnessSession,
        turn: HarnessTurn,
        approval_handler: ApprovalHandler,
        *,
        timeout: float,
    ) -> TurnStream:
        connection = self.connections.get(session.id)
        if (
            connection is None
            or connection.server.closed
            or connection.server.process.returncode is not None
        ):
            raise HarnessSessionUnavailable("The session's Codex app-server has exited")
        if connection.stream is not None and not connection.stream.finished:
            raise RuntimeError("The Codex session already owns an active turn")
        stream = TurnStream(connection, turn, approval_handler, timeout)
        connection.stream = stream
        connection.source.turn_id = turn.id
        self.turns.pop(session.id, None)
        inputs = [{"type": "text", "text": turn.input_text}]
        for attachment in turn.attachments:
            inputs.append(
                {"type": "localImage", "path": str(attachment.path)}
                if (attachment.media_type or "").startswith("image/")
                else {"type": "text", "text": f"Attached file: {attachment.path}"}
            )
        try:
            async with timer_timeout(self.timer, ceil(timeout * 1000)):
                result = await connection.server.request(
                    "turn/start",
                    {
                        "threadId": session.provider_thread_id,
                        "input": inputs,
                        "clientUserMessageId": turn.id,
                        "cwd": str(connection.request.workspace),
                        "model": self.model,
                        "effort": self.effort,
                        "approvalPolicy": "on-request",
                        "sandboxPolicy": (
                            {"type": "dangerFullAccess"}
                            if connection.request.sandbox_policy
                            is SandboxPolicy.DANGER_FULL_ACCESS
                            else {
                                "type": "workspaceWrite",
                                "writableRoots": [str(connection.request.workspace)],
                                "networkAccess": True,
                            }
                        ),
                    },
                )
                stream.provider_turn_id = result["turn"]["id"]
                self.turns[session.id] = (turn.id, result["turn"]["id"])
        except asyncio.CancelledError:
            # The request may already have reached Codex without returning its
            # turn ID. Retire the process so an unowned turn cannot keep running;
            # the retained session input/thread can be resumed by reconciliation.
            stream.finished = True
            connection.stream = None
            self.connections.pop(session.id, None)
            self.close(connection, timeout=timeout)
            raise
        except (
            AppServerError,
            OSError,
            TimeoutError,
            KeyError,
            TypeError,
            ValueError,
        ) as error:
            stream.initial_error = error
        return stream

    async def steer_turn(
        self,
        session: HarnessSession,
        turn: HarnessTurn,
        input_text: str,
        *,
        timeout: float,
    ) -> bool:
        connection = self.connections.get(session.id)
        if (
            connection is None
            or connection.stream is None
            or connection.stream.finished
        ):
            return False
        stream = connection.stream
        if stream.turn.id != turn.id or stream.provider_turn_id is None:
            return False
        try:
            async with timer_timeout(self.timer, ceil(timeout * 1000)):
                result = await connection.server.request(
                    "turn/steer",
                    {
                        "threadId": session.provider_thread_id,
                        "expectedTurnId": stream.provider_turn_id,
                        "input": [{"type": "text", "text": input_text}],
                    },
                )
        except AppServerError, OSError, TimeoutError:
            return False
        return result["turnId"] == stream.provider_turn_id

    async def reconcile_session(
        self,
        session: HarnessSession,
        turn: HarnessTurn | None,
        approval_handler: ApprovalHandler | None,
        *,
        timeout: float,
    ) -> ProviderCallResult[HarnessSessionReconciliation]:
        if session.provider_thread_id is None:
            return ProviderCallResult(
                status=ProviderCallStatus.FAILED,
                failure=HarnessFailure(
                    HarnessFailureReason.STATE_MISMATCH,
                    "The Codex session has no provider thread to reconcile",
                ),
            )
        connection = self.connections.pop(session.id, None)
        if connection is None:
            request = self.sessions.get(session.id)
            if request is None:
                return ProviderCallResult(
                    status=ProviderCallStatus.FAILED,
                    failure=HarnessFailure(
                        HarnessFailureReason.UNAVAILABLE,
                        "No session input owns this Codex session",
                    ),
                )
            connection = Connection(self, request)
            connection.session = session
        for attempt in range(2):
            if (
                not hasattr(connection, "server")
                or connection.server.closed
                or connection.server.process.returncode is not None
                or connection.server.reader.done()
            ):
                if hasattr(connection, "server"):
                    self.close(connection, timeout=timeout)
                try:
                    await connection.open(timeout=timeout)
                except (
                    AppServerError,
                    OSError,
                    TimeoutError,
                    KeyError,
                    TypeError,
                    ValueError,
                ) as error:
                    return ProviderCallResult(
                        status=ProviderCallStatus.FAILED,
                        failure=failure(
                            error,
                            needs_reconciliation=isinstance(
                                error, (AppServerUnavailable, TimeoutError)
                            ),
                        ),
                    )
            try:
                async with timer_timeout(self.timer, ceil(timeout * 1000)):
                    result = await connection.server.request(
                        "thread/read",
                        {
                            "threadId": session.provider_thread_id,
                            "includeTurns": True,
                        },
                    )
                thread = result["thread"]
                if thread["id"] != session.provider_thread_id:
                    raise AppServerError(
                        "Codex returned a different provider thread",
                        reason=HarnessFailureReason.STATE_MISMATCH,
                    )
                previous = connection.stream
                status = thread["status"]["type"]
                stream = None
                if status == "active":
                    turns = [
                        item
                        for item in thread["turns"]
                        if item["status"] == "inProgress"
                    ]
                    if (
                        turn is None
                        or approval_handler is None
                        or len(turns) != 1
                        or self.turns.get(session.id) != (turn.id, turns[0]["id"])
                    ):
                        raise AppServerError(
                            "The active Codex turn cannot be reconciled",
                            reason=HarnessFailureReason.STATE_MISMATCH,
                        )
                    if previous is not None:
                        previous.finished = True
                    stream = TurnStream(connection, turn, approval_handler, timeout)
                    stream.provider_turn_id = turns[0]["id"]
                    connection.stream = stream
                    connection.source.turn_id = turn.id
                    connection.source.turns[turns[0]["id"]] = turn.id
                elif status in {"idle", "notLoaded"}:
                    async with timer_timeout(self.timer, ceil(timeout * 1000)):
                        response = await connection.server.request(
                            "thread/resume",
                            {
                                "threadId": session.provider_thread_id,
                                "cwd": str(connection.request.workspace),
                                "model": self.model,
                                "developerInstructions": "\n\n".join(
                                    connection.request.instructions
                                ),
                                "approvalPolicy": "on-request",
                                "sandbox": SANDBOX_POLICIES[
                                    connection.request.sandbox_policy
                                ],
                            },
                        )
                    if response["thread"]["id"] != session.provider_thread_id:
                        raise AppServerError(
                            "Codex resumed a different provider thread",
                            reason=HarnessFailureReason.STATE_MISMATCH,
                        )
                    if previous is not None:
                        previous.finished = True
                    connection.stream = None
                else:
                    raise AppServerError(
                        f"Unsupported Codex thread status: {status}",
                        reason=HarnessFailureReason.PROTOCOL,
                    )
                self.connections[session.id] = connection
                return ProviderCallResult(
                    status=ProviderCallStatus.CONFIRMED,
                    value=HarnessSessionReconciliation(connection.session, stream),
                )
            except asyncio.CancelledError:
                if hasattr(connection, "server"):
                    self.close(connection, timeout=timeout)
                raise
            except (AppServerUnavailable, TimeoutError) as error:
                logger.warning(
                    "Codex recovery attempt %s failed for session %s: %s",
                    attempt + 1,
                    session.id,
                    error,
                )
                if hasattr(connection, "server"):
                    self.close(connection, timeout=timeout)
                if attempt == 0:
                    continue
                return ProviderCallResult(
                    status=ProviderCallStatus.FAILED,
                    failure=failure(error, needs_reconciliation=True),
                )
            except (
                AppServerError,
                OSError,
                KeyError,
                TypeError,
                ValueError,
            ) as error:
                if hasattr(connection, "server"):
                    self.close(connection, timeout=timeout)
                if isinstance(error, (KeyError, TypeError, ValueError)):
                    error = AppServerError(
                        str(error), reason=HarnessFailureReason.PROTOCOL
                    )
                return ProviderCallResult(
                    status=ProviderCallStatus.FAILED,
                    failure=failure(error, needs_reconciliation=False),
                )
        raise AssertionError("Codex reconciliation did not return")

    async def has_background_job(
        self, session: HarnessSession, *, timeout: float
    ) -> bool:
        connection = self.connections.get(session.id)
        if connection is None:
            return False
        try:
            async with timer_timeout(self.timer, ceil(timeout * 1000)):
                result = await connection.server.request(
                    "thread/backgroundTerminals/list",
                    {
                        "threadId": session.provider_thread_id,
                    },
                )
        except AppServerError, OSError, TimeoutError:
            # An unavailable status cannot establish that the session is idle.
            logger.warning(
                "Could not query Codex background terminals for session %s",
                session.id,
                exc_info=True,
            )
            return True
        try:
            BackgroundTerminalsResponse.model_validate(result)
        except ValidationError:
            logger.warning(
                "Invalid Codex background-terminal response for session %s; "
                "retaining the current handling",
                session.id,
                exc_info=True,
            )
        return bool(result["data"])

    async def stop_session(
        self,
        session: HarnessSession,
        *,
        timeout: float,
    ) -> ProviderCallResult[HarnessSession]:
        self.sessions.pop(session.id, None)
        self.turns.pop(session.id, None)
        connection = self.connections.pop(session.id, None)
        if connection is not None:
            task = self.close(connection, timeout=timeout)
            try:
                async with timer_timeout(self.timer, ceil(timeout * 1000)):
                    await asyncio.shield(task)
            except AppServerError, OSError, TimeoutError:
                logger.warning(
                    "Codex session %s cleanup continues in the background",
                    session.id,
                    exc_info=True,
                )
        return ProviderCallResult(status=ProviderCallStatus.CONFIRMED, value=session)

    async def aclose(self):
        for connection in tuple(self.connections.values()):
            self.close(connection, timeout=self.input.shutdown_timeout)
        self.connections.clear()
        self.sessions.clear()
        self.turns.clear()
        if self.closing:
            try:
                async with timer_timeout(
                    self.timer, ceil(self.input.shutdown_timeout * 1000)
                ):
                    await asyncio.gather(
                        *(asyncio.shield(task) for task in tuple(self.closing)),
                        return_exceptions=True,
                    )
            except TimeoutError:
                logger.warning("Forcing Codex cleanup at the node shutdown deadline")
            finally:
                # A cleanup begun with a longer ordinary-call budget must not
                # outlive node shutdown. Cancellation enters its force/reap path.
                for task in tuple(self.closing):
                    task.cancel()
            await asyncio.gather(
                *(asyncio.shield(task) for task in tuple(self.closing)),
                return_exceptions=True,
            )
