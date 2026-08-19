"""MCP stdio server exposing email-memory-store retrieval and RAG answer tools."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from ..promotion.llm import LLMProviderSpec
from ..runtime import RUNTIME_CONFIG_ENV, load_runtime_config, resolve_runtime_settings
from .answerer import Answerer
from .engine import EFFORT_LEVELS, RetrievalEngine
from .filters import RetrievalFilters, parse_natural_date_range
from .vector_store import VectorStore


SERVER_NAME = "email-memory-store"
CHROMA_DATABASE_FILE = "chroma.sqlite3"
SQLITE_HEADER = b"SQLite format 3\x00"


class MCPConfigurationError(ValueError):
    """A redacted startup error for an unavailable local runtime attachment."""


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="email-memory-store-mcp")
    parser.add_argument(
        "--root",
        default=None,
        help="Runtime root containing an existing Chroma store",
    )
    parser.add_argument(
        "--runtime-config",
        default=None,
        help=(
            "Path to a local runtime attachment TOML file. "
            f"Falls back to {RUNTIME_CONFIG_ENV}."
        ),
    )
    return parser


def _resolve_runtime_root(
    *,
    root: str | Path | None,
    runtime_config: str | Path | None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    env = dict(os.environ if environ is None else environ)
    selected_config = runtime_config if runtime_config is not None else env.get(RUNTIME_CONFIG_ENV)
    if root is None and not selected_config:
        raise MCPConfigurationError(
            "an explicit --root or runtime manifest is required"
        )

    if selected_config:
        try:
            configured = load_runtime_config(selected_config)
        except (OSError, ValueError) as error:
            raise MCPConfigurationError("the runtime manifest is unavailable or invalid") from error
        if root is None and configured.runtime_root is None and configured.provider_name is None:
            raise MCPConfigurationError(
                "the runtime manifest must define runtime_root or runtime_provider"
            )

    try:
        settings = resolve_runtime_settings(
            runtime_root=root,
            work_root=None,
            runtime_config=selected_config,
            environ=env,
        )
    except (OSError, ValueError) as error:
        raise MCPConfigurationError("the runtime attachment could not be resolved") from error
    return settings.runtime_root


def _is_initialized_chroma_store(chroma_path: Path) -> bool:
    database_path = chroma_path / CHROMA_DATABASE_FILE
    if (
        not chroma_path.is_dir()
        or chroma_path.is_symlink()
        or not database_path.is_file()
        or database_path.is_symlink()
    ):
        return False
    try:
        with database_path.open("rb") as database_file:
            return database_file.read(len(SQLITE_HEADER)) == SQLITE_HEADER
    except OSError:
        return False


def _build_engine(
    *,
    root: str | Path | None,
    runtime_config: str | Path | None,
    environ: Mapping[str, str] | None = None,
) -> RetrievalEngine:
    runtime_root = _resolve_runtime_root(
        root=root,
        runtime_config=runtime_config,
        environ=environ,
    )
    chroma_path = runtime_root / "chroma"
    if not _is_initialized_chroma_store(chroma_path):
        raise MCPConfigurationError(
            "the configured runtime does not contain an initialized Chroma store"
        )
    try:
        vector_store = VectorStore(chroma_path)
        collection_counts = vector_store.existing_collection_counts()
    except Exception as error:
        raise MCPConfigurationError("the configured Chroma store could not be opened") from error
    if sum(collection_counts.values()) == 0:
        raise MCPConfigurationError("the configured Chroma store contains no indexed data")
    return RetrievalEngine(vector_store=vector_store)


def _do_search(engine: RetrievalEngine, args: dict[str, Any]) -> dict[str, Any]:
    query = str(args["query"])
    filters = _build_filters(args, query)
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


def _do_ask(engine: RetrievalEngine, args: dict[str, Any]) -> dict[str, Any]:
    query = str(args["query"])
    filters = _build_filters(args, query)
    answerer = Answerer(engine=engine, provider_spec=_provider_spec(args))
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


def build_server(*, engine: RetrievalEngine) -> Server:
    server = Server(SERVER_NAME)

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        return [_search_tool(), _ask_tool()]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        if name == "search":
            payload = await asyncio.to_thread(_do_search, engine, arguments)
        elif name == "ask":
            payload = await asyncio.to_thread(_do_ask, engine, arguments)
        else:
            raise ValueError(f"unknown tool: {name}")
        return [TextContent(type="text", text=json.dumps(payload, indent=2, default=str))]

    return server


async def _run(server: Server) -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        engine = _build_engine(
            root=args.root,
            runtime_config=args.runtime_config,
        )
    except MCPConfigurationError as error:
        print(f"{SERVER_NAME}: {error}", file=sys.stderr)
        raise SystemExit(2) from error
    asyncio.run(_run(build_server(engine=engine)))


if __name__ == "__main__":
    main()
