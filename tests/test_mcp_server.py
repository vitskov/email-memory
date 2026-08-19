from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from email_memory_store.retrieval import mcp_server
from email_memory_store.runtime import RUNTIME_CONFIG_ENV, RuntimeSettings


def _initialized_runtime(root: Path) -> Path:
    chroma_path = root / "chroma"
    chroma_path.mkdir(parents=True)
    (chroma_path / mcp_server.CHROMA_DATABASE_FILE).write_bytes(mcp_server.SQLITE_HEADER)
    return chroma_path


def test_explicit_root_has_precedence_over_manifest(tmp_path: Path, monkeypatch) -> None:
    manifest = tmp_path / "runtime.toml"
    manifest.write_text('runtime_root = "/manifest/runtime"\n', encoding="utf-8")
    captured: dict[str, object] = {}

    def fake_resolve_runtime_settings(**kwargs):
        captured.update(kwargs)
        return RuntimeSettings(runtime_root=Path(str(kwargs["runtime_root"])).expanduser())

    monkeypatch.setattr(mcp_server, "resolve_runtime_settings", fake_resolve_runtime_settings)

    root = mcp_server._resolve_runtime_root(
        root="~/explicit-runtime",
        runtime_config=manifest,
        environ={},
    )

    assert root == Path("~/explicit-runtime").expanduser()
    assert captured["runtime_root"] == "~/explicit-runtime"
    assert captured["runtime_config"] == manifest


def test_runtime_config_option_precedes_environment(tmp_path: Path, monkeypatch) -> None:
    selected_manifest = tmp_path / "selected.toml"
    selected_manifest.write_text('runtime_root = "/selected/runtime"\n', encoding="utf-8")
    env_manifest = tmp_path / "environment.toml"
    env_manifest.write_text('runtime_root = "/environment/runtime"\n', encoding="utf-8")
    captured: dict[str, object] = {}

    def fake_resolve_runtime_settings(**kwargs):
        captured.update(kwargs)
        return RuntimeSettings(runtime_root=Path("/resolved/runtime"))

    monkeypatch.setattr(mcp_server, "resolve_runtime_settings", fake_resolve_runtime_settings)

    root = mcp_server._resolve_runtime_root(
        root=None,
        runtime_config=selected_manifest,
        environ={RUNTIME_CONFIG_ENV: str(env_manifest)},
    )

    assert root == Path("/resolved/runtime")
    assert captured["runtime_config"] == selected_manifest


def test_runtime_config_environment_selects_runtime(tmp_path: Path) -> None:
    manifest = tmp_path / "runtime.toml"
    manifest.write_text(
        f'runtime_root = "{tmp_path / "runtime"}"\n',
        encoding="utf-8",
    )

    root = mcp_server._resolve_runtime_root(
        root=None,
        runtime_config=None,
        environ={RUNTIME_CONFIG_ENV: str(manifest)},
    )

    assert root == tmp_path / "runtime"


def test_runtime_attachment_is_required() -> None:
    with pytest.raises(mcp_server.MCPConfigurationError, match="explicit --root"):
        mcp_server._resolve_runtime_root(root=None, runtime_config=None, environ={})


def test_manifest_must_select_a_runtime(tmp_path: Path) -> None:
    manifest = tmp_path / "runtime.toml"
    manifest.write_text("schema_version = 1\n", encoding="utf-8")

    with pytest.raises(mcp_server.MCPConfigurationError, match="must define runtime_root"):
        mcp_server._resolve_runtime_root(
            root=None,
            runtime_config=manifest,
            environ={},
        )


def test_build_engine_uses_existing_runtime_chroma_store(tmp_path: Path, monkeypatch) -> None:
    runtime_root = tmp_path / "runtime"
    chroma_path = _initialized_runtime(runtime_root)
    captured: dict[str, object] = {}
    sentinel_engine = object()

    class FakeVectorStore:
        def __init__(self, persist_path) -> None:
            captured["persist_path"] = Path(persist_path)

        def existing_collection_counts(self) -> dict[str, int]:
            return {"holographic_facts": 1}

    def fake_engine(*, vector_store):
        captured["vector_store"] = vector_store
        return sentinel_engine

    monkeypatch.setattr(mcp_server, "VectorStore", FakeVectorStore)
    monkeypatch.setattr(mcp_server, "RetrievalEngine", fake_engine)

    engine = mcp_server._build_engine(
        root=runtime_root,
        runtime_config=None,
        environ={},
    )

    assert engine is sentinel_engine
    assert captured["persist_path"] == chroma_path


def test_build_engine_rejects_a_store_without_indexed_data(tmp_path: Path, monkeypatch) -> None:
    runtime_root = tmp_path / "runtime"
    _initialized_runtime(runtime_root)

    class EmptyVectorStore:
        def __init__(self, _persist_path) -> None:
            pass

        def existing_collection_counts(self) -> dict[str, int]:
            return {"holographic_facts": 0}

    monkeypatch.setattr(mcp_server, "VectorStore", EmptyVectorStore)

    with pytest.raises(mcp_server.MCPConfigurationError, match="contains no indexed data"):
        mcp_server._build_engine(
            root=runtime_root,
            runtime_config=None,
            environ={},
        )


