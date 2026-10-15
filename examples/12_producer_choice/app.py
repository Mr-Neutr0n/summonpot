"""Bounded SQLite inventory search followed by an agent-selected exact order."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from pydantic import BaseModel, Field

from summonpot import AgentChoice, Exactly, FromRequest, Operation, Required, Summon


class OrderRequest(BaseModel):
    query: str = Field(min_length=1)
    order_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)


class Candidate(BaseModel):
    identifier: str
    label: str
    stock: int
    revision: int


class OrderReceipt(BaseModel):
    order_id: str
    idempotency_key: str
    candidate: Candidate
    remaining_stock: int
    status: str


def _database_path() -> Path:
    """Resolve at call time; no endpoint captures mutable path configuration."""
    return Path(os.environ.get("INVENTORY_DB", "inventory.sqlite3"))


def initialize_database(path: Path | None = None) -> None:
    database = _database_path() if path is None else path
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS inventory (
                item_id TEXT PRIMARY KEY, label TEXT NOT NULL,
                stock INTEGER NOT NULL CHECK (stock >= 0), revision INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL,
                item_id TEXT NOT NULL, item_label TEXT NOT NULL,
                candidate_stock INTEGER NOT NULL, candidate_revision INTEGER NOT NULL,
                remaining_stock INTEGER NOT NULL, status TEXT NOT NULL
            );
            INSERT OR IGNORE INTO inventory VALUES
                ('A', 'Red Pen', 8, 1), ('B', 'Blue Notebook', 12, 1), ('C', 'Black Notebook', 5, 1);
        """)


def find_candidates(query: str) -> list[Candidate]:
    """Search actual inventory and return validated, bounded candidate rows."""
    initialize_database()
    terms = tuple(term for term in query.casefold().split() if term)
    with sqlite3.connect(_database_path()) as db:
        rows = db.execute(
            "SELECT item_id, label, stock, revision FROM inventory "
            "WHERE stock > 0 ORDER BY item_id"
        ).fetchall()
    matches = [
        row
        for row in rows
        if not terms or all(term in row[1].casefold() for term in terms)
    ]
    return [
        Candidate(identifier=row[0], label=row[1], stock=row[2], revision=row[3])
        for row in matches
    ]


def place_order(
    order_id: str, idempotency_key: str, candidate: Candidate
) -> OrderReceipt:
    """Reserve one unit using revision/stock checks and durable idempotency."""
    initialize_database()
    with sqlite3.connect(_database_path(), timeout=10) as db:
        db.execute("BEGIN IMMEDIATE")
        old = db.execute(
            "SELECT order_id, item_id, item_label, candidate_stock, candidate_revision, remaining_stock, status "
            "FROM orders WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if old is not None:
            if (
                old[0] != order_id
                or old[1] != candidate.identifier
                or old[2] != candidate.label
            ):
                raise ValueError(
                    "Idempotency key already used for a different order or item"
                )
            return OrderReceipt(
                order_id=old[0],
                idempotency_key=idempotency_key,
                candidate=Candidate(
                    identifier=old[1], label=old[2], stock=old[3], revision=old[4]
                ),
                remaining_stock=old[5],
                status=old[6],
            )
        current = db.execute(
            "SELECT label, stock, revision FROM inventory WHERE item_id = ?",
            (candidate.identifier,),
        ).fetchone()
        if current != (candidate.label, candidate.stock, candidate.revision):
            raise ValueError("Inventory candidate is stale; submit a fresh request")
        if current[1] < 1:
            raise ValueError("Item is out of stock")
        remaining = current[1] - 1
        db.execute(
            "UPDATE inventory SET stock = ?, revision = revision + 1 WHERE item_id = ?",
            (remaining, candidate.identifier),
        )
        db.execute(
            "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?, 'reserved')",
            (
                order_id,
                idempotency_key,
                candidate.identifier,
                candidate.label,
                candidate.stock,
                candidate.revision,
                remaining,
            ),
        )
    return OrderReceipt(
        order_id=order_id,
        idempotency_key=idempotency_key,
        candidate=candidate,
        remaining_stock=remaining,
        status="reserved",
    )


find_operation = Operation(
    find_candidates, bind={"query": FromRequest("query")}, output=list[Candidate]
)
order_operation = Operation(
    place_order,
    bind={
        "order_id": FromRequest("order_id"),
        "idempotency_key": FromRequest("idempotency_key"),
        "candidate": AgentChoice(from_result=find_operation),
    },
    output=OrderReceipt,
    after=(find_operation,),
)

summon = Summon("producer-choice-inventory")


@summon("/orders")
def create_order(
    request: OrderRequest,
    candidates=Required(find_operation, calls=Exactly(1)),
    receipt=Required(order_operation, calls=Exactly(1)),
) -> OrderReceipt:
    """Find matching in-stock inventory, reserve exactly one item, and return its receipt."""
    ...


if __name__ == "__main__":
    initialize_database()
    summon.serve(host="127.0.0.1", port=8000)
