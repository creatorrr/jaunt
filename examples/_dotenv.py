from __future__ import annotations

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

        if len(value) >= 2 and value[0] in ("'", '"') and value[-1] == value[0]:
            value = value[1:-1]

        out[key] = value
    return out


def ensure_openai_key(env: dict[str, str], repo_root: Path) -> dict[str, str]:
    """Ensure OPENAI_API_KEY is present, loading <repo_root>/.env as a fallback."""

    merged = dict(env)
    key = (merged.get("OPENAI_API_KEY") or "").strip()
    if key:
        return merged

    dotenv_path = repo_root / ".env"
    if dotenv_path.is_file():
        try:
            vals = load_dotenv(dotenv_path)
        except OSError:
            vals = {}
        for k, v in vals.items():
            if k not in merged:
                merged[k] = v

    key = (merged.get("OPENAI_API_KEY") or "").strip()
    if not key:
        raise RuntimeError(
            "Missing OPENAI_API_KEY. Set it in the environment or add it to <repo_root>/.env."
        )
    return merged
