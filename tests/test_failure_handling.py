"""T7 failure-handling tests, driven by tests/fixtures/failure-handling.json.

All offline: fake LLMs (`async generate_response(prompt, model_name=None) -> str`), no sleeps
(base_delay=0). ASSUMPTIONS (planned interfaces, SPECS.md T7):
- settings SQL_MODEL / RETRY_MODEL / FINAL_RETRY_MODEL are monkeypatched to m-sql / m-retry / m-final.
- generate_sql_node is async and takes a state dict with "user_question"; it may await or not
  await the fake validation_module.validate / db_service.execute_params, so the fakes return
  awaitable results. validate's fake returns (True, None).
- Slack post is replaced by a counting fake for app.services.backend_alerts._post_alert_to_slack.
"""
import asyncio
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from app.core.config import settings

FIXTURE = Path(__file__).parent / "fixtures" / "failure-handling.json"
CASES = json.loads(FIXTURE.read_text())


class Out(BaseModel):
    intent: str
    confidence: float = 0.9


SCHEMAS = {"Out": Out, None: None}


def _run(coro):
    return asyncio.run(coro)


class AwaitableList(list):
    def __await__(self):
        async def _c():
            return self
        return _c().__await__()


class AwaitableTuple(tuple):
    def __await__(self):
        async def _c():
            return self
        return _c().__await__()


async def _maybe_await(x):
    return await x if inspect.isawaitable(x) else x


class FakeLLM:
    def __init__(self, reply="", raises=False):
        self.reply = reply
        self.raises = raises
        self.calls = []

    async def generate_response(self, prompt, model_name=None):
        self.calls.append(model_name)
        if self.raises:
            raise RuntimeError("llm down")
        return self.reply


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    from app.services import backend_alerts as ba
    monkeypatch.setattr(settings, "SQL_MODEL", "m-sql", raising=False)
    monkeypatch.setattr(settings, "RETRY_MODEL", "m-retry", raising=False)
    monkeypatch.setattr(settings, "FINAL_RETRY_MODEL", "m-final", raising=False)
    monkeypatch.setattr(settings, "SLACK_BOT_TOKEN", "", raising=False)
    ba.reset_alerts()
    yield
    ba.reset_alerts()


def _exc(name):
    from app.services import llm, retry_policy
    return {"UnparsableLLMResponse": llm.UnparsableLLMResponse,
            "LLMCallError": llm.LLMCallError,
            "RetriesExhausted": retry_policy.RetriesExhausted,
            "NonRetryable": retry_policy.NonRetryable}[name]


# ---------------------------------------------------------------- scenarios
def _retry(inp, exp):
    from app.services import backend_alerts as ba
    from app.services import llm
    from app.services.retry_policy import NonRetryable, RetryPolicy

    calls = []

    async def operation(model, attempt):
        calls.append((model, attempt))
        if attempt in inp["fail_attempts"]:
            kind = inp["error_kind"]
            if kind == "nonretryable":
                raise NonRetryable("stop")
            if kind == "unparsable":
                llm.parse_llm_json("just prose, no json", None, stage="t")
            raise RuntimeError(f"boom {attempt}")
        return "ok"

    policy = RetryPolicy(base_model=inp["base_model"], max_attempts=inp["max_attempts"], base_delay=0.0)

    async def go():
        return await policy.run(operation, stage="test")

    raises = exp.get("raises")
    if raises:
        with pytest.raises(_exc(raises)) as ei:
            _run(go())
        if raises == "RetriesExhausted":
            e = ei.value
            assert e.attempts == exp["attempts"]
            assert e.models_used == exp["models_used"]
            assert len(e.errors) == exp["errors_len"]
    else:
        res = _run(go())
        assert res.value == "ok"
        assert res.attempts == exp["attempts"]
        assert res.models_used == exp["models_used"]
    assert len(calls) == exp["calls"]
    assert [a for _, a in calls] == list(range(1, exp["calls"] + 1))
    if "models_used" in exp:
        assert [m for m, _ in calls] == exp["models_used"]
    counts = ba.alert_counts()
    assert counts.get("llm_retries_exhausted", 0) == exp["retry_alerts"]
    assert counts.get("unparsable_llm_response", 0) == exp.get("unparsable_alerts", 0)


def _fallback(inp, exp):
    from app.services.deterministic_fallback import DeterministicFallback

    executed = []
    rows = [{"n": 1}]
    r = inp["resolver"]

    class Resolver:
        async def resolve(self, question, user_id):
            return {"status": r["status"], "metric_id": "m1", "sql": "SELECT n FROM t",
                    "bind": {}, "params": {}, "confidence": r["confidence"]}

    mode = inp["execute"]

    def sync_exec(sql, bind):
        executed.append(sql)
        if mode == "raise":
            raise RuntimeError("db down")
        return rows

    async def async_exec(sql, bind):
        return sync_exec(sql, bind)

    fb = DeterministicFallback(Resolver(), async_exec if mode == "async" else sync_exec,
                               threshold=inp["threshold"])
    out = _run(fb.handle(inp["question"], user_id="u1"))
    assert out["source"] == "deterministic_fallback"
    assert out["status"] == exp["status"]
    assert bool(executed) == exp["executed"]
    if exp["status"] == "success":
        assert out["results"] == rows
        assert out["metric_id"] == "m1"
        assert out["sql"] == "SELECT n FROM t"
        assert out["similarity"] == pytest.approx(exp["similarity"])
    else:
        assert isinstance(out["error"], str) and out["error"].strip()
        assert isinstance(out["similarity"], float)
        assert "results" not in out or not out["results"]
        if "similarity" in exp:
            assert out["similarity"] == pytest.approx(exp["similarity"])


