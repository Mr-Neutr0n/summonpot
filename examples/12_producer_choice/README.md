# Producer-constrained inventory choice

This runnable example gives one fixed endpoint goal: find real in-stock SQLite inventory matching the request text, select one of those candidates, reserve one unit, and return the database-backed receipt. The request contains only business data (`query`, `order_id`, and `idempotency_key`); callers cannot choose an action or operation. The producer returns `list[Candidate]`; the consumer receives the exact canonical candidate selected through a request-local opaque token, not a model-authored object or identifier.

The order write uses a fresh SQLite connection per call and `BEGIN IMMEDIATE`. It verifies that the candidate's label, stock, and revision still match the producer snapshot before decrementing stock and inserting the order. The idempotency key is unique: a retry with the same order and item returns the stored receipt without another decrement; reusing it for a different order or item is rejected. A stale inventory candidate fails rather than reserving against outdated evidence. This two-step flow's unique terminal operation is the write itself, which returns the typed receipt persisted by that operation.

Choice domains are bounded by the runtime: an empty or over-128-item candidate list, or projected evidence over 65,536 characters, is unavailable and maps to HTTP 409 before the choice step. The example intentionally does not claim unbounded search, live provider credentials, or a live-provider test. Production runs use normal `SUMMONPOT_MODEL` configuration. The tests install a keyless scripted Pydantic AI `FunctionModel` solely to exercise the real runtime/tool schema/HTTP/SQLite path deterministically; it parses the candidate evidence and selects the requested matching candidate, rather than inventing a result.

## Run

From the repository root with Python 3.13 and the committed lockfile:

```sh
env -u PYTHONPATH -u VIRTUAL_ENV UV_PYTHON=3.13 uv sync --locked --extra serve --extra cli --extra openai
export INVENTORY_DB="$PWD/inventory.sqlite3"
export OPENAI_API_KEY=...                         # configure your provider credential
export SUMMONPOT_MODEL=openai:gpt-4o-mini         # or another supported provider:model
env -u PYTHONPATH -u VIRTUAL_ENV UV_PYTHON=3.13 uv run summonpot serve examples/12_producer_choice/app.py --host 127.0.0.1 --port 8000
```

In another terminal, submit business data (replace the idempotency key for a new order):

```sh
curl -sS -X POST http://127.0.0.1:8000/orders \
  -H 'content-type: application/json' \
  -d '{"query":"blue notebook","order_id":"order-2026-001","idempotency_key":"order-key-2026-001"}'
```

Independently verify the persisted row and stock change with SQLite:

```sh
sqlite3 "$INVENTORY_DB" \
  "SELECT order_id,idempotency_key,item_id,item_label,candidate_stock,candidate_revision,remaining_stock,status FROM orders WHERE order_id='order-2026-001';"
sqlite3 "$INVENTORY_DB" \
  "SELECT item_id,label,stock,revision FROM inventory WHERE item_id='B';"
```

Run the real-runtime HTTP example tests with the same locked environment:

```sh
env -u PYTHONPATH -u VIRTUAL_ENV UV_PYTHON=3.13 uv run pytest tests/test_producer_choice_example.py -q
```
