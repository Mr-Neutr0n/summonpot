"""No-model chain acceptance, separate from the retained agent-loop regressions."""

import asyncio
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from summonpot import Exactly, FromRequest, FromResult, Operation, Required, Summon
from summonpot._execution import _register_endpoint
from summonpot.runtime import Runtime, _OperationOutputError
from summonpot.server import build_app


class Request(BaseModel):
    key: str


class Intermediate(BaseModel):
    key: str


class Receipt(BaseModel):
    value: str


def chain(events, *, asynchronous=False, outcome="success"):
    def load(key: str):
        events.append("load")
        return {"key": key}

    def commit(key: str, suffix: str = "!"):
        events.append("commit")
        if outcome == "failure":
            raise RuntimeError("private application failure")
        if outcome == "invalid":
            return {"value": 42}
        if outcome == "timeout" and not asynchronous:
            time.sleep(0.04)
        return {"value": key + suffix}

    async def async_load(key: str):
        await asyncio.sleep(0)
        return load(key)

    async def async_commit(key: str, suffix: str = "!"):
        if outcome == "timeout":
            events.append("commit")
            await asyncio.sleep(1)
        return commit(key, suffix)

    first = Operation(
        async_load if asynchronous else load,
        bind={"key": FromRequest("key")},
        output=Intermediate,
    )
    last = Operation(
        async_commit if asynchronous else commit,
        bind={"key": FromResult(first, "key")},
        after=(first,),
        output=Receipt,
    )
    app = Summon("direct-chain")

    @app("/chain")
    def endpoint(
        request: Request,
        # Declaration order is deliberately the opposite of execution order.
        receipt=Required(last, calls=Exactly(1)),
        loaded=Required(first, calls=Exactly(1)),
    ) -> Receipt:
        """Load and commit the requested record."""
        ...

    return app


def no_model_runtime(monkeypatch, *, timeout=None):
    def forbidden(*args, **kwargs):
        raise AssertionError("Direct chains must not resolve or construct a model")

    monkeypatch.setattr(Runtime, "model_for", forbidden)
    monkeypatch.setattr(Runtime, "_agent_for", forbidden)
    return Runtime(model="invalid:unconfigured", retries=5, timeout=timeout)


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("transport", ["raw", "http"])
@pytest.mark.parametrize("outcome", ["success", "failure", "invalid", "timeout"])
def test_direct_chain_execution_matrix(monkeypatch, asynchronous, transport, outcome):
    events = []
    app = chain(events, asynchronous=asynchronous, outcome=outcome)
    runtime = no_model_runtime(
        monkeypatch, timeout=0.02 if outcome == "timeout" else None
    )
    app._runtime = runtime
    if transport == "http":
        with TestClient(build_app(app), raise_server_exceptions=False) as client:
            response = client.post("/chain", json={"key": "canonical"})
        expected_status = {
            "success": 200,
            "failure": 500,
            "invalid": 502,
            "timeout": 504,
        }
        assert response.status_code == expected_status[outcome], response.text
        if outcome == "success":
            assert response.json() == {"value": "canonical!"}
        else:
            assert "private application failure" not in response.text
    else:
        call = runtime.call(app.endpoints[0], {"key": "canonical"})
        if outcome == "success":
            assert asyncio.run(call) == Receipt(value="canonical!")
        else:
            error = {
                "failure": RuntimeError,
                "invalid": _OperationOutputError,
                "timeout": TimeoutError,
            }[outcome]
            with pytest.raises(error):
                asyncio.run(call)
    # Failed or uncertain starts are not retried, despite the runtime retry budget.
    assert events == ["load", "commit"]


def test_unregistered_compatibility_plan_cannot_enable_direct_chain(monkeypatch):
    events = []
    app = chain(events)
    plan = _register_endpoint(app.endpoints[0], allow_direct=False)
    assert plan.direct_chain_order is None
    assert events == []


def test_deadline_includes_direct_chain_final_audit(monkeypatch):
    events = []
    app = chain(events)
    runtime = no_model_runtime(monkeypatch, timeout=0.02)
    plan = runtime._plan_for(app.endpoints[0])
    audit = plan.output_auditor
    assert audit is not None

    def slow_audit(value):
        time.sleep(0.04)
        return audit(value)

    finalizing_plan = replace(plan, output_auditor=slow_audit)
    monkeypatch.setattr(runtime, "_plan_for", lambda endpoint: finalizing_plan)
    with pytest.raises(TimeoutError):
        asyncio.run(runtime.call(app.endpoints[0], {"key": "canonical"}))
    assert events == ["load", "commit"]
