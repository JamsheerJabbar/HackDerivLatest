"""Vector store with user-scoped retrieval.

Backends: PgVectorBackend (PostgreSQL + pgvector, HNSW cosine) and InMemoryBackend (tests/dev).
Isolation rule (enforced in every backend): a search returns documents whose
metadata user_id equals the caller's user_id, plus global catalog documents (user_id is None).
A search without a user_id sees global documents only; there is no unfiltered path.
"""
import asyncio
import hashlib
import threading
import json
import math
import re
from typing import Any, Dict, List, Optional, Protocol

from app.core.config import settings
from app.core.logger import logger
from app.services.embeddings import EmbeddingService, embedding_service


class VectorStoreUnavailable(Exception):
    """Raised when the vector backend cannot serve a request."""


def _doc_id(collection: str, text: str, metadata: Dict[str, Any]) -> str:
    key = "\x1f".join([collection, text, str(metadata.get("user_id")),
                       str(metadata.get("level")), str(metadata.get("table")),
                       str(metadata.get("column"))])
    return hashlib.sha256(key.encode()).hexdigest()


class VectorBackend(Protocol):
    async def upsert(self, collection: str, rows: List[Dict[str, Any]]) -> None: ...
    async def search(self, collection: str, embedding: List[float], top_k: int,
                     user_id: Optional[str]) -> List[Dict[str, Any]]: ...


class InMemoryBackend:
    def __init__(self):
        self._rows: Dict[str, Dict[str, Any]] = {}

    async def upsert(self, collection, rows):
        for r in rows:
            self._rows[r["id"]] = {**r, "collection": collection}

    async def search(self, collection, embedding, top_k, user_id):
        def visible(r):
            uid = r["metadata"].get("user_id")
            return r["collection"] == collection and (uid is None or (user_id is not None and uid == user_id))

        def cos(a, b):
            na = math.sqrt(sum(x * x for x in a)) or 1.0
            nb = math.sqrt(sum(x * x for x in b)) or 1.0
            return sum(x * y for x, y in zip(a, b)) / (na * nb)

        scored = [(cos(embedding, r["embedding"]), r) for r in self._rows.values() if visible(r)]
        scored.sort(key=lambda s: s[0], reverse=True)
        return [{"text": r["text"], "metadata": r["metadata"], "score": s} for s, r in scored[:top_k]]


