from __future__ import annotations

import pytest
from pydantic import BaseModel

from summonpot import (
    Exactly,
    FromRequest,
    FromResult,
    Operation,
    Required,
    Summon,
)
from summonpot._execution import _compile_endpoint


class Request(BaseModel):
    key: str


class First(BaseModel):
    value: str


class Response(BaseModel):
    value: str


def produce(key: str) -> First:
    return First(value=key)


def consume(value: str, suffix: str = "!") -> Response:
    return Response(value=value + suffix)


def declarations(*, after=(), second_output=Response, bind=None):
    first = Operation(produce, bind={"key": FromRequest("key")}, output=First)
    second = Operation(
        consume,
        bind=bind or {"value": FromResult(first, "value")},
        output=second_output,
        after=after,
    )
    app = Summon("chain")

    @app("/chain")
    def endpoint(
        request: Request,
        a=Required(first, calls=Exactly(1)),
        b=Required(second, calls=Exactly(1)),
    ) -> Response:
        """Build a response from a producer result."""
        ...

    return app, first, second


def test_compiles_two_required_exactly_once_operations_with_result_provenance():
    app, _, _ = declarations()
    plan = _compile_endpoint(app.endpoints[0])
    assert plan.direct_tool is None
    assert plan.response_producer == 1
    assert plan.tools[1].predecessors == (plan.tools[0].identity,)
    binding = plan.tools[1].bindings[0]
    assert binding.producer_identity == plan.tools[0].identity
    assert binding.source.field == "value"
    assert plan.tools[1].output_adapter is not None
    assert plan.tools[1].output_adapter.validate_python({"value": "x"}).value == "x"
    assert plan.tools[1].bindings[0].validator is not None
    assert plan.tools[1].bindings[0].validator.accepts("x")


def test_from_result_identity_must_name_exact_declared_node():
    first = Operation(produce, bind={"key": FromRequest("key")}, output=First)
    impostor = Operation(produce, bind={"key": FromRequest("key")}, output=First)
    second = Operation(
        consume, bind={"value": FromResult(impostor, "value")}, output=Response
    )
    app = Summon("identity")
    with pytest.raises(TypeError, match="does not declare"):

        @app("/chain")
        def endpoint(
            request: Request,
            a=Required(first, calls=Exactly(1)),
            b=Required(second, calls=Exactly(1)),
        ) -> Response:
            """Use exactly the declared producer."""
            ...


def test_explicit_after_is_valid_independent_success_ordering():
    app, first, _ = declarations()
    # Dependencies use identity; after may redundantly reinforce that order.
    ordered_second = Operation(
        consume,
        bind={"value": FromResult(first, "value")},
        output=Response,
        after=(first,),
    )
    app = Summon("ordered")

    @app("/chain")
    def endpoint(
        request: Request,
        a=Required(first, calls=Exactly(1)),
        b=Required(ordered_second, calls=Exactly(1)),
    ) -> Response:
        """Run the producer before its consumer."""
        ...

    plan = _compile_endpoint(app.endpoints[0])
    assert plan.tools[1].predecessors == (plan.tools[0].identity,)


def test_unsupported_chain_sources_and_output_identity_are_not_admitted():
    first = Operation(produce, bind={"key": FromRequest("key")}, output=First)
    bad_second = Operation(consume, bind={"value": FromRequest("key")}, output=Response)
    app = Summon("bad")
    with pytest.raises(TypeError, match="unenforced"):

        @app("/chain")
        def endpoint(
            request: Request,
            a=Required(first, calls=Exactly(1)),
            b=Required(bad_second, calls=Exactly(1)),
        ) -> Response:
            """Reject disconnected required operations."""
            ...

    # A terminal output is required to be the endpoint's exact annotated response.
    other = Operation(consume, bind={"value": FromResult(first, "value")}, output=First)
    app = Summon("bad-output")
    with pytest.raises(TypeError, match="exact endpoint response"):

        @app("/chain")
        def endpoint2(
            request: Request,
            a=Required(first, calls=Exactly(1)),
            b=Required(other, calls=Exactly(1)),
        ) -> Response:
            """Reject the wrong terminal output."""
            ...


def test_single_operation_legacy_direct_behavior_is_unchanged():
    app = Summon("single")
    op = Operation(produce, bind={"key": FromRequest("key")}, output=Response)

    @app("/single")
    def endpoint(request: Request, a=Required(op, calls=Exactly(1))) -> Response:
        """Preserve the existing direct operation."""
        ...

    assert _compile_endpoint(app.endpoints[0]).direct_tool == 0


def test_reverse_declaration_order_compiles_by_operation_identity():
    first = Operation(produce, bind={"key": FromRequest("key")}, output=First)
    second = Operation(
        consume, bind={"value": FromResult(first, "value")}, output=Response
    )
    app = Summon("reverse")

    @app("/reverse")
    def endpoint(
        request: Request,
        b=Required(second, calls=Exactly(1)),
        a=Required(first, calls=Exactly(1)),
    ) -> Response:
        """Resolve ordering from declarations, not parameter position."""
        ...

    plan = _compile_endpoint(app.endpoints[0])
    assert plan.response_producer == 0
    assert plan.tools[0].predecessors == (1,)
    assert plan.tools[1].predecessors == ()


def test_after_only_pair_has_real_control_dependency():
    first = Operation(produce, bind={"key": FromRequest("key")}, output=First)
    second = Operation(
        consume, bind={"value": FromRequest("key")}, output=Response, after=(first,)
    )
    app = Summon("ordered-only")

    @app("/ordered")
    def endpoint(
        request: Request,
        b=Required(second, calls=Exactly(1)),
        a=Required(first, calls=Exactly(1)),
    ) -> Response:
        """Order two required operations without a data binding."""
        ...

    plan = _compile_endpoint(app.endpoints[0])
    assert plan.response_producer == 0
    assert plan.tools[0].predecessors == (1,)
    assert plan.tools[1].predecessors == ()


def test_cycle_is_rejected_even_for_mutated_declaration_metadata():
    first = Operation(produce, bind={"key": FromRequest("key")}, output=First)
    second = Operation(
        consume, bind={"value": FromResult(first, "value")}, output=Response
    )
    # Deliberately bypass public immutability to probe fail-closed graph admission.
    object.__setattr__(first, "after", (second,))
    app = Summon("cycle")
    with pytest.raises(TypeError, match="unenforced"):

        @app("/cycle")
        def endpoint(
            request: Request,
            a=Required(first, calls=Exactly(1)),
            b=Required(second, calls=Exactly(1)),
        ) -> Response:
            """Never admit a cyclic execution contract."""
            ...


def test_rejects_ambiguous_response_producer():
    first = Operation(produce, bind={"key": FromRequest("key")}, output=Response)
    second = Operation(
        consume, bind={"value": FromResult(first, "value")}, output=Response
    )
    app = Summon("ambiguous")
    with pytest.raises(TypeError, match="unique"):

        @app("/ambiguous")
        def endpoint(
            request: Request,
            a=Required(first, calls=Exactly(1)),
            b=Required(second, calls=Exactly(1)),
        ) -> Response:
            """Reject two operations claiming the endpoint output."""
            ...
