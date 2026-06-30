---
name: "starlette"
description: "Use when generating ASGI web code with Starlette — routes, Request/Response, async endpoints, middleware, and application startup/shutdown."
---

# starlette

## What it is
Starlette is a lightweight ASGI framework for Python web applications and services. Use it
when generated code needs async HTTP endpoints, middleware, request and response handling,
WebSockets, background tasks, static files, or application lifespan setup.

Starlette code should keep request parsing at the edge and move business logic into plain
functions that are easy to test without an ASGI server.

## Core concepts
- `Starlette(routes=[...])` creates an ASGI app.
- `Route(path, endpoint, methods=[...])` maps HTTP requests to sync or async endpoint
  functions.
- `Request` exposes path params, query params, headers, body, and JSON parsing.
- `Response`, `JSONResponse`, `PlainTextResponse`, and redirects define outgoing responses.
- Middleware wraps requests for cross-cutting behavior such as CORS, sessions, auth, and
  error handling.
- Lifespan startup and shutdown are used for pools, clients, caches, and other shared state.

## Common patterns
Define async endpoints and explicit responses:

```python
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


async def get_user(request: Request) -> JSONResponse:
    user_id = request.path_params["user_id"]
    return JSONResponse({"id": user_id})


app = Starlette(
    routes=[
        Route("/health", health),
        Route("/users/{user_id}", get_user, methods=["GET"]),
    ],
)
```

Use lifespan for shared resources:

```python
from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app: Starlette):
    app.state.ready = True
    yield
    app.state.ready = False


app = Starlette(routes=[Route("/health", health)], lifespan=lifespan)
```

## Gotchas
- `await request.json()` can raise for invalid JSON. Map parse errors to a clear `400`
  response when accepting request bodies.
- Do not block the event loop with sync database calls, CPU-heavy work, or sync HTTP clients
  inside async endpoints.
- Middleware order matters because wrappers are nested around the app.
- Store application-wide resources on `app.state`, not module globals, when tests create
  multiple app instances.
- Return Starlette response objects or ASGI-compatible responses; do not return plain dicts
  unless a higher-level framework layer converts them.

## Testing notes
Use `starlette.testclient.TestClient` for sync tests around routing and response behavior, or
an async ASGI client when testing async integration. Build the app in a factory so each test
gets isolated state. Test status codes, JSON bodies, headers, middleware behavior, lifespan
setup, and error mapping without starting a real network server.
