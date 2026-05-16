"""Local FastEmbed embedding adapter."""

from __future__ import annotations

import importlib
import math
from collections.abc import Iterable, Sequence
from typing import Any, ClassVar

DEFAULT_FASTEMBED_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_FASTEMBED_DIMENSIONS = 384


class FastEmbedEmbeddingError(RuntimeError):
    """Raised when a local embedding run cannot produce valid vectors."""


def _build_text_embedding(model_name: str) -> Any:
    module = importlib.import_module("fastembed")
    text_embedding = module.TextEmbedding
    return text_embedding(model_name=model_name)


class FastEmbedEmbeddingClient:
    """Small local embedding client used by admin smoke tests and runtime seams."""

    _models: ClassVar[dict[str, Any]] = {}

    def __init__(
        self,
        *,
        model_name: str = DEFAULT_FASTEMBED_MODEL,
        dimensions: int = DEFAULT_FASTEMBED_DIMENSIONS,
    ) -> None:
        if not model_name.strip():
            raise FastEmbedEmbeddingError("model_name must not be blank")
        if dimensions < 1:
            raise FastEmbedEmbeddingError("dimensions must be positive")
        self._model_name = model_name
        self._dimensions = dimensions

    def prefetch(self) -> None:
        self._model()

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            raise FastEmbedEmbeddingError("at least one text is required")
        cleaned = [text.strip() for text in texts]
        if any(not text for text in cleaned):
            raise FastEmbedEmbeddingError("texts must not be blank")

        vectors = list(self._model().embed(cleaned))
        if len(vectors) != len(cleaned):
            raise FastEmbedEmbeddingError("embedding count did not match input count")
        return [self._normalise_vector(vector) for vector in vectors]

    def _model(self) -> Any:
        model = self._models.get(self._model_name)
        if model is None:
            model = _build_text_embedding(self._model_name)
            self._models[self._model_name] = model
        return model

    def _normalise_vector(self, vector: object) -> list[float]:
        if not isinstance(vector, Iterable):
            raise FastEmbedEmbeddingError("embedding vector is not iterable")
        try:
            values = [float(value) for value in vector]
        except (TypeError, ValueError) as exc:
            raise FastEmbedEmbeddingError("embedding vector is not numeric") from exc
        if len(values) != self._dimensions:
            raise FastEmbedEmbeddingError(
                "embedding dimension mismatch: "
                f"expected {self._dimensions}, got {len(values)}"
            )
        norm = math.sqrt(sum(value * value for value in values))
        if norm == 0:
            raise FastEmbedEmbeddingError("embedding vector norm is zero")
        return [value / norm for value in values]


def prefetch_default_model() -> None:
    FastEmbedEmbeddingClient().prefetch()


if __name__ == "__main__":
    prefetch_default_model()
