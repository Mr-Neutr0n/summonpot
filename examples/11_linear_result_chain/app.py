"""Three-step reservation chain: snapshot, commit, then read its saved receipt."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from pydantic import BaseModel, Field

from summonpot import Exactly, FromRequest, FromResult, Operation, Required, Summon


def database_path() -> Path:
    """Resolve the SQLite location when each operation runs."""
    return Path(os.environ.get("INVENTORY_DB", "inventory.sqlite3"))


class ReservationRequest(BaseModel):
    sku: str = Field(min_length=1)
    quantity: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1)


class InventorySnapshot(BaseModel):
    sku: str
    revision: int
    available: int


class ReservationReference(BaseModel):
    idempotency_key: str


class ReservationReceipt(BaseModel):
    idempotency_key: str
    sku: str
    quantity: int
    inventory_revision: int
    remaining: int
    status: str


def initialize_database(path: Path | None = None) -> None:
    path = database_path() if path is None else path
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS inventory (sku TEXT PRIMARY KEY, revision INTEGER NOT NULL, available INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS reservations (idempotency_key TEXT PRIMARY KEY, sku TEXT NOT NULL, quantity INTEGER NOT NULL, inventory_revision INTEGER NOT NULL, remaining INTEGER NOT NULL, status TEXT NOT NULL);
            INSERT OR IGNORE INTO inventory VALUES ('notebook', 1, 25);
        """)


def lookup_inventory(sku: str) -> InventorySnapshot:
    initialize_database()
    with sqlite3.connect(database_path()) as db:
        row = db.execute(
            "SELECT sku, revision, available FROM inventory WHERE sku = ?", (sku,)
        ).fetchone()
    if row is None:
        raise ValueError("Unknown SKU")
    return InventorySnapshot(sku=row[0], revision=row[1], available=row[2])


def persist_reservation(
    idempotency_key: str, sku: str, quantity: int, revision: int, available: int
) -> ReservationReference:
    initialize_database()
    with sqlite3.connect(database_path(), timeout=10) as db:
        db.execute("BEGIN IMMEDIATE")
        old = db.execute(
            "SELECT sku, quantity FROM reservations WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if old is not None:
            if old != (sku, quantity):
                raise ValueError("Idempotency key already used for another reservation")
        else:
            current = db.execute(
                "SELECT revision, available FROM inventory WHERE sku = ?", (sku,)
            ).fetchone()
            if current != (revision, available):
                raise ValueError(
                    "Inventory changed after lookup; retry with a new request"
                )
            if available < quantity:
                raise ValueError("Insufficient inventory")
            remaining = available - quantity
            next_revision = revision + 1
            db.execute(
                "UPDATE inventory SET revision = ?, available = ? WHERE sku = ?",
                (next_revision, remaining, sku),
            )
            db.execute(
                "INSERT INTO reservations VALUES (?, ?, ?, ?, ?, 'reserved')",
                (idempotency_key, sku, quantity, next_revision, remaining),
            )
    return ReservationReference(idempotency_key=idempotency_key)


def read_saved_receipt(idempotency_key: str) -> ReservationReceipt:
    initialize_database()
    with sqlite3.connect(database_path()) as db:
        row = db.execute(
            "SELECT idempotency_key, sku, quantity, inventory_revision, remaining, status FROM reservations WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
    if row is None:
        raise ValueError("No persisted reservation receipt for reference")
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
    output=ReservationReference,
    after=(lookup_operation,),
)
receipt_operation = Operation(
    read_saved_receipt,
    bind={"idempotency_key": FromResult(reservation_operation, "idempotency_key")},
    output=ReservationReceipt,
    after=(reservation_operation,),
)

summon = Summon("linear-inventory-reservations")


@summon("/reservations")
def reserve_inventory(
    request: ReservationRequest,
    inventory=Required(lookup_operation, calls=Exactly(1)),
    reservation=Required(reservation_operation, calls=Exactly(1)),
    receipt=Required(receipt_operation, calls=Exactly(1)),
) -> ReservationReceipt:
    """Look up stock, persist an idempotent reservation, then read its saved receipt."""
    ...


if __name__ == "__main__":
    initialize_database()
    summon.serve(host="127.0.0.1", port=8000)
