from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel

import summonpot.runtime as runtime_module
from summonpot import Exactly, FromRequest, Operation, Required, Summon
from summonpot.runtime import Runtime


class Request(BaseModel):
    query: str


class Response(BaseModel):
    value: str


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
