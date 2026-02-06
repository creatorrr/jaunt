from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PyPIReadmeError(RuntimeError):
    message: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


def fetch_readme(dist: str, version: str, *, timeout_s: float = 10.0) -> tuple[str, str]:
    """Fetch the PyPI README/description for the exact dist+version.

    Returns: (description_text, description_content_type)
    """

    dist = (dist or "").strip()
    version = (version or "").strip()
    if not dist or not version:
        raise PyPIReadmeError("dist and version must be non-empty.")

    url = f"https://pypi.org/pypi/{dist}/{version}/json"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "jaunt-skillgen/0.1",
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(req, timeout=float(timeout_s)) as resp:
            data = json.load(resp)
    except Exception as e:  # noqa: BLE001 - caller handles warnings
        raise PyPIReadmeError(
            f"Failed fetching PyPI JSON for {dist}=={version}: {type(e).__name__}: {e}"
        ) from e

    info = data.get("info") if isinstance(data, dict) else None
    if not isinstance(info, dict):
        raise PyPIReadmeError(f"PyPI response missing 'info' for {dist}=={version}.")

    desc = info.get("description")
    ctype = info.get("description_content_type")
    text = desc if isinstance(desc, str) else ""
    content_type = ctype if isinstance(ctype, str) and ctype else "text/plain"

    if not text.strip():
        raise PyPIReadmeError(f"PyPI README missing/empty for {dist}=={version}.")

    return text, content_type
