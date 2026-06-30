---
name: "asyncpg"
description: "Use when generating PostgreSQL code with asyncpg — connection pools, parameterized queries ($1 placeholders), transactions, and fetch/execute patterns for async Postgres access."
---

# asyncpg

## What it is
asyncpg is an asyncio-native PostgreSQL driver. Use it when generated code should talk
directly to Postgres without an ORM, especially for explicit SQL, connection pools,
transactions, and high-throughput async services.

Prefer small functions that accept a `Pool` or `Connection` from the caller. That keeps
database access testable and avoids hidden global connections.

## Core concepts
- `asyncpg.connect(...)` opens a single `Connection`; `asyncpg.create_pool(...)` creates a
  reusable `Pool` for web apps and workers.
- Query parameters use PostgreSQL-style numbered placeholders: `$1`, `$2`, `$3`.
- `fetch()` returns a list of `Record` rows, `fetchrow()` returns one row or `None`,
  `fetchval()` returns one scalar value, and `execute()` returns a command-status string.
- `Connection.transaction()` creates an async context manager that commits on success and
  rolls back on exception.
- Prepared statements and type codecs are available when hot paths or custom PostgreSQL
  types need explicit handling.

## Common patterns
Create one pool at application startup and close it at shutdown:

```python
import asyncpg


async def make_pool(dsn: str) -> asyncpg.Pool:
    return await asyncpg.create_pool(dsn, min_size=1, max_size=10)


async def close_pool(pool: asyncpg.Pool) -> None:
    await pool.close()
```

Acquire a connection for each unit of work and bind values with `$N` placeholders:

```python
async def get_user_email(pool: asyncpg.Pool, user_id: int) -> str | None:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "select email from users where id = $1",
            user_id,
        )
```

Use transactions for multi-statement changes:

```python
async def transfer(conn: asyncpg.Connection, source: int, dest: int, cents: int) -> None:
    async with conn.transaction():
        await conn.execute(
            "update accounts set balance_cents = balance_cents - $1 where id = $2",
            cents,
            source,
        )
        await conn.execute(
            "update accounts set balance_cents = balance_cents + $1 where id = $2",
            cents,
            dest,
        )
```

## Gotchas
- Do not use `%s`, `?`, or f-string interpolation for values; asyncpg uses `$1` style
  placeholders and sends values separately.
- Never interpolate untrusted table or column names. If identifiers must be dynamic,
  validate them against an allowlist before formatting SQL.
- Close pools explicitly. Leaked pools keep sockets open and can make tests hang.
- `Record` behaves like both a tuple and mapping, but converting rows to plain dicts at
  module boundaries often makes APIs easier to test.
- Type codecs are connection-local setup; register them on each new pool connection with
  an `init=` callback when needed.

## Testing notes
Unit tests should inject a fake connection or pool object with async `fetch*` and `execute`
methods, or test the SQL-building layer separately from asyncpg. Integration tests need a
real PostgreSQL database and should create isolated schemas or wrap each case in a rollback
transaction. Assert that generated SQL uses `$N` placeholders and that pool acquisition,
transactions, and shutdown paths are awaited.
