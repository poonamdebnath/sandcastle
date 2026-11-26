from __future__ import annotations

import subprocess
import os
from pathlib import Path


def repo_root_from_file(file_path: str) -> Path:
    """Resolve the checkout Sandcastle controls, independently of its install path."""
    configured = os.environ.get("SANDCASTLE_REPO_ROOT", "").strip()
    return Path(configured).expanduser().resolve() if configured else Path.cwd().resolve()


def git_common_dir(repo_root: Path) -> Path:
    result = run_checked_command(["git", "rev-parse", "--git-common-dir"], cwd=repo_root)
    raw_path = result.stdout.strip()
    path = Path(raw_path)
    if path.is_absolute():
        return path
    return (repo_root / path).resolve()


def list_worktree_paths(repo_root: Path) -> tuple[Path, ...]:
    result = run_checked_command(["git", "worktree", "list", "--porcelain"], cwd=repo_root)
    paths: list[Path] = []
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            paths.append(Path(line.removeprefix("worktree ")).resolve())
    return tuple(paths)


def run_command(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )


def run_checked_command(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    result = run_command(command, cwd)
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip() or "command failed"
        raise RuntimeError(f"{' '.join(command)}: {stderr}")
    return result
