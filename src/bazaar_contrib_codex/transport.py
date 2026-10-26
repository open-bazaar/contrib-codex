"""Own a Codex JSON-lines process and route requests independently of output."""

# All request and shutdown budgets are supplied by the owning Harness call.
# ruff: noqa: ASYNC109

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
from collections.abc import Awaitable, Callable, Coroutine
from importlib.metadata import version
from math import ceil
from pathlib import Path
from typing import Any

import psutil
from bazaar_contrib_harness import HarnessFailure, HarnessFailureReason
from bazaar_contrib_timer import Timer
from bazaar_contrib_timer import timeout as timer_timeout
from pydantic import BaseModel, ConfigDict, Field, ValidationError

logger = logging.getLogger(__name__)
cleanup: set[asyncio.Task[Any]] = set()


def background[T](work: Coroutine[Any, Any, T]) -> asyncio.Task[T]:
    """Retain detached cleanup and consume its outcome without delaying callers."""
    task = asyncio.create_task(work)
    cleanup.add(task)

    def finished(task: asyncio.Task[T]):
        cleanup.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.error("Codex background cleanup failed", exc_info=error)

    task.add_done_callback(finished)
    return task


class Packet(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: int | str | None = None
    method: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)
    error: dict[str, Any] | None = None


class AppServerError(RuntimeError):
    """A native request failure or a protocol/state error."""

    def __init__(
        self,
        message: str,
        *,
        reason: HarnessFailureReason = HarnessFailureReason.EXECUTION,
        code: int | str | None = None,
    ):
        super().__init__(message)
        self.reason = reason
        self.code = code


class AppServerUnavailable(AppServerError):
    """The transport cannot confirm what the provider last executed."""


def failure(error: BaseException, *, needs_reconciliation: bool) -> HarnessFailure:
    """Preserve error facts separately from the owning call's recovery policy."""

    if isinstance(error, (AppServerUnavailable, ConnectionError)):
        reason = HarnessFailureReason.CONNECTION_LOST
    elif isinstance(error, AppServerError):
        reason = error.reason
    elif isinstance(error, TimeoutError):
        reason = HarnessFailureReason.TIMEOUT
    elif isinstance(error, (KeyError, TypeError, ValueError)):
        reason = HarnessFailureReason.PROTOCOL
    elif isinstance(error, OSError):
        reason = HarnessFailureReason.UNAVAILABLE
    else:
        reason = HarnessFailureReason.INTERNAL
    return HarnessFailure(
        reason,
        str(error) or type(error).__name__,
        needs_reconciliation=needs_reconciliation
        and reason
        not in {HarnessFailureReason.EXECUTION, HarnessFailureReason.INTERRUPTED},
        code=error.code
        if isinstance(error, AppServerError)
        else error.errno
        if isinstance(error, OSError)
        else None,
    )


