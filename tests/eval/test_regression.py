"""Regression suite: run the offline pipeline over the golden dataset and gate on committed floors.

Run: pytest tests/eval
Update floors after an intentional quality change: UPDATE_EVAL_BASELINE=1 pytest tests/eval
Floors apply to the test embedder (EMBEDDING_BACKEND=hash from conftest), not the production model.
"""
import json
import os
from pathlib import Path

import pytest

from app.eval.golden_builder import GOLDEN_PATH, build_golden_dataset, load_golden, write_golden
from app.eval.runner import EvalRunner, EvalStore, build_baseline_pipeline
from app.modules.catalog.metric_generator import generate_catalog

BASELINE = Path(__file__).resolve().parents[2] / "app" / "data" / "eval_baseline.json"
GATED = ["precision", "recall", "faithfulness", "relevancy"]
MARGIN = 0.05


@pytest.fixture(scope="module")
def golden():
    catalog = generate_catalog()
    if GOLDEN_PATH.exists():
        g = load_golden()
        if g["meta"]["catalog_version"] == catalog["version"]:
            return g, catalog
    g = build_golden_dataset(catalog)
    write_golden(dataset=g)
    return g, catalog


async def test_regression_suite_meets_floors(golden):
    g, catalog = golden
    retriever, generator = build_baseline_pipeline(catalog)
    store = EvalStore()  # configured store (EVAL_DATABASE_URL) so the dashboard sees this run
    run = await EvalRunner(retriever, generator, store).run(g, k=5, label="regression")

    assert store.get_run(run["run_id"]) is not None, "run must be persisted"
    assert run["metrics"]["failures"] == 0
    assert run["n_items"] == len(g["items"]) <= 500

    # drop-beyond-threshold gate versus the latest non-regressed persisted run (EVAL_MAX_DROP)
    assert not run["regressed"], f"regression vs run {run['baseline_run_id']}: {run['regressions']}"

    if os.environ.get("UPDATE_EVAL_BASELINE") == "1":
        BASELINE.write_text(json.dumps({k: round(run["metrics"][k] - MARGIN, 3) for k in GATED}, indent=2))
        pytest.skip("baseline floors rewritten")
    assert BASELINE.exists(), "missing app/data/eval_baseline.json; run with UPDATE_EVAL_BASELINE=1"
    floors = json.loads(BASELINE.read_text())
    below = {k: (round(run["metrics"][k], 3), floors[k]) for k in GATED if run["metrics"][k] < floors[k]}
    assert not below, f"metrics below committed floors (value, floor): {below}"