@pytest.mark.parametrize("store_state", ["missing", "directory-only", "empty-database"])
def test_build_engine_rejects_an_uninitialized_store(
    tmp_path: Path,
    store_state: str,
) -> None:
    runtime_root = tmp_path / "runtime"
    if store_state != "missing":
        (runtime_root / "chroma").mkdir(parents=True)
    if store_state == "empty-database":
        (runtime_root / "chroma" / mcp_server.CHROMA_DATABASE_FILE).touch()

    with pytest.raises(mcp_server.MCPConfigurationError, match="initialized Chroma store"):
        mcp_server._build_engine(
            root=runtime_root,
            runtime_config=None,
            environ={},
        )

    database_path = runtime_root / "chroma" / mcp_server.CHROMA_DATABASE_FILE
    assert not database_path.exists() or database_path.stat().st_size == 0


def test_build_engine_rejects_a_symlinked_store(tmp_path: Path) -> None:
    actual_runtime = tmp_path / "actual-runtime"
    actual_chroma = _initialized_runtime(actual_runtime)
    configured_root = tmp_path / "configured-runtime"
    configured_root.mkdir()
    (configured_root / "chroma").symlink_to(actual_chroma, target_is_directory=True)

    with pytest.raises(mcp_server.MCPConfigurationError, match="initialized Chroma store"):
        mcp_server._build_engine(
            root=configured_root,
            runtime_config=None,
            environ={},
        )


def test_build_engine_rejects_a_symlinked_database(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    chroma_path = runtime_root / "chroma"
    chroma_path.mkdir(parents=True)
    actual_database = tmp_path / "actual.sqlite3"
    actual_database.write_bytes(mcp_server.SQLITE_HEADER)
    (chroma_path / mcp_server.CHROMA_DATABASE_FILE).symlink_to(actual_database)

    with pytest.raises(mcp_server.MCPConfigurationError, match="initialized Chroma store"):
        mcp_server._build_engine(
            root=runtime_root,
            runtime_config=None,
            environ={},
        )


def test_main_fails_before_stdio_without_attachment(monkeypatch, capsys) -> None:
    monkeypatch.delenv(RUNTIME_CONFIG_ENV, raising=False)
    monkeypatch.setattr(
        mcp_server,
        "_run",
        lambda *_args: pytest.fail("stdio must not open without an attachment"),
    )

    with pytest.raises(SystemExit) as error:
        mcp_server.main([])

    assert error.value.code == 2
    assert "explicit --root or runtime manifest" in capsys.readouterr().err


def test_main_redacts_invalid_manifest_path(tmp_path: Path, monkeypatch, capsys) -> None:
    missing_manifest = tmp_path / "private-location" / "runtime.toml"
    monkeypatch.setattr(
        mcp_server,
        "_run",
        lambda *_args: pytest.fail("stdio must not open after a configuration error"),
    )

    with pytest.raises(SystemExit) as error:
        mcp_server.main(["--runtime-config", str(missing_manifest)])

    stderr = capsys.readouterr().err
    assert error.value.code == 2
    assert "runtime manifest is unavailable or invalid" in stderr
    assert str(missing_manifest) not in stderr


def test_server_reuses_one_engine_for_search_and_ask(monkeypatch) -> None:
    class FakeServer:
        def __init__(self, name: str) -> None:
            self.name = name
            self.list_tools_handler = None
            self.call_tool_handler = None

        def list_tools(self):
            def decorator(func):
                self.list_tools_handler = func
                return func

            return decorator

        def call_tool(self):
            def decorator(func):
                self.call_tool_handler = func
                return func

            return decorator

    class FakeEngine:
        def search(self, query: str, **_kwargs):
            return [
                SimpleNamespace(
                    collection="holographic_facts",
                    id="fact-1",
                    score=1.0,
                    document="Synthetic fact",
                    metadata={"thread_id": 7},
                    semantic_rank=0,
                    lexical_rank=0,
                    semantic_distance=0.01,
                )
            ]

    answerer_engines: list[object] = []

    class FakeAnswerer:
        def __init__(self, *, engine, provider_spec=None) -> None:
            answerer_engines.append(engine)

        def answer(self, query: str, **_kwargs):
            return SimpleNamespace(
                query=query,
                answer="Synthetic answer [F:fact-1]",
                used_handles=["F:fact-1"],
                citations=[
                    SimpleNamespace(
                        handle="F:fact-1",
                        collection="holographic_facts",
                        id="fact-1",
                        metadata={"thread_id": 7},
                        document="Synthetic fact",
                    )
                ],
            )

    monkeypatch.setattr(mcp_server, "Server", FakeServer)
    monkeypatch.setattr(mcp_server, "Answerer", FakeAnswerer)
    engine = FakeEngine()

    server = mcp_server.build_server(engine=engine)
    tools = asyncio.run(server.list_tools_handler())
    search_response = asyncio.run(server.call_tool_handler("search", {"query": "synthetic"}))
    ask_response = asyncio.run(server.call_tool_handler("ask", {"query": "synthetic"}))

    assert [tool.name for tool in tools] == ["search", "ask"]
    assert json.loads(search_response[0].text)["results"][0]["id"] == "fact-1"
    assert json.loads(ask_response[0].text)["used_handles"] == ["F:fact-1"]
    assert answerer_engines == [engine]
