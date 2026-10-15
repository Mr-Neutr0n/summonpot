"""Compiler admission for choices constrained by a preceding list producer."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from summonpot import (
    AgentChoice,
    Exactly,
    FromRequest,
    FromResult,
    Operation,
    Required,
    Summon,
)
from summonpot._execution import _compile_endpoint


class Request(BaseModel):
    query: str


class Candidate(BaseModel):
    title: str


class Receipt(BaseModel):
    title: str


def setup(producer_output=list[Candidate], receiving=Candidate):
    def produce(query: str):
        return [Candidate(title=query)]

    first = Operation(
        produce, bind={"query": FromRequest("query")}, output=producer_output
    )

    def choose(item: Candidate) -> Receipt:
        return Receipt(title=item.title)

    second = Operation(
        choose, bind={"item": AgentChoice(from_result=first)}, output=Receipt
    )
    app = Summon("producer-choice")

    @app("/choose")
    def endpoint(
        request: Request,
        a=Required(first, calls=Exactly(1)),
        b=Required(second, calls=Exactly(1)),
    ) -> Receipt:
        """Choose a candidate returned by the producer."""
        ...

    return app, first, second


def test_compiles_candidate_choice_and_projects_opaque_parameter_schema():
    app, _, _ = setup()
    plan = _compile_endpoint(app.endpoints[0])
    first, second = plan.tools
    binding = second.bindings[0]
    assert binding.producer_identity == first.identity
    assert binding.validator is not None and binding.validator.accepts(
        Candidate(title="x")
    )
    assert second.signature.parameters["item"].annotation is Candidate
    assert second.visible_signature.parameters["item"].annotation is str
    assert first.output_projection is not None
    assert first.output_definitions is not None


def test_producer_choice_chain_keeps_unique_order_and_full_terminal_output():
    app, _, _ = setup()
    plan = _compile_endpoint(app.endpoints[0])
    assert plan.tools[1].predecessors == (plan.tools[0].identity,)
    assert plan.response_producer == 1


@pytest.mark.parametrize(
    "output", [list, list[object], list[str], set[Candidate], tuple[Candidate, ...]]
)
def test_rejects_unsupported_or_mismatched_producer_collection(output):
    with pytest.raises(TypeError):
        setup(output)


def test_rejects_foreign_producer():
    app = Summon("foreign")
    foreign = Operation(lambda: [Candidate(title="x")], output=list[Candidate])
    local = Operation(
        lambda query: [Candidate(title=query)],
        bind={"query": FromRequest("query")},
        output=list[Candidate],
    )
    selected = Operation(
        lambda item: Receipt(title=item.title),
        bind={"item": AgentChoice(from_result=foreign)},
        output=Receipt,
    )
    with pytest.raises(TypeError, match="does not declare"):

        @app("/bad")
        def endpoint(
            request: Request,
            a=Required(local, calls=Exactly(1)),
            b=Required(selected, calls=Exactly(1)),
        ) -> Receipt:
            """Reject foreign choice authority."""
            ...


def test_producer_choice_defaults_never_enter_model_schema():
    first = Operation(
        lambda query: [Candidate(title=query)],
        bind={"query": FromRequest("query")},
        output=list[Candidate],
    )

    def choose(item: Candidate = Candidate(title="secret-default")) -> Receipt:
        return Receipt(title=item.title)

    second = Operation(
        choose, bind={"item": AgentChoice(from_result=first)}, output=Receipt
    )
    app = Summon("choice-default")

    @app("/choose")
    def endpoint(
        request: Request,
        a=Required(first, calls=Exactly(1)),
        b=Required(second, calls=Exactly(1)),
    ) -> Receipt:
        """Choose from producer results only, never an ambient default."""
        ...

    import inspect

    plan = _compile_endpoint(app.endpoints[0])
    parameter = plan.tools[1].visible_signature.parameters["item"]
    assert parameter.default is inspect.Parameter.empty
    assert parameter.annotation is str


def test_chain_source_snapshot_is_immutable():
    app, producer, consumer = setup()
    plan = _compile_endpoint(app.endpoints[0])
    assert consumer.bind is not None
    original = consumer.bind["item"]
    object.__setattr__(consumer, "bind", {"item": FromResult(producer, "title")})
    binding = plan.tools[1].bindings[0]
    assert binding.source is original
    assert binding.producer_identity == plan.tools[0].identity
