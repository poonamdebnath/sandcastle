from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from .models import IssueStatus, SandcastlePaths
from .shell import git_common_dir, list_worktree_paths

STATE_ROOT_ENV_VAR = "SANDCASTLE_STATE_ROOT"


def build_paths(repo_root: Path) -> SandcastlePaths:
    sandcastle_root = repo_root / "sandcastle"
    state_dir = resolve_state_dir(repo_root)
    _ensure_shared_state_dir(repo_root, sandcastle_root / "state", state_dir)
    return SandcastlePaths(
        root=sandcastle_root,
        config_dir=sandcastle_root / "config",
        templates_dir=sandcastle_root / "templates",
        state_dir=state_dir,
        leases_dir=state_dir / "leases",
        issues_dir=state_dir / "issues",
        contexts_dir=state_dir / "contexts",
        ci_dir=state_dir / "ci",
        verification_dir=state_dir / "verification",
        implementation_dir=state_dir / "implementation",
        conflict_dir=state_dir / "conflict",
        review_dir=state_dir / "review",
        findings_dir=state_dir / "findings",
        revision_dir=state_dir / "revision",
    )


def resolve_state_dir(repo_root: Path) -> Path:
    configured = os.environ.get(STATE_ROOT_ENV_VAR, "").strip()
    if configured:
        configured_path = Path(configured).expanduser()
        return (
            configured_path
            if configured_path.is_absolute()
            else (repo_root / configured_path).resolve()
        )
    return git_common_dir(repo_root) / "sandcastle-state"


def read_json_file(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json_file(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
    return path


def try_load_issue_status(
    paths: SandcastlePaths, issue_number: int
) -> IssueStatus | None:
    issue_path = paths.issues_dir / f"{issue_number}.json"
    if not issue_path.exists():
        return None

    payload = read_json_file(issue_path)
    return IssueStatus(
        issue_number=int(payload["issue_number"]),
        status=str(payload["status"]),
        branch=_optional_str(payload.get("branch")),
        worktree_path=_optional_str(payload.get("worktree_path")),
        pr_number=_optional_int(payload.get("pr_number")),
        last_updated_utc=_optional_str(payload.get("last_updated_utc")),
        extra=_optional_dict(payload.get("extra")),
    )


def write_issue_status(paths: SandcastlePaths, issue_status: IssueStatus) -> Path:
    paths.issues_dir.mkdir(parents=True, exist_ok=True)
    issue_path = paths.issues_dir / f"{issue_status.issue_number}.json"
    payload = {
        "issue_number": issue_status.issue_number,
        "status": issue_status.status,
        "branch": issue_status.branch,
        "worktree_path": issue_status.worktree_path,
        "pr_number": issue_status.pr_number,
        "last_updated_utc": issue_status.last_updated_utc or utc_now_iso(),
        "extra": issue_status.extra,
    }
    return write_json_file(issue_path, payload)


def delete_issue_runtime_state(paths: SandcastlePaths, issue_number: int) -> list[Path]:
    removed: list[Path] = []
    for path in _issue_runtime_state_paths(paths, issue_number):
        if not path.exists():
            continue
        path.unlink(missing_ok=True)
        removed.append(path)
    return removed


def list_issue_statuses(paths: SandcastlePaths) -> list[IssueStatus]:
    if not paths.issues_dir.exists():
        return []

    statuses: list[IssueStatus] = []
    for issue_path in sorted(paths.issues_dir.glob("*.json")):
        payload = read_json_file(issue_path)
        statuses.append(
            IssueStatus(
                issue_number=int(payload["issue_number"]),
                status=str(payload["status"]),
                branch=_optional_str(payload.get("branch")),
                worktree_path=_optional_str(payload.get("worktree_path")),
                pr_number=_optional_int(payload.get("pr_number")),
                last_updated_utc=_optional_str(payload.get("last_updated_utc")),
                extra=_optional_dict(payload.get("extra")),
            )
        )
    return statuses


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _optional_str(value: object) -> str | None:
    return str(value) if value is not None else None


def _optional_int(value: object) -> int | None:
    return int(value) if value is not None else None


def _optional_dict(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return value
    return {}


def _ensure_shared_state_dir(
    repo_root: Path, legacy_state_dir: Path, shared_state_dir: Path
) -> None:
    shared_state_dir.mkdir(parents=True, exist_ok=True)
    if _state_dir_has_payload(shared_state_dir):
        return

    for candidate in _legacy_state_candidates(repo_root, legacy_state_dir):
        if not candidate.exists() or not _state_dir_has_payload(candidate):
            continue
        shutil.copytree(candidate, shared_state_dir, dirs_exist_ok=True)
        return


def _legacy_state_candidates(repo_root: Path, legacy_state_dir: Path) -> list[Path]:
    candidates: list[Path] = []
    seen: set[Path] = set()

    direct_candidates = [legacy_state_dir]
    for worktree_path in _ordered_worktree_paths(repo_root):
        direct_candidates.append(worktree_path / "sandcastle" / "state")

    for candidate in direct_candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        candidates.append(resolved)
    return candidates


def _ordered_worktree_paths(repo_root: Path) -> list[Path]:
    worktree_paths = list(list_worktree_paths(repo_root))
    return sorted(
        worktree_paths,
        key=lambda path: (
            0 if ".sandcastle-control" in str(path) else 1,
            0 if path.resolve() == repo_root.resolve() else 1,
            str(path),
        ),
    )


def _state_dir_has_payload(state_dir: Path) -> bool:
    if not state_dir.exists():
        return False
    return any(state_dir.rglob("*.json"))


def _issue_runtime_state_paths(paths: SandcastlePaths, issue_number: int) -> tuple[Path, ...]:
    issue_name = f"{issue_number}.json"
    return (
        paths.issues_dir / issue_name,
        paths.contexts_dir / issue_name,
        paths.ci_dir / issue_name,
        paths.verification_dir / issue_name,
        paths.implementation_dir / issue_name,
        paths.conflict_dir / issue_name,
        paths.review_dir / issue_name,
        paths.findings_dir / issue_name,
        paths.revision_dir / issue_name,
        paths.leases_dir / issue_name,
        paths.leases_dir / f"{issue_number}.lock",
    )