class AppServer:
    def __init__(
        self,
        process: asyncio.subprocess.Process,
        handler: Callable[[Packet], Awaitable[dict[str, Any]]],
        notify: Callable[[Packet], None],
        timeout: float,
        *,
        timer: Timer | None = None,
    ):
        self.process = process
        self.handler = handler
        self.notify = notify
        self.timeout = timeout
        self.timer = timer
        self.pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self.requests: dict[int | str, asyncio.Task[None]] = {}
        self.events: asyncio.Queue[Packet | Exception] = asyncio.Queue()
        self.sequence = 0
        self.writing = asyncio.Lock()
        self.reader = asyncio.create_task(self.read())
        self.errors = asyncio.create_task(self.read_errors())
        self.watcher = asyncio.create_task(self.watch())
        self.error: Exception | None = None
        self.closed = False
        self.closing: asyncio.Task[None] | None = None

    @classmethod
    async def start(
        cls,
        workspace: Path,
        environment: dict[str, str],
        handler: Callable[[Packet], Awaitable[dict[str, Any]]],
        notify: Callable[[Packet], None],
        *,
        timeout: float,
        timer: Timer | None = None,
    ) -> AppServer:
        process = await asyncio.create_subprocess_exec(
            "codex",
            "app-server",
            "--listen",
            "stdio://",
            cwd=workspace,
            env=os.environ | environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            start_new_session=os.name != "nt",
        )
        return cls(process, handler, notify, timeout, timer=timer)

    async def initialize(self) -> dict[str, Any]:
        result = await self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "bazaar_contrib_codex",
                    "version": version("bazaar-contrib-codex"),
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        await self.write({"method": "initialized"})
        return result

    async def write(self, packet: dict[str, Any]):
        if self.error is not None:
            raise self.error
        stdin = self.process.stdin
        if (
            self.closed
            or stdin is None
            or stdin.is_closing()
            or self.process.returncode is not None
        ):
            raise AppServerUnavailable("Codex app-server is no longer running")
        async with self.writing:
            stdin.write(json.dumps(packet).encode() + b"\n")
            await stdin.drain()

    async def request(
        self,
        method: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        self.sequence += 1
        id = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[id] = future
        try:
            await self.write({"id": id, "method": method, "params": params})
            return await future
        finally:
            self.pending.pop(id, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

    async def answer(self, packet: Packet):
        try:
            async with (
                timer_timeout(self.timer, ceil(self.timeout * 1000))
                if self.timer is not None
                else asyncio.timeout(self.timeout)
            ):
                result = await self.handler(packet)
                await self.write({"id": packet.id, "result": result})
        except Exception:
            logger.exception(
                "Could not send the response to Codex request %s", packet.id
            )
        finally:
            if packet.id is not None:
                self.requests.pop(packet.id, None)

    async def read(self):
        stdout = self.process.stdout
        assert stdout is not None
        buffer = bytearray()
        try:
            while chunk := await stdout.read(64 * 1024):
                buffer.extend(chunk)
                while (index := buffer.find(b"\n")) >= 0:
                    line = bytes(buffer[:index])
                    del buffer[: index + 1]
                    if not line.strip():
                        continue
                    packet = Packet.model_validate_json(line)
                    if packet.method is not None:
                        if self.closed:
                            continue
                        self.notify(packet)
                        self.events.put_nowait(packet)
                        if packet.id is not None and not self.closed:
                            self.requests[packet.id] = asyncio.create_task(
                                self.answer(packet)
                            )
                        else:
                            if packet.method == "serverRequest/resolved":
                                task = self.requests.pop(
                                    packet.params["requestId"], None
                                )
                                if task is not None:
                                    task.cancel()
                    elif isinstance(packet.id, int):
                        future = self.pending.get(packet.id)
                        if future is not None and not future.done():
                            if packet.error is not None:
                                future.set_exception(
                                    AppServerError(
                                        str(
                                            packet.error.get(
                                                "message", "Codex request failed"
                                            )
                                        ),
                                        code=packet.error.get("code"),
                                    )
                                )
                            else:
                                future.set_result(packet.result)
            if buffer.strip():
                raise AppServerError(
                    "Codex app-server ended with incomplete JSONL",
                    reason=HarnessFailureReason.PROTOCOL,
                )
        except Exception as error:  # noqa: BLE001 -- Isolate provider protocol failures.
            if isinstance(error, (ValidationError, KeyError, TypeError, ValueError)):
                error = AppServerError(str(error), reason=HarnessFailureReason.PROTOCOL)
            self.fail(error)
            if self.process.returncode is None:
                try:
                    self.process.terminate()
                except ProcessLookupError:
                    pass

    def fail(self, error: Exception):
        if self.error is not None:
            return
        self.error = error
        for future in self.pending.values():
            if not future.done():
                future.set_exception(error)
        self.events.put_nowait(error)

    async def watch(self):
        # Process.wait can await pipe EOF after the child exits. A background
        # command retaining stdout must not delay cleanup of its process group.
        if self.timer is None:
            # Standalone metadata inspection has no session or background tools.
            await self.process.wait()
        else:
            while self.process.returncode is None:
                await self.timer.create(50).wait()
        if os.name != "nt":
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        await asyncio.gather(self.reader, self.errors, return_exceptions=True)
        self.fail(
            AppServerUnavailable(
                f"Codex app-server exited ({self.process.returncode})",
                code=self.process.returncode,
            )
        )

    async def read_errors(self):
        stderr = self.process.stderr
        assert stderr is not None
        while chunk := await stderr.read(64 * 1024):
            for line in chunk.decode(errors="replace").splitlines():
                logger.debug("Codex app-server stderr: %s", line)

    def close(self, *, timeout: float) -> asyncio.Task[None]:
        if self.closing is None:
            self.closed = True
            self.closing = background(self._close(timeout=timeout))
        return self.closing

    async def aclose(self, *, timeout: float):
        await asyncio.shield(self.close(timeout=timeout))

    async def _close(self, *, timeout: float):
        loop = asyncio.get_running_loop()
        started_ms = self.timer.now_ms if self.timer is not None else loop.time() * 1000
        deadline_ms = started_ms + timeout * 1000
        tasks = tuple(self.requests.values()) + (self.reader, self.errors, self.watcher)
        children: list[psutil.Process] = []
        try:
            async with (
                timer_timeout(self.timer, ceil(deadline_ms - self.timer.now_ms))
                if self.timer is not None
                else asyncio.timeout_at(deadline_ms / 1000)
            ):
                for task in self.requests.values():
                    task.cancel()
                self.requests.clear()
                try:
                    children = await asyncio.to_thread(
                        psutil.Process(self.process.pid).children, recursive=True
                    )
                except psutil.NoSuchProcess:
                    pass
                if self.process.stdin is not None:
                    self.process.stdin.close()
                for fraction, operation in (
                    (0.6, None),
                    (0.8, "terminate"),
                    (0.9, "kill"),
                ):
                    if self.process.returncode is not None:
                        break
                    if operation is not None:
                        if os.name != "nt":
                            try:
                                os.killpg(
                                    self.process.pid,
                                    signal.SIGTERM
                                    if operation == "terminate"
                                    else signal.SIGKILL,
                                )
                            except ProcessLookupError:
                                pass
                        else:
                            for child in reversed(children):
                                try:
                                    getattr(child, operation)()
                                except psutil.NoSuchProcess:
                                    pass
                            if self.process.returncode is None:
                                getattr(self.process, operation)()
                    try:
                        async with (
                            timer_timeout(
                                self.timer,
                                ceil(
                                    started_ms
                                    + timeout * fraction * 1000
                                    - self.timer.now_ms
                                ),
                            )
                            if self.timer is not None
                            else asyncio.timeout_at(
                                started_ms / 1000 + timeout * fraction
                            )
                        ):
                            await self.process.wait()
                        break
                    except TimeoutError:
                        continue
                if os.name != "nt":
                    try:
                        os.killpg(self.process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                for child in reversed(children):
                    try:
                        child.kill()
                    except psutil.NoSuchProcess:
                        pass
                if self.timer is None:
                    await asyncio.to_thread(
                        psutil.wait_procs,
                        children,
                        timeout=max(0, deadline_ms / 1000 - loop.time()),
                    )
                else:
                    remaining = children
                    while remaining:
                        _, remaining = await asyncio.to_thread(
                            psutil.wait_procs, remaining, timeout=0
                        )
                        if remaining:
                            await self.timer.create(50).wait()
                await self.process.wait()
                await asyncio.gather(*tasks, return_exceptions=True)
        except TimeoutError:
            logger.warning("Codex cleanup reached the caller's shutdown budget")
        finally:
            if self.process.stdin is not None:
                self.process.stdin.close()
            if os.name != "nt":
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            elif self.process.returncode is None:
                self.process.kill()
            for child in reversed(children):
                try:
                    child.kill()
                except psutil.NoSuchProcess:
                    pass
            for task in tasks:
                task.cancel()
            self.fail(AppServerUnavailable("Codex app-server was closed"))
            if self.process.returncode is None or any(
                not task.done() for task in tasks
            ):
                # Reap the killed process inside this owned background task.
                transport = getattr(self.process, "_transport", None)
                if transport is not None:
                    for fd in (1, 2):
                        pipe = transport.get_pipe_transport(fd)
                        if pipe is not None:
                            pipe.close()
                await asyncio.gather(*tasks, return_exceptions=True)
                await self.process.wait()
