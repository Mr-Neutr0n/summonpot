# Three-step linear result chain: inventory reservation

This example demonstrates three real, application-owned SQLite operations: (1) look up an inventory snapshot, (2) atomically reserve against that exact revision and return only an intermediate idempotency-key reference, and (3) independently read the persisted receipt by that reference. The terminal read—not model-authored text—is the endpoint response. `FromResult` supplies validated outputs and `after` declares the ordering.

The write checks the snapshot revision under a SQLite write transaction, rejects stale inventory and insufficient stock, and treats a repeated idempotency key with matching SKU/quantity as the same reservation. A different payload for that key fails. If the terminal read or response fails after the middle operation commits, the reservation remains persisted; retrying with the same key can recover its receipt. No rollback across that boundary is claimed.

## Run

From the repository root, use the locked environment and serving, CLI, and provider extras (Python 3.13 shown):

```sh
env -u PYTHONPATH -u VIRTUAL_ENV UV_PYTHON=3.13 uv sync --locked --extra serve --extra cli --extra openai
export OPENAI_API_KEY=...
export SUMMONPOT_MODEL=openai:gpt-4o-mini  # or another configured provider:model
export INVENTORY_DB="$PWD/inventory.sqlite3"
env -u PYTHONPATH -u VIRTUAL_ENV UV_PYTHON=3.13 uv run summonpot serve examples/11_linear_result_chain/app.py --host 127.0.0.1 --port 8000
```

In another shell, make a reservation (use a unique key for a new reservation):

```sh
curl -sS -X POST http://127.0.0.1:8000/reservations \
  -H 'content-type: application/json' \
  -d '{"sku":"notebook","quantity":2,"idempotency_key":"order-2026-001"}'
```

Verify the authoritative stored receipt independently of the returned HTTP body:

```sh
sqlite3 "$INVENTORY_DB" \
  "SELECT idempotency_key, sku, quantity, inventory_revision, remaining, status FROM reservations WHERE idempotency_key='order-2026-001';"
```

Production model selection uses normal `SUMMONPOT_MODEL` configuration. The tests use a scripted keyless Pydantic AI `FunctionModel` solely as a deterministic harness; it is not installed as the example's provider and does not replace the real runtime, operations, SQLite, or HTTP boundary.
