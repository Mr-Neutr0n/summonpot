"""A two-step inventory reservation backed by application-owned SQLite code."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from pydantic import BaseModel, Field

from summonpot import Exactly, FromRequest, FromResult, Operation, Required, Summon

DB_PATH = Path(os.environ.get("INVENTORY_DB", "inventory.sqlite3"))


class ReservationRequest(BaseModel):
    sku: str = Field(min_length=1)
    quantity: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1)


class InventorySnapshot(BaseModel):
    sku: str
    revision: int
    available: int


class ReservationReceipt(BaseModel):
    idempotency_key: str
    sku: str
    quantity: int
    inventory_revision: int
    remaining: int
    status: str


def initialize_database(path: Path | None = None) -> None:
    """Create the small application-owned inventory and reservation tables."""
    path = DB_PATH if path is None else path
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS inventory (
                sku TEXT PRIMARY KEY, revision INTEGER NOT NULL, available INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reservations (
                idempotency_key TEXT PRIMARY KEY, sku TEXT NOT NULL, quantity INTEGER NOT NULL,
                inventory_revision INTEGER NOT NULL, remaining INTEGER NOT NULL, status TEXT NOT NULL
            );
            INSERT OR IGNORE INTO inventory(sku, revision, available) VALUES ('notebook', 1, 25);
            """
        )


def lookup_inventory(sku: str) -> InventorySnapshot:
    """Read the current stock and revision for the requested SKU."""
    initialize_database()
    with sqlite3.connect(DB_PATH) as connection:
        row = connection.execute(
            "SELECT sku, revision, available FROM inventory WHERE sku = ?", (sku,)
        ).fetchone()
    if row is None:
        raise ValueError("Unknown SKU")
    return InventorySnapshot(sku=row[0], revision=row[1], available=row[2])


def persist_reservation(
    idempotency_key: str, sku: str, quantity: int, revision: int, available: int
) -> ReservationReceipt:
    """Atomically reserve stock and persist the canonical idempotent receipt."""
    initialize_database()
    with sqlite3.connect(DB_PATH, timeout=10) as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT idempotency_key, sku, quantity, inventory_revision, remaining, status "
            "FROM reservations WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if existing:
            if existing[1] != sku or existing[2] != quantity:
                raise ValueError("Idempotency key already used for another reservation")
            row = existing
        else:
            current = connection.execute(
                "SELECT revision, available FROM inventory WHERE sku = ?", (sku,)
            ).fetchone()
            if current is None or current != (revision, available):
                raise ValueError(
                    "Inventory changed after lookup; retry with a new request"
                )
            if available < quantity:
                raise ValueError("Insufficient inventory")
            remaining = available - quantity
            connection.execute(
                "UPDATE inventory SET revision = revision + 1, available = ? WHERE sku = ?",
                (remaining, sku),
            )
            connection.execute(
                "INSERT INTO reservations VALUES (?, ?, ?, ?, ?, 'reserved')",
                (idempotency_key, sku, quantity, revision + 1, remaining),
            )
            row = (idempotency_key, sku, quantity, revision + 1, remaining, "reserved")
    return ReservationReceipt(
        idempotency_key=row[0],
        sku=row[1],
        quantity=row[2],
        inventory_revision=row[3],
        remaining=row[4],
        status=row[5],
    )


lookup_operation = Operation(
    lookup_inventory, bind={"sku": FromRequest("sku")}, output=InventorySnapshot
)
reservation_operation = Operation(
    persist_reservation,
    bind={
        "idempotency_key": FromRequest("idempotency_key"),
        "quantity": FromRequest("quantity"),
        "sku": FromResult(lookup_operation, "sku"),
        "revision": FromResult(lookup_operation, "revision"),
        "available": FromResult(lookup_operation, "available"),
    },
    output=ReservationReceipt,
    after=(lookup_operation,),
)

summon = Summon("inventory-reservations")


@summon("/reservations")
def reserve_inventory(
    request: ReservationRequest,
    inventory=Required(lookup_operation, calls=Exactly(1)),
    receipt=Required(reservation_operation, calls=Exactly(1)),
) -> ReservationReceipt:
    """Look up inventory, reserve the requested quantity exactly once, and return the persisted receipt."""
    ...


if __name__ == "__main__":
    initialize_database()
    summon.serve(host="127.0.0.1", port=8000)
