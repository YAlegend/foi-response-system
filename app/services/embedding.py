"""Pluggable text-embedding provider — local and offline by default.

Mirrors the llm/inbox seam. The default ``keyword`` retrieval needs no embedder;
when ``FOI_RETRIEVAL_PROVIDER=semantic`` the factory returns a local model.

The semantic backend uses **fastembed** (an ONNX model on CPU — no PyTorch). It
makes **no network calls at inference**: the model files are fetched once and
cached, and can be vendored for air-gapped council infrastructure. No request
text or council content ever leaves the machine.
"""
from __future__ import annotations

from functools import lru_cache

from ..config import get_settings


class Embedder:
    """Maps text to dense vectors. ``dim`` is the vector length."""
    dim: int = 0

    def embed(self, texts: list[str]) -> list[list[float]]:  # pragma: no cover - interface
        raise NotImplementedError

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]


class _FastEmbedEmbedder(Embedder):
    """Local ONNX embeddings via fastembed. The model is loaded lazily on first
    use so importing this module stays cheap and dependency-free."""

    def __init__(self, model_name: str, dim: int, cache_dir: str | None = None,
                 offline: bool = False):
        self.model_name = model_name
        self.dim = dim
        self.cache_dir = cache_dir
        self.offline = offline
        self._model = None

    def _ensure(self):
        if self._model is None:
            import os
            if self.offline:
                # Air-gap: fail fast if the model isn't already vendored locally,
                # rather than hanging on a HuggingFace download that can't happen.
                os.environ.setdefault("HF_HUB_OFFLINE", "1")
                os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
            try:
                from fastembed import TextEmbedding  # lazy import
            except ImportError as exc:  # pragma: no cover - depends on optional dep
                raise RuntimeError(
                    "Semantic retrieval needs the optional embedding deps. Install:\n"
                    "  pip install fastembed numpy\n"
                    "or set FOI_RETRIEVAL_PROVIDER=keyword to use the offline ranker."
                ) from exc
            kwargs = {"model_name": self.model_name}
            if self.cache_dir:
                os.makedirs(self.cache_dir, exist_ok=True)
                kwargs["cache_dir"] = self.cache_dir
            self._model = TextEmbedding(**kwargs)
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        model = self._ensure()
        return [[float(x) for x in vec] for vec in model.embed(list(texts))]


def to_bytes(vec) -> bytes:
    """Pack an embedding vector to float32 bytes for storage."""
    import numpy as np  # lazy
    return np.asarray(vec, dtype=np.float32).tobytes()


def from_bytes(blob: bytes):
    """Unpack stored bytes back into a float32 numpy vector."""
    import numpy as np  # lazy
    return np.frombuffer(blob, dtype=np.float32)


@lru_cache
def get_embedder() -> Embedder:
    """Return the configured embedder (cached singleton).

    Raises NotImplementedError for providers without a backend wired here — the
    clearly-marked seam for swapping in another local model later.
    """
    s = get_settings()
    provider = s.retrieval_provider.lower()
    if provider == "semantic":
        return _FastEmbedEmbedder(s.embedding_model, s.embedding_dim,
                                  cache_dir=s.embedding_cache_dir,
                                  offline=s.embedding_offline)
    raise NotImplementedError(
        f"No embedder for retrieval_provider='{s.retrieval_provider}'. "
        "Set FOI_RETRIEVAL_PROVIDER=semantic (and install fastembed) to enable "
        "embeddings, or wire another local backend in app/services/embedding.py."
    )
