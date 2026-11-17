"""Contextual compression of join-table schema context under a token budget."""
import math
import re
from typing import Any, Dict, List

from app.modules.retrieval.hybrid import tokenize


def _tokens(text: str) -> int:
    return math.ceil(len(text.split()) * 1.3)


def _joins(tables: List[str], profile: Dict[str, Any]) -> List[str]:
    out = []
    for t in tables:
        for fk in profile["tables"][t]["fks"]:
            if fk["ref_table"] in tables:
                out.append(f"{t}.{fk['column']} = {fk['ref_table']}.{fk['ref_column']}")
    return out


def compress_join_context(query: str, tables: List[str], profile: Dict[str, Any], token_budget: int = 300) -> Dict[str, Any]:
    tables = [t for t in dict.fromkeys(tables) if t in profile["tables"]]
    if not tables:
        return {"text": "", "tokens": 0, "tables": [], "joins": [], "dropped_columns": 0}
    q = set(tokenize(query))
    joins = _joins(tables, profile)
    join_cols = {(t, fk["column"]) for t in tables for fk in profile["tables"][t]["fks"] if fk["ref_table"] in tables}
    join_cols |= {(fk["ref_table"], fk["ref_column"]) for t in tables for fk in profile["tables"][t]["fks"] if fk["ref_table"] in tables}
    scored = []  # (score, table, column)
    total = 0
    for t in tables:
        for c, meta in profile["tables"][t]["columns"].items():
            total += 1
            words = set(tokenize(c))
            score = 3 * len(words & q) + (2 if (t, c) in join_cols else 0) + (1 if meta["pk"] else 0)
            score += 2 if any(v.lower() in q for v in meta["enum_values"]) else 0
            score += 0.5 if meta["fast_filter"] else 0
            scored.append((score, t, c))
    scored.sort(key=lambda x: (-x[0], x[1], x[2]))

    def render(keep) -> str:
        lines = []
        for t in tables:
            cols = [c for _, tt, c in keep if tt == t]
            lines.append(f"{t}({', '.join(cols)})")
        if joins:
            lines.append("JOIN " + "; ".join(joins))
        return "\n".join(lines)

    base = render([])
    keep: List = []
    if _tokens(base) <= token_budget:
        for item in scored:
            if item[0] <= 0 and keep:  # irrelevant columns are dropped once something relevant is kept
                break
            trial = render(keep + [item])
            if _tokens(trial) > token_budget:
                break
            keep.append(item)
    text = render(keep)
    return {"text": text, "tokens": _tokens(text), "tables": tables, "joins": joins,
            "dropped_columns": total - len(keep)}
