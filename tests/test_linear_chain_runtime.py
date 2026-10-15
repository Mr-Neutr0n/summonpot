"""Public runtime acceptance for bounded three-operation result chains.

These use a keyless FunctionModel to exercise the real agent/runtime path; they
make no claim about provider-backed execution.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from pydantic import AfterValidator, BaseModel
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from summonpot import Exactly, FromRequest, FromResult, Operation, Required, Summon
from summonpot.runtime import Runtime, _OperationExecutionError, _OperationOutputError
from summonpot.server import build_app


class Request(BaseModel):
    key: str


class Loaded(BaseModel):
    key: str


class Enriched(BaseModel):
    value: str


class Receipt(BaseModel):
    value: str
    saved: bool


def three_step_app(events, *, declaration_order=(0, 1, 2), terminal_output=Receipt):
    def load(key: str) -> Loaded:
        events.append(("load", key))
        return Loaded(key=key)

    def enrich(key: str) -> Enriched:
        events.append(("enrich", key))
        return Enriched(value=f"enriched:{key}")

    def save(value: str) -> Receipt:
        events.append(("save", value))
        return Receipt(value=value, saved=True)

    first = Operation(load, bind={"key": FromRequest("key")}, output=Loaded)
    second = Operation(enrich, bind={"key": FromResult(first, "key")}, output=Enriched)
    third = Operation(
        save, bind={"value": FromResult(second, "value")}, output=terminal_output
    )
    operations = (first, second, third)
    app = Summon("three-step")

    def endpoint(
        request: Request,
        a=Required(operations[declaration_order[0]], calls=Exactly(1)),
        b=Required(operations[declaration_order[1]], calls=Exactly(1)),
        c=Required(operations[declaration_order[2]], calls=Exactly(1)),
    ) -> Receipt:
        """Load, enrich, and save the requested record."""
        ...

    endpoint.__annotations__["return"] = terminal_output
    app("/chain")(endpoint)

    return app


def operation_model(
    events, *, corrupt_second=False, duplicate_first=False, early_final_turns=0
):
    turns = 0

    def model(messages, info):
        nonlocal turns
        turns += 1
        tools = info.function_tools
        if turns <= early_final_turns:
            return ModelResponse(parts=[TextPart("premature completion")])
        if tools:
            # On a legal linear chain only the next not-yet-ready operation is
            # visible; emitting its actual call makes this a runtime probe.
            tool = tools[0]
            if duplicate_first and tool.name == "load":
                return ModelResponse(
                    parts=[
                        ToolCallPart(tool.name, {}, tool_call_id="first"),
                        ToolCallPart(tool.name, {}, tool_call_id="duplicate"),
                    ]
                )
            if corrupt_second and tool.name == "enrich":
                events.append(("model-attempt", tool.name))
                # Tool has no model-owned arguments, so the invalid result is
                # produced by the operation callback in the dedicated test.
            return ModelResponse(parts=[ToolCallPart(tool.name, {})])
        if turns <= early_final_turns:
            return ModelResponse(parts=[TextPart("premature completion")])
        return ModelResponse(parts=[TextPart("model-authored response is ignored")])

    return FunctionModel(model)


def run(app, model, key="sample", *, retries=2):
    return asyncio.run(
        Runtime(model=model, retries=retries).call(app.endpoints[0], {"key": key})
    )


def test_three_step_chain_uses_result_edges_not_declaration_order():
    events = []
    app = three_step_app(events, declaration_order=(2, 0, 1))
    visible = []

    def model(messages, info):
        names = [tool.name for tool in info.function_tools]
        visible.append(names)
        if names:
            assert len(names) == 1
            assert (
                info.function_tools[0].parameters_json_schema.get("properties", {})
                == {}
            )
            return ModelResponse(parts=[ToolCallPart(names[0], {})])
        return ModelResponse(parts=[TextPart("not the response")])

    result = run(app, FunctionModel(model))
    assert result == Receipt(value="enriched:sample", saved=True)
    assert events == [
        ("load", "sample"),
        ("enrich", "sample"),
        ("save", "enriched:sample"),
    ]
    assert visible == [["load"], ["enrich"], ["save"], []]


@pytest.mark.parametrize("same_turn", [True, False])
def test_three_step_duplicate_and_stale_model_calls_do_not_replay(same_turn):
    events = []
    app = three_step_app(events)
    turn = 0
    attempts = []

    def model(messages, info):
        nonlocal turn
        turn += 1
        if turn == 1:
            attempts.append("load")
            parts = [ToolCallPart("load", {}, tool_call_id="original")]
            if same_turn:
                attempts.append("load")
                parts.append(ToolCallPart("load", {}, tool_call_id="duplicate"))
            return ModelResponse(parts=parts)
        if turn == 2 and not same_turn:
            attempts.append("load")
            return ModelResponse(parts=[ToolCallPart("load", {}, tool_call_id="stale")])
        if info.function_tools:
            return ModelResponse(parts=[ToolCallPart(info.function_tools[0].name, {})])
        return ModelResponse(parts=[TextPart("complete")])

    result = run(app, FunctionModel(model), retries=3)
    assert attempts == ["load", "load"]
    assert result == Receipt(value="enriched:sample", saved=True)
    assert events == [
        ("load", "sample"),
        ("enrich", "sample"),
        ("save", "enriched:sample"),
    ]


def test_intermediate_output_validation_blocks_third_side_effect():
    events = []

    def load(key: str) -> Loaded:
        events.append(("load", key))
        return Loaded(key=key)

    def invalid_enrich(key: str) -> dict:
        events.append(("enrich", key))
        return {"value": 7}

    def save(value: str) -> Receipt:
        events.append(("save", value))
        return Receipt(value=value, saved=True)

    first = Operation(load, bind={"key": FromRequest("key")}, output=Loaded)
    second = Operation(
        invalid_enrich, bind={"key": FromResult(first, "key")}, output=Enriched
    )
    third = Operation(save, bind={"value": FromResult(second, "value")}, output=Receipt)
    app = Summon("invalid-middle")

    @app("/chain")
    def endpoint(
        request: Request,
        a=Required(first, calls=Exactly(1)),
        b=Required(second, calls=Exactly(1)),
        c=Required(third, calls=Exactly(1)),
    ) -> Receipt:
        """Validate each result before proceeding."""
        ...

    with pytest.raises(_OperationOutputError):
        run(app, operation_model(events))
    assert events == [("load", "sample"), ("enrich", "sample")]


def test_failed_third_write_is_attempted_once_and_prior_steps_are_not_replayed():
    events = []

    def save(value: str) -> Receipt:
        events.append(("save-start", value))
        raise RuntimeError("write failed")

    def load(key: str) -> Loaded:
        events.append(("load", key))
        return Loaded(key=key)

    def enrich(key: str) -> Enriched:
        events.append(("enrich", key))
        return Enriched(value=f"enriched:{key}")

    first = Operation(load, bind={"key": FromRequest("key")}, output=Loaded)
    second = Operation(enrich, bind={"key": FromResult(first, "key")}, output=Enriched)
    third = Operation(save, bind={"value": FromResult(second, "value")}, output=Receipt)
    app = Summon("failed-write")

    @app("/chain")
    def endpoint(
        request: Request,
        a=Required(first, calls=Exactly(1)),
        b=Required(second, calls=Exactly(1)),
        c=Required(third, calls=Exactly(1)),
    ) -> Receipt:
        """Execute each required step at most once."""
        ...

    with pytest.raises(_OperationExecutionError):
        run(app, operation_model(events, early_final_turns=1), retries=3)
    assert events == [
        ("load", "sample"),
        ("enrich", "sample"),
        ("save-start", "enriched:sample"),
    ]


def test_three_concurrent_public_runtime_calls_keep_result_ledgers_isolated():
    events = []
    app = three_step_app(events)
    model = operation_model(events)

    def invoke(key):
        return run(app, model, key)

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(invoke, ("a", "b", "c")))
    assert results == [
        Receipt(value=f"enriched:{key}", saved=True) for key in ("a", "b", "c")
    ]
    assert sorted(events) == sorted(
        [("load", key) for key in ("a", "b", "c")]
        + [("enrich", key) for key in ("a", "b", "c")]
        + [("save", f"enriched:{key}") for key in ("a", "b", "c")]
    )


def test_terminal_validator_runs_once_at_raw_runtime_and_http_boundary():
    events = []

    def terminal_validator(value):
        events.append("terminal-validator")
        value.value += "!"
        return value

    ValidatedReceipt = __import__("typing").Annotated[
        Receipt, AfterValidator(terminal_validator)
    ]
    app = three_step_app(events, terminal_output=ValidatedReceipt)
    model = operation_model(events)

    raw = run(app, model, "raw")
    assert raw == Receipt(value="enriched:raw!", saved=True)
    assert events.count("terminal-validator") == 1

    events.clear()
    app._runtime = Runtime(model=model)
    client = TestClient(build_app(app))
    response = client.post("/chain", json={"key": "http"})
    assert response.status_code == 200
    assert response.json() == {"value": "enriched:http!", "saved": True}
    assert events.count("terminal-validator") == 1
    assert events.count(("save", "enriched:http")) == 1


def test_early_final_output_retries_without_replaying_completed_steps():
    events = []
    app = three_step_app(events)
    result = run(app, operation_model(events, early_final_turns=1), retries=3)
    assert result == Receipt(value="enriched:sample", saved=True)
    assert events == [
        ("load", "sample"),
        ("enrich", "sample"),
        ("save", "enriched:sample"),
    ]
