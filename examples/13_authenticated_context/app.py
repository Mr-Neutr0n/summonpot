"""Authenticated tenant-scoped note creation backed by SQLite."""

from __future__ import annotations

import os
import secrets
import sqlite3
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from summonpot import Exactly, FromContext, FromRequest, Operation, Required, Summon


class Principal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    tenant_id: str


class NoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=4000)
    idempotency_key: str = Field(min_length=1, max_length=128)


class NoteReceipt(BaseModel):
    tenant_id: str
    note_id: str
    text: str
    idempotency_key: str
    status: str


def _configured_tokens() -> dict[str, str]:
    """Read tenant tokens at request time; credentials are never baked in."""
    raw = os.environ.get("TENANT_BEARER_TOKENS", "")
    result: dict[str, str] = {}
    for entry in raw.split(","):
        if not entry:
            continue
        tenant, separator, token = entry.partition(":")
        if (
            not separator
            or not tenant
            or not token
            or tenant in result
            or token in result.values()
        ):
            raise RuntimeError("TENANT_BEARER_TOKENS configuration is invalid")
        result[tenant] = token
    return result


def authenticate(request) -> Principal:
    authorization = request.headers.get("authorization", "")
    scheme, _, presented = authorization.partition(" ")
    if scheme.lower() != "bearer" or not presented:
        raise PermissionError("Authentication required")
    matched_tenant = None
    for tenant, expected in _configured_tokens().items():
        if secrets.compare_digest(presented.encode("utf-8"), expected.encode("utf-8")):
            matched_tenant = tenant
    if matched_tenant is None:
        raise PermissionError("Authentication required")
    return Principal(tenant_id=matched_tenant)


def _db_path() -> Path:
    configured = os.environ.get("AUTH_CONTEXT_DB")
    if not configured:
        raise RuntimeError("Set AUTH_CONTEXT_DB to a writable SQLite path")
    return Path(configured)


def create_note(
    tenant_id: str, note_id: str, text: str, idempotency_key: str
) -> NoteReceipt:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path, timeout=10) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS notes (
            tenant_id TEXT NOT NULL, note_id TEXT NOT NULL, text TEXT NOT NULL,
            idempotency_key TEXT NOT NULL, PRIMARY KEY (tenant_id, note_id),
            UNIQUE (tenant_id, idempotency_key))""")
        db.execute("BEGIN IMMEDIATE")
        prior = db.execute(
            "SELECT note_id, text, idempotency_key FROM notes WHERE tenant_id=? AND idempotency_key=?",
            (tenant_id, idempotency_key),
        ).fetchone()
        if prior:
            if prior != (note_id, text, idempotency_key):
                raise ValueError("Idempotency key already used for different note data")
            row = prior
        else:
            db.execute(
                "INSERT INTO notes VALUES (?, ?, ?, ?)",
                (tenant_id, note_id, text, idempotency_key),
            )
            row = (note_id, text, idempotency_key)
    return NoteReceipt(
        tenant_id=tenant_id,
        note_id=row[0],
        text=row[1],
        idempotency_key=row[2],
        status="created",
    )


create_operation = Operation(
    create_note,
    bind={
        "tenant_id": FromContext("tenant_id"),
        "note_id": FromRequest("note_id"),
        "text": FromRequest("text"),
        "idempotency_key": FromRequest("idempotency_key"),
    },
    output=NoteReceipt,
)

summon = Summon("authenticated-context-notes", context=authenticate)


@summon("/notes")
def add_note(
    request: NoteRequest, receipt=Required(create_operation, calls=Exactly(1))
) -> NoteReceipt:
    """Create one durable note inside the authenticated tenant."""
    ...


if __name__ == "__main__":
    summon.serve(host="127.0.0.1", port=8000)
