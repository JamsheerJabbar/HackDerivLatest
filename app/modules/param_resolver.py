"""Parameterized-first resolution: question -> catalog metric template + validated bind parameters.

Free-form SQL generation is gated separately by authorize_generation (dissatisfied AND role == 'super').
"""
import math
import re
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import settings
from app.modules.catalog import templates as tpl
from app.services.embeddings import embedding_service

_STOP = set("a an the is are was were be of in on at to for and or me my i you we it this that what which who how "
            "show tell give please pls can could would need want hey quick question last past days day weeks week "
            "months month hours hour year now there here with from by as about any all some do does did get list "
            "number count total".split())


UNAVAILABLE_MARGIN = 0.15


def _specs(metric: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Accept the catalog shape ({'parameters': [..]}) and a dict shape ({'params': {name: spec}})."""
    if isinstance(metric.get("params"), dict):
        return metric["params"]
    return {p["name"]: p for p in metric.get("parameters", [])}


def _in_range(spec: Dict[str, Any], v: float) -> bool:
    return ("min" not in spec or v >= spec["min"]) and ("max" not in spec or v <= spec["max"])


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def _days(q: str) -> Optional[int]:
    m = re.search(r"\b(?:last|past|previous)\s+(\d+)\s*(day|days|week|weeks|month|months|year|years|hour|hours)\b", q)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        if unit.startswith("hour"):
            return max(1, math.ceil(n / 24))
        return n * {"day": 1, "week": 7, "month": 30, "year": 365}[unit.rstrip("s")]
    m = re.search(r"\b(\d+)[- ]day\b", q)
    if m:
        return int(m.group(1))
    if re.search(r"\b(?:last|past|previous)\s+week\b", q):
        return 7
    if re.search(r"\b(?:last|past|previous)\s+month\b", q):
        return 30
    if re.search(r"\b(?:last|past|previous)\s+year\b", q):
        return 365
    if re.search(r"\b(today|yesterday)\b", q):
        return 1
    return None


def extract_params(question: str, metric: Dict[str, Any]) -> Dict[str, Any]:
    """Return only valid, declared parameters found in the question. Never raises; invalid values are ignored."""
    q = (question or "").lower()
    specs = _specs(metric)
    out: Dict[str, Any] = {}

    def put(name: str, value: Any) -> None:
        spec = specs.get(name)
        if spec is not None and value is not None and _in_range(spec, value):
            out[name] = int(value) if spec.get("type") == "int" else float(value) if spec.get("type") == "float" else value

    if "days" in specs:
        put("days", _days(q))
    if "limit" in specs:
        m = re.search(r"\btop\s+(\d+)\b|\b(\d+)\s+most\b", q)
        if m:
            put("limit", int(m.group(1) or m.group(2)))
    if "min_amount" in specs:
        m = re.search(r"\b(?:over|above|greater than|more than|exceeding)\s*\$?\s*(\d[\d,]*(?:\.\d+)?)\s*(k|m)?\b", q)
        if m:
            put("min_amount", _num(m.group(1)) * {"k": 1e3, "m": 1e6}.get(m.group(2) or "", 1))
    if "min_count" in specs:
        m = re.search(r"\bat least\s+(\d+)\b|\b(\d+)\s+or more\b", q)
        if m:
            put("min_count", int(m.group(1) or m.group(2)))
    if "value" in specs and specs["value"].get("choices"):
        best: Tuple[int, Any] = (10 ** 9, None)
        for c in specs["value"]["choices"]:
            m = re.search(r"\b" + re.escape(str(c).lower().replace("_", " ")).replace(r"\ ", r"[ _]") + r"\b", q)
            if m and m.start() < best[0]:
                best = (m.start(), c)
        if best[1] is not None:
            out["value"] = best[1]
    return out


def authorize_generation(role: Optional[str], dissatisfied: bool, param_attempted: bool) -> Tuple[bool, str]:
    """Free-form generation needs: user dissatisfaction, a prior parameterized attempt, and role == 'super' exactly."""
    if not dissatisfied:
        return False, "not_dissatisfied"
    if not param_attempted:
        return False, "param_not_attempted"
    if role != "super":
        return False, "generation_requires_super"
    return True, "ok"


def _content(text: str) -> str:
    words = [w for w in re.findall(r"[a-z0-9_]+", (text or "").lower()) if w not in _STOP]
    return " ".join(words)


def _cos(a: List[float], b: List[float]) -> float:
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return 0.0 if na == 0 or nb == 0 else max(0.0, min(1.0, sum(x * y for x, y in zip(a, b)) / (na * nb)))


async def semantic_confidence(question: str, metric: Dict[str, Any]) -> float:
    """Max cosine between the (stopword-stripped) question and the metric's names, synonyms and examples."""
    q = _content(question)
    if not q:
        return 0.0
    phrases = [metric["name"], metric.get("description", ""), *metric.get("synonyms", []), *metric.get("example_queries", [])]
    texts = [q] + [t for t in (_content(p) for p in phrases) if t]
    vecs = await embedding_service.embed(texts)
    return max(_cos(vecs[0], v) for v in vecs[1:]) if len(vecs) > 1 else 0.0


class ParamResolver:
    def __init__(self, service, threshold: Optional[float] = None):
        self.service = service
        self.threshold = settings.PARAM_MATCH_THRESHOLD if threshold is None else threshold

    @staticmethod
    def _miss(confidence: float = 0.0, candidates: Optional[List[Dict[str, Any]]] = None,
              unavailable: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        out = {"status": "no_match", "metric_id": None, "sql": None, "bind": {}, "params": {},
               "confidence": confidence, "candidates": candidates or []}
        if unavailable:
            out["unavailable"] = unavailable
        return out

    async def _unavailable_better_match(self, question: str, user_id: Optional[str], conf: float):
        """If the best metric for this question exists but this database lacks its data, report that
        rather than silently answering a weaker, different question."""
        top = getattr(self.service, "top_unavailable", None)
        if top is None:
            return None
        mid = await top(question, user_id)
        if mid is None:
            return None
        c2 = await semantic_confidence(question, self.service.by_id[mid])
        # a clear margin: with the real model, answerable questions never trigger this and genuinely
        # unavailable ones are 0.2-0.8 ahead (measured); close calls keep the answerable metric
        if c2 >= self.threshold and c2 > conf + UNAVAILABLE_MARGIN:
            return {"metric_id": mid, "reason": self.service.unavailable_reasons[mid], "confidence": c2}
        return None

    async def resolve(self, question: str, user_id: Optional[str] = None) -> Dict[str, Any]:
        if not (question or "").strip():
            return self._miss()
        res = await self.service.retrieve(question, user_id=user_id, k=5)
        cands = res.get("candidates", [])
        if not res.get("metric_ids"):
            return self._miss(candidates=cands, unavailable=await self._unavailable_better_match(question, user_id, 0.0))
        metric = self.service.by_id[res["metric_ids"][0]]
        conf = await semantic_confidence(question, metric)
        blocked = await self._unavailable_better_match(question, user_id, conf)
        if blocked:
            return self._miss(conf, cands, unavailable=blocked)
        if conf < self.threshold:
            return self._miss(conf, cands)
        params = extract_params(question, metric)
        try:
            sql, bind = tpl.render_template(metric, params)
        except ValueError:
            return self._miss(conf, cands)
        return {"status": "matched", "metric_id": metric["id"], "sql": sql, "bind": bind, "params": params,
                "confidence": conf, "candidates": cands}


_shared: Optional["ParamResolver"] = None


def get_shared_resolver() -> "ParamResolver":
    """Process-wide resolver over the catalog, restricted to metrics that can run on the configured database."""
    global _shared
    if _shared is None:
        from app.modules.preprocessing.service import build_advanced_service
        from app.services.database import db_service
        _shared = ParamResolver(build_advanced_service(available_fn=db_service.available_metric_ids,
                                                       reasons_fn=db_service.unavailable_metric_reasons))
    return _shared
