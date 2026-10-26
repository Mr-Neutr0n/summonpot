"""Real-runtime HTTP tests for the three-step SQLite inventory example."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from summonpot import Exactly, FromResult, Operation, Required, Summon
from summonpot.runtime import Runtime
from summonpot.server import build_app

EXAMPLE_DIR = Path(__file__).parents[1] / "examples" / "11_linear_result_chain"
_spec = importlib.util.spec_from_file_location(
    "linear_chain_example", EXAMPLE_DIR / "app.py"
)
assert _spec is not None and _spec.loader is not None
linear_app = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = linear_app
_spec.loader.exec_module(linear_app)


def scripted_model() -> FunctionModel:
    """Keyless test harness only; choose the declared next tool at each step."""

    def respond(messages, info):
        tools = info.function_tools
        if tools:
            assert len(tools) == 1
            return ModelResponse(parts=[ToolCallPart(tools[0].name, {})])
        return ModelResponse(parts=[TextPart("model text is not a receipt")])

    return FunctionModel(respond)


def prepare(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "inventory.sqlite3"
    monkeypatch.setenv("INVENTORY_DB", str(path))
    linear_app.initialize_database()
    linear_app.summon._runtime = Runtime(model=scripted_model())
    return path


def test_http_receipt_is_persisted_and_repeated_request_is_idempotent(
    tmp_path: Path, monkeypatch
) -> None:
    path = prepare(tmp_path, monkeypatch)
    payload = {"sku": "notebook", "quantity": 3, "idempotency_key": "linear-order-1"}
    with TestClient(build_app(linear_app.summon)) as client:
        first = client.post("/reservations", json=payload)
        second = client.post("/reservations", json=payload)
    expected = {
        "idempotency_key": "linear-order-1",
        "sku": "notebook",
        "quantity": 3,
        "inventory_revision": 2,
        "remaining": 22,
        "status": "reserved",
    }
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() == expected
    with sqlite3.connect(path) as db:
        rows = db.execute(
            "SELECT idempotency_key, sku, quantity, inventory_revision, remaining, status FROM reservations"
        ).fetchall()
        inventory = db.execute(
            "SELECT revision, available FROM inventory WHERE sku = 'notebook'"
        ).fetchone()
    assert rows == [("linear-order-1", "notebook", 3, 2, 22, "reserved")]
    assert inventory == (2, 22)


def test_receipt_read_failure_after_commit_preserves_single_reservation(
    tmp_path: Path, monkeypatch
) -> None:
    path = prepare(tmp_path, monkeypatch)
    starts = []

    def fail_read(idempotency_key: str):
        starts.append(idempotency_key)
        raise RuntimeError("read unavailable after committed write")

    receipt = Operation(
        fail_read,
        bind={
            "idempotency_key": FromResult(
                linear_app.reservation_operation, "idempotency_key"
            )
        },
        output=linear_app.ReservationReceipt,
    )
    app = Summon("failed-read", runtime=Runtime(model=scripted_model(), retries=3))

    def endpoint(
        request,
        lookup=Required(linear_app.lookup_operation, calls=Exactly(1)),
        reserve=Required(linear_app.reservation_operation, calls=Exactly(1)),
        result=Required(receipt, calls=Exactly(1)),
    ):
        """Persist the reservation before reading its receipt."""
        ...

    endpoint.__annotations__ = {
        "request": linear_app.ReservationRequest,
        "return": linear_app.ReservationReceipt,
    }
    app("/reservations")(endpoint)
    payload = {"sku": "notebook", "quantity": 4, "idempotency_key": "linear-order-fail"}
    with TestClient(build_app(app), raise_server_exceptions=False) as client:
        response = client.post("/reservations", json=payload)
    assert response.status_code == 500
    assert starts == ["linear-order-fail"]
    with sqlite3.connect(path) as db:
        rows = db.execute(
            "SELECT idempotency_key, sku, quantity, inventory_revision, remaining, status FROM reservations"
        ).fetchall()
        inventory = db.execute(
            "SELECT revision, available FROM inventory WHERE sku = 'notebook'"
        ).fetchone()
    assert rows == [("linear-order-fail", "notebook", 4, 2, 21, "reserved")]
    assert inventory == (2, 21)
