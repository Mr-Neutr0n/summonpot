"""Adversarial public-boundary tests for authenticated endpoint context."""

import asyncio
import threading
import time
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, ValidationError
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from summonpot import (
    AgentChoice,
    Exactly,
    FromContext,
    FromRequest,
    FromResult,
    Operation,
    Required,
    Summon,
)
from summonpot._context import _ContextError
from summonpot.runtime import Runtime
from summonpot.server import build_app

SECRET = "auth-secret-never-leak"


class Request(BaseModel):
    tenant_id: str
    customer_id: str


class Result(BaseModel):
    tenant_id: str
    customer_id: str


class Principal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    tenant_id: str


def make_app(starts, *, method="POST", provider=None):
    def authenticate(request) -> Principal:
        token = request.headers.get("authorization")
        if token not in ("Bearer alice", "Bearer bob"):
            raise PermissionError(SECRET)
        return Principal(tenant_id=token.split()[-1])

    def read(tenant_id: str, customer_id: str) -> Result:
        starts.append((tenant_id, customer_id))
        return Result(tenant_id=tenant_id, customer_id=customer_id)

    op = Operation(
        read,
        bind={
            "tenant_id": FromContext("tenant_id"),
            "customer_id": FromRequest("customer_id"),
        },
        output=Result,
    )
    app = Summon("auth-test", context=provider or authenticate)
    if method == "GET":

        @app("/records", method=method)
        def endpoint(
            tenant_id: str, customer_id: str, result=Required(op, calls=Exactly(1))
        ) -> Result:
            """Read the authenticated tenant record."""
            ...
    else:

        @app("/records", method=method)
        def endpoint(request: Request, result=Required(op, calls=Exactly(1))) -> Result:
            """Read the authenticated tenant record."""
            ...

    return app


def test_concurrent_http_tenants_and_spoofed_body_are_isolated():
    starts = []
    app = make_app(starts)
    app._runtime = Runtime(model="invalid:no-model")
    with TestClient(build_app(app)) as client:

        def call(user):
            return client.post(
                "/records",
                headers={"Authorization": f"Bearer {user}"},
                json={"tenant_id": "attacker", "customer_id": user},
            )

        with concurrent_pair(call) as results:
            assert [r.status_code for r in results] == [200, 200]
            assert {r.json()["tenant_id"] for r in results} == {"alice", "bob"}
    assert sorted(starts) == [("alice", "alice"), ("bob", "bob")]


@contextmanager
def concurrent_pair(fn):
    barrier = threading.Barrier(2)
    out = [None, None]

    def go(i, user):
        barrier.wait()
        out[i] = fn(user)

    ts = [
        threading.Thread(target=go, args=(i, u)) for i, u in enumerate(("alice", "bob"))
    ]
    for t in ts:
        t.start()
    for t in ts:
        t.join(5)
    assert all(not t.is_alive() for t in ts)
    yield out


def test_raw_trusted_and_untrusted_context_have_exact_effect_boundary():
    starts = []
    app = make_app(starts)
    rt = Runtime(model="invalid:no-model")
    ep = app.endpoints[0]
    with pytest.raises(PermissionError):
        asyncio.run(rt.call(ep, {"customer_id": "x", "tenant_id": "mallory"}))
    with pytest.raises(_ContextError):
        asyncio.run(rt.call(ep, {"customer_id": "x"}, context={"tenant_id": "alice"}))
    assert starts == []
    assert asyncio.run(
        rt.call(
            ep,
            {"customer_id": "x", "tenant_id": "mallory"},
            context=Principal(tenant_id="alice"),
        )
    ) == Result(tenant_id="alice", customer_id="x")
    assert starts == [("alice", "x")]


def test_fromcontext_alias_uses_canonical_field_name():
    class Aliased(BaseModel):
        model_config = ConfigDict(frozen=True, extra="forbid")
        tenant_id: str = __import__("pydantic").Field(alias="tenant")

    def auth(request) -> Aliased:
        return Aliased.model_validate({"tenant": "canonical"})

    starts = []
    app = make_app(starts, provider=auth)
    app._runtime = Runtime(model="invalid:no-model")
    with TestClient(build_app(app)) as c:
        r = c.post(
            "/records",
            headers={"Authorization": "Bearer alice"},
            json={"tenant_id": "attacker", "customer_id": "c"},
        )
    assert r.status_code == 200 and r.json()["tenant_id"] == "canonical"
    assert starts == [("canonical", "c")]


