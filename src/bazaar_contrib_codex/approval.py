"""Translate Codex permission requests through the public approval contract."""

from __future__ import annotations

import logging
from typing import Any

from bazaar_contrib_harness import ApprovalHandler, ApprovalRequest, ApprovalScope

from .transport import AppServerError, Packet

logger = logging.getLogger(__name__)

COMMAND = "item/commandExecution/requestApproval"
FILE = "item/fileChange/requestApproval"
ELICITATION = "mcpServer/elicitation/request"
PERMISSIONS = "item/permissions/requestApproval"
USER_INPUT = "item/tool/requestUserInput"

LABELS = {
    ApprovalScope.ONCE: "Allow",
    ApprovalScope.SESSION: "Allow for this session",
    ApprovalScope.ALWAYS: "Allow and don't ask me again",
}


def decline(packet: Packet) -> dict[str, Any]:
    match packet.method:
        case _ if packet.method in (COMMAND, FILE):
            return {"decision": "decline"}
        case _ if packet.method == PERMISSIONS:
            return {"permissions": {}}
        case _ if packet.method == USER_INPUT:
            return {
                "answers": {
                    question["id"]: {"answers": ["__codex_mcp_decline__"]}
                    for question in packet.params.get("questions", [])
                    if question["id"].startswith("mcp_tool_call_approval_")
                }
            }
        case _ if packet.method == ELICITATION:
            return {"action": "decline", "content": None}
        case _:
            raise AppServerError(
                f"Unsupported Codex server request: {packet.method}", code=-32601
            )


async def interact(
    packet: Packet,
    *,
    session_id: str,
    turn_id: str,
    handler: ApprovalHandler,
    timeout: float,  # noqa: ASYNC109
) -> dict[str, Any]:
    params = packet.params
    scopes = [ApprovalScope.ONCE]
    amendment = None
    match packet.method:
        case _ if packet.method in (COMMAND, FILE):
            action = "command_execution" if packet.method == COMMAND else "file_change"
            decisions = params.get("availableDecisions") or []
            if packet.method == FILE or "acceptForSession" in decisions:
                scopes.append(ApprovalScope.SESSION)
            for decision in decisions:
                match decision:
                    case {"acceptWithExecpolicyAmendment": _}:
                        amendment = decision
                    case {
                        "applyNetworkPolicyAmendment": {
                            "network_policy_amendment": {"action": "allow"}
                        }
                    }:
                        amendment = decision
                if amendment is not None:
                    break
            if amendment is not None:
                scopes.append(ApprovalScope.ALWAYS)
        case _ if packet.method == PERMISSIONS:
            action = "permissions"
            scopes.append(ApprovalScope.SESSION)
        case _ if packet.method == USER_INPUT:
            if not params["questions"] or not all(
                question["id"].startswith("mcp_tool_call_approval_")
                for question in params["questions"]
            ):
                logger.warning("Unsupported Codex user-input request: %s", packet.id)
                return decline(packet)
            action = "mcp_tool_approval"
            labels = {option["label"] for option in params["questions"][0]["options"]}
            scopes.extend(
                scope
                for scope in (ApprovalScope.SESSION, ApprovalScope.ALWAYS)
                if LABELS[scope] in labels
            )
        case _ if packet.method == ELICITATION:
            metadata = params.get("_meta") or {}
            if metadata.get("codex_approval_kind") != "mcp_tool_call":
                logger.warning("Unsupported Codex elicitation request: %s", packet.id)
                return decline(packet)
            action = "mcp_tool_approval"
            scopes.extend(
                scope
                for scope in (ApprovalScope.SESSION, ApprovalScope.ALWAYS)
                if scope.value in metadata.get("persist", [])
            )
        case _:
            logger.warning("Unsupported Codex server request: %s", packet.method)
            return decline(packet)
    request = ApprovalRequest(
        request_id=str(packet.id),
        session_id=session_id,
        turn_id=turn_id,
        action=action,
        details=params,
        allowed_scopes=tuple(scopes),
        **({"created_at_ms": params["startedAtMs"]} if "startedAtMs" in params else {}),
    )
    result = await handler.request_approval(request, timeout=timeout)
    if result.decision != "approved":
        return decline(packet)
    match packet.method:
        case _ if packet.method in (COMMAND, FILE):
            decision: Any = "accept"
            if result.scope is ApprovalScope.SESSION:
                decision = "acceptForSession"
            elif result.scope is ApprovalScope.ALWAYS:
                decision = amendment
            return {"decision": decision}
        case _ if packet.method == PERMISSIONS:
            return {
                "permissions": params["permissions"],
                "scope": "session" if result.scope is ApprovalScope.SESSION else "turn",
            }
        case _ if packet.method == USER_INPUT:
            return {
                "answers": {
                    question["id"]: {"answers": [LABELS[result.scope]]}
                    for question in params["questions"]
                }
            }
        case _ if packet.method == ELICITATION:
            response: dict[str, Any] = {
                "action": "accept",
                "content": None,
            }
            if result.scope is not ApprovalScope.ONCE:
                response["_meta"] = {"persist": result.scope.value}
            return response
    return decline(packet)
