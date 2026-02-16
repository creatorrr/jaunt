# Jaunt Spec & Test Examples

## Minimal Project Setup

```toml
# jaunt.toml
version = 1

[paths]
source_roots = ["src"]
test_roots = ["tests"]
generated_dir = "__generated__"

[llm]
provider = "openai"
model = "gpt-5.2"
api_key_env = "OPENAI_API_KEY"
```

## Example 1: Pure Function Spec

```python
# src/my_app/text.py
from __future__ import annotations
import jaunt

@jaunt.magic()
def slugify(title: str) -> str:
    """
    Convert a title to a URL-safe slug.

    Rules:
    - Lowercase the input.
    - Trim leading/trailing whitespace.
    - Replace runs of whitespace with a single "-".
    - Remove characters that are not ASCII letters, digits, "-" or "_".

    Errors:
    - Raise ValueError if result is empty after filtering.

    Examples:
    - "Hello World" -> "hello-world"
    - "  A  B  " -> "a-b"
    """
    raise RuntimeError("spec stub (generated at build time)")
```

## Example 2: Function With Dependencies

```python
# src/my_app/email.py
from __future__ import annotations
import jaunt

@jaunt.magic()
def normalize_email(raw: str) -> str:
    """
    Normalize an email address.

    Rules:
    - Strip surrounding whitespace, lowercase everything.
    - Must contain exactly one "@" with non-empty local and domain parts.
    - Domain must contain at least one ".".

    Errors:
    - Raise ValueError if invalid.
    """
    raise RuntimeError("spec stub (generated at build time)")

@jaunt.magic(deps=[normalize_email])
def is_corporate_email(raw: str, *, domain: str = "example.com") -> bool:
    """
    Return True iff normalize_email(raw) belongs to domain.

    - Normalizes domain via .strip().lower() before comparison.
    - Propagates ValueError from normalize_email unchanged.
    """
    raise RuntimeError("spec stub (generated at build time)")
```

## Example 3: Stateful Class

```python
# src/my_app/cache.py
from __future__ import annotations
from dataclasses import dataclass
import jaunt

@jaunt.magic()
@dataclass
class LRUCache:
    """
    Fixed-capacity least-recently-used cache.

    Parameters:
    - capacity: max items (>= 1)

    Behavior:
    - get(key) -> value | None. Marks key as most-recently-used.
    - set(key, value) -> None. Evicts LRU if at capacity.
    - size() -> int. Current number of keys.

    Constraints:
    - All operations O(1) average-case.
    """
    capacity: int

    def get(self, key: str) -> object | None:
        """See class docstring."""
        raise NotImplementedError

    def set(self, key: str, value: object) -> None:
        """See class docstring."""
        raise NotImplementedError

    def size(self) -> int:
        """See class docstring."""
        raise NotImplementedError
```

## Example 4: Chained Dependencies (JWT Auth)

```python
# src/jwt_demo/specs.py
from __future__ import annotations
from datetime import timedelta
import jaunt
from pydantic import BaseModel

class Claims(BaseModel):
    sub: str
    iat: float
    exp: float

@jaunt.magic()
def create_token(user_id: str, secret: str, *, ttl: timedelta = timedelta(hours=1)) -> str:
    """
    Create HS256-signed JWT.
    - Header: {"alg": "HS256", "typ": "JWT"}
    - Payload: {"sub": user_id, "iat": <now>, "exp": <now + ttl>}
    - base64url encoding, no padding.
    - Raise ValueError if user_id is empty.
    """
    raise RuntimeError("spec stub (generated at build time)")

@jaunt.magic(deps=[create_token, Claims])
def verify_token(token: str, secret: str) -> Claims:
    """
    Verify HS256 JWT and return Claims.
    - Raise ValueError("malformed") if structure wrong.
    - Raise ValueError("invalid signature") if HMAC mismatch.
    - Raise ValueError("expired") if exp <= now.
    """
    raise RuntimeError("spec stub (generated at build time)")

@jaunt.magic(deps=[create_token, verify_token])
def rotate_token(token: str, secret: str, *, ttl: timedelta = timedelta(hours=1)) -> str:
    """Verify old token, issue fresh one for same subject with new ttl."""
    raise RuntimeError("spec stub (generated at build time)")
```

## Example 5: Test Specs

```python
# tests/test_jwt.py
from __future__ import annotations
import jaunt

@jaunt.test()
def test_roundtrip_create_and_verify() -> None:
    """
    Roundtrip:
    - token = create_token("user-42", "s3cret")
    - claims = verify_token(token, "s3cret")
    - assert claims.sub == "user-42"
    - assert claims.exp > claims.iat
    """
    raise AssertionError("spec stub (generated at test time)")

@jaunt.test()
def test_expired_token_raises() -> None:
    """
    - create_token("user-42", "s3cret", ttl=timedelta(seconds=-1))
    - verify_token raises ValueError("expired")
    """
    raise AssertionError("spec stub (generated at test time)")

@jaunt.test()
def test_wrong_secret_raises() -> None:
    """
    - create_token("user-42", "s3cret")
    - verify_token(token, "different") raises ValueError("invalid signature")
    """
    raise AssertionError("spec stub (generated at test time)")
```

## Example 6: Using `prompt=` for Extra Guidance

```python
@jaunt.magic(prompt="Use the `re` module for regex matching. Prefer compiled patterns.")
def extract_emails(text: str) -> list[str]:
    """
    Extract all email addresses from text.
    Return deduplicated list in order of first appearance.
    """
    raise RuntimeError("spec stub (generated at build time)")
```

## Example 7: Async Function

```python
from collections.abc import Awaitable, Callable
import jaunt

@jaunt.magic()
async def retry(
    op: Callable[[], Awaitable[object]],
    *,
    attempts: int,
    base_delay_s: float,
) -> object:
    """
    Retry async operation with exponential backoff.
    - Run op() up to `attempts` times (>= 1).
    - Delay: base_delay_s * 2^(i-1) for retries i=1..(attempts-1).
    - Return result on success.
    - Re-raise last exception if all attempts fail.
    """
    raise RuntimeError("spec stub (generated at build time)")
```

## Workflow Summary

```bash
# 1. Initialize project (if new)
jaunt init

# 2. Write specs in source_roots, tests in test_roots

# 3. Build implementations
jaunt build

# 4. Generate and run tests
jaunt test

# 5. Check what needs rebuilding
jaunt status

# 6. Force full rebuild
jaunt build --force

# 7. Clean generated files
jaunt clean

# 8. Watch for changes
jaunt watch --test
```
