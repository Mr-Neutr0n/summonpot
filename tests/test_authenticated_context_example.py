from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from summonpot.runtime import Runtime
from summonpot.server import build_app

_PATH = Path(__file__).parents[1] / "examples/13_authenticated_context/app.py"
_SPEC = importlib.util.spec_from_file_location("authenticated_context_example", _PATH)
assert _SPEC is not None and _SPEC.loader is not None
app_module = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(app_module)


def test_authenticated_sqlite_writes_are_tenant_scoped(tmp_path, monkeypatch):
    db_path = tmp_path / "outside-checkout.sqlite3"
    monkeypatch.setenv("AUTH_CONTEXT_DB", str(db_path))
    monkeypatch.setenv("TENANT_BEARER_TOKENS", "alpha:secret-alpha,beta:secret-beta")
    app_module.summon._runtime = Runtime(model="invalid:no-model")
    with TestClient(build_app(app_module.summon)) as client:
        missing = client.post(
            "/notes", json={"note_id": "x", "text": "x", "idempotency_key": "x"}
        )
        invalid = client.post(
            "/notes",
            headers={"Authorization": "Bearer nope"},
            json={"note_id": "x", "text": "x", "idempotency_key": "x"},
        )
        forged = client.post(
            "/notes",
            headers={"Authorization": "Bearer secret-alpha"},
            json={
                "note_id": "x",
                "text": "x",
                "idempotency_key": "x",
                "tenant_id": "beta",
            },
        )
        assert (missing.status_code, invalid.status_code, forged.status_code) == (
            401,
            401,
            422,
        )
        assert not db_path.exists()
        alpha = client.post(
            "/notes",
            headers={"Authorization": "Bearer secret-alpha"},
            json={
                "note_id": "same",
                "text": "alpha data",
                "idempotency_key": "alpha-key",
            },
        )
        beta = client.post(
            "/notes",
            headers={"Authorization": "Bearer secret-beta"},
            json={
                "note_id": "same",
                "text": "beta data",
                "idempotency_key": "beta-key",
            },
        )
        assert alpha.status_code == beta.status_code == 200
        assert alpha.json() == {
            "tenant_id": "alpha",
            "note_id": "same",
            "text": "alpha data",
            "idempotency_key": "alpha-key",
            "status": "created",
        }
        assert beta.json()["tenant_id"] == "beta"
        retry = client.post(
            "/notes",
            headers={"Authorization": "Bearer secret-alpha"},
            json={
                "note_id": "same",
                "text": "alpha data",
                "idempotency_key": "alpha-key",
            },
        )
        assert retry.status_code == 200 and retry.json() == alpha.json()
        conflict = client.post(
            "/notes",
            headers={"Authorization": "Bearer secret-alpha"},
            json={"note_id": "same", "text": "changed", "idempotency_key": "alpha-key"},
        )
        assert conflict.status_code != 200
    with sqlite3.connect(db_path) as db:
        rows = db.execute(
            "SELECT tenant_id,note_id,text,idempotency_key FROM notes ORDER BY tenant_id"
        ).fetchall()
    assert rows == [
        ("alpha", "same", "alpha data", "alpha-key"),
        ("beta", "same", "beta data", "beta-key"),
    ]


def test_ambiguous_tenant_credentials_fail_closed(tmp_path, monkeypatch):
    db_path = tmp_path / "notes.sqlite3"
    monkeypatch.setenv("AUTH_CONTEXT_DB", str(db_path))
    monkeypatch.setenv("TENANT_BEARER_TOKENS", "alpha:same-secret,beta:same-secret")
    with TestClient(build_app(app_module.summon)) as client:
        response = client.post(
            "/notes",
            headers={"Authorization": "Bearer same-secret"},
            json={"note_id": "x", "text": "x", "idempotency_key": "x"},
        )
    assert response.status_code == 500
    assert not db_path.exists()
