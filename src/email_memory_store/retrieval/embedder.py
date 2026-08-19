"""Sentence-transformers BGE embedder for retrieval indexing and queries."""

from __future__ import annotations

import threading
from typing import cast

from ..accelerator import resolve_public_runtime_device
from sentence_transformers import SentenceTransformer

DEFAULT_MODEL = "BAAI/bge-base-en-v1.5"
BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


class Embedder:
    def __init__(self, model_name: str = DEFAULT_MODEL, device: str | None = None) -> None:
        self._model = SentenceTransformer(
            model_name,
            device=resolve_public_runtime_device(device),
        )

    def embed_documents(self, texts: list[str], batch_size: int = 64) -> list[list[float]]:
        embeddings = self._model.encode(
            texts,
            batch_size=batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return embeddings.tolist()

    def embed_query(self, text: str) -> list[float]:
        embedding = self._model.encode(
            BGE_QUERY_PREFIX + text,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return embedding.tolist()

    @property
    def dim(self) -> int:
        getter = getattr(self._model, "get_embedding_dimension", None) or self._model.get_sentence_embedding_dimension
        return cast(int, getter())


_DEFAULT_EMBEDDER: Embedder | None = None
_DEFAULT_EMBEDDER_LOCK = threading.Lock()


def get_default_embedder() -> Embedder:
    global _DEFAULT_EMBEDDER
    if _DEFAULT_EMBEDDER is None:
        with _DEFAULT_EMBEDDER_LOCK:
            if _DEFAULT_EMBEDDER is None:
                _DEFAULT_EMBEDDER = Embedder()
    return _DEFAULT_EMBEDDER


def reset_default_embedder() -> None:
    global _DEFAULT_EMBEDDER
    with _DEFAULT_EMBEDDER_LOCK:
        _DEFAULT_EMBEDDER = None
