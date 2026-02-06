from __future__ import annotations

import argparse
import os
import subprocess
import sys
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


def _example_map(repo_root: Path) -> dict[str, tuple[Path, str]]:
    # (project_dir, top-level package name)
    base = repo_root / "examples"
    return {
        "slugify": (base / "01_slugify", "slugify_demo"),
        "lru": (base / "02_lru_cache", "cache_demo"),
        "dice": (base / "03_dice_roller", "dice_demo"),
        "pydantic": (base / "04_pydantic_validation", "pydantic_demo"),
        "jwt": (base / "jwt_auth", "jwt_demo"),
        "markdown": (base / "markdown_render", "md_demo"),
        "limiter": (base / "rate_limiter", "limiter_demo"),
        "csv": (base / "csv_parser", "csv_demo"),
        "diff": (base / "diff_engine", "diff_demo"),
        "expr": (base / "expr_eval", "expr_demo"),
        "toy": (base / "toy_app", "toy_app"),
    }


def _run(args: list[str], *, cwd: Path, env: dict[str, str]) -> int:
    proc = subprocess.run(args, cwd=str(cwd), env=env, check=False)
    return int(proc.returncode)


def _print_outputs(project_dir: Path, pkg: str) -> None:
    print(
        "Generated implementations under:",
        project_dir / "src" / pkg / "__generated__",
    )
    print("Generated tests under:", project_dir / "tests" / "__generated__")
    print("Generated skills under:", project_dir / ".agents" / "skills")


def main(argv: list[str] | None = None) -> int:
    examples = _example_map(_repo_root())
    p = argparse.ArgumentParser(prog="run_example.py")
    p.add_argument("example", choices=sorted(examples))

    sub = p.add_subparsers(dest="command", required=True)

    build_p = sub.add_parser("build", help="Run `jaunt build` for the example project.")
    build_p.add_argument("--force", action="store_true")

    test_p = sub.add_parser("test", help="Run `jaunt test` for the example project.")
    test_p.add_argument("--force", action="store_true")
    test_p.add_argument("--no-run", action="store_true", help="Skip running pytest.")

    ns = p.parse_args(list(sys.argv[1:] if argv is None else argv))

    repo_root = _repo_root()
    env = ensure_openai_key(dict(os.environ), repo_root)
    env = _with_repo_pythonpath(env, repo_root)

    project_dir, pkg = examples[ns.example]
    if not project_dir.is_dir():
        raise SystemExit(f"Missing example directory: {project_dir}")

    if ns.command == "build":
        cmd = [sys.executable, "-m", "jaunt", "build", "--root", str(project_dir)]
        if ns.force:
            cmd.append("--force")
        rc = _run(cmd, cwd=repo_root, env=env)
        if rc == 0:
            _print_outputs(project_dir, pkg)
        return rc

    if ns.command == "test":
        cmd = [sys.executable, "-m", "jaunt", "test", "--root", str(project_dir)]
        if ns.force:
            cmd.append("--force")
        if ns.no_run:
            cmd.append("--no-run")
        rc = _run(cmd, cwd=repo_root, env=env)
        if rc == 0:
            _print_outputs(project_dir, pkg)
        return rc

    raise SystemExit("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())
