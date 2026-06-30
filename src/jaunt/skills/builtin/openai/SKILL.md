---
name: "openai"
description: "Use when generating code that calls the OpenAI Python SDK — client construction, chat/responses APIs, structured outputs, streaming, and sync vs async clients."
---

# openai

## What it is
The OpenAI Python SDK provides typed sync and async clients for OpenAI APIs. Use it when
generated code needs model calls, structured outputs, streaming responses, embeddings, file
uploads, or other OpenAI platform operations.

Keep OpenAI calls behind a narrow application interface. Pass the client in, keep prompts and
schemas versioned, and make network behavior easy to mock in tests.

## Core concepts
- `OpenAI()` is the synchronous client; `AsyncOpenAI()` is the asyncio client.
- The Responses API is the general-purpose text, multimodal, tool, and structured-output
  interface.
- Chat completions are still common in existing code and use `client.chat.completions`.
- Structured outputs use a JSON schema or SDK parsing helpers so callers receive validated
  data instead of free-form text.
- Streaming returns incremental events or chunks; callers must consume and assemble them.

## Common patterns
Construct the client once and pass it to functions:

```python
from openai import OpenAI


def summarize(client: OpenAI, text: str) -> str:
    response = client.responses.create(
        model="gpt-4.1-mini",
        input=f"Summarize this in one sentence:\n\n{text}",
    )
    return response.output_text
```

Use the async client in async web services:

```python
from openai import AsyncOpenAI


async def classify(client: AsyncOpenAI, text: str) -> str:
    response = await client.responses.create(
        model="gpt-4.1-mini",
        input=[
            {"role": "system", "content": "Return one label: bug, feature, or support."},
            {"role": "user", "content": text},
        ],
    )
    return response.output_text.strip()
```

For structured outputs, define an explicit schema or model and reject malformed results
instead of parsing prose with regular expressions.

## Gotchas
- Do not create a new client for every helper call in hot paths. Inject it from application
  startup or a request-scoped dependency.
- Never commit API keys. The SDK reads `OPENAI_API_KEY` by default; pass explicit credentials
  only from secret-managed configuration.
- Do not mix sync clients inside async endpoints; use `AsyncOpenAI` to avoid blocking the
  event loop.
- Streaming code must handle partial events, cancellation, and cleanup.
- Model names, token limits, and API capabilities can change. Keep them configurable where
  product behavior depends on them.

## Testing notes
Unit tests should mock the client method that the code calls and return small objects with
the accessed fields, such as `output_text`. Test prompt construction, schema handling,
retry/error mapping, and sync versus async paths without making network calls. Add contract
tests around your own wrapper so application code does not depend on many SDK response
details.
