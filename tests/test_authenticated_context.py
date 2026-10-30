"""Authenticated context enters only through the trusted application boundary."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict

from summonpot import (
    Exactly,
    FromContext,
    FromRequest,
    Operation,
    Required,
    Summon,
)
from summonpot.runtime import Runtime
from summonpot.server import build_app


class Request(BaseModel):
    customer_id: str
    tenant_id: str = "caller-controlled"


class Response(BaseModel):
    tenant_id: str
    customer_id: str


class Principal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    tenant_id: str


def authenticate(request) -> Principal:
    token = request.headers.get("authorization")
    tenants = {"Bearer alice": "tenant-a", "Bearer bob": "tenant-b"}
    if token not in tenants:
        raise PermissionError("private credential detail")
    return Principal(tenant_id=tenants[token])


def app_for(starts, *, context=authenticate):
    def read_tenant(tenant_id: str, customer_id: str) -> Response:
        starts.append((tenant_id, customer_id))
        return Response(tenant_id=tenant_id, customer_id=customer_id)

    operation = Operation(
        read_tenant,
        bind={
            "tenant_id": FromContext("tenant_id"),
            "customer_id": FromRequest("customer_id"),
        },
        output=Response,
    )
    app = Summon("auth-context", context=context)

    @app("/tenant")
    def endpoint(
        request: Request, result=Required(operation, calls=Exactly(1))
    ) -> Response:
        """Read only the authenticated tenant."""
        ...

    return app


def test_context_requires_a_declared_application_provider():
    with pytest.raises(TypeError):
        app_for([], context=None)


def test_raw_runtime_cannot_forge_context_through_params():
    starts = []
    app = app_for(starts)
    with pytest.raises(PermissionError):
        asyncio.run(
            Runtime(model="invalid:no-model").call(
                app.endpoints[0],
                {
                    "customer_id": "c1",
                    "tenant_id": "attacker",
                    "context": {"tenant_id": "attacker"},
                },
            )
        )
    assert starts == []


def test_trusted_raw_context_executes_without_a_model():
    starts = []
    app = app_for(starts)
    result = asyncio.run(
        Runtime(model="invalid:no-model").call(
            app.endpoints[0],
            {"customer_id": "c1", "tenant_id": "attacker"},
            context=Principal(tenant_id="tenant-a"),
        )
    )
    assert result == Response(tenant_id="tenant-a", customer_id="c1")
    assert starts == [("tenant-a", "c1")]


def test_http_authenticates_and_ignores_caller_tenant():
    starts = []
    app = app_for(starts)
    app._runtime = Runtime(model="invalid:no-model")
    with TestClient(build_app(app)) as client:
        denied = client.post("/tenant", json={"customer_id": "c1"})
        allowed = client.post(
            "/tenant",
            headers={"Authorization": "Bearer alice"},
            json={"customer_id": "c1", "tenant_id": "attacker"},
        )
    assert denied.status_code == 401
    assert "private credential" not in denied.text
    assert allowed.status_code == 200, allowed.text
    assert allowed.json() == {"tenant_id": "tenant-a", "customer_id": "c1"}
    assert starts == [("tenant-a", "c1")]


def test_custom_runtime_cannot_silently_skip_http_authentication():
    starts = []
    app = app_for(starts)

    class BypassRuntime(Runtime):
        async def call(self, endpoint, params, **kwargs):
            return Response(tenant_id="unauthenticated", customer_id="c1")

    app._runtime = BypassRuntime()
    with TestClient(build_app(app), raise_server_exceptions=False) as client:
        response = client.post("/tenant", json={"customer_id": "c1"})
    assert response.status_code == 500
    assert "unauthenticated" not in response.text
    assert starts == []


def test_context_registration_error_does_not_render_literal_identity():
    from typing import Literal

    class RestrictedPrincipal(BaseModel):
        model_config = ConfigDict(frozen=True, extra="forbid")
        tenant_id: Literal["private-identity-literal"]

    def provider(request) -> RestrictedPrincipal:
        return RestrictedPrincipal(tenant_id="private-identity-literal")

    provider.__annotations__["return"] = RestrictedPrincipal

    def wrong_receiver(tenant_id: int) -> Response:
        return Response(tenant_id=str(tenant_id), customer_id="c")

    op = Operation(
        wrong_receiver, bind={"tenant_id": FromContext("tenant_id")}, output=Response
    )
    with pytest.raises(TypeError, match="incompatible") as error:

        @Summon(context=provider)("/wrong")
        def endpoint(
            request: Request, result=Required(op, calls=Exactly(1))
        ) -> Response:
            """Reject an incompatible private identity type."""
            ...

    assert "private-identity-literal" not in str(error.value)
