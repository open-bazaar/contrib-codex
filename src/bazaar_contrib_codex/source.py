"""Publish native protocol traffic independently of turn output consumption."""

from __future__ import annotations

from time import monotonic_ns, time_ns
from typing import TYPE_CHECKING, Literal
from uuid import uuid7

from bazaar_contrib_audit import Audit, AuditContext
from bazaar_contrib_harness import HarnessSourceEvent

if TYPE_CHECKING:
    from .transport import Packet


class SourceAudit:
    def __init__(self, audit: Audit, context: AuditContext):
        self.audit = audit
        self.context = context
        self.id = str(uuid7())
        self.sequence = 0
        self.turn_id: str | None = None
        self.turns: dict[str, str] = {}
        self.requests: dict[tuple[str, int | str], AuditContext] = {}

    async def publish(
        self,
        data: str,
        direction: Literal["sent", "received"],
        packet: Packet | None = None,
    ) -> None:
        occurred_at_ms = time_ns() // 1_000_000
        observed = monotonic_ns()
        self.sequence += 1
        context = self.context
        if packet is not None:
            result = packet.result if isinstance(packet.result, dict) else {}
            thread = packet.params.get("thread") or result.get("thread") or {}
            turn = packet.params.get("turn") or result.get("turn") or {}
            provider_session_id = packet.params.get("threadId") or (
                thread.get("id") if isinstance(thread, dict) else None
            )
            provider_turn_id = packet.params.get("turnId") or (
                turn.get("id") if isinstance(turn, dict) else None
            )
            if (
                provider_session_id is not None
                and self.context.provider_session_id is None
            ):
                self.context = self.context.model_copy(
                    update={"provider_session_id": provider_session_id}
                )
                context = self.context
            if packet.method is None and packet.id is not None:
                context = self.requests.pop((direction, packet.id), context)
            host_turn_id = (
                self.turns.get(provider_turn_id, self.turn_id)
                if provider_turn_id is not None
                else self.turn_id
                if direction == "sent"
                and packet.method is not None
                and packet.method.startswith("turn/")
                else context.turn_id
            )
            if provider_turn_id is not None and host_turn_id is not None:
                self.turns[provider_turn_id] = host_turn_id
            context = context.model_copy(
                update={
                    "turn_id": host_turn_id,
                    "provider_session_id": provider_session_id
                    or context.provider_session_id,
                    "provider_turn_id": provider_turn_id or context.provider_turn_id,
                }
            )
            if packet.method is not None and packet.id is not None:
                self.requests[
                    "received" if direction == "sent" else "sent", packet.id
                ] = context
        event = HarnessSourceEvent(
            provider="codex",
            source_id=self.id,
            sequence=self.sequence,
            direction=direction,
            data=data,
            occurred_at_ms=occurred_at_ms,
            monotonic_ns=observed,
        )
        await self.audit.publish(
            f"harness.source.{direction}",
            context=context,
            payload=event.model_dump(mode="json"),
            occurred_at_ms=occurred_at_ms,
        )