class PgVectorBackend:
    """PostgreSQL + pgvector. Table lives in the nl2sql_meta database (not the business DB)."""

    def __init__(self, url: Optional[str] = None, dim: Optional[int] = None):
        from sqlalchemy import create_engine
        self.url = url or settings.META_DATABASE_URL
        self.dim = dim or settings.EMBEDDING_DIM
        self.engine = create_engine(self.url, pool_pre_ping=True)
        self._ready = False
        self._ddl_lock = threading.Lock()  # DDL runs once even under concurrent first calls
        self._iterative = False  # hnsw.iterative_scan available (pgvector >= 0.8)

    def _ensure(self):
        if self._ready:
            return
        with self._ddl_lock:
            if not self._ready:
                self._create_schema()

    def _create_schema(self):
        from sqlalchemy import text
        with self.engine.begin() as c:
            c.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            c.execute(text(f"""
                CREATE TABLE IF NOT EXISTS vector_documents (
                    id TEXT PRIMARY KEY,
                    collection TEXT NOT NULL,
                    user_id TEXT,
                    text TEXT NOT NULL,
                    metadata JSONB NOT NULL,
                    embedding vector({self.dim}) NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )"""))
            c.execute(text("CREATE INDEX IF NOT EXISTS idx_vecdoc_coll_user ON vector_documents(collection, user_id)"))
            c.execute(text("CREATE INDEX IF NOT EXISTS idx_vecdoc_embedding ON vector_documents "
                           "USING hnsw (embedding vector_cosine_ops)"))
            ver = c.execute(text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")).scalar() or "0"
        parts = [int(x) for x in re.findall(r"\d+", ver)[:2]] + [0, 0]
        self._iterative = (parts[0], parts[1]) >= (0, 8)
        self._ready = True

    @staticmethod
    def _lit(v: List[float]) -> str:
        return "[" + ",".join(f"{x:.7f}" for x in v) + "]"

    def _upsert_sync(self, collection, rows):
        from sqlalchemy import text
        self._ensure()
        sql = text("""
            INSERT INTO vector_documents (id, collection, user_id, text, metadata, embedding)
            VALUES (:id, :collection, :user_id, :text, CAST(:metadata AS jsonb), CAST(:embedding AS vector))
            ON CONFLICT (id) DO UPDATE SET text = EXCLUDED.text, metadata = EXCLUDED.metadata,
                                           embedding = EXCLUDED.embedding""")
        params = [{"id": r["id"], "collection": collection, "user_id": r["metadata"].get("user_id"),
                   "text": r["text"], "metadata": json.dumps(r["metadata"], default=str),
                   "embedding": self._lit(r["embedding"])} for r in rows]
        with self.engine.begin() as c:
            for i in range(0, len(params), 500):  # batched executemany
                c.execute(sql, params[i:i + 500])

    def _search_sync(self, collection, embedding, top_k, user_id):
        from sqlalchemy import text
        self._ensure()
        sql = text("""
            SELECT text, metadata, 1 - (embedding <=> CAST(:q AS vector)) AS score
            FROM vector_documents
            WHERE collection = :collection AND (user_id IS NULL OR user_id = :user_id)
            ORDER BY embedding <=> CAST(:q AS vector)
            LIMIT :k""")
        with self.engine.begin() as c:  # transaction so SET LOCAL applies to this query only
            c.execute(text(f"SET LOCAL hnsw.ef_search = {int(max(top_k, settings.HNSW_EF_SEARCH))}"))
            if self._iterative:
                # the user/collection filter is applied after the HNSW scan; keep scanning until top_k rows pass it
                c.execute(text("SET LOCAL hnsw.iterative_scan = 'relaxed_order'"))
            res = c.execute(sql, {"q": self._lit(embedding), "collection": collection,
                                  "user_id": user_id, "k": top_k})
            return [{"text": r.text, "metadata": r.metadata, "score": float(r.score)} for r in res]

    async def upsert(self, collection, rows):
        await asyncio.to_thread(self._upsert_sync, collection, rows)

    async def search(self, collection, embedding, top_k, user_id):
        return await asyncio.to_thread(self._search_sync, collection, embedding, top_k, user_id)


class VectorStoreService:
    """Embeds text and delegates to a backend; all failures surface as VectorStoreUnavailable."""

    def __init__(self, collection_name: str, backend: Optional[VectorBackend] = None,
                 embedder: Optional[EmbeddingService] = None):
        self.collection_name = collection_name
        self._backend = backend
        self._embedder = embedder or embedding_service

    @property
    def backend(self) -> VectorBackend:
        if self._backend is None:
            try:
                self._backend = PgVectorBackend() if settings.VECTOR_BACKEND == "pgvector" else InMemoryBackend()
            except Exception as e:
                raise VectorStoreUnavailable(str(e)) from e
        return self._backend

    async def search(self, query: str, top_k: int = 5, user_id: Optional[str] = None) -> List[Dict[str, Any]]:
        if top_k <= 0:
            return []
        try:
            (emb,) = await self._embedder.embed([query])
            return await self.backend.search(self.collection_name, emb, top_k, user_id)
        except VectorStoreUnavailable:
            raise
        except Exception as e:
            logger.error(f"Vector search failed: {e}")
            raise VectorStoreUnavailable(str(e)) from e

    async def add_documents(self, documents: List[str], metadata: List[Dict[str, Any]]):
        if len(documents) != len(metadata):
            raise ValueError("documents and metadata must have the same length")
        for m in metadata:
            if "user_id" not in m:  # None marks a global catalog doc; a missing key must never become global silently
                raise ValueError("metadata must include 'user_id' (None for global catalog documents)")
        try:
            embs = await self._embedder.embed(documents)
            rows = [{"id": _doc_id(self.collection_name, d, m), "text": d, "metadata": m, "embedding": e}
                    for d, m, e in zip(documents, metadata, embs)]
            await self.backend.upsert(self.collection_name, rows)
        except VectorStoreUnavailable:
            raise
        except Exception as e:
            logger.error(f"Vector upsert failed: {e}")
            raise VectorStoreUnavailable(str(e)) from e


class VectorStoreFactory:
    @staticmethod
    def get_store(collection_name: str) -> VectorStoreService:
        return VectorStoreService(collection_name)
