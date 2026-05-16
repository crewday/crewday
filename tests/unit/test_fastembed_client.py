from __future__ import annotations

import pytest

from app.adapters.llm.fastembed import (
    FastEmbedEmbeddingClient,
    FastEmbedEmbeddingError,
)


class _FakeTextEmbedding:
    def __init__(self, vectors: list[list[float]]) -> None:
        self.vectors = vectors
        self.seen_texts: list[str] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.seen_texts = texts
        return self.vectors


@pytest.fixture(autouse=True)
def _clear_model_cache() -> None:
    FastEmbedEmbeddingClient._models.clear()


def test_fastembed_client_preserves_order_and_normalizes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeTextEmbedding([[3, 4, 0], [0, 0, 2]])
    monkeypatch.setattr(
        "app.adapters.llm.fastembed._build_text_embedding",
        lambda model_name: fake,
    )

    vectors = FastEmbedEmbeddingClient(model_name="test/embed", dimensions=3).embed(
        [" first ", "second"]
    )

    assert fake.seen_texts == ["first", "second"]
    assert vectors == [[0.6, 0.8, 0.0], [0.0, 0.0, 1.0]]


def test_fastembed_client_rejects_empty_input() -> None:
    with pytest.raises(FastEmbedEmbeddingError, match="at least one text"):
        FastEmbedEmbeddingClient(model_name="test/embed", dimensions=3).embed([])


def test_fastembed_client_rejects_blank_text() -> None:
    with pytest.raises(FastEmbedEmbeddingError, match="must not be blank"):
        FastEmbedEmbeddingClient(model_name="test/embed", dimensions=3).embed([" "])


def test_fastembed_client_rejects_dimension_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeTextEmbedding([[1, 2]])
    monkeypatch.setattr(
        "app.adapters.llm.fastembed._build_text_embedding",
        lambda model_name: fake,
    )

    with pytest.raises(FastEmbedEmbeddingError, match="dimension mismatch"):
        FastEmbedEmbeddingClient(model_name="test/embed", dimensions=3).embed(["text"])
