"""Hybrid semantic + lexical retrieval with Reciprocal Rank Fusion across a tiered effort dial."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .embedder import Embedder, get_default_embedder
from .filters import RetrievalFilters
from .vector_store import VectorStore, get_default_store


EFFORT_LEVELS: tuple[str, ...] = ("light", "medium", "heavy")

EFFORT_COLLECTIONS: dict[str, tuple[str, ...]] = {
    "light": ("holographic_facts",),
    "medium": (
        "holographic_facts",
        "action_items",
        "deadlines",
        "calendar_events",
        "decisions",
        "thread_summaries",
    ),
    "heavy": (
        "holographic_facts",
        "action_items",
        "deadlines",
        "calendar_events",
        "decisions",
        "thread_summaries",
        "message_chunks",
    ),
}


@dataclass(frozen=True)
class RetrievalResult:
    collection: str
    id: str
    score: float
    document: str
    metadata: dict[str, Any]
    semantic_rank: int | None
    lexical_rank: int | None
    semantic_distance: float | None


class RetrievalEngine:
    def __init__(
        self,
        *,
        vector_store: VectorStore | None = None,
        embedder: Embedder | None = None,
    ) -> None:
        self._vector_store = vector_store if vector_store is not None else get_default_store()
        self._embedder = embedder if embedder is not None else get_default_embedder()

    def search(
        self,
        query: str,
        *,
        effort: str = "medium",
        limit: int = 10,
        filters: RetrievalFilters | None = None,
        k_per_collection: int = 20,
        rrf_k: int = 60,
    ) -> list[RetrievalResult]:
        if effort not in EFFORT_COLLECTIONS:
            raise ValueError(f"unknown effort: {effort!r}; must be one of {EFFORT_LEVELS}")
        if not query or not query.strip():
            return []
        query_embedding = self._embedder.embed_query(query)
        merged: list[RetrievalResult] = []
        for collection in EFFORT_COLLECTIONS[effort]:
            merged.extend(
                self._search_collection(
                    collection=collection,
                    query=query,
                    query_embedding=query_embedding,
                    filters=filters,
                    k=k_per_collection,
                    rrf_k=rrf_k,
                )
            )
        merged.sort(key=lambda r: r.score, reverse=True)
        return merged[:limit]

    def _search_collection(
        self,
        *,
        collection: str,
        query: str,
        query_embedding: list[float],
        filters: RetrievalFilters | None,
        k: int,
        rrf_k: int,
    ) -> list[RetrievalResult]:
        where = filters.chroma_where(collection) if filters is not None else None
        has_date = filters is not None and filters.has_date_filter(collection)
        fetch_k = k * 4 if has_date else k
        chroma_collection = self._vector_store.collection(collection)

        sem_response = chroma_collection.query(
            query_embeddings=[query_embedding],
            n_results=fetch_k,
            where=where,
            include=["distances", "metadatas", "documents"],
        )
        sem_ids = sem_response["ids"][0] if sem_response.get("ids") else []
        sem_docs = sem_response["documents"][0] if sem_response.get("documents") else []
        sem_metas = sem_response["metadatas"][0] if sem_response.get("metadatas") else []
        sem_dists = sem_response["distances"][0] if sem_response.get("distances") else []

        try:
            lex_response = chroma_collection.query(
                query_embeddings=[query_embedding],
                n_results=fetch_k,
                where=where,
                where_document={"$contains": query},
                include=["distances", "metadatas", "documents"],
            )
            lex_ids = lex_response["ids"][0] if lex_response.get("ids") else []
            lex_docs = lex_response["documents"][0] if lex_response.get("documents") else []
            lex_metas = lex_response["metadatas"][0] if lex_response.get("metadatas") else []
        except Exception:
            lex_ids, lex_docs, lex_metas = [], [], []

        def passes(meta: dict[str, Any] | None) -> bool:
            if filters is None:
                return True
            return filters.post_filter(collection, meta or {})

        per_id: dict[str, dict[str, Any]] = {}
        sem_kept = 0
        for id_, doc, meta, dist in zip(sem_ids, sem_docs, sem_metas, sem_dists):
            if not passes(meta):
                continue
            per_id[id_] = {
                "document": doc or "",
                "metadata": meta or {},
                "sem_rank": sem_kept,
                "lex_rank": None,
                "sem_dist": dist,
            }
            sem_kept += 1
            if sem_kept >= k:
                break
        lex_kept = 0
        for id_, doc, meta in zip(lex_ids, lex_docs, lex_metas):
            if not passes(meta):
                continue
            if id_ in per_id:
                per_id[id_]["lex_rank"] = lex_kept
            else:
                per_id[id_] = {
                    "document": doc or "",
                    "metadata": meta or {},
                    "sem_rank": None,
                    "lex_rank": lex_kept,
                    "sem_dist": None,
                }
            lex_kept += 1
            if lex_kept >= k:
                break

        out: list[RetrievalResult] = []
        for id_, info in per_id.items():
            score = 0.0
            if info["sem_rank"] is not None:
                score += 1.0 / (rrf_k + info["sem_rank"] + 1)
            if info["lex_rank"] is not None:
                score += 1.0 / (rrf_k + info["lex_rank"] + 1)
            out.append(
                RetrievalResult(
                    collection=collection,
                    id=str(id_),
                    score=score,
                    document=info["document"],
                    metadata=info["metadata"],
                    semantic_rank=info["sem_rank"],
                    lexical_rank=info["lex_rank"],
                    semantic_distance=info["sem_dist"],
                )
            )
        return out
