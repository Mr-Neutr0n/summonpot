"""Executed adversarial boundaries for sequential result chains."""

import asyncio
import threading
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field, field_validator
from pydantic_ai import ModelRetry, UnexpectedModelBehavior
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from summonpot import Exactly, FromRequest, FromResult, Operation, Required, Summon
from summonpot._execution import _new_run, _prepare_request, _register_endpoint
from summonpot.runtime import (
    Runtime,
    _invoke_bound_operation,
    _OperationInputError,
    _OperationOutputError,
    _runtime_deadline,
)
from summonpot.server import build_app


class Req(BaseModel):
    key: str


class Produced(BaseModel):
    key: str


class Answer(BaseModel):
    key: str


def app_for(events, *, invalid_producer=False, invalid_terminal=False):
    def produce(key: str):
        events.append(("produce", key))
        return {"key": 42 if invalid_producer else key}

    def consume(key: str):
        events.append(("consume", key))
        return {"key": 42 if invalid_terminal else key}

    first = Operation(produce, bind={"key": FromRequest("key")}, output=Produced)
    second = Operation(consume, bind={"key": FromResult(first, "key")}, output=Answer)
    app = Summon("adversarial")

    @app("/chain")
    def endpoint(
        request: Req,
        a=Required(first, calls=Exactly(1)),
        b=Required(second, calls=Exactly(1)),
    ) -> Answer:
        """Run the validated chain."""
        ...

    # Preserve adversarial agent-loop coverage now that fully bound chains go direct.
    _register_endpoint(app.endpoints[0], allow_direct=False)
    return app


def ready_model():
    def model(messages, info):
        if info.function_tools:
            assert len(info.function_tools) == 1
            return ModelResponse(parts=[ToolCallPart(info.function_tools[0].name, {})])
        return ModelResponse(parts=[TextPart("completed")])

    return FunctionModel(model)


def test_concurrent_requests_never_read_other_run_result():
    events = []
    app = app_for(events)
    runtime = Runtime(model=ready_model())
    keys = [f"request-{index}" for index in range(20)]

    async def exercise():
        return await asyncio.gather(
            *(runtime.call(app.endpoints[0], {"key": key}) for key in keys)
        )

    results = asyncio.run(exercise())
    assert [result.key for result in results] == keys
    assert sorted(events) == sorted(
        [(operation, key) for key in keys for operation in ("produce", "consume")]
    )


def test_model_cannot_spoof_framework_bound_arguments():
    events = []
    app = app_for(events)

    def model(messages, info):
        return ModelResponse(
            parts=[ToolCallPart(info.function_tools[0].name, {"key": "forged"})]
        )

    with pytest.raises(UnexpectedModelBehavior):
        asyncio.run(
            Runtime(model=FunctionModel(model), retries=0).call(
                app.endpoints[0], {"key": "canonical"}
            )
        )
    assert events == []


@pytest.mark.parametrize("same_turn", [True, False])
def test_actual_duplicate_tool_calls_never_replay_effects(same_turn):
    events = []
    app = app_for(events)
    turns = 0
    attempts = []

    def model(messages, info):
        nonlocal turns
        turns += 1
        if turns == 1:
            attempts.append("produce")
            parts = [ToolCallPart("produce", {}, tool_call_id="first")]
            if same_turn:
                attempts.append("produce")
                parts.append(ToolCallPart("produce", {}, tool_call_id="duplicate"))
            return ModelResponse(parts=parts)
        if turns == 2 and not same_turn:
            attempts.append("produce")
            return ModelResponse(
                parts=[ToolCallPart("produce", {}, tool_call_id="late-duplicate")]
            )
        if info.function_tools:
            return ModelResponse(parts=[ToolCallPart(info.function_tools[0].name, {})])
        return ModelResponse(parts=[TextPart("completed")])

    result = asyncio.run(
        Runtime(model=FunctionModel(model), retries=3).call(
            app.endpoints[0], {"key": "once"}
        )
    )
    assert attempts == ["produce", "produce"]
    assert result == Answer(key="once")
    assert events == [("produce", "once"), ("consume", "once")]


def test_invalid_model_completion_after_write_retries_without_replay():
    events = []
    app = app_for(events)
    invalid_sent = False

    def model(messages, info):
        nonlocal invalid_sent
        if info.function_tools:
            return ModelResponse(parts=[ToolCallPart(info.function_tools[0].name, {})])
        if not invalid_sent:
            invalid_sent = True
            return ModelResponse(parts=[ToolCallPart("not_a_declared_tool", {})])
        return ModelResponse(parts=[TextPart("completed")])

    result = asyncio.run(
        Runtime(model=FunctionModel(model), retries=2).call(
            app.endpoints[0], {"key": "once"}
        )
    )
    assert invalid_sent
    assert result == Answer(key="once")
    assert events == [("produce", "once"), ("consume", "once")]


