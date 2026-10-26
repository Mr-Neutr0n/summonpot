# Three-step linear result chain: inventory reservation

This example demonstrates a model-free, three-operation SQLite chain: (1) look up an inventory snapshot, (2) atomically reserve against that exact revision and return only an intermediate idempotency-key reference, and (3) independently read the persisted receipt by that reference. The terminal read—not model-authored text—is the endpoint response. `FromResult` supplies validated outputs and `after` declares the ordering. No provider credential or model selection is needed for this fully resolved chain.

The write checks the snapshot revision under a SQLite write transaction, rejects stale inventory and insufficient stock, and treats a repeated idempotency key with matching SKU/quantity as the same reservation. A different payload for that key fails. If the terminal read or response fails after the middle operation commits, the reservation remains persisted; retrying with the same key can recover its receipt. No rollback across that boundary is claimed.

## Run

This model-free path is included from 0.12.0; historical v0.11.0 runs these chains through
the agent runtime. From the repository checkout, use the locked environment and serving
and CLI extras (Python 3.13 shown):

```sh
env -u PYTHONPATH -u VIRTUAL_ENV UV_PYTHON=3.13 uv sync --locked --extra serve --extra cli
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

No `SUMMONPOT_MODEL` setting or provider extra is needed on this path. Chains with an
agent-owned choice still require normal model configuration. SQLite transactions and
cross-request idempotency remain application-owned, not framework database adapters.
