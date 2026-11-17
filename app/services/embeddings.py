"""Embedding service (sentence-transformers, 384 dims) with an in-process cache.

If the model cannot be loaded (offline, missing weights) or EMBEDDING_BACKEND=hash,
a deterministic hashing embedder is used so the pipeline and tests keep working.
The hash embedder is lexical only; it is a degraded mode, logged as a warning.
"""
import asyncio
import hashlib
import math
import re
from collections import OrderedDict
from typing import List, Optional

from app.core.config import settings
from app.core.logger import logger

_TOKEN = re.compile(r"[a-z0-9_]+")


def _hash_embed(text: str, dim: int) -> List[float]:
    vec = [0.0] * dim
    for tok in _TOKEN.findall(text.lower()):
        h = int.from_bytes(hashlib.sha256(tok.encode()).digest()[:8], "big")
        vec[h % dim] += 1.0 if (h >> 63) & 1 else -1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


class EmbeddingService:
    def __init__(self, model_name: Optional[str] = None, dim: Optional[int] = None,
                 backend: Optional[str] = None, cache_size: int = 4096):
        self.model_name = model_name or settings.EMBEDDING_MODEL
        self.dim = dim or settings.EMBEDDING_DIM
        self.backend = backend or settings.EMBEDDING_BACKEND
        self._model = None
        self._cache: "OrderedDict[str, List[float]]" = OrderedDict()
        self._cache_size = cache_size

    def _load(self):
        if self._model is not None or self.backend == "hash":
            return
        try:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(self.model_name)
        except Exception as e:  # offline / missing weights
            logger.warning(f"Embedding model load failed ({e}); using hash embedder")
            self.backend = "hash"

    def _embed_sync(self, texts: List[str]) -> List[List[float]]:
        self._load()
        if self.backend == "hash":
            return [_hash_embed(t, self.dim) for t in texts]
        arr = self._model.encode(texts, normalize_embeddings=True)
        return [list(map(float, row)) for row in arr]

    async def embed(self, texts: List[str]) -> List[List[float]]:
        out: List[Optional[List[float]]] = [None] * len(texts)
        missing = []
        for i, t in enumerate(texts):
            hit = self._cache.get(t)
            if hit is not None:
                self._cache.move_to_end(t)
                out[i] = hit
            else:
                missing.append(i)
        if missing:
            vecs = await asyncio.to_thread(self._embed_sync, [texts[i] for i in missing])
            for i, v in zip(missing, vecs):
                out[i] = v
                self._cache[texts[i]] = v
                if len(self._cache) > self._cache_size:
                    self._cache.popitem(last=False)
        return out  # type: ignore[return-value]


embedding_service = EmbeddingService()
