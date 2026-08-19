from __future__ import annotations

import os
from pathlib import Path
from threading import Lock

import chromadb


COLLECTION_NAMES = (
    "holographic_facts",
    "action_items",
    "deadlines",
    "calendar_events",
    "decisions",
    "thread_summaries",
    "message_chunks",
)

def default_persist_path(*, environ: dict[str, str] | None = None) -> Path:
    """Return the generic XDG state location for persisted vector collections."""
    env = os.environ if environ is None else environ
    state_home = env.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "email-memory-store" / "chroma"


# Retain the public constant for callers that imported it before path resolution
# became configurable. New default construction calls ``default_persist_path``.
DEFAULT_PERSIST_PATH = default_persist_path()


class VectorStore:
    def __init__(self, persist_path: str | Path | None = None) -> None:
        self._path = Path(persist_path) if persist_path is not None else default_persist_path()
        self._path = self._path.expanduser()
        self._path.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(self._path))

    def collection(self, name: str):
        if name not in COLLECTION_NAMES:
            raise ValueError(f"Unknown collection name: {name}")
        return self._client.get_or_create_collection(
            name=name,
            metadata={"hnsw:space": "cosine"},
        )

    def upsert(
        self,
        collection_name: str,
        *,
        ids: list[str],
        embeddings: list[list[float]],
        metadatas: list[dict],
        documents: list[str],
    ) -> None:
        self.collection(collection_name).upsert(
            ids=ids,
            embeddings=embeddings,
            metadatas=metadatas,
            documents=documents,
        )

    def delete(self, collection_name: str, ids: list[str]) -> None:
        self.collection(collection_name).delete(ids=ids)

    def existing_ids(self, collection_name: str) -> set[str]:
        response = self.collection(collection_name).get(include=[])
        return set(response["ids"])

    def count(self, collection_name: str) -> int:
        return self.collection(collection_name).count()

    def existing_collection_counts(self) -> dict[str, int]:
        """Return counts without creating any missing application collections."""
        counts: dict[str, int] = {}
        for listed_collection in self._client.list_collections():
            name = (
                listed_collection
                if isinstance(listed_collection, str)
                else listed_collection.name
            )
            if name not in COLLECTION_NAMES:
                continue
            collection = (
                self._client.get_collection(name)
                if isinstance(listed_collection, str)
                else listed_collection
            )
            counts[name] = collection.count()
        return counts

    def query(
        self,
        collection_name: str,
        *,
        query_embedding: list[float],
        n_results: int = 10,
        where: dict | None = None,
    ) -> dict:
        return self.collection(collection_name).query(
            query_embeddings=[query_embedding],
            n_results=n_results,
            where=where,
            include=["distances", "metadatas", "documents"],
        )


_DEFAULT_STORE: VectorStore | None = None
_DEFAULT_STORE_LOCK = Lock()


def get_default_store() -> VectorStore:
    global _DEFAULT_STORE
    if _DEFAULT_STORE is None:
        with _DEFAULT_STORE_LOCK:
            if _DEFAULT_STORE is None:
                _DEFAULT_STORE = VectorStore()
    return _DEFAULT_STORE