def test_agent_schema_hides_injected_context_and_model_spoof_cannot_override():
    starts, seen = [], []

    def provider(request) -> Principal:
        return Principal(tenant_id=SECRET)

    def read(tenant_id: str, customer_id: str, style: str) -> str:
        starts.append((tenant_id, customer_id, style))
        # Deliberately do not disclose identity in this application's tool result.
        return customer_id

    op = Operation(
        read,
        bind={
            "tenant_id": FromContext("tenant_id"),
            "customer_id": FromRequest("customer_id"),
            "style": AgentChoice(),
        },
        output=str,
    )
    app = Summon(context=provider)

    @app("/records")
    def endpoint(request: Request, result=Required(op, calls=Exactly(1))) -> str:
        """Read the customer's requested view inside its authenticated scope."""
        ...

    def model_fn(messages, info):
        seen.append(messages)
        assert SECRET not in str(messages)
        schema = info.function_tools[0].parameters_json_schema
        assert set(schema["properties"]) == {"style"}
        if len(seen) == 1:
            return ModelResponse(
                parts=[ToolCallPart("read", {"style": "short", "tenant_id": "spoof"})]
            )
        if len(seen) == 2:
            assert starts == []
            return ModelResponse(parts=[ToolCallPart("read", {"style": "short"})])
        from pydantic_ai.messages import TextPart

        return ModelResponse(parts=[TextPart("c")])

    app._runtime = Runtime(model=FunctionModel(model_fn), retries=2)
    with TestClient(build_app(app)) as client:
        response = client.post(
            "/records", json={"customer_id": "c", "tenant_id": "attacker"}
        )
    assert response.status_code == 200, response.text
    assert len(seen) == 3
    assert starts == [(SECRET, "c", "short")]


def test_denied_auth_redacts_secret_and_has_zero_effects(caplog):
    starts = []
    app = make_app(starts)
    app._runtime = Runtime(model="invalid:no-model")
    with TestClient(build_app(app)) as c:
        r = c.post("/records", json={"tenant_id": "attacker", "customer_id": "x"})
    assert r.status_code == 401 and SECRET not in r.text
    assert starts == [] and SECRET not in caplog.text


def test_provider_failure_is_fixed_500_and_redacted():
    def broken(request) -> Principal:
        raise RuntimeError(SECRET)

    starts = []
    app = make_app(starts, provider=broken)
    app._runtime = Runtime(model="invalid:no-model")
    with TestClient(build_app(app)) as c:
        r = c.post("/records", json={"tenant_id": "x", "customer_id": "c"})
    assert r.status_code == 500 and SECRET not in r.text
    assert starts == []


def test_auth_provider_timeout_precedes_operation_effect():
    def slow(request) -> Principal:
        time.sleep(0.1)
        return Principal(tenant_id="alice")

    starts = []
    app = make_app(starts, provider=slow)
    app._runtime = Runtime(model="invalid:no-model", timeout=0.01)
    with TestClient(build_app(app)) as c:
        r = c.post("/records", json={"tenant_id": "x", "customer_id": "c"})
    assert r.status_code == 504
    assert starts == []


@pytest.mark.parametrize(
    "method,path,kwargs",
    [
        ("GET", "/records", {"params": {"tenant_id": "x", "customer_id": "c"}}),
        ("POST", "/records", {"json": {"tenant_id": "x", "customer_id": "c"}}),
    ],
)
def test_synthetic_context_parameter_does_not_leak_openapi(method, path, kwargs):
    starts = []
    app = make_app(starts, method=method)
    schema = build_app(app).openapi()
    operation = schema["paths"]["/records"][method.lower()]
    assert all(p["name"] != "_context_request" for p in operation.get("parameters", []))
    assert "_context_request" not in str(operation)


