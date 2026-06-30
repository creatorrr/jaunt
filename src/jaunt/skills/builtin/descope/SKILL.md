---
name: "descope"
description: "Use when generating authentication/authorization code with the Descope SDK — session and JWT validation, auth methods, and tenant/role/permission checks."
---

# descope

## What it is
Descope is an authentication and user-management platform with a Python SDK for validating
sessions, checking JWTs, and calling management APIs. Use it when generated code needs to
protect web routes, identify the current user, or enforce tenant, role, or permission rules.

Authentication code should be small, explicit, and easy to replace in tests. Keep SDK calls
behind a thin boundary and pass the validated claims to application logic.

## Core concepts
- `DescopeClient(project_id=...)` is the main SDK client.
- Session validation checks a session JWT or bearer token and returns validated claims or
  raises an SDK exception.
- JWT validation verifies token signature and claims for non-cookie or service-to-service
  flows.
- Authorization commonly depends on tenant membership plus roles or permissions in the
  validated claims.
- Management APIs need a management key and should not run in request handlers unless the
  operation really needs privileged access.

## Common patterns
Validate a bearer token at the web boundary and pass claims inward:

```python
from descope import DescopeClient


descope_client = DescopeClient(project_id="P2abc123")


def require_session(authorization: str) -> dict:
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise PermissionError("missing bearer token")
    return descope_client.validate_session(session_token=token)
```

Check tenant and permission claims before doing tenant-scoped work:

```python
def require_permission(claims: dict, tenant_id: str, permission: str) -> str:
    tenants = claims.get("tenants", {})
    tenant_claims = tenants.get(tenant_id) or {}
    permissions = set(tenant_claims.get("permissions", []))
    if permission not in permissions:
        raise PermissionError("permission denied")
    return str(claims["sub"])
```

Keep login and auth-method flows separate from validation. For example, OTP, magic-link,
OAuth, and SSO endpoints should call the relevant SDK auth method and then return the
provider response or session token according to the app's own API contract.

## Gotchas
- Never trust decoded JWT payloads without SDK validation. Signature, issuer, audience, and
  expiry checks must happen before claims are used.
- Tenant roles and project-level roles are not the same. Verify which claim shape the app
  uses and check the tenant-specific entry for tenant-scoped resources.
- Do not log raw JWTs, refresh tokens, one-time codes, or magic-link URLs.
- Management keys are privileged secrets. Load them from the environment and keep management
  clients out of browser-facing code.
- Map SDK validation failures to consistent HTTP 401 responses and authorization failures to
  HTTP 403 responses.

## Testing notes
Mock the Descope client at the boundary and test accepted claims, missing tokens, expired or
invalid sessions, wrong tenants, and missing permissions. Unit tests should not call Descope
over the network. For route tests, inject a fake validator that returns a representative
claims dictionary and assert that downstream code receives only the user and tenant data it
needs.
