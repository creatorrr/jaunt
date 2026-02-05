from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from _dotenv import ensure_openai_key


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _with_repo_pythonpath(env: dict[str, str], repo_root: Path) -> dict[str, str]:
    merged = dict(env)
    src = str(repo_root / "src")
    cur = merged.get("PYTHONPATH") or ""
    if cur:
        parts = cur.split(os.pathsep)
        if src not in parts:
            merged["PYTHONPATH"] = src + os.pathsep + cur
    else:
        merged["PYTHONPATH"] = src
    return merged


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _run(cmd: list[str], *, cwd: Path, env: dict[str, str]) -> int:
    proc = subprocess.run(cmd, cwd=str(cwd), env=env, check=False)
    return int(proc.returncode)


def _scaffold(project_dir: Path) -> None:
    _write(
        project_dir / "jaunt.toml",
        "\n".join(
            [
                "version = 1",
                "",
                "[paths]",
                "source_roots = [\"src\"]",
                "test_roots = [\"tests\"]",
                "generated_dir = \"__generated__\"",
                "",
                "[llm]",
                "provider = \"openai\"",
                "model = \"gpt-5.2\"",
                "api_key_env = \"OPENAI_API_KEY\"",
                "",
                "[test]",
                "pytest_args = [\"-q\"]",
                "",
            ]
        ),
    )

    _write(
        project_dir / "src" / "dice_demo" / "__init__.py",
        "\n".join(
            [
                "from .specs import parse_dice, roll",
                "",
                "__all__ = [\"parse_dice\", \"roll\"]",
                "",
            ]
        ),
    )

    _write(
        project_dir / "src" / "dice_demo" / "specs.py",
        "\n".join(
            [
                "from __future__ import annotations",
                "",
                "import random",
                "",
                "import jaunt",
                "",
                "",
                "@jaunt.magic()",
                "def parse_dice(expr: str) -> tuple[int, int, int]:",
                "    \"\"\"",
                "    Parse dice expressions like \"d6\", \"2d6+3\", \"2d6-1\".",
                "",
                "    Return (count, sides, bonus).",
                "",
                "    Rules:",
                "    - Allow surrounding whitespace.",
                "    - \"d6\" means (1, 6, 0).",
                "    - count and sides must be >= 1.",
                "    - bonus defaults to 0 and may be negative.",
                "    - Raise ValueError on invalid syntax.",
                "    \"\"\"",
                "    raise RuntimeError(\"spec stub (generated at build time)\")",
                "",
                "",
                "@jaunt.magic(deps=parse_dice)",
                "def roll(expr: str, *, rng: random.Random) -> int:",
                "    \"\"\"",
                "    Roll a dice expression using a provided RNG and return the total.",
                "",
                "    - Uses parse_dice(expr) to parse inputs.",
                "    - Rolls `count` times with rng.randint(1, sides).",
                "    - Returns sum(rolls) + bonus.",
                "",
                "    Determinism example:",
                "    - With rng=random.Random(0), roll(\"2d6+3\", rng=rng) == 11.",
                "    \"\"\"",
                "    raise RuntimeError(\"spec stub (generated at build time)\")",
                "",
            ]
        ),
    )

    _write(project_dir / "tests" / "__init__.py", "")
    _write(
        project_dir / "tests" / "specs.py",
        "\n".join(
            [
                "from __future__ import annotations",
                "",
                "import random",
                "",
                "import jaunt",
                "",
                "from dice_demo import parse_dice, roll",
                "",
                "",
                "@jaunt.test()",
                "def test_parse_dice_variants() -> None:",
                "    \"\"\"",
                "    parse_dice should accept:",
                "    - \"d6\" -> (1, 6, 0)",
                "    - \"2d6+3\" -> (2, 6, 3)",
                "    - \"2d6-1\" -> (2, 6, -1)",
                "    - whitespace around tokens, ex: \"  2d6 + 3  \"",
                "    and raise ValueError on invalid inputs.",
                "    \"\"\"",
                "    raise AssertionError(\"spec stub (generated at test time)\")",
                "",
                "",
                "@jaunt.test()",
                "def test_roll_is_deterministic_with_seeded_rng() -> None:",
                "    \"\"\"",
                "    With rng=random.Random(0), the first two d6 rolls are 4 and 4, so:",
                "    - roll(\"2d6+3\", rng=rng) == 11",
                "    \"\"\"",
                "    raise AssertionError(\"spec stub (generated at test time)\")",
                "",
            ]
        ),
    )


def _print_head(path: Path, *, lines: int = 40) -> None:
    try:
        text = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    head = "\n".join(text[:lines])
    print(f"\n=== {path} (first {lines} lines) ===\n{head}\n")


def _run_demo(project_dir: Path, *, run_tests: bool, keep: bool) -> int:
    repo_root = _repo_root()
    env = ensure_openai_key(dict(os.environ), repo_root)
    env = _with_repo_pythonpath(env, repo_root)

    _scaffold(project_dir)
    print(f"Project directory: {project_dir}")

    rc = _run([sys.executable, "-m", "jaunt", "build"], cwd=project_dir, env=env)
    impl = project_dir / "src" / "dice_demo" / "__generated__" / "specs.py"
    if rc != 0:
        print(f"Build failed (exit {rc}).")
        return rc

    _print_head(impl, lines=40)

    if not run_tests:
        return 0

    # Avoid calling the build backend twice; we already built above.
    rc = _run(
        [sys.executable, "-m", "jaunt", "test", "--no-build", "--pytest-args=-q"],
        cwd=project_dir,
        env=env,
    )
    if rc != 0:
        print(f"Tests failed (exit {rc}).")
        return rc

    gen_tests = project_dir / "tests" / "__generated__" / "specs.py"
    print(f"Generated tests: {gen_tests}")
    if keep:
        print("Temp project kept (omit --keep to auto-cleanup).")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="demo_on_the_fly.py")
    p.add_argument("--test", action="store_true", help="Also run `jaunt test`.")
    p.add_argument("--keep", action="store_true", help="Keep the temp project directory.")
    ns = p.parse_args(list(sys.argv[1:] if argv is None else argv))

    if ns.keep:
        project_dir = Path(tempfile.mkdtemp(prefix="jaunt-demo-"))
        return _run_demo(project_dir, run_tests=bool(ns.test), keep=True)

    with tempfile.TemporaryDirectory(prefix="jaunt-demo-") as td:
        project_dir = Path(td)
        return _run_demo(project_dir, run_tests=bool(ns.test), keep=False)


if __name__ == "__main__":
    raise SystemExit(main())
