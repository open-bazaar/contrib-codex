"""Observe real native background work, reconciliation and interruption."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import uuid7

import psutil
from bazaar_contrib_harness import (
    ApprovalPolicy,
    ApprovalRequest,
    ApprovalResult,
    HarnessFailureReason,
    HarnessInput,
    HarnessTurn,
    ProviderCallStatus,
    SessionInput,
)

from bazaar_contrib_codex.runtime import CodexHarness

root = Path(sys.argv[1]).resolve()


class Approval:
    async def request_approval(
        self,
        request: ApprovalRequest,
        *,
        timeout: float,  # noqa: ASYNC109
    ) -> ApprovalResult:
        # This session uses the native NEVER policy. Unexpected interactions
        # are declined and retained, rather than authorizing extra operations.
        print(
            json.dumps({"unexpected_approval": request.model_dump(mode="json")}),
            flush=True,
        )
        return ApprovalResult(request_id=request.request_id, decision="rejected")


async def exercise():
    os.environ.update(json.loads((root / "environment.private.json").read_text()))
    workspace = root / "workspaces/assistant"
    harness = CodexHarness(
        HarnessInput(agent_id="assistant", agent_name="Assistant", workspace=workspace),
        sys.argv[2],
        None,
    )
    events = (root / "background.events.jsonl").open("w")
    result = {}
    children = []
    connection = None
    try:
        started = await harness.start_session(
            SessionInput(
                id=str(uuid7()),
                workspace=workspace,
                approval_policy=ApprovalPolicy.NEVER,
            ),
            timeout=30,
        )
        assert started.status is ProviderCallStatus.CONFIRMED and started.value
        session = started.value
        connection = harness.connections[session.id]
        assert not await harness.has_background_job(session, timeout=30)
        turn = HarnessTurn(
            id=str(uuid7()),
            session_id=session.id,
            input_text=(
                "Start a local HTTP preview server for the current folder in a "
                "persistent terminal. Choose an unused loopback port and leave "
                "the server running after telling me its address."
            ),
        )
        approval = Approval()
        stream = await harness.start_turn(session, turn, approval, timeout=30)
        completed = None

        def record(event):
            packet = event.model_dump(mode="json")
            events.write(json.dumps(packet) + "\n")
            events.flush()
            print(json.dumps({"background_event": packet}), flush=True)
            assert event.kind != "compaction.started", (
                "Background acceptance entered native context compaction; "
                "inspect background.events.jsonl"
            )

        async for event in stream:
            record(event)
            if event.kind in {"completed", "failed", "cancelled", "unknown"}:
                completed = event
        assert completed and completed.kind == "completed"
        result["preview_completion"] = completed.model_dump(mode="json")
        result["background_after_completion"] = await harness.has_background_job(
            session, timeout=30
        )
        assert result["background_after_completion"], (
            "No real background terminal was observed; inspect background.events.jsonl"
        )
        terminals = await connection.server.request(
            "thread/backgroundTerminals/list",
            {"threadId": session.provider_thread_id},
            timeout=30,
        )
        assert terminals["data"]
        result["background_terminals"] = terminals
        result["session"] = session.model_dump(mode="json")
        result["preview_turn"] = turn.model_dump(mode="json")
        completed_reconciliation = await harness.reconcile_session(
            session, turn, approval, timeout=30
        )
        assert completed_reconciliation.status is ProviderCallStatus.CONFIRMED
        assert completed_reconciliation.value and completed_reconciliation.value.stream
        assert completed_reconciliation.value.session == session
        terminal = None
        async for event in completed_reconciliation.value.stream:
            record(event)
            if event.kind in {"completed", "failed", "cancelled", "unknown"}:
                terminal = event
        assert terminal and terminal.kind == "completed"
        assert terminal.provider_turn_id == completed.provider_turn_id
        result["completed_reconciliation"] = terminal.model_dump(mode="json")
        result[
            "background_after_completed_reconcile"
        ] = await harness.has_background_job(session, timeout=30)
        assert result["background_after_completed_reconcile"]

        # A persistent terminal can outlive its completed turn. Use a follow-up
        # turn to observe active reconciliation and cancellation separately.
        turn = HarnessTurn(
            id=str(uuid7()),
            session_id=session.id,
            input_text="Check whether the preview responds, keeping its server running.",
        )
        result["interrupt_turn"] = turn.model_dump(mode="json")
        stream = await harness.start_turn(session, turn, approval, timeout=30)
        started_turn = None
        async for event in stream:
            record(event)
            if event.kind == "started":
                started_turn = event
                break
        assert started_turn
        result["active_turn_started"] = started_turn.model_dump(mode="json")
        reconciliation = await harness.reconcile_session(
            session, turn, approval, timeout=30
        )
        assert reconciliation.status is ProviderCallStatus.CONFIRMED
        assert reconciliation.value and reconciliation.value.stream
        recovered = reconciliation.value.stream
        result["reconciled_session"] = reconciliation.value.session.model_dump(
            mode="json"
        )
        assert reconciliation.value.session == session
        children = await asyncio.to_thread(
            psutil.Process(connection.server.process.pid).children, recursive=True
        )
        result["owned_children_before_close"] = [child.pid for child in children]
        await recovered.aclose()
        async with asyncio.timeout(30):
            while True:
                packet = await connection.server.events.get()
                if isinstance(packet, Exception):
                    raise packet
                events.write(
                    json.dumps({"native_after_close": packet.model_dump(mode="json")})
                    + "\n"
                )
                events.flush()
                if packet.method == "turn/completed":
                    native_terminal = packet.params["turn"]
                    assert native_terminal["id"] == stream.provider_turn_id
                    assert native_terminal["status"] == "interrupted"
                    result["native_interruption"] = packet.model_dump(mode="json")
                    break
        reconciled = await harness.reconcile_session(
            session, turn, approval, timeout=30
        )
        assert reconciled.status is ProviderCallStatus.CONFIRMED
        assert reconciled.value and reconciled.value.stream
        terminal = None
        async for event in reconciled.value.stream:
            record(event)
            if event.kind in {"completed", "failed", "cancelled", "unknown"}:
                terminal = event
                break
        assert terminal, "No native interruption terminal was observed"
        assert (
            terminal.failure
            and terminal.failure.reason is HarnessFailureReason.INTERRUPTED
        )
        result["interruption"] = terminal.model_dump(mode="json")
        result["background_after_interrupt"] = await harness.has_background_job(
            session, timeout=30
        )
        stopped = await harness.stop_session(session, timeout=30)
        assert stopped.status is ProviderCallStatus.CONFIRMED
        result["stop_status"] = stopped.status.value
    finally:
        if connection is not None:
            try:
                current = await asyncio.to_thread(
                    psutil.Process(connection.server.process.pid).children,
                    recursive=True,
                )
                children = list(
                    {child.pid: child for child in [*children, *current]}.values()
                )
            except psutil.NoSuchProcess:
                pass
        await harness.aclose()
        events.close()
        alive = []
        for child in children:
            try:
                if child.is_running() and child.status() != psutil.STATUS_ZOMBIE:
                    alive.append(child.pid)
            except psutil.NoSuchProcess:
                pass
        result["live_owned_children"] = alive
        result["connections_after_close"] = len(harness.connections)
        result["app_server_exit"] = (
            connection.server.process.returncode if connection else None
        )
        result["pending_requests"] = len(connection.server.pending) if connection else 0
        result["pending_interactions"] = (
            len(connection.server.requests) if connection else 0
        )
        (root / "background.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps({"background_result": result}), flush=True)
        assert not alive, alive
        assert not result["connections_after_close"]
        assert not result["pending_requests"] and not result["pending_interactions"]
        assert connection is None or result["app_server_exit"] is not None


if __name__ == "__main__":
    asyncio.run(exercise())
