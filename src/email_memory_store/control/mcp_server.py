"""Typed MCP server for bounded Email Memory status and operations."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
import json
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool, ToolAnnotations

from . import jobs


SERVER_NAME = "email-memory-store-control"


def tool_definitions() -> list[Tool]:
    return [
        Tool(
            name="system_status",
            description=(
                "Return redacted Email Memory deployment readiness and the active "
                "operation, if any. Never returns local paths or credentials."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            annotations=ToolAnnotations(
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=False,
            ),
        ),
        Tool(
            name="job_start",
            description=(
                "Start one Email Memory operation in a worker detached from this MCP "
                "server process. Status is recorded durably; operations are serialized "
                "with the deployment maintenance lock."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": list(jobs.ACTIONS),
                    }
                },
                "required": ["action"],
                "additionalProperties": False,
            },
            annotations=ToolAnnotations(
                readOnlyHint=False,
                destructiveHint=True,
                idempotentHint=False,
                openWorldHint=False,
            ),
        ),
        Tool(
            name="job_status",
            description="Return a redacted durable status for one Email Memory job.",
            inputSchema={
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "pattern": "^[0-9a-f]{32}$",
                    }
                },
                "required": ["job_id"],
                "additionalProperties": False,
            },
            annotations=ToolAnnotations(
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=False,
            ),
        ),
    ]


def _exact_arguments(arguments: dict[str, Any], expected: set[str]) -> None:
    if set(arguments) != expected:
        raise ValueError("invalid arguments")


def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "system_status":
        _exact_arguments(arguments, set())
        return jobs.system_status()
    if name == "job_start":
        _exact_arguments(arguments, {"action"})
        action = arguments["action"]
        if not isinstance(action, str) or action not in jobs.ACTIONS:
            raise ValueError("invalid action")
        return jobs.start_job(action)
    if name == "job_status":
        _exact_arguments(arguments, {"job_id"})
        job_id = arguments["job_id"]
        if not isinstance(job_id, str):
            raise ValueError("invalid job identifier")
        return jobs.job_status(job_id)
    raise ValueError("unknown tool")


def build_server() -> Server:
    server = Server(SERVER_NAME)

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        return tool_definitions()

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        payload = await asyncio.to_thread(call_tool, name, arguments)
        return [TextContent(type="text", text=json.dumps(payload, sort_keys=True))]

    return server


async def _run(server: Server) -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream, server.create_initialization_options()
        )


def main(_argv: Sequence[str] | None = None) -> None:
    asyncio.run(_run(build_server()))


if __name__ == "__main__":
    main()
