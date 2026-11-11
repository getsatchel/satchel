"""Embedding providers (design §5.2).

``EmbeddingProvider`` is the only thing the rest of satchel sees. Chunks store the provider's
``model_id`` so that search only compares vectors from the same model.
"""

from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import httpx

from satchel.settings import Settings, get_settings

_WORD = re.compile(r"[a-z0-9]+")


@runtime_checkable
class EmbeddingProvider(Protocol):
    model_id: str
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed documents/passages."""
        ...

    def embed_query(self, text: str) -> list[float]:
        """Embed a search query (some models use an asymmetric prefix)."""
        ...


class HashEmbedder:
    """Deterministic feature-hashing embedder for tests and offline use.

    Hashes word unigrams and bigrams into ``dim`` signed buckets and L2-normalises. It is
    lexical, not semantic.
    """

    def __init__(self, dim: int = 1024) -> None:
        self.dim = dim
        self.model_id = f"hash-{dim}"

    def _one(self, text: str) -> list[float]:
        words = _WORD.findall(text.lower())
        feats = words + [f"{a}_{b}" for a, b in zip(words, words[1:], strict=False)]
        vec = [0.0] * self.dim
        for f in feats:
            h = int.from_bytes(hashlib.blake2b(f.encode(), digest_size=8).digest(), "big")
            vec[h % self.dim] += 1.0 if (h >> 63) & 1 else -1.0
        norm = math.sqrt(sum(x * x for x in vec))
        if norm == 0:
            vec[0] = 1.0
            return vec
        return [x / norm for x in vec]

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._one(text)


_QUERY_PREFIX = {
    "mixedbread-ai/mxbai-embed-large-v1": "Represent this sentence for searching relevant "
    "passages: ",
    "BAAI/bge-large-en-v1.5": "Represent this sentence for searching relevant passages: ",
}


class FastEmbedProvider:
    """Local ONNX embeddings via the ``fastembed`` package (no network after first download)."""

    def __init__(
        self,
        model_name: str = "mixedbread-ai/mxbai-embed-large-v1",
        cache_dir: str | None = None,
        expected_dim: int = 1024,
    ) -> None:
        self.model_name = model_name
        self.model_id = f"fastembed:{model_name}"
        self.dim = expected_dim
        self._cache_dir = str(Path(cache_dir).expanduser()) if cache_dir else None
        self._model: Any = None

    def _load(self) -> Any:
        if self._model is None:
            from fastembed import TextEmbedding

            self._model = TextEmbedding(model_name=self.model_name, cache_dir=self._cache_dir)
            probe = next(iter(self._model.embed(["dimension probe"])))
            if len(probe) != self.dim:
                raise ValueError(
                    f"{self.model_name} produces {len(probe)}-d vectors, expected {self.dim}"
                )
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        model = self._load()
        return [[float(x) for x in v] for v in model.embed(texts, batch_size=16)]

    def embed_query(self, text: str) -> list[float]:
        prefix = _QUERY_PREFIX.get(self.model_name, "")
        return self.embed([prefix + text])[0]


class OpenAICompatibleProvider:
    """Any ``POST {base_url}/embeddings`` endpoint (OpenAI, Voyage-compatible, TEI, vLLM...)."""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str | None = None,
        dim: int = 1024,
        batch_size: int = 64,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.model_id = f"openai_compat:{model}"
        self.dim = dim
        self._batch = batch_size
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = client or httpx.Client(timeout=60.0)

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self._batch):
            batch = texts[i : i + self._batch]
            resp = self._client.post(
                f"{self.base_url}/embeddings",
                headers=self._headers,
                json={"model": self.model, "input": batch, "dimensions": self.dim},
            )
            resp.raise_for_status()
            data = sorted(resp.json()["data"], key=lambda d: d["index"])
            vecs = [[float(x) for x in d["embedding"]] for d in data]
            if len(vecs) != len(batch) or any(len(v) != self.dim for v in vecs):
                raise ValueError(f"embedding endpoint returned wrong shape (want {self.dim}-d)")
            out.extend(vecs)
        return out

    def embed_query(self, text: str) -> list[float]:
        return self.embed([text])[0]


def make_embedder(settings: Settings | None = None) -> EmbeddingProvider:
    s = settings or get_settings()
    if s.embedding_provider == "hash":
        return HashEmbedder(s.embedding_dim)
    if s.embedding_provider == "fastembed":
        return FastEmbedProvider(s.fastembed_model, s.fastembed_cache_dir, s.embedding_dim)
    return OpenAICompatibleProvider(
        s.embedding_base_url, s.embedding_model, s.embedding_api_key, s.embedding_dim
    )
