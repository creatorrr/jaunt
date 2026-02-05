from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: Path) -> dict[str, str]:
    """Parse a tiny subset of .env files (KEY=VALUE, no interpolation)."""

    out: dict[str, str] = {}
    text = path.read_text(encoding="utf-8")
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue

        # Very small quoting support; matches common .env usage.
        if len(value) >= 2 and value[0] in ("'", '"') and value[-1] == value[0]:
            value = value[1:-1]

        out[key] = value
    return out


def load_dotenv_into_environ(path: Path) -> bool:
    """Load `path` into `os.environ`, without overriding existing keys.

    Returns True if the file existed and was parsed, otherwise False.
    """

    if not path.is_file():
        return False

    try:
        vals = load_dotenv(path)
    except OSError:
        return False

    for k, v in vals.items():
        # Do not override the process environment (including empty-but-present keys).
        if k in os.environ:
            continue
        os.environ[k] = v
    return True

