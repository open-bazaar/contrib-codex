"""Implement the public Harness with session-owned Codex app-server processes."""

# Provider budgets come from the public Harness contract.
# ruff: noqa: ASYNC109

from __future__ import annotations

import asyncio
import logging
from math import ceil
from pathlib import Path
from typing import Any

from bazaar_contrib_harness import (
    ApprovalHandler,
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
from bazaar_contrib_timer import Timer
from bazaar_contrib_timer import timeout as timer_timeout
from pydantic import BaseModel

from .approval import decline, interact
from .transport import AppServer, AppServerError, AppServerUnavailable, Packet, failure

logger = logging.getLogger(__name__)

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
        try:
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
                        return self.output("started", packet)
                    case "turn/completed":
                        status = params["turn"]["status"]
                        self.finished = True
                        error = params["turn"].get("error")
                        terminal = turn_failure(
                            params["turn"], needs_reconciliation=True
                        )
                        return self.output(
                            "completed" if terminal is None else "failed",
                            packet,
                            text=terminal.message if terminal else "",
                            failure=terminal,
                            status=status,
                            error=error,
                        )
                    case "item/agentMessage/delta":
                        return self.output(
                            "text",
                            packet,
                            text=params["delta"],
                            item_id=params["itemId"],
                        )
                    case "item/reasoning/summaryTextDelta" | "item/reasoning/textDelta":
                        return self.output("reasoning", packet, text=params["delta"])
                    case "item/plan/delta":
                        return self.output("plan", packet, text=params["delta"])
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
                        if item["type"] == "contextCompaction":
                            return self.output(
                                "compaction.started"
                                if packet.method == "item/started"
                                else "compaction.completed",
                                packet,
                                item=item,
                            )
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
                        usage = params["tokenUsage"]
                        metadata = {}
                        for key in ("total", "last"):
                            value = usage[key]
                            metadata[key] = {
                                "input_tokens": max(
                                    0, value["inputTokens"] - value["cachedInputTokens"]
                                ),
                                "cached_input_tokens": value["cachedInputTokens"],
                                "cache_write_input_tokens": value.get(
                                    "cacheWriteInputTokens"
                                ),
                                "output_tokens": value["outputTokens"],
                                "reasoning_output_tokens": value.get(
                                    "reasoningOutputTokens"
                                ),
                                "total_tokens": value["totalTokens"],
                            }
                        return self.output(
                            "usage",
                            packet,
                            **metadata,
                            model_context_window=usage.get("modelContextWindow"),
                        )
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
                                    text=params.get("message")
                                    or params.get("delta", ""),
                                    item_id=params.get("itemId"),
                                )
                        continue
        except Exception as error:  # noqa: BLE001 -- Report native stream failures.
            self.finished = True
            if isinstance(error, (KeyError, TypeError, ValueError)):
                error = AppServerError(str(error), reason=HarnessFailureReason.PROTOCOL)
            return HarnessOutput(
                session_id=self.turn.session_id,
                turn_id=self.turn.id,
                kind="failed",
                text=str(error),
                failure=failure(error, needs_reconciliation=True),
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
        try:
            self.server = await AppServer.start(
                self.request.workspace,
                self.request.environment,
                self.answer,
                self.notify,
                timer=self.owner.timer,
                timeout=timeout,
            )
            result = await self.server.initialize()
            self.home = Path(result["codexHome"])
            await self.watch()
        except BaseException:
            if hasattr(self, "server"):
                self.owner.close(self)
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

    async def watch(self):
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
        model: str | None,
        effort: str | None,
    ):
        self.input = input
        self.timer = timer
        self.model = model
        self.effort = effort
        self.connections: dict[str, Connection] = {}
        self.sessions: dict[str, SessionInput] = {}
        self.turns: dict[str, tuple[str, str]] = {}
        self.events: asyncio.Queue[HarnessExpire] = asyncio.Queue()
        self.closing: set[asyncio.Task[None]] = set()

    def close(self, connection: Connection) -> asyncio.Task[None]:
        task = connection.server.close(timeout=self.input.shutdown_timeout)
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
            self.close(previous)
        connection = Connection(self, request)
        try:
            async with timer_timeout(
                self.timer, ceil(min(timeout, self.input.startup_timeout) * 1000)
            ):
                await connection.open(timeout=timeout)
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
                self.close(connection)
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
        except (
            AppServerError,
            OSError,
            TimeoutError,
            KeyError,
            TypeError,
            ValueError,
        ) as error:
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
        timeout = min(timeout, self.input.startup_timeout)
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
        try:
            async with timer_timeout(self.timer, ceil(timeout * 1000)):
                for attempt in range(2):
                    try:
                        if (
                            not hasattr(connection, "server")
                            or connection.server.closed
                            or connection.server.process.returncode is not None
                            or connection.server.reader.done()
                        ):
                            if hasattr(connection, "server"):
                                self.close(connection)
                            await connection.open(timeout=timeout)
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
                                or self.turns.get(session.id)
                                != (turn.id, turns[0]["id"])
                            ):
                                raise AppServerError(
                                    "The active Codex turn cannot be reconciled",
                                    reason=HarnessFailureReason.STATE_MISMATCH,
                                )
                            if previous is not None:
                                previous.finished = True
                            stream = TurnStream(
                                connection, turn, approval_handler, timeout
                            )
                            stream.provider_turn_id = turns[0]["id"]
                            connection.stream = stream
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
                            value=HarnessSessionReconciliation(
                                connection.session, stream
                            ),
                        )
                    except asyncio.CancelledError:
                        if hasattr(connection, "server"):
                            self.close(connection)
                        raise
                    except AppServerUnavailable as error:
                        logger.warning(
                            "Codex recovery attempt %s failed for session %s: %s",
                            attempt + 1,
                            session.id,
                            error,
                        )
                        if hasattr(connection, "server"):
                            self.close(connection)
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
                            self.close(connection)
                        if isinstance(error, (KeyError, TypeError, ValueError)):
                            error = AppServerError(
                                str(error), reason=HarnessFailureReason.PROTOCOL
                            )
                        return ProviderCallResult(
                            status=ProviderCallStatus.FAILED,
                            failure=failure(error, needs_reconciliation=False),
                        )
                raise AssertionError("Codex reconciliation did not return")

        except TimeoutError as error:
            if hasattr(connection, "server"):
                self.close(connection)
            return ProviderCallResult(
                status=ProviderCallStatus.FAILED,
                failure=failure(error, needs_reconciliation=True),
            )

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
            return True
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
            task = self.close(connection)
            try:
                async with timer_timeout(
                    self.timer, ceil(min(timeout, self.input.shutdown_timeout) * 1000)
                ):
                    await asyncio.shield(task)
            except TimeoutError as error:
                return ProviderCallResult(
                    status=ProviderCallStatus.FAILED,
                    failure=failure(error, needs_reconciliation=False),
                )
        return ProviderCallResult(status=ProviderCallStatus.CONFIRMED, value=session)

    async def aclose(self):
        for connection in tuple(self.connections.values()):
            self.close(connection)
        self.connections.clear()
        self.sessions.clear()
        self.turns.clear()
        if self.closing:
            await asyncio.gather(
                *(asyncio.shield(task) for task in tuple(self.closing)),
                return_exceptions=True,
            )
