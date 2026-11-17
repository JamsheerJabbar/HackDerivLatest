import asyncio
import re
from typing import Any, Dict, List, Optional

from app.core.config import settings
from app.core.logger import logger
from app.modules.preprocessing.components.table_retriever import TableRetriever
from app.modules.preprocessing.components.column_retriever import ColumnRetriever
from app.modules.preprocessing.components.few_shot_retriever import FewShotRetriever
from app.modules.preprocessing.components.entity_extractor import EntityExtractor

class PreprocessingService:
    """
    Orchestrates parallel execution of preprocessing sub-tasks.
    """
    def __init__(self):
        self.table_retriever = TableRetriever()
        self.column_retriever = ColumnRetriever()
        self.few_shot_retriever = FewShotRetriever()
        self.entity_extractor = EntityExtractor()

    async def process(self, query: str, domain: str = "general") -> Dict[str, Any]:
        """
        Runs all preprocessing components in parallel.
        Domain is used to filter few-shot examples.
        """
        # Create tasks for parallel execution
        t1 = self.table_retriever.retrieve(query)
        t2 = self.column_retriever.retrieve(query)
        t3 = self.few_shot_retriever.retrieve(query, domain=domain)
        t4 = self.entity_extractor.extract(query)

        # Execute all tasks concurrently
        tables, columns, few_shots, entities = await asyncio.gather(t1, t2, t3, t4)

        metric_candidates = None
        if settings.ADVANCED_RETRIEVAL_ENABLED:
            try:
                if getattr(self, "_advanced", None) is None:
                    self._advanced = build_advanced_service()
                res = await self._advanced.retrieve(query)
                metric_candidates = {"metric_ids": res["metric_ids"], "join_context": res["join_context"],
                                     "sub_queries": res["sub_queries"]}
            except Exception as e:  # never break the legacy path
                logger.warning(f"Advanced retrieval failed, continuing without it: {e}")

        return {
            "metric_candidates": metric_candidates,
            "relevant_tables": tables,
            "relevant_columns": columns,
            "few_shot_examples": few_shots,
            "entities": entities,
            "domain": domain
        }