@pytest.mark.parametrize("invalid_producer", [True, False])
def test_invalid_operation_output_after_effect_does_not_retry(invalid_producer):
    events = []
    app = app_for(
        events, invalid_producer=invalid_producer, invalid_terminal=not invalid_producer
    )
    with pytest.raises(_OperationOutputError):
        asyncio.run(
            Runtime(model=ready_model(), retries=3).call(
                app.endpoints[0], {"key": "effect"}
            )
        )
    expected = [("produce", "effect")]
    if not invalid_producer:
        expected.append(("consume", "effect"))
    assert events == expected


def test_sync_worker_cancellation_retains_uncertain_slot():
    events = []
    app = app_for(events)
    plan = Runtime(model="test")._plan_for(app.endpoints[0])
    run = _new_run(plan, _prepare_request(plan, {"key": "slow"}))
    entered, release = threading.Event(), threading.Event()

    def slow(key):
        entered.set()
        release.wait(2)
        events.append(key)
        return {"key": key}

    tool = replace(plan.tools[0], fn=slow)

    async def exercise():
        task = asyncio.create_task(_invoke_bound_operation(tool, run, {}))
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert run.states[0].outcome == "outcome_unknown"
            assert run.states[0].started == 1 and run.states[0].succeeded == 0
            with pytest.raises(ModelRetry):
                await _invoke_bound_operation(tool, run, {})
        finally:
            release.set()

    asyncio.run(exercise())
    assert events == ["slow"]


def test_prestart_deadline_expiry_has_no_effect():
    events = []
    app = app_for(events)
    plan = Runtime(model="test")._plan_for(app.endpoints[0])
    run = _new_run(plan, _prepare_request(plan, {"key": "expired"}))
    token = _runtime_deadline.set(time.monotonic() - 1)
    try:
        with pytest.raises(TimeoutError):
            asyncio.run(_invoke_bound_operation(plan.tools[0], run, {}))
    finally:
        _runtime_deadline.reset(token)
    assert events == []
    assert run.states[0].started == 0


def test_from_result_receiving_constraints_reject_before_consumer():
    events = []

    class BroadProduced(BaseModel):
        key: object

    def produce(key: str):
        events.append(("produce", key))
        return {"key": "12"}

    def consume(key: int):
        events.append(("consume", key))
        return Answer(key=str(key))

    first = Operation(produce, bind={"key": FromRequest("key")}, output=BroadProduced)
    second = Operation(consume, bind={"key": FromResult(first, "key")}, output=Answer)
    app = Summon("receiving")

    @app("/chain")
    def endpoint(
        request: Req,
        a=Required(first, calls=Exactly(1)),
        b=Required(second, calls=Exactly(1)),
    ) -> Answer:
        """Require the receiving native type without coercion."""
        ...

    with pytest.raises(_OperationInputError):
        asyncio.run(
            Runtime(model=ready_model()).call(app.endpoints[0], {"key": "input"})
        )
    assert events == [("produce", "input")]


def test_aliases_and_terminal_validators_preserve_raw_http_parity():
    calls = []

    class AliasProduced(BaseModel):
        key: str = Field(alias="producerKey")

    class AliasAnswer(BaseModel):
        key: str = Field(alias="publicKey")

        @field_validator("key", mode="after")
        @classmethod
        def transform(cls, value):
            calls.append(value)
            return value + "!"

    def produce(key: str):
        return {"producerKey": key}

    def consume(key: str):
        return {"publicKey": key}

    first = Operation(produce, bind={"key": FromRequest("key")}, output=AliasProduced)
    second = Operation(
        consume, bind={"key": FromResult(first, "key")}, output=AliasAnswer
    )
    app = Summon("aliases")

    @app("/chain")
    def endpoint(
        request: Req,
        a=Required(first, calls=Exactly(1)),
        b=Required(second, calls=Exactly(1)),
    ) -> AliasAnswer:
        """Return the exact transformed receipt."""
        ...

    runtime = Runtime(model=ready_model())
    raw = asyncio.run(runtime.call(app.endpoints[0], {"key": "same"}))
    app._runtime = runtime
    with TestClient(build_app(app)) as client:
        response = client.post("/chain", json={"key": "same"})
    assert response.status_code == 200
    assert response.json() == raw.model_dump(by_alias=True) == {"publicKey": "same!"}
    assert calls == ["same", "same"]


def test_public_metadata_mutation_cannot_replace_compiled_operations():
    events = []
    app = app_for(events)
    runtime = Runtime(model=ready_model())
    public = app.endpoints[0]
    public.tools[0].fn = lambda **kwargs: events.append(("evil", "evil"))
    public.tools.reverse()
    result = asyncio.run(runtime.call(public, {"key": "original"}))
    assert result == Answer(key="original")
    assert events == [("produce", "original"), ("consume", "original")]
