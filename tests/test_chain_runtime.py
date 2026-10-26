"""Runtime acceptance for the narrow sequential result-chain contract."""

import asyncio

from pydantic import BaseModel
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from summonpot import Exactly, FromRequest, FromResult, Operation, Required, Summon
from summonpot._execution import _register_endpoint
from summonpot.runtime import Runtime


class ChainRequest(BaseModel):
    identifier: str


class Intermediate(BaseModel):
    identifier: str


class Receipt(BaseModel):
    identifier: str
    recorded: bool


def chain_app(events):
    def lookup(identifier: str) -> Intermediate:
        events.append(("lookup", identifier))
        return Intermediate(identifier=identifier)

    def record(identifier: str) -> Receipt:
        events.append(("record", identifier))
        return Receipt(identifier=identifier, recorded=True)

    first = Operation(
        lookup, bind={"identifier": FromRequest("identifier")}, output=Intermediate
    )
    last = Operation(
        record, bind={"identifier": FromResult(first, "identifier")}, output=Receipt
    )
    summon = Summon("chain")

    @summon("/record")
    def endpoint(
        request: ChainRequest,
        a=Required(first, calls=Exactly(1)),
        b=Required(last, calls=Exactly(1)),
    ) -> Receipt:
        """Look up the identifier and record it, returning the exact receipt."""
        ...

    # Keep this module's model-retry assertions on the agent execution branch.
    _register_endpoint(summon.endpoints[0], allow_direct=False)
    return summon


def test_invalid_intermediate_never_starts_downstream():
    events = []

    def broken_lookup(identifier: str) -> dict:
        events.append(("lookup", identifier))
        return {"identifier": 4}

    first = Operation(
        broken_lookup,
        bind={"identifier": FromRequest("identifier")},
        output=Intermediate,
    )
    second = Operation(
        lambda identifier: events.append(("record", identifier)),
        bind={"identifier": FromResult(first, "identifier")},
        output=Receipt,
    )
    summon = Summon("chain-invalid")

    @summon("/record")
    def endpoint(
        request: ChainRequest,
        a=Required(first, calls=Exactly(1)),
        b=Required(second, calls=Exactly(1)),
    ) -> Receipt:
        """Execute a validated two-step operation."""
        ...

    def model(messages, info):
        if info.function_tools:
            name = info.function_tools[0].name
            return ModelResponse(parts=[ToolCallPart(name, {})])
        return ModelResponse(parts=[TextPart("done")])

    try:
        asyncio.run(
            Runtime(model=FunctionModel(model), retries=0).call(
                summon.endpoints[0], {"identifier": "canonical"}
            )
        )
    except Exception:
        pass
    else:
        raise AssertionError("invalid producer output was accepted")
    assert events == [("lookup", "canonical")]


def test_duplicate_start_is_reserved_even_when_model_calls_again():
    events = []
    summon = chain_app(events)
    calls = 0

    def model(messages, info):
        nonlocal calls
        if info.function_tools:
            calls += 1
            return ModelResponse(parts=[ToolCallPart(info.function_tools[0].name, {})])
        return ModelResponse(parts=[TextPart("done")])

    asyncio.run(
        Runtime(model=FunctionModel(model), retries=0).call(
            summon.endpoints[0], {"identifier": "once"}
        )
    )
    assert events == [("lookup", "once"), ("record", "once")]


def test_chain_returns_terminal_receipt_not_model_claim():
    events = []
    summon = chain_app(events)
    advertised = []

    def model(messages, info):
        names = [tool.name for tool in info.function_tools]
        advertised.append(names)
        if names:
            assert len(names) == 1
            assert (
                info.function_tools[0].parameters_json_schema.get("properties", {})
                == {}
            )
            return ModelResponse(parts=[ToolCallPart(names[0], {})])
        # Completion is not an authoritative application response.
        if info.output_tools:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        info.output_tools[0].name,
                        {"identifier": "forged", "recorded": False},
                    )
                ]
            )
        return ModelResponse(parts=[TextPart("forged success")])

    result = asyncio.run(
        Runtime(model=FunctionModel(model)).call(
            summon.endpoints[0], {"identifier": "canonical"}
        )
    )
    assert result == Receipt(identifier="canonical", recorded=True)
    assert events == [("lookup", "canonical"), ("record", "canonical")]
    assert advertised == [["lookup"], ["record"], []]


def test_early_model_completion_retries_until_required_chain_finishes():
    events = []
    summon = chain_app(events)
    turns = 0

    def model(messages, info):
        nonlocal turns
        turns += 1
        if turns == 1 or not info.function_tools:
            return ModelResponse(parts=[TextPart("done")])
        return ModelResponse(parts=[ToolCallPart(info.function_tools[0].name, {})])

    result = asyncio.run(
        Runtime(model=FunctionModel(model), retries=2).call(
            summon.endpoints[0], {"identifier": "early"}
        )
    )
    assert result == Receipt(identifier="early", recorded=True)
    assert events == [("lookup", "early"), ("record", "early")]


def test_kernel_rejects_unready_and_duplicate_concurrent_calls():
    import pytest
    from pydantic_ai import ModelRetry

    from summonpot._execution import _new_run, _prepare_request
    from summonpot.runtime import _invoke_bound_operation

    events = []
    app = chain_app(events)
    plan = Runtime(model="test")._plan_for(app.endpoints[0])
    run = _new_run(plan, _prepare_request(plan, {"identifier": "race"}))

    async def exercise():
        with pytest.raises(ModelRetry):
            await _invoke_bound_operation(plan.tools[1], run, {})
        assert events == []
        results = await asyncio.gather(
            *[_invoke_bound_operation(plan.tools[0], run, {}) for _ in range(2)],
            return_exceptions=True,
        )
        assert sum(isinstance(result, ModelRetry) for result in results) == 1
        assert run.states[0].started == run.states[0].succeeded == 1
        await _invoke_bound_operation(plan.tools[1], run, {})
        with pytest.raises(ModelRetry):
            await _invoke_bound_operation(plan.tools[1], run, {})

    asyncio.run(exercise())
    assert events == [("lookup", "race"), ("record", "race")]


def test_cancelled_started_operation_retains_uncertain_reservation():
    from dataclasses import replace

    import pytest
    from pydantic_ai import ModelRetry

    from summonpot._execution import _new_run, _prepare_request
    from summonpot.runtime import _invoke_bound_operation

    app = chain_app([])
    plan = Runtime(model="test")._plan_for(app.endpoints[0])
    run = _new_run(plan, _prepare_request(plan, {"identifier": "cancel"}))

    async def exercise():
        started = asyncio.Event()

        async def operation(identifier):
            started.set()
            await asyncio.Event().wait()

        tool = replace(plan.tools[0], fn=operation)
        task = asyncio.create_task(_invoke_bound_operation(tool, run, {}))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert run.states[0].outcome == "outcome_unknown"
        assert run.states[0].started == 1
        assert run.states[0].succeeded == 0
        with pytest.raises(ModelRetry):
            await _invoke_bound_operation(tool, run, {})

    asyncio.run(exercise())