def _sql_node(inp, exp, monkeypatch):
    import app.orchestration.workflow as wf
    from app.services.deterministic_fallback import DeterministicFallback

    script = inp["gen_script"]
    models = []
    gen_calls = []

    class SqlGen:
        async def generate(self, query, ctx=None, *a, **k):
            idx = len(gen_calls)
            gen_calls.append(query)
            models.append((ctx or {}).get("model_name"))
            step = script[min(idx, len(script) - 1)]
            if step == "raise":
                raise RuntimeError("llm exploded")
            if step == "llm_error":
                return "Error generating response: boom"
            if step == "guidance":
                return "Error: please specify a time range for your question."
            if step == "exec_fail":
                return "SELECT bad"
            return "SELECT 1"

    class Validation:
        def validate(self, *a, **k):
            return AwaitableTuple((True, None))

    class Db:
        def execute_params(self, sql, params=None, *a, **k):
            if sql == "SELECT bad":
                raise RuntimeError("no such column")
            return AwaitableList([{"x": 1}])

    class Resolver:
        async def resolve(self, question, user_id):
            return {"status": "matched", "metric_id": "m1", "sql": "SELECT metric FROM t",
                    "bind": {}, "params": {}, "confidence": inp["fallback_similarity"]}

    fb_calls = []
    fb = DeterministicFallback(Resolver(), lambda sql, bind: [{"metric": 1}])
    orig_handle = fb.handle

    async def spy(*a, **k):
        fb_calls.append(1)
        return await orig_handle(*a, **k)

    fb.handle = spy

    monkeypatch.setattr(wf, "sql_generation_module", SqlGen())
    monkeypatch.setattr(wf, "validation_module", Validation())
    monkeypatch.setattr(wf, "db_service", Db())
    monkeypatch.setattr(wf, "get_fallback", lambda: fb, raising=False)

    state = {"user_question": "show flagged transactions", "domain": "general", "user_id": "u1",
             "retry_count": 0, "entities": {"resolved_entities": {}}}
    out = _run(wf.generate_sql_node(state))

    assert out["status"] == exp["status"]
    assert len(gen_calls) == exp["gen_calls"]
    if "models" in exp:
        assert models == exp["models"]
    assert bool(fb_calls) == exp.get("fallback_called", False)
    if exp["status"] == "needs_clarification":
        return
    assert out["generated_sql"] == exp["generated_sql"]
    assert out["query_result"] == exp["query_result"]
    assert out.get("fallback") == exp["fallback"]
    if "has_validation_error" in exp:
        assert bool(out.get("validation_error")) == exp["has_validation_error"]
    if exp["status"] == "failed":
        assert isinstance(out["validation_error"], str) and out["validation_error"].strip()


def _parse(inp, exp):
    from app.services import backend_alerts as ba
    from app.services.llm import parse_llm_json

    text = "x" * 2000 if inp["text"] == "$LONG" else inp["text"]
    schema = SCHEMAS[inp["schema"]]
    kwargs = {"stage": inp["stage"]} if "stage" in inp else {}
    if exp["raises"]:
        with pytest.raises(_exc(exp["raises"])) as ei:
            parse_llm_json(text, schema, **kwargs)
        if "stage" in exp:
            assert ei.value.stage == exp["stage"]
        if "raw_max_len" in exp:
            assert len(ei.value.raw) <= exp["raw_max_len"]
            alert = ba.recent_alerts("unparsable_llm_response")[0]
            assert alert["context"].get("stage") == exp["stage"]
            for v in alert["context"].values():
                if isinstance(v, str):
                    assert len(v) <= exp["alert_raw_max_len"]
    else:
        val = parse_llm_json(text, schema, **kwargs)
        if exp["is_model"]:
            assert isinstance(val, Out)
            assert val.model_dump() == exp["value"]
        else:
            assert isinstance(val, dict) and val == exp["value"]
    assert ba.alert_counts() == exp["alerts"]


def _structured(inp, exp):
    from app.services import backend_alerts as ba
    from app.services.llm import generate_structured

    fake = FakeLLM(inp["reply"])
    if exp["raises"]:
        with pytest.raises(_exc(exp["raises"])) as ei:
            _run(generate_structured("p", SCHEMAS[inp["schema"]], stage="intent", llm=fake))
        if "stage" in exp:
            assert ei.value.stage == exp["stage"]
    else:
        val = _run(generate_structured("p", SCHEMAS[inp["schema"]], stage="intent", llm=fake))
        assert isinstance(val, Out) and val.model_dump() == exp["value"]
    assert len(fake.calls) == exp["llm_calls"]
    assert ba.alert_counts() == exp["alerts"]