class AdvancedRetrievalService:
    """Intent (small model) -> multi-query/hybrid search -> answer-aware + weight-boosted rerank -> join compression.

    llm=None means heuristic intent only (no LLM call); pass an LLM object to use the small-model classifier.
    """

    CANDIDATES = 20

    def __init__(self, catalog: Dict[str, Any], profile: Dict[str, Any], store, answer_bank,
                 weights: Dict[str, float], llm=None, available_ids: Optional[set] = None,
                 unavailable_reasons: Optional[Dict[str, str]] = None):
        from app.modules.retrieval.hybrid import BM25Index, HybridRetriever
        from app.modules.retrieval.intent_small import IntentClassifier
        self.catalog, self.profile, self.store = catalog, profile, store
        self.answer_bank, self.weights, self.llm = answer_bank, weights, llm
        self.available_ids = available_ids  # metrics that can run on the configured database (None = all)
        self.unavailable_reasons = unavailable_reasons or {}
        self.by_id = {m["id"]: m for m in catalog["metrics"]}
        self.classifier = IntentClassifier(llm=llm) if llm is not None else None
        self.bm25 = BM25Index({m["id"]: " ".join([m["id"], m["name"], m["description"], *m["keywords"],
                                                  *m.get("synonyms", []), *m["example_queries"], m["interpretation"]])
                               for m in catalog["metrics"] if self._is_available(m["id"])})
        self.bm25_all = BM25Index({m["id"]: " ".join([m["id"], m["name"], m["description"], *m["keywords"],
                                                      *m.get("synonyms", []), *m["example_queries"]])
                                   for m in catalog["metrics"]}) if self.unavailable_reasons else None
        self._indexed = False
        self._lock = asyncio.Lock()
        self._HybridRetriever = HybridRetriever

    async def ensure_indexed(self) -> None:
        async with self._lock:
            if not self._indexed:
                from app.modules.catalog.metric_generator import index_catalog
                await index_catalog(self.store, self.catalog, self.profile)
                self._indexed = True

    def _hybrid_for(self, user_id: Optional[str]):
        """Per-call retriever: user_id is closed over, never stored on the shared service."""
        async def vector_ids(query: str) -> List[str]:
            return await self._vector_ids(query, user_id)
        return self._HybridRetriever(vector_ids, self.bm25, top_candidates=50)

    async def top_unavailable(self, query: str, user_id: Optional[str] = None) -> Optional[str]:
        """The best-matching metric over the WHOLE catalog, if it exists but cannot run on this database."""
        if not self.unavailable_reasons or not (query or "").strip():
            return None
        await self.ensure_indexed()
        hits = await self.store.search(query, 60, user_id)
        vec = []
        for h in hits:
            mid = h["metadata"].get("metric_id")
            if h["metadata"].get("level") == "metric" and mid in self.by_id and mid not in vec:
                vec.append(mid)
        from app.modules.retrieval.hybrid import rrf_fuse
        lex = [i for i, _ in self.bm25_all.search(query, 20)]
        fused = rrf_fuse([r for r in (vec[:20], lex) if r], top=1)
        return fused[0] if fused and fused[0] in self.unavailable_reasons else None

    async def _vector_ids(self, query: str, user_id: Optional[str]) -> List[str]:
        await self.ensure_indexed()
        hits = await self.store.search(query, 60, user_id)
        ids: List[str] = []
        for h in hits:
            mid = h["metadata"].get("metric_id")
            if h["metadata"].get("level") == "metric" and mid not in ids and self._is_available(mid):
                ids.append(mid)
        return ids

    def _is_available(self, mid: Optional[str]) -> bool:
        return mid in self.by_id and (self.available_ids is None or mid in self.available_ids)

    async def _classify(self, query: str) -> Dict[str, Any]:
        if self.classifier is not None:
            return await self.classifier.classify(query)
        from app.modules.retrieval.intent_small import heuristic_complexity, heuristic_intent
        return {"intent": heuristic_intent(query), "complexity": heuristic_complexity(query),
                "confidence": 0.5, "model": None, "source": "heuristic", "latency_ms": 0.0}

    async def retrieve(self, query: str, user_id: Optional[str] = None, k: int = 5) -> Dict[str, Any]:
        from app.modules.retrieval.compression import compress_join_context
        from app.modules.retrieval.multi_query import decompose, multi_query_search
        from app.modules.retrieval.rerank import rerank
        intent = await self._classify(query)
        out: Dict[str, Any] = {"intent": intent, "sub_queries": [query], "metric_ids": [], "candidates": [],
                               "join_context": None}
        if not (query or "").strip():
            return out
        hybrid = self._hybrid_for(user_id)
        if intent["complexity"] == "complex":
            out["sub_queries"] = decompose(query)
            ids = await multi_query_search(lambda q: hybrid.search(q, self.CANDIDATES), query,
                                           out["sub_queries"], self.CANDIDATES)
        else:
            ids = await hybrid.search(query, self.CANDIDATES)
        cands = []
        for rank, mid in enumerate(ids):
            m = self.by_id[mid]
            cands.append({"id": mid, "score": 1 - rank / max(len(ids), 1), "table": m["tables"][0],
                          "fast": m["fast"], "access": m.get("access")})
        ranked = await rerank(query, cands, self.answer_bank, self.weights, k,
                              alpha=settings.RERANK_ALPHA, boost=settings.RERANK_BOOST)
        out["candidates"], out["metric_ids"] = ranked, [c["id"] for c in ranked]
        top_tables = list(dict.fromkeys(t for c in ranked[:3] for t in self.by_id[c["id"]]["tables"]))
        if len(top_tables) > 1:
            out["join_context"] = compress_join_context(query, top_tables, self.profile)["text"]
        return out


def build_advanced_service(seed: int = 0, llm=None, available_fn=None, reasons_fn=None) -> AdvancedRetrievalService:
    """Ready-to-use service over the generated catalog (in-memory vectors, seeded-sandbox answers)."""
    from app.eval.golden_builder import FIXED_NOW, seed_sandbox
    from app.modules.catalog import schema_profiler as sp
    from app.modules.catalog import templates as tpl
    from app.modules.catalog.metric_generator import generate_catalog, load_table_weights
    from app.modules.retrieval.rerank import AnswerBank
    from app.services.vector_store import InMemoryBackend, VectorStoreService
    profile = sp.profile_schema()
    catalog = generate_catalog(profile)
    conn = sp.build_sandbox()
    seed_sandbox(conn, seed)

    def answer_fn(m: Dict[str, Any]) -> str:
        sql, bind = tpl.render_template(m, {}, now=FIXED_NOW)
        cur = conn.execute(sql, bind)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchmany(3)]
        return f"{m['name']}: " + "; ".join(", ".join(f"{k}={v}" for k, v in r.items()) for r in rows)

    return AdvancedRetrievalService(catalog, profile, VectorStoreService("advanced_catalog", backend=InMemoryBackend()),
                                    AnswerBank.build(catalog, answer_fn), load_table_weights(), llm=llm,
                                    available_ids=available_fn(catalog) if available_fn else None,
                                    unavailable_reasons=reasons_fn(catalog) if reasons_fn else None)


# Singleton instance
preprocessing_service = PreprocessingService()
