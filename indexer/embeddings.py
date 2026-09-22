"""Konfigurierbarer Embedding-Wrapper.

Unterstützt Provider 'local' (sentence-transformers). Weitere Provider
(openai, aicore) können später ergänzt werden, ohne die Aufrufer zu ändern.
"""

from __future__ import annotations

from functools import lru_cache


class Embedder:
    """Erzeugt Embeddings über einen konfigurierbaren Provider."""

    def __init__(self, provider: str = "local", model: str = "sentence-transformers/all-MiniLM-L6-v2"):
        self.provider = provider
        self.model = model
        if provider == "local":
            self._st_model = _load_local_model(model)
        else:
            raise ValueError(f"Unbekannter Embedding-Provider: {provider}")

    def embed(self, text: str) -> list[float]:
        """Embeddet einen einzelnen Text."""
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embeddet mehrere Texte auf einmal (effizienter)."""
        if self.provider == "local":
            vectors = self._st_model.encode(texts, normalize_embeddings=False)
            return [v.tolist() for v in vectors]
        raise ValueError(f"Unbekannter Embedding-Provider: {self.provider}")


@lru_cache(maxsize=2)
def _load_local_model(model_name: str):
    """Lädt und cached ein sentence-transformers Modell (Laden ist teuer)."""
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(model_name)