def _generate_text(inp, exp):
    from app.services.llm import generate_text

    fake = FakeLLM(inp["reply"], raises=inp["llm_raises"])
    if exp["raises"]:
        with pytest.raises(_exc(exp["raises"])):
            _run(generate_text("p", llm=fake))
    else:
        assert _run(generate_text("p", llm=fake)) == exp["value"]


def _alerts(inp, exp, monkeypatch):
    from app.services import backend_alerts as ba

    op = inp["op"]
    if op == "shape":
        a = ba.emit_backend_alert("k1", "m", a=1)
        assert sorted(a.keys()) == exp["keys"]
        assert a["kind"] == "k1" and a["message"] == "m"
        assert a["context"] == exp["context"]
        assert ba.alert_counts() == exp["counts"]
        assert ba.recent_alerts()[0]["id"] == a["id"]
    elif op == "redact":
        a = ba.emit_backend_alert("k1", "m", authorization="Bearer x", token="t", password="p",
                                  secret="s", jwt="j", api_key="k", keep="v")
        assert a["context"] == exp["context"]
        assert ba.recent_alerts()[0]["context"] == exp["context"]
    elif op == "truncate":
        a = ba.emit_backend_alert("k1", "m", big="x" * 5000)
        assert len(a["context"]["big"]) <= exp["max_len"]
    elif op == "unserializable":
        cyc = {}
        cyc["self"] = cyc
        a = ba.emit_backend_alert("k1", "m", obj=object(), s={1, 2}, cyc=cyc, f=lambda: 1)
        assert isinstance(a, dict)
    elif op == "no_loop":
        with pytest.raises(RuntimeError):
            asyncio.get_running_loop()
        a = ba.emit_backend_alert("k1", "m")
        assert a["kind"] == exp["kind"]
    elif op == "ring":
        for i in range(inp["emit_count"]):
            ba.emit_backend_alert("k1", f"m{i}")
        rec = ba.recent_alerts()
        assert len(rec) == exp["recent_len"]
        assert rec[0]["message"] == exp["newest_message"]
        assert rec[-1]["message"] == exp["oldest_kept_message"]
    elif op == "filter":
        ba.emit_backend_alert("a", "a1")
        ba.emit_backend_alert("b", "b1")
        ba.emit_backend_alert("a", "a2")
        assert [x["message"] for x in ba.recent_alerts(kind="a")] == exp["messages"]
        assert ba.alert_counts() == exp["counts"]
    elif op == "reset":
        ba.emit_backend_alert("k1", "m")
        ba.reset_alerts()
        assert len(ba.recent_alerts()) == exp["recent_len"]
        assert ba.alert_counts() == exp["counts"]
    elif op == "slack":
        posts = []

        def fake_post(*a, **k):
            posts.append(a)

            async def _noop():
                return None
            return _noop()

        monkeypatch.setattr(ba, "_post_alert_to_slack", fake_post, raising=False)
        monkeypatch.setattr(settings, "SLACK_BOT_TOKEN", inp["token"], raising=False)
        monkeypatch.setattr(settings, "BACKEND_ALERT_SLACK_THROTTLE_SECONDS", inp["throttle"], raising=False)

        async def go():
            for kind in inp["emits"]:
                ba.emit_backend_alert(kind, "m")
            for _ in range(5):
                await asyncio.sleep(0)

        _run(go())
        assert len(posts) == exp["posts"]
    else:
        raise AssertionError(f"unknown op {op}")


def _intent(inp, exp, monkeypatch):
    import app.modules.intent_classification as ic
    from app.services import backend_alerts as ba

    fake = FakeLLM(inp["reply"])
    monkeypatch.setattr(ic, "llm_service", fake)
    module = ic.IntentClassificationModule()
    res = _run(module.classify("What is the transaction volume?", domain="operations"))
    assert list(res)[:4] == exp["result"]
    assert len(fake.calls) == exp["llm_calls"]
    assert ba.alert_counts() == exp["alerts"]


def _route(inp, exp):
    import app.orchestration.workflow as wf
    assert wf.route_after_sql_generation(dict(inp["state"])) == exp["route"]


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_failure_handling(case, monkeypatch):
    inp, exp = case["input"], case["expected"]
    sc = inp["scenario"]
    if sc == "retry":
        _retry(inp, exp)
    elif sc == "fallback":
        _fallback(inp, exp)
    elif sc == "sql_node":
        _sql_node(inp, exp, monkeypatch)
    elif sc == "parse":
        _parse(inp, exp)
    elif sc == "structured":
        _structured(inp, exp)
    elif sc == "generate_text":
        _generate_text(inp, exp)
    elif sc == "alerts":
        _alerts(inp, exp, monkeypatch)
    elif sc == "intent":
        _intent(inp, exp, monkeypatch)
    elif sc == "route":
        _route(inp, exp)
    else:
        raise AssertionError(f"unknown scenario {sc}")
