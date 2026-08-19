"""MCP stdio server exposing email-memory-store retrieval and RAG answer tools."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from ..promotion.llm import LLMProviderSpec
from .answerer import Answerer
from .engine import EFFORT_LEVELS, RetrievalEngine
from .filters import RetrievalFilters, parse_natural_date_range


SERVER_NAME = "email-memory-store"


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _build_filters(args: dict[str, Any], query: str) -> RetrievalFilters:
    date_from, date_to = parse_natural_date_range(query)
    arg_from = _parse_iso(args.get("date_from"))
    arg_to = _parse_iso(args.get("date_to"))
    return RetrievalFilters(
        date_from=arg_from or date_from,
        date_to=arg_to or date_to,
        thread_id=args.get("thread_id"),
        thread_key=args.get("thread_key"),
    )


def _provider_spec(args: dict[str, Any]) -> LLMProviderSpec | None:
    provider = args.get("provider")
    if not provider:
        return None
    return LLMProviderSpec(name=str(provider), model=args.get("model"))


def _search_tool() -> Tool:
    return Tool(
        name="search",
        description=(
            "Hybrid semantic + lexical search across email memory store. "
            "Returns a ranked list of matches across holographic facts, action items, "
            "deadlines, decisions, thread summaries, and message chunks. "
            "Effort dial: light=facts only, medium=+extracted entities, heavy=+message chunks."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Free-text query"},
                "effort": {"type": "string", "enum": list(EFFORT_LEVELS), "default": "medium"},
                "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 100},
                "thread_id": {"type": "integer"},
                "thread_key": {"type": "string"},
                "date_from": {"type": "string", "description": "ISO 8601 datetime lower bound"},
                "date_to": {"type": "string", "description": "ISO 8601 datetime upper bound"},
            },
            "required": ["query"],
        },
    )


def _ask_tool() -> Tool:
    return Tool(
        name="ask",
        description=(
            "Answer a natural-language question about your email using RAG. "
            "Retrieves relevant rows then asks the configured LLM to synthesize "
            "an answer with inline citation handles (F:/A:/D:/DC:/S:/M:). "
            "Refuses if retrieval is empty."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "effort": {"type": "string", "enum": list(EFFORT_LEVELS), "default": "medium"},
                "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
                "thread_id": {"type": "integer"},
                "thread_key": {"type": "string"},
                "date_from": {"type": "string"},
                "date_to": {"type": "string"},
                "provider": {"type": "string", "description": "LLM provider name (hermes-default, codex-cli, claude-code-cli)"},
                "model": {"type": "string"},
            },
            "required": ["query"],
        },
    )


def _do_search(args: dict[str, Any]) -> dict[str, Any]:
    query = str(args["query"])
    filters = _build_filters(args, query)
    engine = RetrievalEngine()
    results = engine.search(
        query,
        effort=args.get("effort", "medium"),
        limit=int(args.get("limit", 10)),
        filters=filters,
    )
    return {
        "query": query,
        "effort": args.get("effort", "medium"),
        "results": [
            {
                "collection": r.collection,
                "id": r.id,
                "score": r.score,
                "document": r.document,
                "metadata": r.metadata,
                "semantic_rank": r.semantic_rank,
                "lexical_rank": r.lexical_rank,
                "semantic_distance": r.semantic_distance,
            }
            for r in results
        ],
    }


def _do_ask(args: dict[str, Any]) -> dict[str, Any]:
    query = str(args["query"])
    filters = _build_filters(args, query)
    answerer = Answerer(provider_spec=_provider_spec(args))
    result = answerer.answer(
        query,
        effort=args.get("effort", "medium"),
        limit=int(args.get("limit", 10)),
        filters=filters,
    )
    return {
        "query": result.query,
        "answer": result.answer,
        "used_handles": result.used_handles,
        "citations": [
            {
                "handle": c.handle,
                "collection": c.collection,
                "id": c.id,
                "metadata": c.metadata,
                "document": c.document,
            }
            for c in result.citations
        ],
    }


def build_server() -> Server:
    server = Server(SERVER_NAME)

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        return [_search_tool(), _ask_tool()]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        if name == "search":
            payload = await asyncio.to_thread(_do_search, arguments)
        elif name == "ask":
            payload = await asyncio.to_thread(_do_ask, arguments)
        else:
            raise ValueError(f"unknown tool: {name}")
        return [TextContent(type="text", text=json.dumps(payload, indent=2, default=str))]

    return server


async def _run() -> None:
    server = build_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
