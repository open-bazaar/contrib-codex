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
from collections import deque
from collections.abc import Awaitable, Callable, Coroutine
from importlib.metadata import version
from math import ceil
from pathlib import Path
from typing import Any

import psutil
from bazaar_contrib_harness import HarnessFailure, HarnessFailureReason
from bazaar_contrib_timer import Timer
from bazaar_contrib_timer import timeout as timer_timeout
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    TypeAdapter,
    ValidationError,
)

from .source import SourceAudit

logger = logging.getLogger(__name__)
cleanup: set[asyncio.Task[Any]] = set()
RESULT = TypeAdapter(dict[str, Any])


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
    id: StrictInt | StrictStr | None = None
    method: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    result: Any = None
    error: Any = None


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
        and reason is not HarnessFailureReason.EXECUTION,
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
        source: SourceAudit | None = None,
    ):
        self.process = process
        self.handler = handler
        self.notify = notify
        self.timeout = timeout
        self.timer = timer
        self.source = source
        self.pending: dict[int, asyncio.Future[Any]] = {}
        self.requests: dict[int | str, asyncio.Task[None]] = {}
        self.handled: set[tuple[str | None, int | str]] = set()
        self.stderr_tail: deque[str] = deque(maxlen=64)
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
        source: SourceAudit | None = None,
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
        return cls(process, handler, notify, timeout, timer=timer, source=source)

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
            try:
                data = json.dumps(packet)
                stdin.write(data.encode() + b"\n")
                if self.source is not None:
                    await self.source.publish(
                        data, "sent", Packet.model_validate(packet)
                    )
                await stdin.drain()
            except OSError as error:
                message = f"Codex app-server exited ({self.process.returncode})"
                if self.stderr_tail:
                    message += f": {self.stderr_tail[-1]}"
                raise AppServerUnavailable(
                    message, code=self.process.returncode
                ) from error

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
            return RESULT.validate_python(await future)
        finally:
            self.pending.pop(id, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

    async def answer(self, packet: Packet):
        try:
            try:
                response = {"id": packet.id, "result": await self.handler(packet)}
            except Exception as error:
                logger.exception("Rejecting Codex request %s", packet.id)
                response = {
                    "id": packet.id,
                    "error": {
                        "code": error.code
                        if isinstance(error, AppServerError) and error.code is not None
                        else -32603,
                        "message": str(error) or type(error).__name__,
                    },
                }
            async with (
                timer_timeout(self.timer, ceil(self.timeout * 1000))
                if self.timer is not None
                else asyncio.timeout(self.timeout)
            ):
                await self.write(response)
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
            while True:
                chunk = await stdout.read(64 * 1024)
                buffer.extend(chunk)
                while (index := buffer.find(b"\n")) >= 0 or (not chunk and buffer):
                    if index < 0:
                        index = len(buffer)
                    line = bytes(buffer[:index])
                    del buffer[: index + 1]
                    data = line.decode()
                    try:
                        packet = Packet.model_validate_json(data)
                    except ValidationError:
                        if self.source is not None:
                            await self.source.publish(data, "received")
                        raise
                    if self.source is not None:
                        await self.source.publish(data, "received", packet)
                    if packet.method is not None:
                        if self.closed:
                            continue
                        if packet.id is not None:
                            key = (packet.params.get("turnId"), packet.id)
                            if key in self.handled or packet.id in self.requests:
                                logger.warning(
                                    "Ignoring duplicate Codex request %s", packet.id
                                )
                                continue
                            self.handled.add(key)
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
                            if "error" in packet.model_fields_set:
                                error = (
                                    packet.error
                                    if isinstance(packet.error, dict)
                                    else {}
                                )
                                future.set_exception(
                                    AppServerError(
                                        str(
                                            error.get("message", "Codex request failed")
                                        ),
                                        code=error.get("code"),
                                    )
                                )
                            else:
                                future.set_result(packet.result)
                if not chunk:
                    break
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
        await self.process.wait()
        if os.name != "nt":
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        await asyncio.gather(self.reader, self.errors, return_exceptions=True)
        message = f"Codex app-server exited ({self.process.returncode})"
        if self.stderr_tail:
            message += f": {self.stderr_tail[-1]}"
        self.fail(
            AppServerUnavailable(
                message,
                code=self.process.returncode,
            )
        )

    async def read_errors(self):
        stderr = self.process.stderr
        assert stderr is not None
        buffer = bytearray()
        while chunk := await stderr.read(64 * 1024):
            buffer.extend(chunk)
            while (index := buffer.find(b"\n")) >= 0:
                line = bytes(buffer[:index]).decode(errors="replace").rstrip("\r")
                del buffer[: index + 1]
                self.stderr_tail.append(line)
                logger.debug("Codex app-server stderr: %s", line)
        if buffer:
            line = buffer.decode(errors="replace").rstrip("\r")
            self.stderr_tail.append(line)
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
                    if operation is not None and self.process.returncode is None:
                        try:
                            getattr(self.process, operation)()
                        except ProcessLookupError:
                            pass
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
            if self.source is not None:
                await self.source.audit.publish(
                    "harness.source.closed",
                    context=self.source.context,
                    payload={
                        "source_id": self.source.id,
                        "sequence": self.source.sequence,
                        "returncode": self.process.returncode,
                    },
                )
