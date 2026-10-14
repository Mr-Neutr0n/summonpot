"""HTTP integration coverage for the inventory result-chain example."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from summonpot.runtime import Runtime
from summonpot.server import build_app

EXAMPLE_DIR = Path(__file__).parents[1] / "examples" / "10_result_chain"
_spec = importlib.util.spec_from_file_location(
    "inventory_chain_example", EXAMPLE_DIR / "app.py"
)
assert _spec is not None and _spec.loader is not None
inventory_app = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = inventory_app
_spec.loader.exec_module(inventory_app)


def make_model(*, fail_after_persist: bool = False) -> FunctionModel:
    def respond(messages, info):
        tools = info.function_tools
        if tools:
            assert len(tools) == 1
            return ModelResponse(parts=[ToolCallPart(tools[0].name, {})])
        if fail_after_persist:
            raise RuntimeError("intentional test failure after persistence")
        return ModelResponse(parts=[TextPart("not an authoritative receipt")])

    return FunctionModel(respond)


def setup_db(tmp_path: Path) -> Path:
    path = tmp_path / "inventory.sqlite3"
    inventory_app.DB_PATH = path
    inventory_app.initialize_database()
    return path


def test_http_returns_persisted_canonical_receipt(tmp_path: Path) -> None:
    path = setup_db(tmp_path)
    inventory_app.summon._runtime = Runtime(model=make_model())
    with TestClient(build_app(inventory_app.summon)) as client:
        response = client.post(
            "/reservations",
            json={"sku": "notebook", "quantity": 3, "idempotency_key": "http-order-1"},
        )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "idempotency_key": "http-order-1",
        "sku": "notebook",
        "quantity": 3,
        "inventory_revision": 2,
        "remaining": 22,
        "status": "reserved",
    }
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT idempotency_key, sku, quantity, inventory_revision, remaining, status "
            "FROM reservations WHERE idempotency_key = ?",
            ("http-order-1",),
        ).fetchone()
    assert row == ("http-order-1", "notebook", 3, 2, 22, "reserved")


def test_http_failure_after_write_does_not_claim_rollback(tmp_path: Path) -> None:
    path = setup_db(tmp_path)
    inventory_app.summon._runtime = Runtime(model=make_model(fail_after_persist=True))
    with TestClient(
        build_app(inventory_app.summon), raise_server_exceptions=False
    ) as client:
        response = client.post(
            "/reservations",
            json={
                "sku": "notebook",
                "quantity": 4,
                "idempotency_key": "http-order-fail",
            },
        )
    assert response.status_code >= 500
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT idempotency_key, sku, quantity, inventory_revision, remaining, status "
            "FROM reservations WHERE idempotency_key = ?",
            ("http-order-fail",),
        ).fetchone()
        inventory = connection.execute(
            "SELECT revision, available FROM inventory WHERE sku = 'notebook'"
        ).fetchone()
    assert row == ("http-order-fail", "notebook", 4, 2, 21, "reserved")
    assert inventory == (2, 21)
