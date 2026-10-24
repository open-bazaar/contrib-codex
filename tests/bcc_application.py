"""Observe a real Codex turn using bcc in an isolated installed application."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import tomllib
from pathlib import Path
from uuid import uuid7

import psutil
import tomli_w
from bazaar_contrib_channel import ChannelApprovalRequest, ChannelTarget, SenderIdentity
from bazaar_contrib_harness import ApprovalResult, ApprovalScope
from bcn_codex_test_channel import ChannelMessage

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("root", type=Path)
parser.add_argument(
    "scenario",
    choices=("mcp-once", "mcp-session", "mcp-always", "bcc", "recovery", "refresh"),
)
arguments = parser.parse_args()
root = arguments.root.resolve()
scenario = arguments.scenario
command = Path(__file__).with_name("run_application.py")
FORECAST_FEEDBACK_LIMIT = 8


async def exercise():
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(
            ("BCN_", "BCC_", "CODEX_", "OPENAI_", "MYSCRM_", "APOLLO_")
        )
    } | json.loads((root / "environment.private.json").read_text())
    log = (root / "node.log").open("wb")
    events = (root / "events.jsonl").open("w")
    process = None
    writer = None
    collector = None
    queue = asyncio.Queue()
    phase = "startup"
    database = None
    instances = []
    summary = []
    inbound_messages = []
    forecast_checks = []
    workspace = root / "workspaces/assistant"

    async def packets(reader):
        pending = bytearray()
        while chunk := await reader.read(8192):
            pending.extend(chunk)
            while (end := pending.find(b"\n")) >= 0:
                line = pending[:end]
                del pending[: end + 1]
                yield json.loads(line)
        if pending:
            yield json.loads(pending)

    async def collect(reader):
        async for packet in packets(reader):
            events.write(json.dumps({"phase": phase, **packet}) + "\n")
            events.flush()
            print(json.dumps({"phase": phase, **packet}), flush=True)
            await queue.put(packet)
        await queue.put(None)

    async def start():
        nonlocal process, writer, collector, queue, phase, database
        phase = "startup"
        document = tomllib.loads((root / "node.toml").read_text())
        database = next(
            entry["config"]["path"]
            for package in document["contribs"]
            for entry in package["enabled"]
            if entry["entry"] == "sqlite"
        )
        endpoint = workspace / "channel.json"
        endpoint.unlink(missing_ok=True)
        queue = asyncio.Queue()
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(command),
            str(root / "node.toml"),
            cwd=root,
            env=environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=log,
            stderr=log,
        )
        print(
            json.dumps({"node_started": process.pid, "database": database}), flush=True
        )
        while not endpoint.exists():
            if process.returncode is not None:
                raise RuntimeError("The isolated node exited; inspect node.log")
            await asyncio.sleep(0.05)
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", json.loads(endpoint.read_text())["port"]
        )
        collector = asyncio.create_task(collect(reader))

    async def stop():
        nonlocal phase
        phase = "shutdown"
        if process is None:
            return
        try:
            children = await asyncio.to_thread(
                psutil.Process(process.pid).children, recursive=True
            )
        except psutil.NoSuchProcess:
            children = []
        if process.returncode is None:
            assert process.stdin is not None
            process.stdin.close()
        await process.wait()
        if collector is not None:
            await collector
        if writer is not None:
            writer.close()
            await writer.wait_closed()
        alive = []
        for child in children:
            try:
                if child.is_running() and child.status() != psutil.STATUS_ZOMBIE:
                    alive.append(child.pid)
            except psutil.NoSuchProcess:
                pass
        record = {
            "node_pid": process.pid,
            "database": database,
            "codex_home": environment["CODEX_HOME"],
            "node_returncode": process.returncode,
            "live_owned_children": alive,
        }
        instances.append(record)
        (root / "cleanup.json").write_text(
            json.dumps({"instances": instances}, indent=2) + "\n"
        )
        print(json.dumps({"node_stopped": record}), flush=True)
        assert process.returncode == 0, record
        assert not alive, record

    async def restart(number):
        nonlocal process, writer, collector
        await stop()
        process = writer = collector = None
        config = root / "node.toml"
        document = tomllib.loads(config.read_text())
        for package in document["contribs"]:
            for entry in package["enabled"]:
                if entry["entry"] == "sqlite":
                    entry["config"]["path"] = str(root / f"node-{number}.sqlite3")
        config.write_text(tomli_w.dumps(document))
        await start()

    try:
        await start()

        async def inbound(body):
            assert writer is not None
            message = ChannelMessage(
                id=str(uuid7()),
                target=ChannelTarget(kind="dm", thread_id="tester"),
                sender=SenderIdentity(id="tester", name="Tester"),
                mentions_agent=True,
                body=body,
                metadata={"review": "approved"},
            )
            inbound_messages.append(message.model_dump(mode="json"))
            (root / "inbound.json").write_text(
                json.dumps(inbound_messages, indent=2) + "\n"
            )
            writer.write(
                json.dumps({"inbound": message.model_dump(mode="json")}).encode()
                + b"\n"
            )
            await writer.drain()
            return message.id

        async def finish(
            label,
            steer=False,
            stop_at_started=False,
            reject=False,
            scope=ApprovalScope.ONCE,
            forecast=False,
            input_message_id=None,
        ):
            nonlocal phase
            assert writer is not None
            phase = label
            print(json.dumps({"phase_start": label}), flush=True)
            calls = []
            outbound = []
            interactions = []
            tools = []
            started_tools = []
            user_messages = set()
            steered = False
            terminal = None
            completion = None
            canonical_message_id = None
            entered = input_message_id is None
            previous_items = set()
            while (packet := await queue.get()) is not None:
                audit = packet.get("audit", {})
                if (
                    audit.get("event_name") == "channel.received"
                    and audit["payload"].get("provider_message_id") == input_message_id
                ):
                    canonical_message_id = audit["payload"]["id"]
                event = packet.get("event", {})
                item = event.get("metadata", {}).get("item", {})
                if item.get("type") == "userMessage" and canonical_message_id:
                    entered = (
                        entered
                        or item.get("clientId") == ("turn-" + canonical_message_id)
                        or any(
                            canonical_message_id[:8]
                            in re.findall(
                                r"(?:first|latest) msg=([a-f0-9-]+)",
                                content.get("text", ""),
                            )
                            for content in item.get("content", [])
                        )
                    )
                assert event.get("kind") != "compaction.started", (
                    f"Context compaction entered during {label}; inspect events.jsonl"
                )
                if (
                    steer
                    and not steered
                    and event.get("kind") in {"text", "tool.started"}
                ):
                    await inbound("Please keep your reply brief.")
                    steered = True
                if "outbound" in packet:
                    writer.write(json.dumps({"id": packet["id"]}).encode() + b"\n")
                    await writer.drain()
                    outbound.append(packet["outbound"])
                if "approval" in packet:
                    request = ChannelApprovalRequest.model_validate(packet["approval"])
                    decision = ApprovalResult(
                        request_id=request.approval.request_id,
                        decision="rejected"
                        if reject and not interactions
                        else "approved",
                        scope=scope
                        if scope in request.approval.allowed_scopes
                        else ApprovalScope.ONCE,
                    )
                    writer.write(
                        json.dumps(
                            {
                                "id": packet["id"],
                                "decision": decision.model_dump(mode="json"),
                            }
                        ).encode()
                        + b"\n"
                    )
                    await writer.drain()
                    interactions.append(
                        {
                            "request": request.model_dump(mode="json"),
                            "decision": decision.model_dump(mode="json"),
                        }
                    )
                if event.get("kind") == "tool.started":
                    if entered:
                        started_tools.append({"kind": event["kind"], "item": item})
                    else:
                        previous_items.add(item["id"])
                if (
                    entered
                    and item.get("id") not in previous_items
                    and event.get("kind") in {"tool.completed", "tool.failed"}
                ):
                    tools.append({"kind": event["kind"], "item": item})
                if (
                    event.get("kind") == "tool.completed"
                    and item.get("type") == "commandExecution"
                    and item.get("exitCode") == 0
                    and re.search(
                        r"\bbcc(?:\.cmd)?(?=[\s\"']|$)", item.get("command", "")
                    )
                ):
                    calls.append(item["command"])
                if item.get("type") == "userMessage":
                    user_messages.add(item["id"])
                if (
                    entered
                    and forecast
                    and item.get("id") not in previous_items
                    and event.get("kind") in {"tool.completed", "tool.failed"}
                    and item.get("type") == "mcpToolCall"
                    and item.get("server") == "weather"
                    and item.get("tool") == "get_forecast"
                ):
                    terminal = event
                    completion = "mcp_result"
                    break
                if entered and (
                    event.get("kind") in {"completed", "cancelled", "failed", "unknown"}
                    or (stop_at_started and event.get("kind") == "started")
                ):
                    terminal = event
                    completion = "turn_event"
                    break
            assert terminal, (
                "The channel closed before the requested state was observed"
            )
            if completion != "mcp_result":
                assert terminal["kind"] == (
                    "started" if stop_at_started else "completed"
                ), terminal
            weather_log = root / "weather-requests.jsonl"
            weather_queries = (
                [json.loads(line) for line in weather_log.read_text().splitlines()]
                if weather_log.exists()
                else []
            )
            record = {
                "phase": label,
                "bcc_calls": calls,
                "outbound": outbound,
                "interactions": interactions,
                "tools": tools,
                "started_tools": started_tools,
                "session": terminal["session_id"],
                "provider_thread": terminal["provider_session_id"],
                "provider_turn": terminal["provider_turn_id"],
                "state": terminal["kind"],
                "completion": completion,
                "input_message_id": input_message_id,
                "canonical_message_id": canonical_message_id,
                "input_entered_native_turn": entered,
                "previous_item_ids": sorted(previous_items),
                "steered": steered,
                "native_user_messages": len(user_messages),
                "weather_queries": [
                    {
                        key: query[key]
                        for key in (
                            "arguments",
                            "points_status",
                            "forecast_status",
                            "result",
                        )
                    }
                    for query in weather_queries
                ],
            }
            summary.append(record)
            (root / "progress.json").write_text(json.dumps(summary, indent=2) + "\n")
            print(json.dumps({"phase_complete": label, **record}), flush=True)
            return record

        def verify_forecast(record, arguments, scope=None):
            """Check actual MCP execution against retained live HTTP results."""
            calls = [
                tool
                for tool in record["tools"]
                if tool["item"].get("type") == "mcpToolCall"
                and tool["item"].get("server") == "weather"
                and tool["item"].get("tool") == "get_forecast"
            ]
            assert calls and all(tool["kind"] == "tool.completed" for tool in calls), (
                record
            )
            assert any(tool["item"]["arguments"] == arguments for tool in calls)
            queries = [
                query
                for query in record["weather_queries"]
                if query["arguments"] == arguments
            ]
            assert queries and all(
                query["points_status"] == query["forecast_status"] == 200
                and query["result"]["latitude"] == arguments["latitude"]
                and query["result"]["longitude"] == arguments["longitude"]
                and query["result"]["forecast_url"].startswith(
                    "https://api.weather.gov/gridpoints/"
                )
                and query["result"]["updated_at"]
                and query["result"]["periods"]
                for query in queries
            ), record
            assert any(
                tool["item"]["result"]["structuredContent"] == query["result"]
                for tool in calls
                for query in queries
            )
            interactions = [
                interaction
                for interaction in record["interactions"]
                if interaction["request"]["approval"]["action"] == "mcp_tool_approval"
            ]
            if scope is None:
                assert not interactions, interactions
            else:
                assert interactions and all(
                    interaction["decision"]["decision"] == "approved"
                    and interaction["decision"]["scope"] == scope.value
                    and scope.value
                    in interaction["request"]["approval"]["allowed_scopes"]
                    for interaction in interactions
                ), record

        async def query_forecast(label, request, latitude, longitude, scope=None):
            """Observe the MCP result without waiting for a natural turn ending."""
            nonlocal phase
            phase = label
            attempts = []
            arguments = {"latitude": latitude, "longitude": longitude}
            checkpoint = {
                "phase": label,
                "arguments": arguments,
                "scope": scope.value if scope else None,
                "attempts": attempts,
                "feedback_count": 0,
                "feedback_limit": FORECAST_FEEDBACK_LIMIT,
                "verified": False,
            }
            forecast_checks.append(checkpoint)
            input_message_id = await inbound(request)
            context = None
            retry = 0
            while True:
                phase = label if retry == 0 else f"{label}-retry-{retry}"
                attempts.append(phase)
                checkpoint["feedback_count"] = retry
                (root / "mcp-checkpoints.json").write_text(
                    json.dumps(forecast_checks, indent=2) + "\n"
                )
                record = await finish(
                    phase,
                    scope=scope or ApprovalScope.ONCE,
                    forecast=True,
                    input_message_id=input_message_id,
                )
                actual_context = (record["session"], record["provider_thread"])
                if context is None:
                    context = actual_context
                assert actual_context == context, record
                called = any(
                    tool["item"].get("type") == "mcpToolCall"
                    and tool["item"].get("server") == "weather"
                    and tool["item"].get("tool") == "get_forecast"
                    for tool in record["started_tools"] + record["tools"]
                )
                approval_requested = any(
                    interaction["request"]["approval"]["action"] == "mcp_tool_approval"
                    for interaction in record["interactions"]
                )
                if called or approval_requested or retry == FORECAST_FEEDBACK_LIMIT:
                    verify_forecast(record, arguments, scope)
                    checkpoint["verified"] = True
                    (root / "mcp-checkpoints.json").write_text(
                        json.dumps(forecast_checks, indent=2) + "\n"
                    )
                    return record
                retry += 1
                feedback = (
                    "The requested weather forecast has not been retrieved yet. "
                    "Please call the weather MCP tool get_forecast with latitude "
                    f"{latitude}, longitude {longitude} to retrieve the actual forecast."
                )
                print(
                    json.dumps({"phase": label, "retry": retry, "feedback": feedback}),
                    flush=True,
                )
                phase = f"{label}-retry-{retry}"
                input_message_id = await inbound(feedback)

        if scenario == "mcp-once":
            first = await query_forecast(
                "mcp-once",
                "What is the weather forecast for Seattle "
                "(latitude 47.6062, longitude -122.3321)?",
                47.6062,
                -122.3321,
                ApprovalScope.ONCE,
            )
        elif scenario == "bcc":
            await inbound("Please remind me to drink some water in five seconds.")
            first = await finish("bcc", steer=True)
            assert first["bcc_calls"], (
                "No successful bcc call was observed; inspect events.jsonl"
            )
            assert any(
                interaction["request"]["approval"]["action"] == "command_execution"
                for interaction in first["interactions"]
            ), "No native command approval reached the channel"
            assert first["steered"] and first["native_user_messages"] > 1

            await inbound("What reminder did I ask you to set? Please reply to me.")
            continued = await finish("continued", reject=True)
            assert (
                continued["session"] == first["session"]
                and continued["provider_thread"] == first["provider_thread"]
            )
            rejected = {
                interaction["request"]["approval"]["details"]["itemId"]
                for interaction in continued["interactions"]
                if interaction["decision"]["decision"] == "rejected"
            }
            assert rejected and any(
                tool["kind"] == "tool.failed" and tool["item"]["id"] in rejected
                for tool in continued["tools"]
            ), continued

        elif scenario == "mcp-session":
            first = await query_forecast(
                "mcp-session",
                "What is the weather forecast for New York "
                "(latitude 40.7128, longitude -74.0060)?",
                40.7128,
                -74.0060,
                ApprovalScope.SESSION,
            )
            remembered_session = await query_forecast(
                "mcp-session-again",
                "What is the weather forecast for Chicago "
                "(latitude 41.8781, longitude -87.6298)?",
                41.8781,
                -87.6298,
            )
            assert remembered_session["session"] == first["session"]
            assert remembered_session["provider_thread"] == first["provider_thread"]

        elif scenario == "mcp-always":
            first = await query_forecast(
                "mcp-session",
                "What is the weather forecast for New York "
                "(latitude 40.7128, longitude -74.0060)?",
                40.7128,
                -74.0060,
                ApprovalScope.SESSION,
            )
            (workspace / "AGENTS.md").write_text("Keep replies concise.\n")
            await restart(1)
            refreshed = await query_forecast(
                "refreshed",
                "What is the weather forecast for Boston "
                "(latitude 42.3601, longitude -71.0589)?",
                42.3601,
                -71.0589,
                ApprovalScope.ALWAYS,
            )
            assert (
                refreshed["session"] != first["session"]
                and refreshed["provider_thread"] != first["provider_thread"]
            )

            (workspace / "AGENTS.md").write_text("Keep replies brief and clear.\n")
            await restart(2)
            remembered_always = await query_forecast(
                "mcp-always",
                "What is the weather forecast for San Francisco "
                "(latitude 37.7749, longitude -122.4194)?",
                37.7749,
                -122.4194,
            )
            assert remembered_always["session"] != refreshed["session"]
            assert remembered_always["provider_thread"] != refreshed["provider_thread"]

        elif scenario == "recovery":
            assert process is not None
            await inbound("Please remember that I prefer brief replies.")
            first = await finish("initial")
            parent = psutil.Process(process.pid)
            servers = [
                child
                for child in parent.children()
                if "app-server" in child.cmdline()
                and child.environ().get("CODEX_HOME") == str(root / "codex-home")
            ]
            assert len(servers) == 1
            server = servers[0]
            owned = [server, *server.children(recursive=True)]
            (root / "recovery-process.json").write_text(
                json.dumps(
                    {
                        "pid": server.pid,
                        "ppid": server.ppid(),
                        "cmdline": server.cmdline(),
                    }
                )
                + "\n"
            )
            for child in reversed(owned):
                try:
                    await asyncio.to_thread(child.terminate)
                except psutil.NoSuchProcess:
                    pass
            await asyncio.to_thread(psutil.wait_procs, owned)
            await inbound("What reply style did I ask for? Please reply to me.")
            recovered = await finish("recovered")
            assert (
                recovered["session"] == first["session"]
                and recovered["provider_thread"] == first["provider_thread"]
            )
            current = [
                child
                for child in parent.children()
                if "app-server" in child.cmdline()
                and child.environ().get("CODEX_HOME") == str(root / "codex-home")
            ]
            assert len(current) == 1 and current[0].pid != server.pid
            await inbound("Tell me briefly what you can help me with.")
            await finish("stop-active", stop_at_started=True)

        elif scenario == "refresh":
            await inbound("Please remember that I prefer brief replies.")
            first = await finish("initial")
            (workspace / "AGENTS.md").write_text("Keep replies concise.\n")
            await asyncio.sleep(1)
            await inbound("What reply style did I ask for? Please reply to me.")
            refreshed = await finish("refreshed")
            assert refreshed["session"] != first["session"]
            assert refreshed["provider_thread"] != first["provider_thread"]

    finally:
        await stop()
        log.close()
        events.close()
    (root / "verified.json").write_text(json.dumps(summary, indent=2) + "\n")


if __name__ == "__main__":
    asyncio.run(exercise())
