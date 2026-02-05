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
    return {
        "slugify": (repo_root / "examples" / "01_slugify", "slugify_demo"),
        "lru": (repo_root / "examples" / "02_lru_cache", "cache_demo"),
        "dice": (repo_root / "examples" / "03_dice_roller", "dice_demo"),
    }


def _run(args: list[str], *, cwd: Path, env: dict[str, str]) -> int:
    proc = subprocess.run(args, cwd=str(cwd), env=env, check=False)
    return int(proc.returncode)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="run_example.py")
    p.add_argument("example", choices=["slugify", "lru", "dice"])

    sub = p.add_subparsers(dest="command", required=True)

    build_p = sub.add_parser("build", help="Run `jaunt build` in the example project.")
    build_p.add_argument("--force", action="store_true")

    test_p = sub.add_parser("test", help="Run `jaunt test` in the example project.")
    test_p.add_argument("--force", action="store_true")
    test_p.add_argument("--no-run", action="store_true", help="Skip running pytest.")

    ns = p.parse_args(list(sys.argv[1:] if argv is None else argv))

    repo_root = _repo_root()
    env = ensure_openai_key(dict(os.environ), repo_root)
    env = _with_repo_pythonpath(env, repo_root)

    project_dir, pkg = _example_map(repo_root)[ns.example]
    if not project_dir.is_dir():
        raise SystemExit(f"Missing example directory: {project_dir}")

    if ns.command == "build":
        cmd = [sys.executable, "-m", "jaunt", "build"]
        if ns.force:
            cmd.append("--force")
        rc = _run(cmd, cwd=project_dir, env=env)
        print(f"Generated implementations under: {project_dir / 'src' / pkg / '__generated__'}")
        return rc

    if ns.command == "test":
        cmd = [sys.executable, "-m", "jaunt", "test"]
        if ns.force:
            cmd.append("--force")
        if ns.no_run:
            cmd.append("--no-run")
        rc = _run(cmd, cwd=project_dir, env=env)
        print(f"Generated implementations under: {project_dir / 'src' / pkg / '__generated__'}")
        print(f"Generated tests under: {project_dir / 'tests' / '__generated__'}")
        return rc

    raise SystemExit("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())
