from __future__ import annotations

import re
import shutil
from pathlib import Path

from .models import RepoConfig
from .shell import list_worktree_paths, run_checked_command, run_command


def create_issue_worktree(
    repo_root: Path, repo_config: RepoConfig, issue_number: int, issue_title: str
) -> tuple[str, str]:
    branch_name = build_branch_name(
        repo_config.branch_name_pattern, issue_number, issue_title
    )
    worktree_path = repo_root / repo_config.worktree.path_pattern.format(
        issue_number=issue_number
    )

    if worktree_path.exists():
        raise RuntimeError(f"worktree path already exists: {worktree_path}")

    _ensure_branch_absent(repo_root, branch_name)
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    run_checked_command(
        [
            "git",
            "worktree",
            "add",
            "-b",
            branch_name,
            str(worktree_path),
            repo_config.base_ref,
        ],
        cwd=repo_root,
    )
    return branch_name, str(worktree_path)


def fetch_base_ref(repo_root: Path, repo_config: RepoConfig) -> None:
    run_checked_command(
        ["git", "fetch", repo_config.remote, repo_config.default_branch], cwd=repo_root
    )


def branch_ahead_count(repo_root: Path, base_ref: str, branch_name: str) -> int:
    result = run_checked_command(
        ["git", "rev-list", "--count", f"{base_ref}..{branch_name}"],
        cwd=repo_root,
    )
    return int(result.stdout.strip() or "0")


def branch_behind_count(repo_root: Path, branch_name: str, base_ref: str) -> int:
    result = run_checked_command(
        ["git", "rev-list", "--count", f"{branch_name}..{base_ref}"],
        cwd=repo_root,
    )
    return int(result.stdout.strip() or "0")


def ref_sha(repo_root: Path, ref_name: str) -> str:
    result = run_checked_command(["git", "rev-parse", ref_name], cwd=repo_root)
    return result.stdout.strip()


def merge_base_sha(worktree_path: str, left_ref: str, right_ref: str) -> str:
    result = run_checked_command(
        ["git", "merge-base", left_ref, right_ref],
        cwd=Path(worktree_path),
    )
    return result.stdout.strip()


def push_branch(repo_root: Path, remote: str, branch_name: str) -> None:
    run_checked_command(
        ["git", "push", "--set-upstream", remote, branch_name],
        cwd=repo_root,
    )


def remote_branch_exists(repo_root: Path, remote: str, branch_name: str) -> bool:
    result = run_command(
        ["git", "ls-remote", "--exit-code", "--heads", remote, branch_name],
        cwd=repo_root,
    )
    if result.returncode == 0:
        return True
    if result.returncode == 2:
        return False
    stderr = result.stderr.strip() or result.stdout.strip() or "ls-remote failed"
    raise RuntimeError(
        f"could not inspect remote branch state for {remote}/{branch_name}: {stderr}"
    )


def branch_exists(repo_root: Path, branch_name: str) -> bool:
    result = run_command(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"],
        cwd=repo_root,
    )
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    stderr = result.stderr.strip() or result.stdout.strip() or "show-ref failed"
    raise RuntimeError(f"could not inspect branch state for {branch_name}: {stderr}")


def remove_worktree(repo_root: Path, worktree_path: str) -> None:
    run_checked_command(
        ["git", "worktree", "remove", worktree_path],
        cwd=repo_root,
    )


def force_remove_worktree(repo_root: Path, worktree_path: str) -> None:
    run_checked_command(
        ["git", "worktree", "remove", "--force", worktree_path],
        cwd=repo_root,
    )


