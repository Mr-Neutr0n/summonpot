"""Registration and compilation boundaries for longer linear chains."""

from __future__ import annotations

from pydantic import BaseModel

from summonpot import Exactly, FromRequest, FromResult, Required, Summon
from summonpot._execution import _compile_endpoint
from summonpot._validation import _chain_contract_tool_indices
from summonpot.contracts import Operation


class Request(BaseModel):
    value: int


class FirstResult(BaseModel):
    value: int


class SecondResult(BaseModel):
    value: int


class Response(BaseModel):
    value: int


def test_three_operation_chain_registers_and_compiles_in_topological_order():
    summon = Summon("svc")

    def first(value: int) -> FirstResult:
        return FirstResult(value=value)

    first_op = Operation(
        first, bind={"value": FromRequest("value")}, output=FirstResult
    )

    def second(value: int) -> SecondResult:
        return SecondResult(value=value)

    second_op = Operation(
        second,
        bind={"value": FromResult(first_op, "value")},
        output=SecondResult,
        after=(first_op,),
    )

    def third(value: int) -> Response:
        return Response(value=value)

    third_op = Operation(
        third,
        bind={"value": FromResult(second_op, "value")},
        output=Response,
        after=(second_op,),
    )

    @summon("/chain")
    def endpoint(request: Request) -> Response:
        """Run a linear chain."""
        ...

    # Reversed declaration order proves graph order, not parameter order, drives compilation.
    @summon("/compiled")
    def compiled(
        request: Request,
        a=Required(third_op, calls=Exactly(1)),
        b=Required(second_op, calls=Exactly(1)),
        c=Required(first_op, calls=Exactly(1)),
    ) -> Response:
        """Compile a linear chain."""
        ...

    endpoint_def = summon.endpoints[-1]
    assert _chain_contract_tool_indices(endpoint_def.tools) == (2, 1, 0)
    plan = _compile_endpoint(endpoint_def)
    assert plan.response_producer == 0
    assert [tool.predecessors for tool in plan.tools] == [(1,), (2,), ()]


def test_linear_topology_requires_exactly_one_ready_operation_at_every_step():
    from types import SimpleNamespace

    import pytest

    from summonpot import Depends

    def identity(value: int):
        return {"value": value}

    first = Operation(
        identity, bind={"value": FromRequest("value")}, output=FirstResult
    )
    second = Operation(
        identity, bind={"value": FromResult(first, "value")}, output=SecondResult
    )
    third = Operation(
        identity, bind={"value": FromResult(second, "value")}, output=Response
    )

    def tool(contract):
        return SimpleNamespace(contract=contract, required=True, bounds=Exactly(1))

    assert _chain_contract_tool_indices([tool(third), tool(first), tool(second)]) == (
        1,
        2,
        0,
    )

    # A branch grants an unresolved ordering choice after its common producer.
    branched = Operation(
        identity, bind={"value": FromResult(first, "value")}, output=Response
    )
    assert (
        _chain_contract_tool_indices([tool(first), tool(second), tool(branched)])
        is None
    )
    disconnected = Operation(
        identity, bind={"value": FromRequest("value")}, output=Response
    )
    assert (
        _chain_contract_tool_indices([tool(first), tool(second), tool(disconnected)])
        is None
    )
    assert (
        _chain_contract_tool_indices([tool(first), tool(second), tool(second)]) is None
    )
    object.__setattr__(first, "after", (third,))
    assert (
        _chain_contract_tool_indices([tool(first), tool(second), tool(third)]) is None
    )

    app = Summon("optional-chain")
    with pytest.raises(TypeError, match="unenforced"):

        @app("/invalid")
        def endpoint(
            request: Request,
            a=Required(first, calls=Exactly(1)),
            b=Depends(second),
            c=Required(third, calls=Exactly(1)),
        ) -> Response:
            """Reject optional contracted steps rather than weakening enforcement."""
            ...


def test_long_chain_can_read_an_earlier_producer_and_redundant_ordering_edge():
    from types import SimpleNamespace

    def identity(value: int):
        return {"value": value}

    contracts = [
        Operation(identity, bind={"value": FromRequest("value")}, output=FirstResult)
    ]
    for index in range(1, 12):
        contracts.append(
            Operation(
                identity,
                bind={"value": FromResult(contracts[0], "value")},
                output=Response if index == 11 else SecondResult,
                after=(contracts[-1],),
            )
        )
    tools = [
        SimpleNamespace(contract=contract, required=True, bounds=Exactly(1))
        for contract in reversed(contracts)
    ]
    assert _chain_contract_tool_indices(tools) == tuple(reversed(range(12)))
