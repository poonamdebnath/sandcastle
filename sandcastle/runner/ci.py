from __future__ import annotations

import time
from pathlib import Path

from .github import (
    get_branch_head_sha,
    get_commit_check_rollup,
    get_pr_check_rollup,
    get_run_failed_log,
)
from .state_store import utc_now_iso, write_json_file

_SUCCESS_CONCLUSIONS = {"SUCCESS", "NEUTRAL", "SKIPPED"}


def wait_for_pr_checks(
    *,
    repo_root: Path,
    ci_dir: Path,
    issue_number: int,
    pr_number: int,
    timeout_seconds: int,
    poll_interval_seconds: int,
) -> Path:
    deadline = time.monotonic() + timeout_seconds
    saw_checks = False
    checks: list[dict[str, object]] = []

    while True:
        checks = _normalize_checks(get_pr_check_rollup(repo_root, pr_number))
        if checks:
            saw_checks = True

        if (
            saw_checks
            and checks
            and all(check["status"] == "COMPLETED" for check in checks)
        ):
            break

        if time.monotonic() >= deadline:
            return write_json_file(
                ci_dir / f"{issue_number}.json",
                {
                    "issue_number": issue_number,
                    "pr_number": pr_number,
                    "status": "timed_out",
                    "checks": checks,
                    "failed_checks": [],
                    "completed": False,
                    "saw_checks": saw_checks,
                    "written_at_utc": utc_now_iso(),
                },
            )

        time.sleep(max(poll_interval_seconds, 1))

    failed_checks = _load_failed_checks(repo_root, checks)
    overall_status = "passed" if not failed_checks else "failed"
    return write_json_file(
        ci_dir / f"{issue_number}.json",
        {
            "issue_number": issue_number,
            "pr_number": pr_number,
            "status": overall_status,
            "checks": checks,
            "failed_checks": failed_checks,
            "completed": True,
            "saw_checks": saw_checks,
            "written_at_utc": utc_now_iso(),
        },
    )


def snapshot_pr_checks(
    *,
    repo_root: Path,
    ci_dir: Path,
    issue_number: int,
    pr_number: int,
) -> Path:
    checks = _normalize_checks(get_pr_check_rollup(repo_root, pr_number))
    completed = bool(checks) and all(check["status"] == "COMPLETED" for check in checks)
    failed_checks = _load_failed_checks(repo_root, checks) if completed else []
    if completed:
        overall_status = "passed" if not failed_checks else "failed"
    else:
        overall_status = "pending"
    return write_json_file(
        ci_dir / f"{issue_number}.json",
        {
            "issue_number": issue_number,
            "pr_number": pr_number,
            "status": overall_status,
            "checks": checks,
            "failed_checks": failed_checks,
            "completed": completed,
            "saw_checks": bool(checks),
            "written_at_utc": utc_now_iso(),
        },
    )


def snapshot_main_checks(
    *,
    repo_root: Path,
    ci_dir: Path,
    issue_number: int,
    branch_name: str,
) -> Path:
    head_sha = get_branch_head_sha(repo_root, branch_name)
    checks = _normalize_checks(get_commit_check_rollup(repo_root, head_sha))
    completed = bool(checks) and all(check["status"] == "COMPLETED" for check in checks)
    failed_checks = _load_failed_checks(repo_root, checks) if completed else []
    if completed:
        overall_status = "passed" if not failed_checks else "failed"
    else:
        overall_status = "pending"
    return write_json_file(
        ci_dir / f"{issue_number}-post-merge.json",
        {
            "issue_number": issue_number,
            "branch_name": branch_name,
            "head_sha": head_sha,
            "status": overall_status,
            "checks": checks,
            "failed_checks": failed_checks,
            "completed": completed,
            "saw_checks": bool(checks),
            "written_at_utc": utc_now_iso(),
        },
    )


def _load_failed_checks(
    repo_root: Path, checks: list[dict[str, object]]
) -> list[dict[str, object]]:
    failed_checks: list[dict[str, object]] = []
    for check in checks:
        conclusion = str(check.get("conclusion", "")).upper()
        if conclusion in _SUCCESS_CONCLUSIONS or conclusion == "":
            continue

        details_url = str(check.get("details_url", "") or "")
        log_excerpt = get_run_failed_log(repo_root, details_url)
        failed_checks.append(
            {
                **check,
                "log_excerpt": log_excerpt[:4000] if log_excerpt else "",
            }
        )
    return failed_checks


def _normalize_checks(raw_checks: list[dict[str, object]]) -> list[dict[str, object]]:
    checks: list[dict[str, object]] = []
    for item in raw_checks:
        if not isinstance(item, dict):
            continue
        checks.append(
            {
                "name": str(item.get("name", item.get("context", "check"))),
                "status": str(item.get("status", "")).upper(),
                "conclusion": str(item.get("conclusion", "")).upper(),
                "workflow_name": str(item.get("workflowName", "") or ""),
                "details_url": str(item.get("detailsUrl", "") or ""),
                "started_at": str(item.get("startedAt", "") or ""),
                "completed_at": str(item.get("completedAt", "") or ""),
                "kind": str(item.get("__typename", "") or ""),
            }
        )
    return checks
