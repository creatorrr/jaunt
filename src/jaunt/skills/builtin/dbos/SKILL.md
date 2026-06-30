---
name: "dbos"
description: "Use when generating durable workflow code with DBOS — @DBOS.workflow / @DBOS.step / @DBOS.transaction decorators, durability and exactly-once semantics, and idempotent step design."
---

# dbos

## What it is
DBOS is a durable execution framework for Python. It records workflow progress so a process
can recover after crashes, continue from completed steps, and avoid re-running work that has
already been committed. Use it for business processes that need reliable orchestration around
databases, queues, external APIs, and background jobs.

Generated DBOS code should make the workflow easy to resume. Put orchestration in workflows,
side effects in steps or transactions, and make every retried operation safe.

## Core concepts
- `@DBOS.workflow()` marks a durable workflow entry point. Workflows should orchestrate and
  call steps, transactions, or child workflows.
- `@DBOS.step()` marks a durable step. A step may be retried after failure, so design it to
  be idempotent or guarded by an idempotency key.
- `@DBOS.transaction()` marks database work that DBOS executes with transactional semantics.
- Workflow inputs and outputs should be serializable because DBOS persists execution state.
- Use workflow IDs or application-level keys to deduplicate requests that may be submitted
  more than once.

## Common patterns
Keep the workflow as a readable sequence and isolate side effects:

```python
from dbos import DBOS


@DBOS.step()
def charge_customer(customer_id: str, cents: int, idempotency_key: str) -> str:
    # Pass the idempotency key to the payment provider.
    return "payment_123"


@DBOS.transaction()
def record_payment(order_id: str, payment_id: str) -> None:
    # Write the durable database state for the completed side effect.
    ...


@DBOS.workflow()
def checkout(order_id: str, customer_id: str, cents: int) -> str:
    payment_id = charge_customer(customer_id, cents, f"checkout:{order_id}")
    record_payment(order_id, payment_id)
    return payment_id
```

Use child steps for operations that can fail independently. Avoid burying important side
effects inside helper functions that are not decorated or not visible in the workflow.

## Gotchas
- A workflow may resume after the Python process exits. Do not depend on in-memory state,
  open sockets, or local locks for correctness.
- Retried steps can call external services again unless the external call is idempotent.
  Always carry stable idempotency keys for payments, emails, webhooks, and queue publishes.
- Keep non-deterministic decisions such as timestamps, random IDs, and external reads inside
  steps or transactions so replay does not change workflow control flow.
- Do not pass unserializable objects such as database connections, clients, or file handles
  through workflow inputs and outputs.
- Database writes that define business state should live in `@DBOS.transaction()` functions
  or otherwise have clear transactional boundaries.

## Testing notes
Unit-test step and transaction functions as normal Python functions with fake clients and
repositories. Workflow tests should assert the orchestration order and idempotency behavior
without requiring a real external provider. Include restart or retry tests when the code
handles payments, notifications, or other non-repeatable effects. Keep fixtures deterministic
so the same workflow input produces the same durable path.
