"""Implement the public Harness with session-owned Codex app-server processes."""

# Provider budgets come from the public Harness contract.
# ruff: noqa: ASYNC109

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from bazaar_contrib_harness import (
    ApprovalHandler,
    ApprovalPolicy,
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
)
from pydantic import BaseModel

from .approval import decline, interact
from .transport import AppServer, AppServerError, AppServerUnavailable, Packet, failure

logger = logging.getLogger(__name__)

APPROVAL_POLICIES = {
    ApprovalPolicy.ON_REQUEST: "on-request",
    ApprovalPolicy.NEVER: "never",
}
SANDBOX_POLICIES = {
    SandboxPolicy.WORKSPACE_WRITE: "workspace-write",
    SandboxPolicy.DANGER_FULL_ACCESS: "danger-full-access",
}


class FsWatchResponse(BaseModel):
    path: Path


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
            message = error.get("message") or "Codex execution failed"
        case "interrupted":
            reason = HarnessFailureReason.INTERRUPTED
            message = error.get("message") or "Codex execution was interrupted"
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
        self.text: dict[str, str] = {}

    def __aiter__(self) -> TurnStream:
        return self

    def output(
        self,
        kind: str,
        packet: Packet,
        *,
        text: str = "",
        failure: HarnessFailure | None = None,
        **metadata: Any,
    ) -> HarnessOutput:
        return HarnessOutput(
            session_id=self.turn.session_id,
            turn_id=self.turn.id,
            kind=kind,
            text=text,
            failure=failure,
            metadata=metadata,
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
        while True:
            packet = await self.connection.server.events.get()
            if isinstance(packet, Exception):
                self.finished = True
                return HarnessOutput(
                    session_id=self.turn.session_id,
                    turn_id=self.turn.id,
                    kind="failed",
                    text=str(packet),
                    failure=failure(
                        packet,
                        needs_reconciliation=not (
                            isinstance(packet, AppServerError)
                            and not isinstance(packet, AppServerUnavailable)
                        ),
                    ),
                    provider="codex",
                    provider_session_id=self.connection.session.provider_thread_id,
                    provider_turn_id=self.provider_turn_id,
                )
            params = packet.params
            if (
                params.get("threadId", self.connection.session.provider_thread_id)
                != self.connection.session.provider_thread_id
            ):
                continue
            provider_turn_id = params.get("turnId") or (params.get("turn") or {}).get(
                "id"
            )
            if provider_turn_id is not None:
                if (
                    self.provider_turn_id is not None
                    and provider_turn_id != self.provider_turn_id
                ):
                    continue
                self.provider_turn_id = provider_turn_id
            match packet.method:
                case "turn/started":
                    return self.output("started", packet)
                case "turn/completed":
                    status = params["turn"]["status"]
                    self.finished = True
                    error = params["turn"].get("error")
                    terminal = turn_failure(params["turn"], needs_reconciliation=True)
                    return self.output(
                        "completed" if terminal is None else "failed",
                        packet,
                        text=terminal.message
                        if terminal
                        else "\n".join(self.text.values()),
                        failure=terminal,
                        status=status,
                        error=error,
                    )
                case "item/agentMessage/delta":
                    id = params["itemId"]
                    self.text[id] = self.text.get(id, "") + params["delta"]
                    return self.output("text", packet, text=params["delta"], item_id=id)
                case "item/reasoning/summaryTextDelta" | "item/reasoning/textDelta":
                    return self.output("reasoning", packet, text=params["delta"])
                case "item/plan/delta":
                    return self.output("plan", packet, text=params["delta"])
                case "item/started" | "item/completed":
                    item = params["item"]
                    if item["type"] == "agentMessage":
                        if (
                            packet.method == "item/completed"
                            and item["id"] not in self.text
                        ):
                            self.text[item["id"]] = item["text"]
                            return self.output(
                                "text", packet, text=item["text"], item_id=item["id"]
                            )
                        return self.output("message", packet, item=item)
                    if item["type"] == "userMessage":
                        return self.output("message", packet, item=item)
                    if item["type"] in {"reasoning", "plan"}:
                        return self.output(item["type"], packet, item=item)
                    if item["type"] == "contextCompaction":
                        return self.output(
                            "compaction.started"
                            if packet.method == "item/started"
                            else "compaction.completed",
                            packet,
                            item=item,
                        )
                    if item["type"] not in {
                        "commandExecution",
                        "fileChange",
                        "mcpToolCall",
                        "dynamicToolCall",
                        "collabAgentToolCall",
                        "webSearch",
                        "imageView",
                        "imageGeneration",
                        "subAgentActivity",
                        "sleep",
                        "functionCallOutput",
                    }:
                        return self.output("provider.event", packet, item=item)
                    kind = (
                        "tool.started"
                        if packet.method == "item/started"
                        else "tool.completed"
                    )
                    if packet.method == "item/completed" and (
                        item.get("status") in {"failed", "declined", "errored"}
                        or item.get("error") is not None
                    ):
                        kind = "tool.failed"
                    return self.output(kind, packet, item=item)
                case "item/commandExecution/outputDelta":
                    return self.output(
                        "tool.output",
                        packet,
                        text=params["delta"],
                        item_id=params["itemId"],
                    )
                case "item/fileChange/outputDelta" | "item/fileChange/patchUpdated":
                    return self.output("tool.patch", packet, **params)
                case "item/commandExecution/terminalInteraction":
                    return self.output("tool.interaction", packet, **params)
                case "thread/tokenUsage/updated":
                    return self.output("usage", packet, **params)
                case "error":
                    return self.output(
                        "provider.retry"
                        if params.get("willRetry", False)
                        else "provider.error",
                        packet,
                        text=params["error"]["message"],
                        **params,
                    )
                case _:
                    if packet.method and packet.method.startswith("item/"):
                        suffix = packet.method.rsplit("/", 1)[-1]
                        if suffix in {"progress", "outputDelta"}:
                            return self.output(
                                "tool.progress"
                                if suffix == "progress"
                                else "tool.output",
                                packet,
                                text=params.get("message") or params.get("delta", ""),
                                item_id=params.get("itemId"),
                            )
                    return self.output("provider.event", packet)

    async def aclose(self):
        if (
            not self.finished
            and self.provider_turn_id is not None
            and self.connection.server.process.returncode is None
        ):
            await self.connection.server.request(
                "turn/interrupt",
                {
                    "threadId": self.connection.session.provider_thread_id,
                    "turnId": self.provider_turn_id,
                },
                timeout=self.timeout,
            )
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
        self.timeout = timeout
        for attempt in range(2):
            self.server = await AppServer.start(
                self.request.workspace,
                self.request.environment,
                self.answer,
                self.notify,
            )
            try:
                result = await self.server.initialize(timeout=timeout)
                self.home = Path(result["codexHome"])
                await self.watch(timeout=timeout)
                return
            except TimeoutError as error:
                logger.warning(
                    "Codex initialization attempt %s failed for session %s: %s",
                    attempt + 1,
                    self.session.id,
                    error,
                )
                await self.server.aclose(timeout=timeout)
                if attempt:
                    raise
            except BaseException:
                await self.server.aclose(timeout=timeout)
                raise

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

    async def watch(self, *, timeout: float):
        assert self.home is not None
        self.watch_targets = {
            "workspace": self.request.workspace / "AGENTS.md",
            "codex-home": self.home / "AGENTS.md",
        }
        for id, path in self.watch_targets.items():
            result = FsWatchResponse.model_validate(
                await self.server.request(
                    "fs/watch",
                    {
                        "watchId": id,
                        "path": str(path),
                    },
                    timeout=timeout,
                )
            )
            if result.path != path:
                raise AppServerError(
                    "fs/watch returned a different path",
                    reason=HarnessFailureReason.PROTOCOL,
                )


class RecoveredStream:
    """Expose a terminal turn confirmed by Codex's persisted thread history."""

    def __init__(self, output: HarnessOutput):
        self.output: HarnessOutput | None = output

    def __aiter__(self) -> RecoveredStream:
        return self

    async def __anext__(self) -> HarnessOutput:
        if self.output is None:
            raise StopAsyncIteration
        output, self.output = self.output, None
        return output

    async def aclose(self):
        self.output = None


class CodexHarness:
    def __init__(
        self,
        input: HarnessInput,
        model: str | None,
        effort: str | None,
    ):
        self.input = input
        self.model = model
        self.effort = effort
        self.connections: dict[str, Connection] = {}
        self.events: asyncio.Queue[HarnessExpire] = asyncio.Queue()

    async def receive_event(self) -> HarnessExpire:
        return await self.events.get()

    async def start_session(
        self,
        request: SessionInput,
        *,
        timeout: float,
    ) -> ProviderCallResult[HarnessSession]:
        previous = self.connections.pop(request.id, None)
        if previous is not None:
            await previous.server.aclose(timeout=timeout)
        connection = Connection(self, request)
        try:
            await connection.open(timeout=timeout)
        except Exception as error:  # noqa: BLE001 -- Report provider startup failures.
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
            result = await connection.server.request(
                "thread/start",
                {
                    "cwd": str(request.workspace),
                    "model": self.model,
                    "developerInstructions": "\n\n".join(request.instructions),
                    "approvalPolicy": APPROVAL_POLICIES[request.approval_policy],
                    "sandbox": SANDBOX_POLICIES[request.sandbox_policy],
                },
                timeout=timeout,
            )
            connection.session = HarnessSession(
                id=request.id, provider_thread_id=result["thread"]["id"]
            )
            self.connections[request.id] = connection
            return ProviderCallResult(
                status=ProviderCallStatus.CONFIRMED, value=connection.session
            )
        except BaseException as error:
            await connection.server.aclose(timeout=timeout)
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
        if connection is None or connection.server.process.returncode is not None:
            raise HarnessSessionUnavailable("The session's Codex app-server has exited")
        if connection.stream is not None and not connection.stream.finished:
            raise RuntimeError("The Codex session already owns an active turn")
        stream = TurnStream(connection, turn, approval_handler, timeout)
        connection.stream = stream
        inputs = [{"type": "text", "text": turn.input_text}]
        for attachment in turn.attachments:
            inputs.append(
                {"type": "localImage", "path": str(attachment.path)}
                if (attachment.media_type or "").startswith("image/")
                else {"type": "text", "text": f"Attached file: {attachment.path}"}
            )
        try:
            result = await connection.server.request(
                "turn/start",
                {
                    "threadId": session.provider_thread_id,
                    "input": inputs,
                    "clientUserMessageId": turn.id,
                    "cwd": str(connection.request.workspace),
                    "model": self.model,
                    "effort": self.effort,
                    "sandboxPolicy": (
                        {"type": "dangerFullAccess"}
                        if connection.request.sandbox_policy
                        is SandboxPolicy.DANGER_FULL_ACCESS
                        else {
                            "type": "workspaceWrite",
                            "writableRoots": [str(connection.request.workspace)],
                            "networkAccess": connection.request.network_access,
                        }
                    ),
                },
                timeout=timeout,
            )
            stream.provider_turn_id = result["turn"]["id"]
        except (AppServerError, OSError, TimeoutError) as error:
            connection.server.events.put_nowait(error)
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
            result = await connection.server.request(
                "turn/steer",
                {
                    "threadId": session.provider_thread_id,
                    "expectedTurnId": stream.provider_turn_id,
                    "input": [{"type": "text", "text": input_text}],
                },
                timeout=timeout,
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
        connection = self.connections.get(session.id)
        if connection is None:
            return ProviderCallResult(
                status=ProviderCallStatus.FAILED,
                failure=HarnessFailure(
                    HarnessFailureReason.UNAVAILABLE,
                    "No execution context owns this Codex session",
                ),
            )
        for attempt in range(2):
            try:
                if connection.server.process.returncode is not None:
                    await connection.server.aclose(timeout=timeout)
                    await connection.open(timeout=timeout)
                result = await connection.server.request(
                    "thread/read",
                    {
                        "threadId": session.provider_thread_id,
                        "includeTurns": True,
                    },
                    timeout=timeout,
                )
                thread = result["thread"]
                if thread["id"] != session.provider_thread_id:
                    raise AppServerError(
                        "Codex returned a different provider thread",
                        reason=HarnessFailureReason.STATE_MISMATCH,
                    )
                previous = connection.stream
                status = (thread.get("status") or {}).get("type")
                if status == "active":
                    turns = [
                        item
                        for item in thread["turns"]
                        if item["status"] == "inProgress"
                    ]
                    if turn is None:
                        raise AppServerError(
                            "Recovery was called without the active turn",
                            reason=HarnessFailureReason.STATE_MISMATCH,
                        )
                    if previous is None:
                        raise AppServerError(
                            "No local execution stream owns the active Codex turn",
                            reason=HarnessFailureReason.STATE_MISMATCH,
                        )
                    if previous.turn.id != turn.id:
                        raise AppServerError(
                            f"Recovery requested turn {turn.id}, but the local stream owns {previous.turn.id}",
                            reason=HarnessFailureReason.STATE_MISMATCH,
                        )
                    if approval_handler is None:
                        raise AppServerError(
                            "The active Codex turn has no approval handler for recovery",
                            reason=HarnessFailureReason.UNAVAILABLE,
                        )
                    if previous.provider_turn_id is None:
                        raise AppServerError(
                            "The Codex turn identifier was not received before recovery",
                            reason=HarnessFailureReason.UNAVAILABLE,
                        )
                    if len(turns) != 1:
                        raise AppServerError(
                            f"Expected one active Codex turn, received {len(turns)}",
                            reason=HarnessFailureReason.PROTOCOL,
                        )
                    if turns[0]["id"] != previous.provider_turn_id:
                        raise AppServerError(
                            f"Codex is executing turn {turns[0]['id']}, expected {previous.provider_turn_id}",
                            reason=HarnessFailureReason.STATE_MISMATCH,
                        )
                elif status in {"idle", "notLoaded"}:
                    response = await connection.server.request(
                        "thread/resume",
                        {
                            "threadId": session.provider_thread_id,
                            "cwd": str(connection.request.workspace),
                            "model": self.model,
                            "developerInstructions": "\n\n".join(
                                connection.request.instructions
                            ),
                            "approvalPolicy": APPROVAL_POLICIES[
                                connection.request.approval_policy
                            ],
                            "sandbox": SANDBOX_POLICIES[
                                connection.request.sandbox_policy
                            ],
                        },
                        timeout=timeout,
                    )
                    if response["thread"]["id"] != session.provider_thread_id:
                        raise AppServerError(
                            "Codex resumed a different provider thread",
                            reason=HarnessFailureReason.STATE_MISMATCH,
                        )
                else:
                    raise AppServerError(
                        f"Unsupported Codex thread status: {status}",
                        reason=HarnessFailureReason.PROTOCOL,
                    )
                stream: TurnStream | RecoveredStream | None = None
                if (
                    turn is not None
                    and previous is not None
                    and previous.turn.id == turn.id
                ):
                    stored = next(
                        (
                            item
                            for item in thread["turns"]
                            if item["id"] == previous.provider_turn_id
                        ),
                        None,
                    )
                    if stored is None:
                        raise AppServerError(
                            "The active turn is absent from Codex history",
                            reason=HarnessFailureReason.STATE_MISMATCH,
                        )
                    if stored["status"] == "inProgress":
                        if approval_handler is None:
                            raise AppServerError(
                                "The recovered turn has no approval handler",
                                reason=HarnessFailureReason.UNAVAILABLE,
                            )
                        stream = TurnStream(connection, turn, approval_handler, timeout)
                        stream.provider_turn_id = stored["id"]
                        connection.stream = stream
                    else:
                        terminal = turn_failure(stored, needs_reconciliation=False)
                        text = (
                            terminal.message
                            if terminal
                            else "\n".join(
                                item["text"]
                                for item in stored["items"]
                                if item["type"] == "agentMessage"
                            )
                        )
                        stream = RecoveredStream(
                            HarnessOutput(
                                session_id=session.id,
                                turn_id=turn.id,
                                kind="completed" if terminal is None else "failed",
                                text=text,
                                failure=terminal,
                                provider="codex",
                                provider_event="thread/read",
                                provider_session_id=session.provider_thread_id,
                                provider_turn_id=stored["id"],
                                raw=result,
                            )
                        )
                return ProviderCallResult(
                    status=ProviderCallStatus.CONFIRMED,
                    value=HarnessSessionReconciliation(connection.session, stream),
                )
            except asyncio.CancelledError:
                await connection.server.aclose(timeout=timeout)
                raise
            except (AppServerUnavailable, TimeoutError) as error:
                logger.warning(
                    "Codex recovery attempt %s failed for session %s: %s",
                    attempt + 1,
                    session.id,
                    error,
                )
                await connection.server.aclose(timeout=timeout)
                if attempt == 0:
                    continue
                return ProviderCallResult(
                    status=ProviderCallStatus.FAILED,
                    failure=failure(error, needs_reconciliation=True),
                )
            except (AppServerError, OSError) as error:
                await connection.server.aclose(timeout=timeout)
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
            result = await connection.server.request(
                "thread/backgroundTerminals/list",
                {
                    "threadId": session.provider_thread_id,
                },
                timeout=timeout,
            )
        except AppServerError, OSError, TimeoutError:
            # An unavailable status cannot establish that the session is idle.
            return True
        return bool(result["data"])

    async def stop_session(
        self,
        session: HarnessSession,
        *,
        timeout: float,
    ) -> ProviderCallResult[HarnessSession]:
        connection = self.connections.pop(session.id, None)
        if connection is not None:
            await connection.server.aclose(timeout=timeout)
        return ProviderCallResult(status=ProviderCallStatus.CONFIRMED, value=session)

    async def aclose(self):
        for connection in tuple(self.connections.values()):
            await self.stop_session(connection.session, timeout=connection.timeout)