def test_untrusted_and_wrong_principal_have_zero_effects():
    starts = []
    app = make_app(starts)
    rt = Runtime(model="invalid:no-model")
    ep = app.endpoints[0]
    with pytest.raises(PermissionError):
        asyncio.run(rt.call(ep, {"customer_id": "x", "tenant_id": "spoof"}))
    for ctx in ({"tenant_id": "alice"}, Principal.model_construct(tenant_id=42)):
        with pytest.raises(_ContextError):
            asyncio.run(
                rt.call(ep, {"customer_id": "x", "tenant_id": "spoof"}, context=ctx)
            )
    assert starts == []


def test_principal_is_frozen_and_provider_reference_is_compiled():
    def original(request) -> Principal:
        return Principal(tenant_id="alice")

    starts = []
    app = make_app(starts, provider=original)
    with pytest.raises(ValidationError):
        Principal(tenant_id="alice").tenant_id = "bad"
    app._context = None
    app.endpoints[0].tools.clear()
    app._runtime = Runtime(model="invalid:no-model")
    with TestClient(build_app(app)) as c:
        r = c.post(
            "/records",
            headers={"Authorization": "Bearer alice"},
            json={"tenant_id": "x", "customer_id": "c"},
        )
    assert r.status_code == 200


def test_auth_provider_cancelled_does_not_start_operation():
    started = threading.Event()

    async def provider(request) -> Principal:
        started.set()
        await asyncio.sleep(30)
        return Principal(tenant_id="alice")

    # A valid provider must return model; runtime cancellation checked in raw task.
    starts = []
    app = make_app(starts, provider=provider)
    app._runtime = Runtime(model="invalid:no-model", timeout=0.01)
    with TestClient(build_app(app)) as c:
        r = c.post(
            "/records",
            headers={"Authorization": "Bearer alice"},
            json={"tenant_id": "x", "customer_id": "c"},
        )
    assert r.status_code == 504 and starts == []


def test_context_secrets_absent_openapi_and_raw_errors(caplog):
    starts = []
    app = make_app(starts)
    app._runtime = Runtime(model="invalid:no-model")
    schema = str(build_app(app).openapi())
    assert SECRET not in schema
    with TestClient(build_app(app)) as c:
        r = c.post(
            "/records",
            headers={"Authorization": "Bearer invalid"},
            json={"tenant_id": "x", "customer_id": "c"},
        )
    assert SECRET not in r.text and SECRET not in caplog.text


class Intermediate(BaseModel):
    customer_id: str


@pytest.mark.parametrize("transport", ["raw", "http"])
def test_entire_chain_context_is_preflighted_before_upstream_effects(transport):
    from typing import Literal

    starts = []

    def provider(request) -> Principal:
        return Principal(tenant_id="denied")

    def load(customer_id: str) -> Intermediate:
        starts.append("load")
        return Intermediate(customer_id=customer_id)

    def save(customer_id: str, tenant_id: Literal["allowed"]) -> Result:
        starts.append("save")
        return Result(tenant_id=tenant_id, customer_id=customer_id)

    first = Operation(
        load, bind={"customer_id": FromRequest("customer_id")}, output=Intermediate
    )
    second = Operation(
        save,
        bind={
            "customer_id": FromResult(first, "customer_id"),
            "tenant_id": FromContext("tenant_id"),
        },
        output=Result,
    )
    app = Summon(context=provider)

    @app("/chain")
    def endpoint(
        request: Request,
        a=Required(first, calls=Exactly(1)),
        b=Required(second, calls=Exactly(1)),
    ) -> Result:
        """Preflight identity constraints before the first operation."""
        ...

    app._runtime = Runtime(model="invalid:no-model")
    if transport == "raw":
        with pytest.raises(_ContextError):
            asyncio.run(
                app._runtime.call(
                    app.endpoints[0],
                    {"customer_id": "c", "tenant_id": "spoof"},
                    context=Principal(tenant_id="denied"),
                )
            )
    else:
        with TestClient(build_app(app)) as client:
            response = client.post(
                "/chain", json={"customer_id": "c", "tenant_id": "spoof"}
            )
        assert response.status_code == 500
    assert starts == []


