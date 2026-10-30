"""Real descendants stay owned through parent exit and close cancellation."""

from __future__ import annotations

import asyncio
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import psutil
import pytest
from bazaar_contrib_audit import AUDIT, AUDIT_EVENT, AuditContext, AuditEvent, audit
from bazaar_core import Composition, Context, Contrib, ContribEntry, ContribHub

from bazaar_contrib_codex.source import SourceAudit
from bazaar_contrib_codex.transport import AppServer, AppServerUnavailable, Packet


@pytest.mark.parametrize("orphaned", [False, True], ids=["child", "grandchild"])
@pytest.mark.parametrize("ending", ["exit", "close", "zero", "cancel"])
async def test_close_cleans_descendants_with_inherited_pipes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, orphaned: bool, ending: str
):
    ready = asyncio.Event()
    closed = asyncio.Event()
    events: list[AuditEvent] = []
    descendants: list[psutil.Process] = []
    root_ready = False
    exercised = False

    async def record(ctx: Context, event: AuditEvent):
        nonlocal root_ready
        events.append(event)
        if event.event_name == "harness.source.closed":
            closed.set()
        elif event.event_name == "harness.source.received":
            payload = cast(dict[str, Any], event.payload)
            packet = Packet.model_validate_json(payload["data"])
            if packet.method == "test/leaf":
                descendants.append(
                    await asyncio.to_thread(psutil.Process, packet.params["pid"])
                )
            elif packet.method == "test/root-ready":
                root_ready = True
            if descendants and root_ready:
                ready.set()

    leaf = (
        "import json,os,sys,threading;"
        "print('leaf stderr',file=sys.stderr,flush=True);"
        "print(json.dumps({'method':'test/leaf','params':{'pid':os.getpid()}}),flush=True);"
        "threading.Event().wait()"
    )
    group = (
        "{'creationflags':subprocess.CREATE_NEW_PROCESS_GROUP} if os.name=='nt' else {}"
    )
    descendant = leaf
    if orphaned:
        descendant = (
            "import os,subprocess,sys;"
            f"subprocess.Popen([sys.executable,'-c',{leaf!r}],"
            f"stdin=subprocess.DEVNULL,**({group}))"
        )
    script = (
        "import os,subprocess,sys;"
        f"child=subprocess.Popen([sys.executable,'-c',{descendant!r}],"
        f"stdin=subprocess.DEVNULL,**({group}));"
        + ("assert child.wait()==0;" if orphaned else "")
        + f"print({json.dumps({'method': 'test/root-ready'})!r},flush=True);"
        "sys.exit(7 if sys.stdin.readline() else 0)"
    )
    create_process = asyncio.create_subprocess_exec

    async def start_process(*args: str, **kwargs: Any) -> asyncio.subprocess.Process:
        assert args == ("codex", "app-server", "--listen", "stdio://")
        # Replace only the executable; keep the production spawn and ownership path.
        return await create_process(sys.executable, "-c", script, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", start_process)

    async def answer(packet: Packet) -> dict[str, Any]:
        raise AssertionError(f"Unexpected server request: {packet.method}")

    @asynccontextmanager
    async def exercise(ctx: Context):
        nonlocal exercised
        server = await AppServer.start(
            tmp_path,
            {},
            answer,
            lambda packet: None,
            timeout=30,
            source=SourceAudit(
                ctx.accept(AUDIT),
                AuditContext(
                    agent_id="process-cleanup",
                    harness="codex",
                    session_id=str(uuid4()),
                ),
            ),
        )
        try:
            await ready.wait()
            if ending == "exit":
                await server.write({"method": "test/exit"})
                await server.watcher
                assert isinstance(server.error, AppServerUnavailable)
                assert server.error.code == 7
                await asyncio.to_thread(psutil.wait_procs, descendants)
                assert not await asyncio.to_thread(
                    lambda: any(process.is_running() for process in descendants)
                )
                await server.aclose(timeout=30)
            elif ending == "cancel":
                closing = server.close(timeout=30)
                asyncio.get_running_loop().call_soon(closing.cancel)
                with pytest.raises(asyncio.CancelledError):
                    await closing
            else:
                await server.aclose(timeout=0 if ending == "zero" else 30)
            await closed.wait()
            assert len(descendants) == 1
            await asyncio.to_thread(psutil.wait_procs, descendants)
            assert not await asyncio.to_thread(
                lambda: any(process.is_running() for process in descendants)
            )
            exercised = True
            yield
        finally:
            if server.closing is None:
                await server.aclose(timeout=0)
            for process in descendants:
                try:
                    await asyncio.to_thread(process.kill)
                except psutil.NoSuchProcess:
                    pass
            await asyncio.to_thread(psutil.wait_procs, descendants)

    async with (
        ContribHub() as hub,
        hub.enable(
            Composition(
                entries=(
                    ContribEntry(name="audit", definition=audit),
                    ContribEntry(
                        name="recorder",
                        definition=Contrib(listen={AUDIT_EVENT: record}),
                    ),
                )
            )
        ),
        hub.enable(
            Composition(
                entries=(
                    ContribEntry(
                        name="exercise",
                        definition=Contrib(accepts=[AUDIT], lifespan=exercise),
                    ),
                )
            )
        ),
    ):
        pass
    assert exercised
    closures = [
        event for event in events if event.event_name == "harness.source.closed"
    ]
    assert len(closures) == 1
    payload = cast(dict[str, Any], closures[0].payload)
    assert payload["sequence"] == (3 if ending == "exit" else 2)
