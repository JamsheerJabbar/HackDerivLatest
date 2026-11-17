"""Eval dashboard API: run history, metric time series, trigger a run."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from app.eval.runner import EvalRunner, EvalStore, build_baseline_pipeline

router = APIRouter(prefix="/eval", tags=["eval"])
_store: Optional[EvalStore] = None


def get_store() -> EvalStore:
    global _store
    if _store is None:
        _store = EvalStore()
    return _store


@router.get("/runs")
def list_runs(limit: int = 50, store: EvalStore = Depends(get_store)):
    return {"runs": store.list_runs(min(max(limit, 1), 500))}


@router.get("/runs/{run_id}")
def get_run(run_id: str, store: EvalStore = Depends(get_store)):
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run


@router.get("/metrics")
def metrics(store: EvalStore = Depends(get_store)):
    return store.metric_series()


@router.post("/run")
async def trigger_run(label: Optional[str] = None, k: int = 5, store: EvalStore = Depends(get_store)):
    from app.eval.golden_builder import GOLDEN_PATH, build_golden_dataset, load_golden, write_golden
    if not GOLDEN_PATH.exists():
        write_golden(dataset=build_golden_dataset())
    retriever, generator = build_baseline_pipeline()
    return await EvalRunner(retriever, generator, store).run(load_golden(), k=min(max(k, 1), 50), label=label)
