from __future__ import annotations

import asyncio
import time
from typing import Annotated

import pytest
from pydantic import BaseModel, BeforeValidator
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel

import summonpot.runtime as runtime_module
from summonpot import Exactly, FromRequest, Operation, Required, Summon
from summonpot.runtime import Runtime


class Request(BaseModel):
    query: str


class Response(BaseModel):
    value: str


def test_legacy_capability_validation_expiry_prevents_async_effect():
    starts = 0

    def slow(value: int) -> int:
        time.sleep(0.15)
        return value

    async def effect(value: int) -> str:
        nonlocal starts
        starts += 1
        return "done"

    effect.__annotations__["value"] = Annotated[int, BeforeValidator(slow)]

    def generate(messages, info):
        return ModelResponse(parts=[ToolCallPart("effect", {"value": 1})])

    summon = Summon(
        "svc", runtime=Runtime(model=FunctionModel(generate), timeout=0.08, retries=0)
    )

    @summon("/value")
    def endpoint(value: int, result=Required(effect)) -> str:
        """Run the legacy capability."""
        ...

    # Prebuild the agent so only provider-side tool argument validation overruns.
    summon._runtime._agent_for(summon.endpoints[0])
    from fastapi.testclient import TestClient

    from summonpot.server import build_app

    response = TestClient(build_app(summon), raise_server_exceptions=False).post(
        "/value", json={"value": 1}
    )
    assert response.status_code == 504
    assert starts == 0


def test_legacy_capability_validation_expiry_prevents_sync_effect():
    starts = 0

    def slow(value: int) -> int:
        time.sleep(0.15)
        return value

    def effect(value: int) -> str:
        nonlocal starts
        starts += 1
        return "done"

    effect.__annotations__["value"] = Annotated[int, BeforeValidator(slow)]

    def generate(messages, info):
        return ModelResponse(parts=[ToolCallPart("effect", {"value": 1})])

    summon = Summon(
        "svc", runtime=Runtime(model=FunctionModel(generate), timeout=0.08, retries=0)
    )

    @summon("/value")
    def endpoint(value: int, result=Required(effect)) -> str:
        """Run the legacy capability."""
        ...

    summon._runtime._agent_for(summon.endpoints[0])
    from fastapi.testclient import TestClient

    from summonpot.server import build_app

    response = TestClient(build_app(summon), raise_server_exceptions=False).post(
        "/value", json={"value": 1}
    )
    assert response.status_code == 504
    assert starts == 0


def test_runtime_deadline_from_worker_is_not_redacted_as_application_failure(
    monkeypatch,
):
    import asyncio

    from fastapi.testclient import TestClient

    from summonpot.server import build_app

    starts = 0

    def effect(query: str) -> Response:
        nonlocal starts
        starts += 1
        return Response(value="done")

    operation = Operation(effect, bind={"query": FromRequest("query")}, output=Response)
    summon = Summon("svc", runtime=Runtime(timeout=0.01))

    @summon("/deadline")
    def endpoint(
        request: Request, result=Required(operation, calls=Exactly(1))
    ) -> Response:
        """Run one bounded capability."""
        ...

    class NoTimeout:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    original_to_thread = asyncio.to_thread

    async def delayed_worker(fn, /, *args, **kwargs):
        await asyncio.sleep(0.02)
        return await original_to_thread(fn, *args, **kwargs)

    monkeypatch.setattr(runtime_module.asyncio, "timeout", lambda _: NoTimeout())
    monkeypatch.setattr(runtime_module.asyncio, "to_thread", delayed_worker)
    response = TestClient(build_app(summon), raise_server_exceptions=False).post(
        "/deadline", json={"query": "ok"}
    )

    assert response.status_code == 504
    assert response.json() == {
        "detail": "Endpoint timed out before producing a valid response."
    }
    assert starts == 0


def test_deadline_is_checked_after_waiting_for_operation_admission(monkeypatch):
    starts = 0

    def perform(query: str) -> Response:
        nonlocal starts
        starts += 1
        return Response(value="done")

    operation = Operation(
        perform, bind={"query": FromRequest("query")}, output=Response
    )
    summon = Summon("svc")

    @summon("/deadline")
    def endpoint(
        request: Request, result=Required(operation, calls=Exactly(1))
    ) -> Response:
        """Return the operation result."""
        ...

    original = runtime_module._invoke_bound_operation

    async def wait_then_invoke(tool, run, supplied, **kwargs):
        await run.lock.acquire()

        async def release_after_expiration():
            await asyncio.sleep(0.03)
            run.lock.release()

        releaser = asyncio.create_task(release_after_expiration())
        try:
            return await original(tool, run, supplied, **kwargs)
        finally:
            await releaser

    monkeypatch.setattr(runtime_module, "_invoke_bound_operation", wait_then_invoke)
    with pytest.raises(TimeoutError):
        asyncio.run(
            Runtime(timeout=0.01).call(summon.endpoints[0], {"query": "ok", "value": 1})
        )
    assert starts == 0