def test_chain_context_snapshot_survives_provider_model_storage_mutation():
    principal = Principal(tenant_id="original")
    starts = []

    def provider(request) -> Principal:
        return principal

    def load(customer_id: str) -> Intermediate:
        object.__setattr__(principal, "tenant_id", "mutated-after-admission")
        return Intermediate(customer_id=customer_id)

    def save(customer_id: str, tenant_id: str) -> Result:
        starts.append(tenant_id)
        return Result(tenant_id=tenant_id, customer_id=customer_id)

    first = Operation(
        load, bind={"customer_id": FromRequest("customer_id")}, output=Intermediate
    )
    second = Operation(
        save,
        bind={
            "customer_id": FromResult(first, "customer_id"),
            "tenant_id": FromContext("tenant_id"),
        },
        output=Result,
    )
    app = Summon(context=provider)

    @app("/chain")
    def endpoint(
        request: Request,
        a=Required(first, calls=Exactly(1)),
        b=Required(second, calls=Exactly(1)),
    ) -> Result:
        """Keep the authenticated identity stable through the chain."""
        ...

    result = asyncio.run(
        Runtime(model="invalid:no-model").call(
            app.endpoints[0],
            {"customer_id": "c", "tenant_id": "spoof"},
            context=principal,
        )
    )
    assert result.tenant_id == "original"
    assert starts == ["original"]


@pytest.mark.parametrize("query", [True, False])
def test_query_name_collision_and_no_body_use_real_authenticated_runtime(query):
    starts, turns = [], []

    def read(tenant_id: str, customer_id: str = "empty") -> Result:
        starts.append((tenant_id, customer_id))
        return Result(tenant_id=tenant_id, customer_id=customer_id)

    bind: dict[str, FromContext | FromRequest] = {"tenant_id": FromContext("tenant_id")}
    if query:
        bind["customer_id"] = FromRequest("_context_request")
    op = Operation(read, bind=bind, output=Result)
    app = Summon(context=authenticate_for_collision)
    if query:

        @app("/records", method="GET")
        def endpoint(
            _context_request: str, result=Required(op, calls=Exactly(1))
        ) -> Result:
            """Keep the public query name separate from the HTTP request."""
            ...
    else:

        @app("/records")
        def endpoint(result=Required(op, calls=Exactly(1))) -> Result:
            """Authenticate a parameterless endpoint."""
            ...

    def model(messages, info):
        turns.append(True)
        if len(turns) == 1:
            return ModelResponse(parts=[ToolCallPart("read", {})])
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {"tenant_id": "alice", "customer_id": "c" if query else "empty"},
                )
            ]
        )

    app._runtime = Runtime(model=FunctionModel(model))
    built = build_app(app)
    schema = built.openapi()["paths"]["/records"]["get" if query else "post"]
    assert [p["name"] for p in schema.get("parameters", [])] == (
        ["_context_request"] if query else []
    )
    assert "requestBody" not in schema
    with TestClient(built) as client:
        response = client.request(
            "GET" if query else "POST",
            "/records",
            params={"_context_request": "c"} if query else None,
        )
    assert response.status_code == 200, response.text
    assert turns == [True, True]
    assert starts == [("alice", "c" if query else "empty")]


def test_request_task_cancellation_stops_async_authentication_before_effects():
    import httpx

    entered = asyncio.Event()
    cleaned, starts = [], []

    async def provider(request) -> Principal:
        entered.set()
        try:
            await asyncio.sleep(30)
            return Principal(tenant_id="alice")
        finally:
            cleaned.append(True)

    app = make_app(starts, provider=provider)

    async def exercise():
        transport = httpx.ASGITransport(app=build_app(app))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://local"
        ) as client:
            task = asyncio.create_task(
                client.post("/records", json={"tenant_id": "spoof", "customer_id": "c"})
            )
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(exercise())
    assert cleaned == [True]
    assert starts == []


def test_principal_validation_hooks_are_rejected_instead_of_rewriting_identity():
    from pydantic import field_validator

    class HookPrincipal(BaseModel):
        model_config = ConfigDict(frozen=True, extra="forbid")
        tenant_id: str

        @field_validator("tenant_id")
        @classmethod
        def change(cls, value: str) -> str:
            return value + "-changed"

    def provider(request) -> HookPrincipal:
        return HookPrincipal(tenant_id="original")

    with pytest.raises(TypeError, match=r"receiving|hook"):
        Summon(context=provider)


def authenticate_for_collision(request) -> Principal:
    return Principal(tenant_id="alice")