def list_worktree_changes(worktree_path: str) -> list[str]:
    result = run_checked_command(
        ["git", "status", "--short", "--untracked-files=all"],
        cwd=Path(worktree_path),
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def list_worktree_changed_files(worktree_path: str) -> list[str]:
    paths: list[str] = []
    for line in list_worktree_changes(worktree_path):
        if len(line) < 4:
            continue
        path_text = line[3:].strip()
        if " -> " in path_text:
            path_text = path_text.split(" -> ", 1)[1].strip()
        if path_text:
            paths.append(path_text)
    return paths


def list_changed_files_between(
    worktree_path: str, before_ref: str, after_ref: str
) -> list[str]:
    if before_ref == after_ref:
        return []
    result = run_checked_command(
        ["git", "diff", "--name-only", "--find-renames", f"{before_ref}..{after_ref}"],
        cwd=Path(worktree_path),
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def diff_stat_between(worktree_path: str, before_ref: str, after_ref: str) -> str:
    if before_ref == after_ref:
        return ""
    result = run_checked_command(
        ["git", "diff", "--stat", "--find-renames", f"{before_ref}..{after_ref}"],
        cwd=Path(worktree_path),
    )
    return result.stdout.strip()


def delete_local_branch(repo_root: Path, branch_name: str) -> None:
    run_checked_command(
        ["git", "branch", "-d", branch_name],
        cwd=repo_root,
    )


def force_delete_local_branch(repo_root: Path, branch_name: str) -> None:
    run_checked_command(
        ["git", "branch", "-D", branch_name],
        cwd=repo_root,
    )


def delete_remote_branch(repo_root: Path, remote: str, branch_name: str) -> None:
    run_checked_command(
        ["git", "push", remote, "--delete", branch_name],
        cwd=repo_root,
    )


def branch_unmerged_count(repo_root: Path, base_ref: str, branch_name: str) -> int:
    result = run_checked_command(
        ["git", "rev-list", "--count", f"{base_ref}..{branch_name}"],
        cwd=repo_root,
    )
    return int(result.stdout.strip() or "0")


def current_head_sha(worktree_path: str) -> str:
    result = run_checked_command(["git", "rev-parse", "HEAD"], cwd=Path(worktree_path))
    return result.stdout.strip()


def merge_base_into_worktree(
    worktree_path: str, base_ref: str
) -> tuple[int, str, str]:
    result = run_command(
        ["git", "merge", "--no-ff", "--no-edit", base_ref],
        cwd=Path(worktree_path),
    )
    return result.returncode, result.stdout, result.stderr


def list_unmerged_files(worktree_path: str) -> list[str]:
    result = run_checked_command(
        ["git", "diff", "--name-only", "--diff-filter=U"],
        cwd=Path(worktree_path),
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def is_merge_in_progress(worktree_path: str) -> bool:
    result = run_command(
        ["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"],
        cwd=Path(worktree_path),
    )
    return result.returncode == 0


def abort_merge(worktree_path: str) -> None:
    run_checked_command(["git", "merge", "--abort"], cwd=Path(worktree_path))


def stage_all_changes(worktree_path: str) -> None:
    run_checked_command(["git", "add", "-A"], cwd=Path(worktree_path))


def stage_paths(worktree_path: str, paths: list[str]) -> None:
    if not paths:
        return
    run_checked_command(["git", "add", "--", *paths], cwd=Path(worktree_path))


def commit_merge_resolution(worktree_path: str) -> None:
    run_checked_command(["git", "commit", "--no-edit"], cwd=Path(worktree_path))


def commit_staged_changes(worktree_path: str, message: str) -> None:
    run_checked_command(["git", "commit", "-m", message], cwd=Path(worktree_path))


def worktree_is_registered(repo_root: Path, worktree_path: str) -> bool:
    target = Path(worktree_path).resolve()
    return target in set(list_worktree_paths(repo_root))


def remove_orphaned_worktree_directory(
    repo_root: Path, managed_root: str, worktree_path: str
) -> None:
    managed_root_path = (repo_root / managed_root).resolve()
    target = Path(worktree_path).resolve()
    if managed_root_path not in [target, *target.parents]:
        raise RuntimeError(
            f"refusing to remove orphaned directory outside managed worktree root: {target}"
        )
    if worktree_is_registered(repo_root, worktree_path):
        raise RuntimeError(
            f"refusing to remove still-registered worktree directory: {target}"
        )
    if target.exists():
        shutil.rmtree(target)


def build_branch_name(pattern: str, issue_number: int, issue_title: str) -> str:
    slug = slugify(issue_title)
    return pattern.format(issue_number=issue_number, slug=slug)


def slugify(value: str) -> str:
    lowered = value.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    if not slug:
        return "issue"
    return slug[:48].rstrip("-")


def _ensure_branch_absent(repo_root: Path, branch_name: str) -> None:
    if branch_exists(repo_root, branch_name):
        raise RuntimeError(f"branch already exists: {branch_name}")
