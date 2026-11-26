from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
from contextlib import redirect_stdout
from dataclasses import dataclass
from pathlib import Path

from .ci import snapshot_main_checks, snapshot_pr_checks, wait_for_pr_checks
from .conflict_resolution import resolve_pr_conflicts
from .context import fetch_issue_context, write_issue_context
from .eligibility import evaluate_issue, is_held_only_by_label_policy
from .findings import collect_findings
from .github import (
    comment_on_issue,
    close_issue,
    create_pr,
    ensure_issue_assignee,
    ensure_pull_request_assignee,
    find_pr_for_head,
    get_pr,
    get_pr_check_rollup,
    get_current_login,
    get_issue,
    list_open_issues,
    mark_pr_ready,
    merge_pr,
)
from .implementation import run_implementation_command
from .issue_leases import IssueLeaseUnavailable, active_issue_lease, issue_lease_guard
from .models import (
    ClaimPolicy,
    CompletionPolicy,
    GatesConfig,
    HandoffPolicy,
    ImplementationPolicy,
    IssueCandidate,
    IssuePolicy,
    PullOrderPolicy,
    RepoConfig,
    SchedulerPolicy,
    WorktreePolicy,
    CleanupPolicy,
    GithubPolicy,
    EligibilityDecision,
    IssueStatus,
    GateCommand,
    TestsPolicy,
    ReviewPolicy,
    BranchPolicy,
    BacklogSelection,
    BlockOnPolicy,
)
from .revision import write_revision_plan
from .review_loop import write_review_result
from .shell import repo_root_from_file
from .state_store import (
    build_paths,
    delete_issue_runtime_state,
    list_issue_statuses,
    read_json_file,
    try_load_issue_status,
    utc_now_iso,
    write_json_file,
    write_issue_status,
)
from .templates import render_template
from .verify import run_verification
from .worktree import (
    branch_ahead_count,
    branch_behind_count,
    branch_exists,
    commit_staged_changes,
    current_head_sha,
    create_issue_worktree,
    delete_remote_branch,
    delete_local_branch,
    fetch_base_ref,
    force_remove_worktree,
    force_delete_local_branch,
    list_worktree_changed_files,
    list_worktree_changes,
    push_branch,
    remove_orphaned_worktree_directory,
    remote_branch_exists,
    ref_sha,
    remove_worktree,
    stage_paths,
    worktree_is_registered,
)

HUMAN_WAIT_STATUSES = frozenset(
    {"ready-for-human", "waiting-human-review", "approved-awaiting-merge"}
)
DONE_STATUSES = frozenset({"closed", "failed-startup-cleaned", "no-op"})
ACTIVE_DETAIL_STATUSES = frozenset(
    {
        "claiming",
        "claimed",
        "branch-created",
        "context-loaded",
        "implementing",
        "verifying",
        "reviewing",
        "revising",
        "review-fix-loop",
        "ready-for-pr",
        "resolving-conflicts",
        "merged",
    }
)
IMPLEMENTATION_RUNTIME_PATH_PREFIXES = (".sandcastle-agent/",)
RESUMABLE_ISSUE_STATUSES = frozenset(
    {
        "branch-created",
        "claimed",
        "implementing",
        "revising",
        "pr-open",
        "ready-for-pr",
        "ready-for-human",
    }
)
BLOCKED_RESUME_KIND_PULL_REQUEST_ASSIGNMENT = "pull-request-assignment"
BLOCK_SCOPE_ISSUE = "issue"
BLOCK_SCOPE_QUEUE = "queue"
POST_MERGE_HEALTH_STATUS = "awaiting-post-merge-health"


@dataclass(frozen=True)
class _OrchestrationStepResult:
    exit_code: int
    payload: dict[str, object]
    chosen_issue_number: int | None = None


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.repo_root:
        os.environ["SANDCASTLE_REPO_ROOT"] = str(Path(args.repo_root).expanduser().resolve())

    repo_root = repo_root_from_file(__file__)
    paths = build_paths(repo_root)
    issue_policy = _load_issue_policy(paths.config_dir / "issue-policy.json")
    repo_config = _load_repo_config(paths.config_dir / "repo.json")
    gates_config = _load_gates_config(paths.config_dir / "gates.json")

    try:
        if args.command == "discover":
            return _handle_discover(repo_root, issue_policy, repo_config)
        if args.command == "auto-step":
            return _handle_auto_step(
                repo_root, paths, issue_policy, repo_config, gates_config
            )
        if args.command == "process-next":
            return _handle_process_next(
                repo_root, paths, issue_policy, repo_config, gates_config
            )
        if args.command == "process-issue":
            return _handle_process_issue(
                repo_root,
                paths,
                issue_policy,
                repo_config,
                gates_config,
                args.issue_number,
            )
        if args.command == "run-loop":
            return _handle_run_loop(
                repo_root,
                paths,
                issue_policy,
                repo_config,
                gates_config,
                args.poll_interval_seconds,
                args.max_idle_polls,
            )
        if args.command == "cleanup":
            return _handle_cleanup(repo_root, paths, repo_config, args.issue_number)
        if args.command == "collect-findings":
            return _handle_collect_findings(paths, repo_config, args.issue_number)
        if args.command == "implement":
            return _handle_implement(
                repo_root, paths, repo_config, gates_config, args.issue_number
            )
        if args.command == "load-context":
            return _handle_load_context(
                repo_root, paths, issue_policy, args.issue_number
            )
        if args.command == "review":
            return _handle_review(paths, issue_policy, repo_config, args.issue_number)
        if args.command == "revise":
            return _handle_revise(paths, args.issue_number)
        if args.command == "run-issue":
            return _handle_run_issue(
                repo_root, paths, issue_policy, repo_config, args.issue_number
            )
        if args.command == "sync-pr":
            return _handle_sync_pr(repo_root, paths, repo_config, args.issue_number)
        if args.command == "wait-ci":
            return _handle_wait_ci(
                repo_root,
                paths,
                repo_config,
                args.issue_number,
                args.timeout_seconds,
                args.poll_interval_seconds,
            )
        if args.command == "resume":
            return _handle_resume(
                repo_root, paths, issue_policy, repo_config, args.issue_number
            )
        if args.command == "report":
            return _handle_report(paths, repo_config)
        if args.command == "morning-handover":
            return _handle_morning_handover(paths, repo_config)
        if args.command == "status":
            return _handle_status(repo_root, paths, issue_policy, args.issue_number)
        if args.command == "verify":
            return _handle_verify(paths, issue_policy, gates_config, args.issue_number)
    except KeyboardInterrupt:
        if args.command == "run-loop":
            active_statuses = _list_active_issues(paths)
            backlog = _select_backlog_issue(repo_root, paths, issue_policy)
            print(
                json.dumps(
                    {
                        "status": "stopped",
                        "reason": "operator interrupted run-loop",
                        "stop_category": "operator-action",
                        **_scheduler_snapshot(
                            active_statuses, backlog, repo_config.scheduler
                        ),
                    },
                    indent=2,
                )
            )
            return 130
        raise

    parser.error(f"unknown command: {args.command}")
    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sandcastle")
    parser.add_argument(
        "--repo-root",
        help="Path to the Git checkout containing sandcastle/config and templates (defaults to the current directory).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "auto-step",
        help="Automatically pick the next eligible issue or advance the active one.",
    )
    subparsers.add_parser(
        "process-next",
        help="Advance one issue until it reaches a stable waiting, blocked, or done boundary.",
    )
    process_issue_parser = subparsers.add_parser(
        "process-issue",
        help="Force orchestration attention onto one issue while still obeying Sandcastle safety rules.",
    )
    process_issue_parser.add_argument("issue_number", type=int)
    run_loop_parser = subparsers.add_parser(
        "run-loop",
        help="Continuously poll Sandcastle state, cleanup merged PRs, and start the next eligible issue.",
    )
    run_loop_parser.add_argument(
        "--poll-interval-seconds",
        type=int,
        default=60,
    )
    run_loop_parser.add_argument(
        "--max-idle-polls",
        type=int,
        default=0,
        help="Optional idle-poll limit for bounded runs; 0 keeps polling indefinitely.",
    )
    subparsers.add_parser("discover", help="List issues and evaluate eligibility.")

    cleanup_parser = subparsers.add_parser(
        "cleanup", help="Clean up a merged issue worktree and branch."
    )
    cleanup_parser.add_argument("issue_number", type=int)

    collect_findings_parser = subparsers.add_parser(
        "collect-findings",
        help="Normalize findings from verification and PR discussion.",
    )
    collect_findings_parser.add_argument("issue_number", type=int)

    implement_parser = subparsers.add_parser(
        "implement", help="Write the current implementation-phase artifact."
    )
    implement_parser.add_argument("issue_number", type=int)

    load_context_parser = subparsers.add_parser(
        "load-context", help="Fetch and persist GitHub issue context."
    )
    load_context_parser.add_argument("issue_number", type=int)

    review_parser = subparsers.add_parser(
        "review", help="Write the current internal review artifact."
    )
    review_parser.add_argument("issue_number", type=int)

    revise_parser = subparsers.add_parser(
        "revise", help="Write the current revise-loop artifact."
    )
    revise_parser.add_argument("issue_number", type=int)

    run_issue_parser = subparsers.add_parser(
        "run-issue", help="Claim an eligible issue and set up its branch/worktree."
    )
    run_issue_parser.add_argument("issue_number", type=int)

    sync_pr_parser = subparsers.add_parser(
        "sync-pr", help="Sync local state from the current PR lifecycle."
    )
    sync_pr_parser.add_argument("issue_number", type=int)

    wait_ci_parser = subparsers.add_parser(
        "wait-ci", help="Wait for PR CI checks and capture failed CI logs."
    )
    wait_ci_parser.add_argument("issue_number", type=int)
    wait_ci_parser.add_argument("--timeout-seconds", type=int, default=900)
    wait_ci_parser.add_argument("--poll-interval-seconds", type=int, default=15)

    resume_parser = subparsers.add_parser(
        "resume", help="Continue an existing issue run."
    )
    resume_parser.add_argument("issue_number", type=int)

    subparsers.add_parser(
        "report",
        help="Show and persist a queue-wide Sandcastle operator report.",
    )

    subparsers.add_parser(
        "morning-handover",
        help="Show and persist the concise Sandcastle morning handover summary.",
    )

    status_parser = subparsers.add_parser(
        "status", help="Show current status for an issue."
    )
    status_parser.add_argument("issue_number", type=int)

    verify_parser = subparsers.add_parser(
        "verify", help="Run configured verification gates in the issue worktree."
    )
    verify_parser.add_argument("issue_number", type=int)

    return parser


def _handle_discover(
    repo_root: Path, issue_policy: IssuePolicy, repo_config: RepoConfig
) -> int:
    limit = max(issue_policy.pull_order.max_issues_per_run, 25)
    issues = list_open_issues(repo_root, limit=limit)

    rows = []
    for issue in issues:
        decision = evaluate_issue(issue, issue_policy)
        rows.append(
            {
                "number": issue.number,
                "title": issue.title,
                "eligible": decision.eligible,
                "reasons": list(decision.reasons),
                "labels": list(issue.labels),
                "assignees": list(issue.assignees),
                "url": issue.url,
                "base_ref": repo_config.base_ref,
                "dependencies": _serialize_dependency_status(decision),
                "pickup_policy": _issue_pickup_policy(issue, issue_policy.pull_order),
            }
        )

    print(json.dumps({"issues": rows}, indent=2))
    return 0


def _handle_auto_step(
    repo_root: Path,
    paths,
    issue_policy: IssuePolicy,
    repo_config: RepoConfig,
    gates_config: GatesConfig,
) -> int:
    result = _execute_orchestration_step(
        repo_root, paths, issue_policy, repo_config, gates_config
    )
    _persist_operator_reports(paths, repo_config)
    _emit_json(result.payload)
    return result.exit_code


def _handle_process_next(
    repo_root: Path,
    paths,
    issue_policy: IssuePolicy,
    repo_config: RepoConfig,
    gates_config: GatesConfig,
) -> int:
    exit_code, summary = _process_until_boundary(
        repo_root,
        paths,
        issue_policy,
        repo_config,
        gates_config,
    )
    _persist_operator_reports(paths, repo_config)
    _emit_json(summary)
    return exit_code


def _handle_process_issue(
    repo_root: Path,
    paths,
    issue_policy: IssuePolicy,
    repo_config: RepoConfig,
    gates_config: GatesConfig,
    issue_number: int,
) -> int:
    exit_code, summary = _process_until_boundary(
        repo_root,
        paths,
        issue_policy,
        repo_config,
        gates_config,
        target_issue_number=issue_number,
    )
    _persist_operator_reports(paths, repo_config)
    _emit_json(summary)
    return exit_code


def _emit_json(payload: dict[str, object]) -> None:
    print(json.dumps(payload, indent=2))


def _capture_operation_output(operation, *args):
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        result = operation(*args)
    raw_output = buffer.getvalue().strip()
    if not raw_output:
        return result, None
    try:
        payload = json.loads(raw_output)
    except json.JSONDecodeError:
        payload = {"raw_output": raw_output}
    return result, payload


def _resulting_state_payload(local_status: IssueStatus) -> dict[str, object]:
    return {
        "status": local_status.status,
        "operator_state": _operator_state(local_status),
        "operator_wait_reason": _operator_wait_reason(local_status),
    }


def _base_orchestration_payload(
    repo_root: Path,
    paths,
    issue_policy: IssuePolicy,
    repo_config: RepoConfig,
    step_kind: str,
    lease_conflicts: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    active_statuses = _list_active_issues(paths)
    blocked_issue_count = len(_list_blocked_issues(paths))
    backlog = _safe_select_backlog_issue(repo_root, paths, issue_policy)
    return {
        "step_kind": step_kind,
        "lease_conflicts": [] if lease_conflicts is None else lease_conflicts,
        **_scheduler_snapshot(
            active_statuses,
            backlog,
            repo_config.scheduler,
            blocked_issue_count=blocked_issue_count,
        ),
    }


def _attach_issue_result(
    payload: dict[str, object],
    paths,
    issue_number: int,
    handler_payload: dict[str, object] | None = None,
) -> None:
    payload["issue_number"] = issue_number
    payload["chosen_issue_number"] = issue_number
    local_status = try_load_issue_status(paths, issue_number)
    if local_status is not None:
        payload["status"] = local_status.status
        payload["operator_state"] = _operator_state(local_status)
        payload["resulting_state"] = _resulting_state_payload(local_status)
        return

    if handler_payload is None:
        return

    status = handler_payload.get("status")
    operator_state = handler_payload.get("operator_state")
    if operator_state is None and status in DONE_STATUSES:
        operator_state = "done"
    if status is not None:
        payload["status"] = status
    if operator_state is not None:
        payload["operator_state"] = operator_state
    if status is not None or operator_state is not None:
        payload["resulting_state"] = {
            "status": status,
            "operator_state": operator_state,
        }


def _execute_orchestration_step(
    repo_root: Path,
    paths,
    issue_policy: IssuePolicy,
    repo_config: RepoConfig,
    gates_config: GatesConfig,
    target_issue_number: int | None = None,
) -> _OrchestrationStepResult:
    blocked_statuses = _list_blocked_issues(paths)
    lease_conflicts: list[dict[str, object]] = []
    queue_blocker = next(
        (status for status in blocked_statuses if _blocked_issue_scope(status) == BLOCK_SCOPE_QUEUE),
        None,
    )
    if queue_blocker is not None:
        payload = _base_orchestration_payload(
            repo_root,
            paths,
            issue_policy,
            repo_config,
            step_kind="blocked-stop",
            lease_conflicts=lease_conflicts,
        )
        payload.update(
            {
                "status": "blocked",
                "operator_state": "blocked",
                "block_scope": BLOCK_SCOPE_QUEUE,
                "stop_category": "repo-global-blocker",
                "issue_number": queue_blocker.issue_number,
                "chosen_issue_number": queue_blocker.issue_number,
                "resulting_state": {
                    "status": "blocked",
                    "operator_state": "blocked",
                },
                "message": "queue is paused by a repo-global blocker that requires human intervention",
            }
        )
        return _OrchestrationStepResult(
            exit_code=1,
            payload=payload,
            chosen_issue_number=queue_blocker.issue_number,
        )

    active_statuses = _list_active_issues(paths)
    main_is_unhealthy = not bool(_load_main_health(paths).get("healthy"))
    if target_issue_number is not None:
        return _execute_targeted_step(
            repo_root,
            paths,
            issue_policy,
            repo_config,
            gates_config,
            target_issue_number,
            active_statuses,
            lease_conflicts,
            main_is_unhealthy,
        )

    for blocked_status in blocked_statuses:
        if not _is_failed_startup_cleanup_candidate(
            repo_root, repo_config, blocked_status
        ):
            continue
        try:
            with issue_lease_guard(
                paths,
                repo_config.scheduler,
                issue_number=blocked_status.issue_number,
                operation="cleanup-failed-startup",
                status=blocked_status.status,
                branch=blocked_status.branch,
                worktree_path=blocked_status.worktree_path,
                pr_number=blocked_status.pr_number,
            ):
                exit_code, handler_payload = _capture_operation_output(
                    _handle_cleanup,
                    repo_root, paths, repo_config, blocked_status.issue_number
                )
                payload = _base_orchestration_payload(
                    repo_root,
                    paths,
                    issue_policy,
                    repo_config,
                    step_kind="cleanup-failed-startup",
                    lease_conflicts=lease_conflicts,
                )
                _attach_issue_result(
                    payload, paths, blocked_status.issue_number, handler_payload
                )
                return _OrchestrationStepResult(
                    exit_code=exit_code,
                    payload=payload,
                    chosen_issue_number=blocked_status.issue_number,
                )
        except IssueLeaseUnavailable as error:
            lease_conflicts.append(error.payload)

    backlog = _select_backlog_issue(repo_root, paths, issue_policy)
    for actionable in _ordered_actionable_issues(active_statuses):
        if main_is_unhealthy and actionable.status != "merged":
            continue
        if (
            backlog.selected_issue is not None
            and _count_active_work(active_statuses) == 0
            and actionable.status != "merged"
        ):
            actionable = _record_resume_priority(paths, actionable, backlog)
            print(
                json.dumps(
                    {
                        "status": "resuming-existing-branch",
                        "issue_number": actionable.issue_number,
                        "reason": actionable.extra.get("resume_priority_reason"),
                        "resumed_ahead_of_issue_number": actionable.extra.get(
                            "resumed_ahead_of_issue_number"
                        ),
                        "resumed_ahead_of_issue_title": actionable.extra.get(
                            "resumed_ahead_of_issue_title"
                        ),
                    },
                    indent=2,
                )
            )
        try:
            with issue_lease_guard(
                paths,
                repo_config.scheduler,
                issue_number=actionable.issue_number,
                operation=f"advance:{actionable.status}",
                status=actionable.status,
                branch=actionable.branch,
                worktree_path=actionable.worktree_path,
                pr_number=actionable.pr_number,
            ):
                exit_code, handler_payload = _capture_operation_output(
                    _advance_active_issue,
                    repo_root, paths, issue_policy, repo_config, gates_config, actionable
                )
                payload = _base_orchestration_payload(
                    repo_root,
                    paths,
                    issue_policy,
                    repo_config,
                    step_kind="advance-active",
                    lease_conflicts=lease_conflicts,
                )
                _attach_issue_result(
                    payload, paths, actionable.issue_number, handler_payload
                )
                return _OrchestrationStepResult(
                    exit_code=exit_code,
                    payload=payload,
                    chosen_issue_number=actionable.issue_number,
                )
        except IssueLeaseUnavailable as error:
            lease_conflicts.append(error.payload)

    if not main_is_unhealthy and _can_pick_new_issue(active_statuses, repo_config.scheduler):
        next_issue = backlog.selected_issue
        if next_issue is None:
            monitorable = _select_next_monitorable_issue(active_statuses)
            if monitorable is not None:
                _, handler_payload = _capture_operation_output(
                    _refresh_issue_observation,
                    repo_root,
                    paths,
                    repo_config,
                    monitorable,
                )
                payload = _base_orchestration_payload(
                    repo_root,
                    paths,
                    issue_policy,
                    repo_config,
                    step_kind="observe-waiting",
                    lease_conflicts=lease_conflicts,
                )
                _attach_issue_result(
                    payload, paths, monitorable.issue_number, handler_payload
                )
                return _OrchestrationStepResult(
                    exit_code=0,
                    payload=payload,
                    chosen_issue_number=monitorable.issue_number,
                )

            payload = _base_orchestration_payload(
                repo_root,
                paths,
                issue_policy,
                repo_config,
                step_kind="idle",
                lease_conflicts=lease_conflicts,
            )
            payload.update(
                {
                    "status": "idle",
                    "operator_state": "waiting",
                    "message": _waiting_reason(
                        active_statuses,
                        backlog,
                        repo_config.scheduler,
                        blocked_issue_count=len(blocked_statuses),
                    ),
                }
            )
            return _OrchestrationStepResult(exit_code=0, payload=payload)

        try:
            with issue_lease_guard(
                paths,
                repo_config.scheduler,
                issue_number=next_issue.number,
                operation="claim-and-setup",
                status="eligible",
            ):
                exit_code, handler_payload = _capture_operation_output(
                    _handle_run_issue,
                    repo_root, paths, issue_policy, repo_config, next_issue.number
                )
                payload = _base_orchestration_payload(
                    repo_root,
                    paths,
                    issue_policy,
                    repo_config,
                    step_kind="claim-and-setup",
                    lease_conflicts=lease_conflicts,
                )
                _attach_issue_result(payload, paths, next_issue.number, handler_payload)
                return _OrchestrationStepResult(
                    exit_code=exit_code,
                    payload=payload,
                    chosen_issue_number=next_issue.number,
                )
        except IssueLeaseUnavailable as error:
            lease_conflicts.append(error.payload)

    monitorable = _select_next_monitorable_issue(active_statuses)
    if monitorable is not None:
        _, handler_payload = _capture_operation_output(
            _refresh_issue_observation,
            repo_root,
            paths,
            repo_config,
            monitorable,
        )
        payload = _base_orchestration_payload(
            repo_root,
            paths,
            issue_policy,
            repo_config,
            step_kind="observe-waiting",
            lease_conflicts=lease_conflicts,
        )
        _attach_issue_result(payload, paths, monitorable.issue_number, handler_payload)
        return _OrchestrationStepResult(
            exit_code=0,
            payload=payload,
            chosen_issue_number=monitorable.issue_number,
        )

    if main_is_unhealthy:
        payload = _base_orchestration_payload(
            repo_root,
            paths,
            issue_policy,
            repo_config,
            step_kind="suspended-unhealthy-main",
            lease_conflicts=lease_conflicts,
        )
        payload.update(
            {
                "status": "idle",
                "operator_state": "waiting",
                "message": "main is unhealthy; Sandcastle is only running monitoring work until default-branch health recovers",
            }
        )
        return _OrchestrationStepResult(exit_code=0, payload=payload)

    payload = _base_orchestration_payload(
        repo_root,
        paths,
        issue_policy,
        repo_config,
        step_kind="idle",
        lease_conflicts=lease_conflicts,
    )
    payload.update(
        {
            "status": "idle",
            "operator_state": "waiting",
            "message": "active issue limits prevent picking new backlog work right now",
        }
    )
    return _OrchestrationStepResult(exit_code=0, payload=payload)


def _execute_targeted_step(
    repo_root: Path,
    paths,
    issue_policy: IssuePolicy,
    repo_config: RepoConfig,
    gates_config: GatesConfig,
    issue_number: int,
    active_statuses: list[IssueStatus],
    lease_conflicts: list[dict[str, object]],
    main_is_unhealthy: bool,
) -> _OrchestrationStepResult:
    local_status = try_load_issue_status(paths, issue_number)
    if (
        local_status is not None
        and local_status.status == "blocked"
        and _is_failed_startup_cleanup_candidate(repo_root, repo_config, local_status)
    ):
        try:
            with issue_lease_guard(
                paths,
                repo_config.scheduler,
                issue_number=local_status.issue_number,
                operation="cleanup-failed-startup",
                status=local_status.status,
                branch=local_status.branch,
                worktree_path=local_status.worktree_path,
                pr_number=local_status.pr_number,
            ):
                exit_code, handler_payload = _capture_operation_output(
                    _handle_cleanup,
                    repo_root,
                    paths,
                    repo_config,
                    local_status.issue_number,
                )
                payload = _base_orchestration_payload(
                    repo_root,
                    paths,
                    issue_policy,
                    repo_config,
                    step_kind="cleanup-targeted-failed-startup",
                    lease_conflicts=lease_conflicts,
                )
                _attach_issue_result(payload, paths, issue_number, handler_payload)
                return _OrchestrationStepResult(
                    exit_code=exit_code,
                    payload=payload,
                    chosen_issue_number=issue_number,
                )
        except IssueLeaseUnavailable as error:
            lease_conflicts.append(error.payload)

    if local_status is not None and local_status.status in DONE_STATUSES:
        payload = _base_orchestration_payload(
            repo_root,
            paths,
            issue_policy,
            repo_config,
            step_kind="target-already-done",
            lease_conflicts=lease_conflicts,
        )
        _attach_issue_result(payload, paths, issue_number)
        return _OrchestrationStepResult(
            exit_code=0,
            payload=payload,
            chosen_issue_number=issue_number,
        )

    if (
        local_status is not None
        and main_is_unhealthy
        and _action_priority(local_status) is not None
        and local_status.status != "merged"
    ):
        payload = _base_orchestration_payload(
            repo_root,
            paths,
            issue_policy,
            repo_config,
            step_kind="target-suspended-unhealthy-main",
            lease_conflicts=lease_conflicts,
        )
        _attach_issue_result(payload, paths, issue_number)
        payload.update(
            {
                "status": "idle",
                "operator_state": "waiting",
                "message": "main is unhealthy; targeted code-mutating work is suspended until default-branch health recovers",
            }
        )
        return _OrchestrationStepResult(
            exit_code=0,
            payload=payload,
            chosen_issue_number=issue_number,
        )

    if local_status is not None and _action_priority(local_status) is not None:
        try:
            with issue_lease_guard(
                paths,
                repo_config.scheduler,
                issue_number=local_status.issue_number,
                operation=f"advance:{local_status.status}",
                status=local_status.status,
                branch=local_status.branch,
                worktree_path=local_status.worktree_path,
                pr_number=local_status.pr_number,
            ):
                exit_code, handler_payload = _capture_operation_output(
                    _advance_active_issue,
                    repo_root,
                    paths,
                    issue_policy,
                    repo_config,
                    gates_config,
                    local_status,
                )
                payload = _base_orchestration_payload(
                    repo_root,
                    paths,
                    issue_policy,
                    repo_config,
                    step_kind="advance-targeted-issue",
                    lease_conflicts=lease_conflicts,
                )
                _attach_issue_result(payload, paths, issue_number, handler_payload)
                return _OrchestrationStepResult(
                    exit_code=exit_code,
                    payload=payload,
                    chosen_issue_number=issue_number,
                )
        except IssueLeaseUnavailable as error:
            lease_conflicts.append(error.payload)

    if local_status is not None and _needs_background_observation(local_status):
        _, handler_payload = _capture_operation_output(
            _refresh_issue_observation,
            repo_root,
            paths,
            repo_config,
            local_status,
        )
        payload = _base_orchestration_payload(
            repo_root,
            paths,
            issue_policy,
            repo_config,
            step_kind="observe-targeted-issue",
            lease_conflicts=lease_conflicts,
        )
        _attach_issue_result(payload, paths, issue_number, handler_payload)
        return _OrchestrationStepResult(
            exit_code=0,
            payload=payload,
            chosen_issue_number=issue_number,
        )

    if local_status is not None:
        payload = _base_orchestration_payload(
            repo_root,
            paths,
            issue_policy,
            repo_config,
            step_kind="target-noop",
            lease_conflicts=lease_conflicts,
        )
        _attach_issue_result(payload, paths, issue_number)
        return _OrchestrationStepResult(
            exit_code=0,
            payload=payload,
            chosen_issue_number=issue_number,
        )

    issue = get_issue(repo_root, issue_number)
    decision = evaluate_issue(issue, issue_policy)
    if not decision.eligible:
        payload = _base_orchestration_payload(
            repo_root,
            paths,
            issue_policy,
            repo_config,
            step_kind="target-ineligible",
            lease_conflicts=lease_conflicts,
        )
        payload.update(
            {
                "status": "error",
                "issue_number": issue_number,
                "chosen_issue_number": issue_number,
                "message": "issue is not eligible for Sandcastle pickup",
                "reasons": list(decision.reasons),
            }
        )
        return _OrchestrationStepResult(
            exit_code=1,
            payload=payload,
            chosen_issue_number=issue_number,
        )

    if main_is_unhealthy:
        payload = _base_orchestration_payload(
            repo_root,
            paths,
            issue_policy,
            repo_config,
            step_kind="target-suspended-unhealthy-main",
            lease_conflicts=lease_conflicts,
        )
        payload.update(
            {
                "status": "idle",
                "operator_state": "waiting",
                "issue_number": issue_number,
                "chosen_issue_number": issue_number,
                "message": "main is unhealthy; Sandcastle will not start new targeted work until default-branch health recovers",
            }
        )
        return _OrchestrationStepResult(
            exit_code=0,
            payload=payload,
            chosen_issue_number=issue_number,
        )

    if not _can_pick_new_issue(active_statuses, repo_config.scheduler):
        payload = _base_orchestration_payload(
            repo_root,
            paths,
            issue_policy,
            repo_config,
            step_kind="target-capacity-blocked",
            lease_conflicts=lease_conflicts,
        )
        payload.update(
            {
                "status": "idle",
                "operator_state": "waiting",
                "issue_number": issue_number,
                "chosen_issue_number": issue_number,
                "message": "scheduler capacity prevents starting the targeted issue right now",
            }
        )
        return _OrchestrationStepResult(
            exit_code=0,
            payload=payload,
            chosen_issue_number=issue_number,
        )

    try:
        with issue_lease_guard(
            paths,
            repo_config.scheduler,
            issue_number=issue_number,
            operation="claim-and-setup",
            status="eligible",
        ):
            exit_code, handler_payload = _capture_operation_output(
                _handle_run_issue,
                repo_root,
                paths,
                issue_policy,
                repo_config,
                issue_number,
            )
            payload = _base_orchestration_payload(
                repo_root,
                paths,
                issue_policy,
                repo_config,
                step_kind="claim-and-setup-targeted",
                lease_conflicts=lease_conflicts,
            )
            _attach_issue_result(payload, paths, issue_number, handler_payload)
            return _OrchestrationStepResult(
                exit_code=exit_code,
                payload=payload,
                chosen_issue_number=issue_number,
            )
    except IssueLeaseUnavailable as error:
        lease_conflicts.append(error.payload)

    payload = _base_orchestration_payload(
        repo_root,
        paths,
        issue_policy,
        repo_config,
        step_kind="target-lease-conflict",
        lease_conflicts=lease_conflicts,
    )
    payload.update(
        {
            "status": "idle",
            "operator_state": "waiting",
            "issue_number": issue_number,
            "chosen_issue_number": issue_number,
            "message": "the targeted issue is currently leased by another Sandcastle operation",
        }
    )
    return _OrchestrationStepResult(
        exit_code=0,
        payload=payload,
        chosen_issue_number=issue_number,
    )


def _process_until_boundary(
    repo_root: Path,
    paths,
    issue_policy: IssuePolicy,
    repo_config: RepoConfig,
    gates_config: GatesConfig,
    target_issue_number: int | None = None,
    max_steps: int = 50,
) -> tuple[int, dict[str, object]]:
    chosen_issue_number = target_issue_number

    for micro_step in range(1, max_steps + 1):
        before_statuses = list_issue_statuses(paths)
        before_active = _list_active_issues(paths)
        before_signature = _active_signatures(before_active)
        before_blocked_signature = _blocked_signatures(before_statuses)

        step_result = _execute_orchestration_step(
            repo_root,
            paths,
            issue_policy,
            repo_config,
            gates_config,
            target_issue_number=chosen_issue_number,
        )
        if chosen_issue_number is None and step_result.chosen_issue_number is not None:
            chosen_issue_number = step_result.chosen_issue_number

        after_statuses = list_issue_statuses(paths)
        after_active = _list_active_issues(paths)
        after_signature = _active_signatures(after_active)
        after_blocked_signature = _blocked_signatures(after_statuses)

        if (
            step_result.payload.get("status") == "blocked"
            and step_result.payload.get("block_scope") == BLOCK_SCOPE_QUEUE
        ):
            return 1, _build_boundary_summary(
                repo_root,
                paths,
                issue_policy,
                repo_config,
                chosen_issue_number or step_result.chosen_issue_number,
                status="blocked",
                stop_category=str(
                    step_result.payload.get("stop_category") or "repo-global-blocker"
                ),
                stop_reason=str(
                    step_result.payload.get("message")
                    or "queue is paused by a repo-global blocker that requires human intervention"
                ),
                micro_steps=micro_step,
                last_step=step_result,
            )

        resulting_status = (
            None
            if chosen_issue_number is None
            else try_load_issue_status(paths, chosen_issue_number)
        )
        if resulting_status is not None and resulting_status.status == "blocked":
            return 1, _build_boundary_summary(
                repo_root,
                paths,
                issue_policy,
                repo_config,
                chosen_issue_number,
                status="blocked",
                stop_category="real-blocker",
                stop_reason="blocked issue requires human intervention before Sandcastle can continue",
                micro_steps=micro_step,
                last_step=step_result,
            )
        if resulting_status is not None:
            operator_state = _operator_state(resulting_status)
            if operator_state == "waiting":
                return 0, _build_boundary_summary(
                    repo_root,
                    paths,
                    issue_policy,
                    repo_config,
                    chosen_issue_number,
                    status="waiting",
                    stop_category="stable-waiting",
                    stop_reason=_operator_wait_reason(resulting_status)
                    or f"issue {chosen_issue_number} reached a stable waiting state",
                    micro_steps=micro_step,
                    last_step=step_result,
                )
            if operator_state == "done":
                return 0, _build_boundary_summary(
                    repo_root,
                    paths,
                    issue_policy,
                    repo_config,
                    chosen_issue_number,
                    status="done",
                    stop_category="done-boundary",
                    stop_reason=f"issue {chosen_issue_number} reached a done boundary",
                    micro_steps=micro_step,
                    last_step=step_result,
                )

        if (
            chosen_issue_number is not None
            and step_result.payload.get("status") in DONE_STATUSES
        ):
            return 0, _build_boundary_summary(
                repo_root,
                paths,
                issue_policy,
                repo_config,
                chosen_issue_number,
                status="done",
                stop_category="done-boundary",
                stop_reason=f"issue {chosen_issue_number} reached a done boundary",
                micro_steps=micro_step,
                last_step=step_result,
            )

        if (
            step_result.payload.get("operator_state") == "waiting"
            and resulting_status is None
        ):
            return 0, _build_boundary_summary(
                repo_root,
                paths,
                issue_policy,
                repo_config,
                chosen_issue_number,
                status="waiting",
                stop_category="idle-wait",
                stop_reason=str(
                    step_result.payload.get("message")
                    or _waiting_reason(
                        after_active,
                        _safe_select_backlog_issue(repo_root, paths, issue_policy),
                        repo_config.scheduler,
                        blocked_issue_count=len(_list_blocked_issues(paths)),
                    )
                ),
                micro_steps=micro_step,
                last_step=step_result,
            )

        no_state_change = (
            before_signature == after_signature
            and before_blocked_signature == after_blocked_signature
        )
        if step_result.exit_code != 0 and no_state_change:
            return step_result.exit_code, _build_boundary_summary(
                repo_root,
                paths,
                issue_policy,
                repo_config,
                chosen_issue_number,
                status="error",
                stop_category="runtime-error",
                stop_reason=str(
                    step_result.payload.get("message")
                    or "orchestration step failed without changing Sandcastle state"
                ),
                micro_steps=micro_step,
                last_step=step_result,
            )

    return 1, _build_boundary_summary(
        repo_root,
        paths,
        issue_policy,
        repo_config,
        chosen_issue_number,
        status="error",
        stop_category="iteration-limit",
        stop_reason=f"orchestration exceeded the {max_steps}-step safety limit without reaching a stable boundary",
        micro_steps=max_steps,
        last_step=None,
    )


def _build_boundary_summary(
    repo_root: Path,
    paths,
    issue_policy: IssuePolicy,
    repo_config: RepoConfig,
    chosen_issue_number: int | None,
    *,
    status: str,
    stop_category: str,
    stop_reason: str,
    micro_steps: int,
    last_step: _OrchestrationStepResult | None,
) -> dict[str, object]:
    active_statuses = _list_active_issues(paths)
    blocked_statuses = _list_blocked_issues(paths)
    backlog = _safe_select_backlog_issue(repo_root, paths, issue_policy)
    summary = {
        "status": status,
        "operator_state": status if status in {"waiting", "blocked", "done"} else None,
        "chosen_issue_number": chosen_issue_number,
        "stop_category": stop_category,
        "stop_reason": stop_reason,
        "micro_steps": micro_steps,
        **_scheduler_snapshot(
            active_statuses,
            backlog,
            repo_config.scheduler,
            blocked_issue_count=len(blocked_statuses),
        ),
    }
    if last_step is not None:
        summary["last_step_kind"] = last_step.payload.get("step_kind")
        summary["lease_conflicts"] = last_step.payload.get("lease_conflicts", [])

    if chosen_issue_number is not None:
        summary["issue_number"] = chosen_issue_number
        local_status = try_load_issue_status(paths, chosen_issue_number)
        if local_status is not None:
            summary["resulting_state"] = _resulting_state_payload(local_status)
        elif last_step is not None and last_step.payload.get("resulting_state") is not None:
            summary["resulting_state"] = last_step.payload.get("resulting_state")
        else:
            summary["resulting_state"] = None
    else:
        summary["resulting_state"] = None

    if status == "blocked":
        blocked_scope = (
            last_step.payload.get("block_scope") if last_step is not None else None
        )
        if blocked_scope not in {BLOCK_SCOPE_ISSUE, BLOCK_SCOPE_QUEUE}:
            local_status = (
                None
                if chosen_issue_number is None
                else try_load_issue_status(paths, chosen_issue_number)
            )
            if local_status is not None and local_status.status == "blocked":
                blocked_scope = _blocked_issue_scope(local_status)
        summary["block_scope"] = (
            blocked_scope
            if blocked_scope in {BLOCK_SCOPE_ISSUE, BLOCK_SCOPE_QUEUE}
            else BLOCK_SCOPE_ISSUE
        )

    return summary


def _safe_select_backlog_issue(
    repo_root: Path, paths, issue_policy: IssuePolicy
) -> BacklogSelection:
    try:
        return _select_backlog_issue(repo_root, paths, issue_policy)
    except Exception:
        return BacklogSelection(selected_issue=None)


def _handle_run_loop(
    repo_root: Path,
    paths,
    issue_policy: IssuePolicy,
    repo_config: RepoConfig,
    gates_config: GatesConfig,
    poll_interval_seconds: int,
    max_idle_polls: int,
) -> int:
    idle_polls = 0
    poll_interval_seconds = max(1, poll_interval_seconds)

    while True:
        exit_code, summary = _process_until_boundary(
            repo_root, paths, issue_policy, repo_config, gates_config
        )
        _persist_operator_reports(paths, repo_config)
        if summary["status"] == "blocked":
            _emit_json(summary)
            if summary.get("block_scope") == BLOCK_SCOPE_QUEUE:
                return exit_code
            idle_polls = 0
            continue

        if summary["status"] == "error":
            summary["exit_code"] = exit_code
            _emit_json(summary)
            return exit_code

        if summary["status"] == "done":
            idle_polls = 0
            continue

        idle_polls += 1
        waiting_summary = {
            **summary,
            "status": "waiting",
            "operator_state": "waiting",
            "poll_interval_seconds": poll_interval_seconds,
            "idle_polls": idle_polls,
        }
        _emit_json(waiting_summary)
        if max_idle_polls > 0 and idle_polls >= max_idle_polls:
            stopped_summary = {
                **waiting_summary,
                "status": "stopped",
                "stop_category": "operator-policy",
                "stop_reason": "bounded run reached the configured max idle polls",
            }
            _emit_json(stopped_summary)
            return 0
        time.sleep(poll_interval_seconds)


def _handle_status(
    repo_root: Path, paths, issue_policy: IssuePolicy, issue_number: int
) -> int:
    issue = get_issue(repo_root, issue_number)
    decision = evaluate_issue(issue, issue_policy)
    local_status = try_load_issue_status(paths, issue_number)

    response = {
        "issue": {
            "number": issue.number,
            "title": issue.title,
            "state": issue.state,
            "labels": list(issue.labels),
            "assignees": list(issue.assignees),
            "url": issue.url,
        },
        "eligibility": {
            "eligible": decision.eligible,
            "reasons": list(decision.reasons),
            "dependencies": _serialize_dependency_status(decision),
        },
        "local_state": (
            None
            if local_status is None
            else _serialize_local_state(local_status)
        ),
    }

    print(json.dumps(response, indent=2))
    return 0


def _handle_report(paths, repo_config: RepoConfig) -> int:
    report, _ = _persist_operator_reports(paths, repo_config)
    _emit_json(report)
    return 0


def _handle_morning_handover(paths, repo_config: RepoConfig) -> int:
    _, morning_handover = _persist_operator_reports(paths, repo_config)
    _emit_json(morning_handover)
    return 0


def _persist_operator_reports(
    paths, repo_config: RepoConfig
) -> tuple[dict[str, object], dict[str, object]]:
    report = _build_operator_report(paths, repo_config)
    morning_handover = _optional_payload_mapping(report.get("morning_handover"))
    write_json_file(paths.state_dir / "operator-report.json", report)
    write_json_file(paths.state_dir / "morning-handover.json", morning_handover)
    return report, morning_handover


def _build_operator_report(paths, repo_config: RepoConfig) -> dict[str, object]:
    tracked_statuses = list_issue_statuses(paths)
    active_statuses = _list_active_issues(paths)
    blocked_statuses = _list_blocked_issues(paths)
    pending_approvals = _pending_approval_entries(active_statuses)
    cleanup_tasks = _cleanup_task_entries(tracked_statuses)
    main_health = _load_main_health(paths)
    open_incidents = _load_open_incidents(paths)
    last_successful_merge = _last_successful_merge(tracked_statuses)
    required_human_actions = _required_human_actions(
        blocked_statuses,
        pending_approvals,
        main_health,
        open_incidents,
        cleanup_tasks,
    )
    queue_health = _queue_health_payload(
        tracked_statuses,
        blocked_statuses,
        cleanup_tasks,
        main_health,
        required_human_actions,
    )
    current_issue = _current_issue_summary(active_statuses, blocked_statuses)
    morning_handover = _build_morning_handover_payload(
        queue_health,
        current_issue,
        blocked_statuses,
        pending_approvals,
        open_incidents,
        cleanup_tasks,
        last_successful_merge,
        required_human_actions,
    )
    return {
        "generated_at_utc": utc_now_iso(),
        "repo": repo_config.repo,
        "queue_health": queue_health,
        "current_issue": current_issue,
        "active_issues": [_issue_report_entry(status) for status in active_statuses],
        "blocked_issues": [_issue_report_entry(status) for status in blocked_statuses],
        "pending_approvals": pending_approvals,
        "cleanup_tasks": cleanup_tasks,
        "open_incidents": open_incidents,
        "main_health": main_health,
        "last_successful_merge": last_successful_merge,
        "required_human_actions": required_human_actions,
        "morning_handover": morning_handover,
    }


def _queue_health_payload(
    tracked_statuses: list[IssueStatus],
    blocked_statuses: list[IssueStatus],
    cleanup_tasks: list[dict[str, object]],
    main_health: dict[str, object],
    required_human_actions: list[dict[str, object]],
) -> dict[str, object]:
    active_count = sum(
        1 for status in tracked_statuses if _operator_state(status) == "active"
    )
    waiting_count = sum(
        1 for status in tracked_statuses if _operator_state(status) == "waiting"
    )
    done_count = sum(1 for status in tracked_statuses if _operator_state(status) == "done")
    main_status = str(main_health.get("status") or "healthy")
    if main_status == "unhealthy":
        status = "unhealthy-main"
        summary = "main is unhealthy; new merges should remain paused"
    elif blocked_statuses:
        status = "attention-needed"
        summary = (
            "issue-local blockers need attention, but unrelated eligible work can still continue"
        )
    elif required_human_actions:
        status = "attention-needed"
        summary = "operator attention is required before the queue is fully healthy"
    elif active_count or waiting_count:
        status = "healthy"
        summary = "queue is progressing without known blockers"
    else:
        status = "idle"
        summary = "no tracked Sandcastle work is currently active"
    return {
        "status": status,
        "summary": summary,
        "tracked_issue_count": len(tracked_statuses),
        "active_issue_count": active_count,
        "waiting_issue_count": waiting_count,
        "blocked_issue_count": len(blocked_statuses),
        "done_issue_count": done_count,
        "cleanup_task_count": len(cleanup_tasks),
        "requires_human_action": bool(required_human_actions),
    }


def _current_issue_summary(
    active_statuses: list[IssueStatus],
    blocked_statuses: list[IssueStatus],
) -> dict[str, object] | None:
    selected = _select_next_actionable_issue(active_statuses)
    if selected is None:
        selected = _select_next_monitorable_issue(active_statuses)
    if selected is None and blocked_statuses:
        selected = blocked_statuses[0]
    if selected is None:
        return None
    return _issue_report_entry(selected)


def _issue_report_entry(local_status: IssueStatus) -> dict[str, object]:
    issue_title = _optional_payload_str(local_status.extra.get("issue_title"))
    blocked_next_action = _optional_payload_str(local_status.extra.get("blocked_next_action"))
    return {
        "issue_number": local_status.issue_number,
        "issue_title": issue_title,
        "status": local_status.status,
        "operator_state": _operator_state(local_status),
        "operator_wait_reason": _operator_wait_reason(local_status),
        "active_agent_role": _active_agent_role(local_status),
        "review_status": _review_status(local_status),
        "validation_status": _validation_status(local_status),
        "ci_status": _optional_payload_str(local_status.extra.get("ci_status")),
        "branch": local_status.branch,
        "worktree_path": local_status.worktree_path,
        "pr_number": local_status.pr_number,
        "pr_url": _optional_payload_str(local_status.extra.get("pr_url")),
        "blocked_reason": _optional_payload_str(local_status.extra.get("blocked_reason")),
        "next_action": blocked_next_action,
        "last_updated_utc": local_status.last_updated_utc,
    }


def _active_agent_role(local_status: IssueStatus) -> str | None:
    if local_status.status == "reviewing":
        return "review-agent"
    if local_status.status == "resolving-conflicts":
        return "conflict-resolution-agent"
    if local_status.status in {
        "claiming",
        "claimed",
        "branch-created",
        "context-loaded",
        "implementing",
        "verifying",
        "revising",
        "review-fix-loop",
        "ready-for-pr",
    }:
        return "development-agent"
    if local_status.status == "pr-open" and _operator_state(local_status) == "active":
        return "development-agent"
    return None


def _validation_status(local_status: IssueStatus) -> str | None:
    for key in ("verification_status", "ci_status", "auto_merge_status"):
        value = _optional_payload_str(local_status.extra.get(key))
        if value:
            return value
    return None


def _review_status(local_status: IssueStatus) -> str | None:
    for key in ("review_status", "findings_status", "pr_review_decision"):
        value = _optional_payload_str(local_status.extra.get(key))
        if value:
            return value
    return None


def _pending_approval_entries(
    active_statuses: list[IssueStatus],
) -> list[dict[str, object]]:
    approvals: list[dict[str, object]] = []
    for status in active_statuses:
        if not _is_waiting_for_human_status(status.status):
            continue
        approvals.append(
            {
                "issue_number": status.issue_number,
                "issue_title": _optional_payload_str(status.extra.get("issue_title")),
                "status": status.status,
                "pr_number": status.pr_number,
                "pr_url": _optional_payload_str(status.extra.get("pr_url")),
                "waiting_for": (
                    "human review"
                    if status.status == "waiting-human-review"
                    else "merge approval"
                ),
                "next_action": (
                    f"Review PR #{status.pr_number} for issue #{status.issue_number}."
                    if status.pr_number is not None
                    else f"Review the waiting branch for issue #{status.issue_number}."
                ),
                "last_updated_utc": status.last_updated_utc,
            }
        )
    approvals.sort(
        key=lambda item: (
            str(item.get("last_updated_utc") or ""),
            int(item["issue_number"]),
        )
    )
    return approvals


def _cleanup_task_entries(
    tracked_statuses: list[IssueStatus],
) -> list[dict[str, object]]:
    tasks: list[dict[str, object]] = []
    for status in tracked_statuses:
        cleanup_actions = status.extra.get("cleanup_actions")
        if status.status == "merged" or cleanup_actions:
            actions = (
                list(cleanup_actions)
                if isinstance(cleanup_actions, list)
                else []
            )
            tasks.append(
                {
                    "issue_number": status.issue_number,
                    "issue_title": _optional_payload_str(status.extra.get("issue_title")),
                    "status": status.status,
                    "pr_number": status.pr_number,
                    "pr_url": _optional_payload_str(status.extra.get("pr_url")),
                    "cleanup_completed": bool(status.extra.get("cleanup_completed")),
                    "actions": actions,
                    "next_action": (
                        "Retry cleanup and verify the branch and worktree were removed."
                        if not bool(status.extra.get("cleanup_completed"))
                        else None
                    ),
                    "last_updated_utc": status.last_updated_utc,
                }
            )
    tasks.sort(
        key=lambda item: (
            str(item.get("last_updated_utc") or ""),
            int(item["issue_number"]),
        )
    )
    return tasks


def _load_main_health(paths) -> dict[str, object]:
    path = paths.state_dir / "main-health.json"
    if not path.exists():
        return {
            "status": "healthy",
            "healthy": True,
            "reason": None,
            "incident_issue_number": None,
            "next_action": None,
        }
    payload = read_json_file(path)
    status = str(payload.get("status") or "healthy")
    return {
        "status": status,
        "healthy": status != "unhealthy",
        "reason": _optional_payload_str(payload.get("reason")),
        "incident_issue_number": payload.get("incident_issue_number"),
        "next_action": _optional_payload_str(payload.get("next_action")),
    }


def _load_open_incidents(paths) -> list[dict[str, object]]:
    path = paths.state_dir / "incidents.json"
    if not path.exists():
        return []
    payload = read_json_file(path)
    incidents_payload = payload.get("incidents")
    if not isinstance(incidents_payload, list):
        return []

    incidents: list[dict[str, object]] = []
    for item in incidents_payload:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "open")
        if status in {"closed", "resolved"}:
            continue
        incidents.append(
            {
                "id": _optional_payload_str(item.get("id")),
                "status": status,
                "summary": _optional_payload_str(item.get("summary")),
                "issue_number": item.get("issue_number"),
                "requires_human_action": bool(item.get("requires_human_action")),
                "next_action": _optional_payload_str(item.get("next_action")),
            }
        )
    return incidents


def _load_incident_records(paths) -> list[dict[str, object]]:
    path = paths.state_dir / "incidents.json"
    if not path.exists():
        return []
    payload = read_json_file(path)
    incidents = payload.get("incidents")
    if not isinstance(incidents, list):
        return []
    return [item for item in incidents if isinstance(item, dict)]


def _write_incident_records(paths, incidents: list[dict[str, object]]) -> None:
    write_json_file(paths.state_dir / "incidents.json", {"incidents": incidents})


def _post_merge_incident_id(local_status: IssueStatus) -> str:
    existing = _optional_payload_str(local_status.extra.get("post_merge_incident_id"))
    if existing:
        return existing
    return f"main-health-issue-{local_status.issue_number}"


def _matching_post_merge_incident(
    incidents: list[dict[str, object]], local_status: IssueStatus
) -> tuple[int | None, dict[str, object] | None]:
    incident_id = _post_merge_incident_id(local_status)
    for index, incident in enumerate(incidents):
        if _optional_payload_str(incident.get("id")) == incident_id:
            return index, incident

    for index, incident in enumerate(incidents):
        if (
            incident.get("status") not in {"resolved", "closed"}
            and incident.get("issue_number") == local_status.issue_number
            and incident.get("pr_number") == local_status.pr_number
        ):
            return index, incident
    return None, None


def _record_post_merge_failure(
    paths,
    local_status: IssueStatus,
    health_payload: dict[str, object],
) -> dict[str, object]:
    incidents = _load_incident_records(paths)
    incident_index, existing = _matching_post_merge_incident(incidents, local_status)
    incident_id = _post_merge_incident_id(local_status)
    now = utc_now_iso()
    summary = (
        f"Main health failed after merge of issue #{local_status.issue_number}."
    )
    next_action = (
        "Investigate the failing main build before allowing more merges."
    )
    incident = {
        **({} if existing is None else existing),
        "id": incident_id,
        "status": "open",
        "summary": summary,
        "issue_number": local_status.issue_number,
        "pr_number": local_status.pr_number,
        "merge_commit_sha": _optional_payload_str(local_status.extra.get("pr_head_sha")),
        "main_head_sha": _optional_payload_str(health_payload.get("head_sha")),
        "current_health": str(health_payload.get("status") or "failed"),
        "failing_checks": health_payload.get("failed_checks", []),
        "attempted_recovery": {
            "strategy": "Keep monitoring main until all required checks pass.",
            "last_checked_at_utc": now,
            "last_known_status": str(health_payload.get("status") or "failed"),
        },
        "requires_human_action": True,
        "next_action": next_action,
        "opened_at_utc": _optional_payload_str(
            None if existing is None else existing.get("opened_at_utc")
        )
        or now,
        "last_updated_utc": now,
    }
    if incident_index is None:
        incidents.append(incident)
    else:
        incidents[incident_index] = incident
    _write_incident_records(paths, incidents)
    return incident


def _resolve_post_merge_incident(
    paths,
    local_status: IssueStatus,
    health_payload: dict[str, object],
) -> dict[str, object] | None:
    incidents = _load_incident_records(paths)
    incident_index, existing = _matching_post_merge_incident(incidents, local_status)
    if incident_index is None or existing is None:
        return None

    resolved = {
        **existing,
        "status": "resolved",
        "current_health": str(health_payload.get("status") or "passed"),
        "main_head_sha": _optional_payload_str(health_payload.get("head_sha"))
        or _optional_payload_str(existing.get("main_head_sha")),
        "resolved_at_utc": utc_now_iso(),
        "last_updated_utc": utc_now_iso(),
        "requires_human_action": False,
    }
    incidents[incident_index] = resolved
    _write_incident_records(paths, incidents)
    return resolved


def _write_main_health_status(
    paths,
    *,
    status: str,
    local_status: IssueStatus,
    health_payload: dict[str, object],
    incident: dict[str, object] | None,
    reason: str,
    next_action: str | None,
) -> None:
    write_json_file(
        paths.state_dir / "main-health.json",
        {
            "status": status,
            "reason": reason,
            "incident_issue_number": (
                None if incident is None else incident.get("issue_number")
            ),
            "next_action": next_action,
            "source_issue_number": local_status.issue_number,
            "pr_number": local_status.pr_number,
            "head_sha": _optional_payload_str(health_payload.get("head_sha")),
            "failed_checks": health_payload.get("failed_checks", []),
            "checked_at_utc": utc_now_iso(),
        },
    )


def _last_successful_merge(
    tracked_statuses: list[IssueStatus],
) -> dict[str, object] | None:
    merged_candidates = [
        status
        for status in tracked_statuses
        if status.status == "closed" and status.extra.get("pr_merged_at")
    ]
    if not merged_candidates:
        return None
    merged_candidates.sort(
        key=lambda status: (
            str(status.extra.get("pr_merged_at") or ""),
            status.issue_number,
        )
    )
    latest = merged_candidates[-1]
    return {
        "issue_number": latest.issue_number,
        "issue_title": _optional_payload_str(latest.extra.get("issue_title")),
        "pr_number": latest.pr_number,
        "pr_url": _optional_payload_str(latest.extra.get("pr_url")),
        "merged_at": _optional_payload_str(latest.extra.get("pr_merged_at")),
        "branch": _optional_payload_str(latest.extra.get("closed_branch")) or latest.branch,
    }


def _required_human_actions(
    blocked_statuses: list[IssueStatus],
    pending_approvals: list[dict[str, object]],
    main_health: dict[str, object],
    open_incidents: list[dict[str, object]],
    cleanup_tasks: list[dict[str, object]],
) -> list[dict[str, object]]:
    actions: list[dict[str, object]] = []
    for status in blocked_statuses:
        actions.append(
            {
                "source": "blocked-issue",
                "issue_number": status.issue_number,
                "summary": _optional_payload_str(status.extra.get("blocked_reason"))
                or f"issue #{status.issue_number} is blocked",
                "next_action": _optional_payload_str(status.extra.get("blocked_next_action"))
                or "Inspect the blocked issue and decide the next safe move.",
            }
        )
    for approval in pending_approvals:
        actions.append(
            {
                "source": "pending-approval",
                "issue_number": approval["issue_number"],
                "summary": f"issue #{approval['issue_number']} is waiting for {approval['waiting_for']}",
                "next_action": approval["next_action"],
            }
        )
    if not bool(main_health.get("healthy")) and main_health.get("next_action"):
        actions.append(
            {
                "source": "main-health",
                "issue_number": main_health.get("incident_issue_number"),
                "summary": _optional_payload_str(main_health.get("reason"))
                or "main is unhealthy",
                "next_action": _optional_payload_str(main_health.get("next_action")),
            }
        )
    for incident in open_incidents:
        if not incident.get("requires_human_action") or not incident.get("next_action"):
            continue
        actions.append(
            {
                "source": "incident",
                "issue_number": incident.get("issue_number"),
                "summary": incident.get("summary"),
                "next_action": incident.get("next_action"),
            }
        )
    for cleanup in cleanup_tasks:
        if cleanup.get("cleanup_completed") or not cleanup.get("next_action"):
            continue
        actions.append(
            {
                "source": "cleanup",
                "issue_number": cleanup["issue_number"],
                "summary": f"cleanup is still pending for issue #{cleanup['issue_number']}",
                "next_action": cleanup["next_action"],
            }
        )
    return actions


def _build_morning_handover_payload(
    queue_health: dict[str, object],
    current_issue: dict[str, object] | None,
    blocked_statuses: list[IssueStatus],
    pending_approvals: list[dict[str, object]],
    open_incidents: list[dict[str, object]],
    cleanup_tasks: list[dict[str, object]],
    last_successful_merge: dict[str, object] | None,
    required_human_actions: list[dict[str, object]],
) -> dict[str, object]:
    headline = _morning_handover_headline(
        queue_health,
        blocked_statuses,
        pending_approvals,
        open_incidents,
        current_issue,
    )
    return {
        "generated_at_utc": utc_now_iso(),
        "headline": headline,
        "queue_health": queue_health,
        "current_issue": current_issue,
        "blocked_issues": [_issue_report_entry(status) for status in blocked_statuses],
        "pending_approvals": pending_approvals,
        "open_incidents": open_incidents,
        "cleanup_tasks": cleanup_tasks,
        "last_successful_merge": last_successful_merge,
        "required_human_actions": required_human_actions,
    }


def _morning_handover_headline(
    queue_health: dict[str, object],
    blocked_statuses: list[IssueStatus],
    pending_approvals: list[dict[str, object]],
    open_incidents: list[dict[str, object]],
    current_issue: dict[str, object] | None,
) -> str:
    queue_status = str(queue_health.get("status") or "idle")
    if queue_status == "unhealthy-main":
        return (
            "Sandcastle is paused because main is unhealthy; investigate the latest incident before merging more work."
        )
    if blocked_statuses:
        if current_issue is not None and current_issue.get("operator_state") != "blocked":
            return (
                f"Sandcastle has {len(blocked_statuses)} blocked issue(s), but queue work continues on "
                f"issue #{current_issue['issue_number']}."
            )
        return (
            f"Sandcastle has {len(blocked_statuses)} blocked issue(s); unrelated eligible work can continue "
            f"once more backlog work is available."
        )
    if current_issue is not None:
        return (
            f"Sandcastle is currently on issue #{current_issue['issue_number']} "
            f"({current_issue['status']})."
        )
    if open_incidents:
        return "Sandcastle is idle, but open incidents still need attention."
    return "Sandcastle is idle and no tracked issues currently need attention."


def _handle_collect_findings(paths, repo_config: RepoConfig, issue_number: int) -> int:
    local_status = try_load_issue_status(paths, issue_number)
    if local_status is None:
        print(
            json.dumps(
                {
                    "error": "no local state exists for this issue",
                    "issue_number": issue_number,
                },
                indent=2,
            )
        )
        return 1

    verification_payload = None
    verification_path = local_status.extra.get("verification_path")
    if verification_path:
        verification_payload = read_json_file(Path(str(verification_path)))
    ci_payload = _load_ci_payload(local_status)

    findings_path = collect_findings(
        repo_root=repo_root_from_file(__file__),
        repo_name=repo_config.repo,
        findings_dir=paths.findings_dir,
        issue_number=issue_number,
        pr_number=local_status.pr_number,
        verification_payload=verification_payload,
        ci_payload=ci_payload,
    )
    findings_payload = read_json_file(findings_path)
    findings_fingerprint = _findings_fingerprint(findings_payload)
    repeated_findings = _is_repeated_finding_set(local_status, findings_fingerprint)
    updated = IssueStatus(
        issue_number=local_status.issue_number,
        status=(
            "blocked"
            if repeated_findings
            else "revising"
            if findings_payload["status"] == "findings"
            else "ready-for-human"
        ),
        branch=local_status.branch,
        worktree_path=local_status.worktree_path,
        pr_number=local_status.pr_number,
        last_updated_utc=utc_now_iso(),
        extra={
            **local_status.extra,
            "findings_path": str(findings_path),
            "findings_status": findings_payload["status"],
            "findings_count": len(findings_payload.get("findings", [])),
            "findings_fingerprint": findings_fingerprint,
            "findings_source_implementation_path": local_status.extra.get(
                "implementation_path"
            ),
            "findings_source_head_sha": _current_issue_revision_sha(local_status),
            "findings_are_stale": False,
            "error": (
                "review findings repeated unchanged after a new implementation pass"
                if repeated_findings
                else None
            ),
        },
    )
    write_issue_status(paths, updated)
    print(
        json.dumps(
            {
                "issue_number": issue_number,
                "status": updated.status,
                "operator_state": _operator_state(updated),
                "findings_path": str(findings_path),
                "findings_count": len(findings_payload.get("findings", [])),
                "repeated_findings": repeated_findings,
            },
            indent=2,
        )
    )
    return 0


def _handle_cleanup(
    repo_root: Path, paths, repo_config: RepoConfig, issue_number: int
) -> int:
    local_status = try_load_issue_status(paths, issue_number)
    if local_status is None:
        print(
            json.dumps(
                {
                    "error": "no local state exists for this issue",
                    "issue_number": issue_number,
                },
                indent=2,
            )
        )
        return 1

    if _is_failed_startup_cleanup_candidate(repo_root, repo_config, local_status):
        return _cleanup_failed_startup_issue(
            repo_root, paths, repo_config, local_status
        )
    cleanup_status = _hydrate_cleanup_state(repo_root, local_status)
    if cleanup_status.pr_number is None:
        print(
            json.dumps(
                {
                    "error": "cleanup requires either a merged PR or a safe failed-startup state",
                    "issue_number": issue_number,
                    "status": cleanup_status.status,
                },
                indent=2,
            )
        )
        return 1
    synced = _sync_pr_state(repo_root, paths, repo_config, cleanup_status)
    if synced.status != "merged":
        print(
            json.dumps(
                {
                    "error": "cleanup only runs after merge",
                    "issue_number": issue_number,
                    "status": synced.status,
                    "pr_number": synced.pr_number,
                },
                indent=2,
            )
        )
        return 1

    fetch_base_ref(repo_root, repo_config)

    cleanup_actions = _existing_cleanup_actions(local_status)
    branch_deleted = False
    remote_branch_deleted = False
    worktree_removed = False

    if repo_config.cleanup.remove_worktree_after_merge and synced.worktree_path:
        worktree_path = Path(synced.worktree_path)
        if not worktree_path.exists():
            worktree_removed = True
            _append_cleanup_action(
                cleanup_actions,
                f"worktree {synced.worktree_path} was already absent",
            )
        else:
            try:
                remove_worktree(repo_root, synced.worktree_path)
                worktree_removed = True
                _append_cleanup_action(
                    cleanup_actions, f"removed worktree {synced.worktree_path}"
                )
            except RuntimeError as error:
                if not worktree_is_registered(repo_root, synced.worktree_path):
                    remove_orphaned_worktree_directory(
                        repo_root,
                        repo_config.worktree.root,
                        synced.worktree_path,
                    )
                    worktree_removed = True
                    _append_cleanup_action(
                        cleanup_actions,
                        f"removed orphaned managed worktree directory {synced.worktree_path} after git had already deregistered it",
                    )
                else:
                    worktree_changes = list_worktree_changes(synced.worktree_path)
                    if _can_force_remove_worktree(worktree_changes):
                        force_remove_worktree(repo_root, synced.worktree_path)
                        worktree_removed = True
                        _append_cleanup_action(
                            cleanup_actions,
                            f"force-removed worktree {synced.worktree_path} after merged PR because it only contained Sandcastle runtime artifacts",
                        )
                    else:
                        _append_cleanup_action(
                            cleanup_actions,
                            f"kept worktree {synced.worktree_path} because cleanup could not remove it ({error})",
                        )

    if repo_config.cleanup.delete_local_branch_after_merge and synced.branch:
        if not branch_exists(repo_root, synced.branch):
            branch_deleted = True
            _append_cleanup_action(
                cleanup_actions, f"local branch {synced.branch} was already absent"
            )
        else:
            try:
                delete_local_branch(repo_root, synced.branch)
                branch_deleted = True
                _append_cleanup_action(
                    cleanup_actions, f"deleted local branch {synced.branch}"
                )
            except RuntimeError:
                try:
                    force_delete_local_branch(repo_root, synced.branch)
                    branch_deleted = True
                    _append_cleanup_action(
                        cleanup_actions,
                        f"force-deleted local branch {synced.branch} after merged PR",
                    )
                except RuntimeError as error:
                    _append_cleanup_action(
                        cleanup_actions,
                        f"kept local branch {synced.branch} because cleanup could not delete it ({error})",
                    )

    if repo_config.cleanup.delete_remote_branch_after_merge and synced.branch:
        if not remote_branch_exists(repo_root, repo_config.remote, synced.branch):
            remote_branch_deleted = True
            _append_cleanup_action(
                cleanup_actions,
                f"remote branch {repo_config.remote}/{synced.branch} was already absent",
            )
        else:
            try:
                delete_remote_branch(repo_root, repo_config.remote, synced.branch)
                remote_branch_deleted = True
                _append_cleanup_action(
                    cleanup_actions,
                    f"deleted remote branch {repo_config.remote}/{synced.branch}",
                )
            except RuntimeError as error:
                if _is_already_absent_remote_branch_error(error):
                    remote_branch_deleted = True
                    _append_cleanup_action(
                        cleanup_actions,
                        f"remote branch {repo_config.remote}/{synced.branch} was already absent",
                    )
                else:
                    raise

    completed = IssueStatus(
        issue_number=synced.issue_number,
        status=POST_MERGE_HEALTH_STATUS,
        branch=None if branch_deleted else synced.branch,
        worktree_path=None if worktree_removed else synced.worktree_path,
        pr_number=synced.pr_number,
        last_updated_utc=utc_now_iso(),
        extra={
            **synced.extra,
            "cleanup_actions": cleanup_actions,
            "cleanup_completed": True,
            "completion_gate": "post-merge-health",
            "completion_state": "awaiting-post-merge-health",
            "completion_ready": False,
            "post_merge_health_status": "pending",
            "closed_branch": synced.branch,
            "closed_remote_branch": synced.branch if remote_branch_deleted else None,
            "closed_worktree_path": synced.worktree_path,
        },
    )

    write_issue_status(paths, completed)
    print(
        json.dumps(
            {
                "issue_number": issue_number,
                "status": completed.status,
                "cleanup_actions": cleanup_actions,
                "branch": completed.branch,
                "worktree_path": completed.worktree_path,
                "completion_state": completed.extra.get("completion_state"),
            },
            indent=2,
        )
    )
    return 0


def _handle_post_merge_health(
    repo_root: Path, paths, repo_config: RepoConfig, issue_number: int
) -> int:
    local_status = try_load_issue_status(paths, issue_number)
    if local_status is None or local_status.status != POST_MERGE_HEALTH_STATUS:
        print(
            json.dumps(
                {
                    "error": "issue is not waiting on post-merge health",
                    "issue_number": issue_number,
                },
                indent=2,
            )
        )
        return 1

    health_path = snapshot_main_checks(
        repo_root=repo_root,
        ci_dir=paths.ci_dir,
        issue_number=issue_number,
        branch_name=repo_config.default_branch,
    )
    health_payload = read_json_file(health_path)
    health_status = str(health_payload.get("status") or "pending")

    if health_status == "failed":
        incident = _record_post_merge_failure(paths, local_status, health_payload)
        _write_main_health_status(
            paths,
            status="unhealthy",
            local_status=local_status,
            health_payload=health_payload,
            incident=incident,
            reason="post-merge CI failed on main",
            next_action=str(incident.get("next_action") or ""),
        )
        updated = IssueStatus(
            issue_number=local_status.issue_number,
            status=local_status.status,
            branch=local_status.branch,
            worktree_path=local_status.worktree_path,
            pr_number=local_status.pr_number,
            last_updated_utc=utc_now_iso(),
            extra={
                **local_status.extra,
                "post_merge_health_path": str(health_path),
                "post_merge_health_status": health_status,
                "post_merge_incident_id": incident.get("id"),
                "completion_state": "main-unhealthy",
                "completion_ready": False,
            },
        )
        write_issue_status(paths, updated)
        print(
            json.dumps(
                {
                    "issue_number": issue_number,
                    "status": updated.status,
                    "main_health_status": "unhealthy",
                    "post_merge_health_status": health_status,
                    "incident_id": incident.get("id"),
                    "failed_checks": len(health_payload.get("failed_checks", [])),
                },
                indent=2,
            )
        )
        return 0

    if health_status == "passed":
        resolved_incident = _resolve_post_merge_incident(paths, local_status, health_payload)
        _write_main_health_status(
            paths,
            status="healthy",
            local_status=local_status,
            health_payload=health_payload,
            incident=resolved_incident,
            reason="post-merge health checks passed on main",
            next_action=None,
        )
        closed = IssueStatus(
            issue_number=local_status.issue_number,
            status="closed",
            branch=local_status.branch,
            worktree_path=local_status.worktree_path,
            pr_number=local_status.pr_number,
            last_updated_utc=utc_now_iso(),
            extra={
                **local_status.extra,
                "post_merge_health_path": str(health_path),
                "post_merge_health_status": health_status,
                "completion_state": "complete",
                "completion_ready": True,
            },
        )

        issue = get_issue(repo_root, issue_number)
        if repo_config.cleanup.close_issue_after_merge and issue.state.upper() != "CLOSED":
            close_issue(
                repo_root,
                issue_number,
                _render_completion_comment(paths, issue_number, closed),
            )
            cleanup_actions = _existing_cleanup_actions(closed)
            _append_cleanup_action(cleanup_actions, f"closed GitHub issue #{issue_number}")
            closed = IssueStatus(
                issue_number=closed.issue_number,
                status=closed.status,
                branch=closed.branch,
                worktree_path=closed.worktree_path,
                pr_number=closed.pr_number,
                last_updated_utc=utc_now_iso(),
                extra={
                    **closed.extra,
                    "cleanup_actions": cleanup_actions,
                    "issue_closed": True,
                },
            )
        elif not repo_config.cleanup.close_issue_after_merge:
            _write_completion_comment(repo_root, paths, issue_number, closed)

        write_issue_status(paths, closed)
        print(
            json.dumps(
                {
                    "issue_number": issue_number,
                    "status": closed.status,
                    "main_health_status": "healthy",
                    "post_merge_health_status": health_status,
                    "incident_resolved": resolved_incident is not None,
                },
                indent=2,
            )
        )
        return 0

    open_incident = _matching_post_merge_incident(
        _load_incident_records(paths), local_status
    )[1]
    main_status = "unhealthy" if open_incident is not None else "monitoring"
    _write_main_health_status(
        paths,
        status=main_status,
        local_status=local_status,
        health_payload=health_payload,
        incident=open_incident,
        reason=(
            "main is still recovering from the latest post-merge incident"
            if open_incident is not None
            else "post-merge health checks are still running on main"
        ),
        next_action=(
            _optional_payload_str(open_incident.get("next_action"))
            if open_incident is not None
            else None
        ),
    )
    updated = IssueStatus(
        issue_number=local_status.issue_number,
        status=local_status.status,
        branch=local_status.branch,
        worktree_path=local_status.worktree_path,
        pr_number=local_status.pr_number,
        last_updated_utc=utc_now_iso(),
        extra={
            **local_status.extra,
            "post_merge_health_path": str(health_path),
            "post_merge_health_status": health_status,
            "completion_state": "awaiting-post-merge-health",
            "completion_ready": False,
        },
    )
    write_issue_status(paths, updated)
    print(
        json.dumps(
            {
                "issue_number": issue_number,
                "status": updated.status,
                "main_health_status": main_status,
                "post_merge_health_status": health_status,
            },
            indent=2,
        )
    )
    return 0


def _handle_implement(
    repo_root: Path,
    paths,
    repo_config: RepoConfig,
    gates_config: GatesConfig,
    issue_number: int,
) -> int:
    local_status = try_load_issue_status(paths, issue_number)
    if (
        local_status is None
        or not local_status.branch
        or not local_status.worktree_path
    ):
        print(
            json.dumps(
                {
                    "error": "issue is not ready for implementation phase",
                    "issue_number": issue_number,
                },
                indent=2,
            )
        )
        return 1

    issue_title = str(local_status.extra.get("issue_title", f"Issue {issue_number}"))
    context_path = local_status.extra.get("context_path")
    findings_path = local_status.extra.get("findings_path")
    revision_path = local_status.extra.get("revision_path")
    implementation_mode = (
        "review-fix"
        if local_status.status in {"review-fix-loop", "revising"}
        or findings_path
        or revision_path
        else "initial"
    )
    prior_review_fix_attempt_count = int(
        local_status.extra.get("review_fix_attempt_count", 0) or 0
    )
    if (
        implementation_mode == "review-fix"
        and prior_review_fix_attempt_count >= gates_config.review.max_revision_loops
    ):
        blocked = IssueStatus(
            issue_number=local_status.issue_number,
            status="blocked",
            branch=local_status.branch,
            worktree_path=local_status.worktree_path,
            pr_number=local_status.pr_number,
            last_updated_utc=utc_now_iso(),
            extra={
                **local_status.extra,
                "implementation_mode": implementation_mode,
                "review_fix_attempt_count": prior_review_fix_attempt_count,
                "error": (
                    "review-fix loop reached the configured retry cap before Sandcastle could continue"
                ),
            },
        )
        write_issue_status(paths, blocked)
        print(
            json.dumps(
                {
                    "issue_number": issue_number,
                    "status": blocked.status,
                    "error": blocked.extra.get("error"),
                    "review_fix_attempt_count": prior_review_fix_attempt_count,
                    "max_review_fix_attempts": gates_config.review.max_revision_loops,
                },
                indent=2,
            )
        )
        return 1
    implementation_path = run_implementation_command(
        repo_root=repo_root,
        implementation_dir=paths.implementation_dir,
        policy=repo_config.implementation,
        issue_number=issue_number,
        branch=local_status.branch,
        worktree_path=local_status.worktree_path,
        issue_title=issue_title,
        context_path=str(context_path) if context_path else None,
        implementation_mode=implementation_mode,
        findings_path=str(findings_path) if findings_path else None,
        revision_path=str(revision_path) if revision_path else None,
    )
    implementation_payload = read_json_file(implementation_path)
    review_fix_attempt_count = (
        prior_review_fix_attempt_count + 1
        if implementation_mode == "review-fix"
        else prior_review_fix_attempt_count
    )
    if implementation_payload["status"] != "succeeded":
        blocked = IssueStatus(
            issue_number=local_status.issue_number,
            status="blocked",
            branch=local_status.branch,
            worktree_path=local_status.worktree_path,
            pr_number=local_status.pr_number,
            last_updated_utc=utc_now_iso(),
            extra={
                **local_status.extra,
                "implementation_path": str(implementation_path),
                "implementation_status": str(implementation_payload["status"]),
                "implementation_returncode": implementation_payload.get("returncode"),
                "implementation_mode": implementation_mode,
                "implementation_changed_files": _implementation_changed_files(
                    implementation_payload
                ),
                "implementation_diff_summary": _implementation_diff_summary(
                    implementation_payload
                ),
                "implementation_pre_command_head_sha": _implementation_optional_str(
                    implementation_payload.get("pre_command_head_sha")
                ),
                "implementation_post_command_head_sha": _implementation_optional_str(
                    implementation_payload.get("post_command_head_sha")
                ),
                "implementation_input_findings_path": (
                    str(findings_path) if findings_path else None
                ),
                "implementation_input_revision_path": (
                    str(revision_path) if revision_path else None
                ),
                "review_fix_attempt_count": review_fix_attempt_count,
                "error": _implementation_failure_reason(implementation_payload),
            },
        )
        write_issue_status(paths, blocked)
        if _is_failed_startup_cleanup_candidate(repo_root, repo_config, blocked):
            return _cleanup_failed_startup_issue(repo_root, paths, repo_config, blocked)
        print(
            json.dumps(
                {
                    "issue_number": issue_number,
                    "status": blocked.status,
                    "implementation_path": str(implementation_path),
                    "implementation_status": implementation_payload["status"],
                    "returncode": implementation_payload.get("returncode"),
                    "error": blocked.extra.get("error"),
                },
                indent=2,
            )
        )
        return 1

    meaningful_changed_files = _meaningful_implementation_changed_files(
        implementation_payload
    )
    if not meaningful_changed_files:
        if implementation_mode == "initial" and local_status.pr_number is None:
            completed = _complete_no_op_issue(
                repo_root=repo_root,
                paths=paths,
                repo_config=repo_config,
                local_status=local_status,
                implementation_path=implementation_path,
                implementation_payload=implementation_payload,
                implementation_mode=implementation_mode,
                findings_path=findings_path,
                revision_path=revision_path,
                review_fix_attempt_count=review_fix_attempt_count,
            )
            print(
                json.dumps(
                    {
                        "issue_number": issue_number,
                        "status": completed.status,
                        "operator_state": _operator_state(completed),
                        "implementation_path": str(implementation_path),
                        "implementation_status": "no-op",
                        "returncode": implementation_payload.get("returncode"),
                        "message": completed.extra.get("no_op_reason"),
                        "cleanup_actions": completed.extra.get("cleanup_actions", []),
                    },
                    indent=2,
                )
            )
            return 0

        blocked = IssueStatus(
            issue_number=local_status.issue_number,
            status="blocked",
            branch=local_status.branch,
            worktree_path=local_status.worktree_path,
            pr_number=local_status.pr_number,
            last_updated_utc=utc_now_iso(),
            extra={
                **local_status.extra,
                "implementation_path": str(implementation_path),
                "implementation_status": "no-op",
                "implementation_returncode": implementation_payload.get("returncode"),
                "implementation_mode": implementation_mode,
                "implementation_changed_files": _implementation_changed_files(
                    implementation_payload
                ),
                "implementation_diff_summary": _implementation_diff_summary(
                    implementation_payload
                ),
                "implementation_pre_command_head_sha": _implementation_optional_str(
                    implementation_payload.get("pre_command_head_sha")
                ),
                "implementation_post_command_head_sha": _implementation_optional_str(
                    implementation_payload.get("post_command_head_sha")
                ),
                "implementation_input_findings_path": (
                    str(findings_path) if findings_path else None
                ),
                "implementation_input_revision_path": (
                    str(revision_path) if revision_path else None
                ),
                "review_fix_attempt_count": review_fix_attempt_count,
                "error": (
                    "implementation command exited 0 but produced no meaningful code changes during a review-fix pass"
                ),
            },
        )
        write_issue_status(paths, blocked)
        print(
            json.dumps(
                {
                    "issue_number": issue_number,
                    "status": blocked.status,
                    "implementation_path": str(implementation_path),
                    "implementation_status": "no-op",
                    "returncode": implementation_payload.get("returncode"),
                    "error": blocked.extra.get("error"),
                },
                indent=2,
            )
        )
        return 1

    committed_head_sha = _implementation_optional_str(
        implementation_payload.get("post_command_head_sha")
    )
    runner_committed = False
    runner_pushed = False
    worktree_changed_files = _meaningful_worktree_changed_files(
        str(local_status.worktree_path)
    )
    if worktree_changed_files:
        commit_message = _implementation_commit_message(issue_number, implementation_mode)
        try:
            stage_paths(str(local_status.worktree_path), worktree_changed_files)
            commit_staged_changes(str(local_status.worktree_path), commit_message)
            committed_head_sha = current_head_sha(str(local_status.worktree_path))
            runner_committed = True
        except RuntimeError as error:
            blocked = IssueStatus(
                issue_number=local_status.issue_number,
                status="blocked",
                branch=local_status.branch,
                worktree_path=local_status.worktree_path,
                pr_number=local_status.pr_number,
                last_updated_utc=utc_now_iso(),
                extra={
                    **local_status.extra,
                    "implementation_path": str(implementation_path),
                    "implementation_status": "failed",
                    "implementation_returncode": implementation_payload.get("returncode"),
                    "implementation_mode": implementation_mode,
                    "implementation_changed_files": meaningful_changed_files,
                    "implementation_diff_summary": _implementation_diff_summary(
                        implementation_payload
                    ),
                    "implementation_pre_command_head_sha": _implementation_optional_str(
                        implementation_payload.get("pre_command_head_sha")
                    ),
                    "implementation_post_command_head_sha": _implementation_optional_str(
                        implementation_payload.get("post_command_head_sha")
                    ),
                    "implementation_input_findings_path": (
                        str(findings_path) if findings_path else None
                    ),
                    "implementation_input_revision_path": (
                        str(revision_path) if revision_path else None
                    ),
                    "review_fix_attempt_count": review_fix_attempt_count,
                    "error": f"Sandcastle could not commit implementation changes: {error}",
                },
            )
            write_issue_status(paths, blocked)
            print(
                json.dumps(
                    {
                        "issue_number": issue_number,
                        "status": blocked.status,
                        "implementation_path": str(implementation_path),
                        "implementation_status": "failed",
                        "error": blocked.extra.get("error"),
                    },
                    indent=2,
                )
            )
            return 1

    if local_status.pr_number is not None:
        try:
            push_branch(repo_root, repo_config.remote, local_status.branch)
            runner_pushed = True
        except RuntimeError as error:
            blocked = IssueStatus(
                issue_number=local_status.issue_number,
                status="blocked",
                branch=local_status.branch,
                worktree_path=local_status.worktree_path,
                pr_number=local_status.pr_number,
                last_updated_utc=utc_now_iso(),
                extra={
                    **local_status.extra,
                    "implementation_path": str(implementation_path),
                    "implementation_status": "failed",
                    "implementation_returncode": implementation_payload.get("returncode"),
                    "implementation_mode": implementation_mode,
                    "implementation_changed_files": meaningful_changed_files,
                    "implementation_diff_summary": _implementation_diff_summary(
                        implementation_payload
                    ),
                    "implementation_pre_command_head_sha": _implementation_optional_str(
                        implementation_payload.get("pre_command_head_sha")
                    ),
                    "implementation_post_command_head_sha": _implementation_optional_str(
                        implementation_payload.get("post_command_head_sha")
                    ),
                    "implementation_head_sha": committed_head_sha,
                    "implementation_input_findings_path": (
                        str(findings_path) if findings_path else None
                    ),
                    "implementation_input_revision_path": (
                        str(revision_path) if revision_path else None
                    ),
                    "review_fix_attempt_count": review_fix_attempt_count,
                    "error": f"Sandcastle could not push implementation changes: {error}",
                },
            )
            write_issue_status(paths, blocked)
            print(
                json.dumps(
                    {
                        "issue_number": issue_number,
                        "status": blocked.status,
                        "implementation_path": str(implementation_path),
                        "implementation_status": "failed",
                        "error": blocked.extra.get("error"),
                    },
                    indent=2,
                )
            )
            return 1

    updated = IssueStatus(
        issue_number=local_status.issue_number,
        status="implementing",
        branch=local_status.branch,
        worktree_path=local_status.worktree_path,
        pr_number=local_status.pr_number,
        last_updated_utc=utc_now_iso(),
        extra={
            **local_status.extra,
            "implementation_path": str(implementation_path),
            "implementation_status": str(implementation_payload["status"]),
            "implementation_returncode": implementation_payload.get("returncode"),
            "implementation_mode": implementation_mode,
            "implementation_changed_files": meaningful_changed_files,
            "implementation_diff_summary": _implementation_diff_summary(
                implementation_payload
            ),
            "implementation_pre_command_head_sha": _implementation_optional_str(
                implementation_payload.get("pre_command_head_sha")
            ),
            "implementation_post_command_head_sha": _implementation_optional_str(
                implementation_payload.get("post_command_head_sha")
            ),
            "implementation_head_sha": committed_head_sha,
            "implementation_runner_committed": runner_committed,
            "implementation_runner_pushed": runner_pushed,
            "implementation_input_findings_path": (
                str(findings_path) if findings_path else None
            ),
            "implementation_input_revision_path": (
                str(revision_path) if revision_path else None
            ),
            "review_fix_attempt_count": review_fix_attempt_count,
            "verification_status": None,
            "verification_path": None,
            "verified_implementation_path": None,
            "verified_head_sha": None,
            "review_path": None,
            "review_status": None,
            "review_finding_count": None,
            "reviewed_implementation_path": None,
            "reviewed_head_sha": None,
            "findings_path": None,
            "findings_status": None,
            "findings_count": None,
            "findings_source_implementation_path": None,
            "findings_source_head_sha": None,
            "revision_path": None,
            "verification_is_stale": None,
            "review_is_stale": None,
            "findings_are_stale": None,
            "auto_ready_status": None,
            "auto_ready_reason": None,
            "auto_merge_status": None,
            "auto_merge_reason": None,
            "auto_merge_requested_head_sha": None,
        },
    )
    write_issue_status(paths, updated)
    print(
        json.dumps(
            {
                "issue_number": issue_number,
                "status": updated.status,
                "implementation_path": str(implementation_path),
                "implementation_status": implementation_payload["status"],
                "returncode": implementation_payload.get("returncode"),
                "changed_files": meaningful_changed_files,
                "head_sha": committed_head_sha,
                "runner_committed": runner_committed,
                "runner_pushed": runner_pushed,
            },
            indent=2,
        )
    )
    return 0 if implementation_payload["status"] == "succeeded" else 1


def _handle_load_context(
    repo_root: Path, paths, issue_policy: IssuePolicy, issue_number: int
) -> int:
    issue = get_issue(repo_root, issue_number)
    decision = evaluate_issue(issue, issue_policy)
    if not decision.eligible:
        print(
            json.dumps(
                {
                    "error": "issue is not eligible for context loading",
                    "issue_number": issue.number,
                    "reasons": list(decision.reasons),
                },
                indent=2,
            )
        )
        return 1

    local_status = try_load_issue_status(paths, issue_number)
    if local_status is None:
        print(
            json.dumps(
                {
                    "error": "no local state exists for this issue",
                    "issue_number": issue.number,
                },
                indent=2,
            )
        )
        return 1

    context_payload = fetch_issue_context(repo_root, issue_number)
    context_path = write_issue_context(
        paths.contexts_dir, issue_number, context_payload
    )
    updated = IssueStatus(
        issue_number=local_status.issue_number,
        status=local_status.status,
        branch=local_status.branch,
        worktree_path=local_status.worktree_path,
        pr_number=local_status.pr_number,
        last_updated_utc=utc_now_iso(),
        extra={
            **local_status.extra,
            "context_loaded": True,
            "context_path": str(context_path),
            "context_comment_count": len(context_payload.get("comments", [])),
        },
    )
    write_issue_status(paths, updated)
    print(
        json.dumps(
            {
                "issue_number": issue_number,
                "context_path": str(context_path),
                "comment_count": len(context_payload.get("comments", [])),
                "title": context_payload.get("title"),
            },
            indent=2,
        )
    )
    return 0


def _handle_review(
    paths,
    issue_policy: IssuePolicy,
    repo_config: RepoConfig,
    issue_number: int,
) -> int:
    del issue_policy
    local_status = try_load_issue_status(paths, issue_number)
    if local_status is None or not local_status.branch:
        print(
            json.dumps(
                {
                    "error": "issue is not ready for review phase",
                    "issue_number": issue_number,
                },
                indent=2,
            )
        )
        return 1

    implementation_path = local_status.extra.get("implementation_path")
    verification_status = local_status.extra.get("verification_status")
    repo_root = repo_root_from_file(__file__)
    fetch_base_ref(repo_root, repo_config)
    pr_head_sha = None
    if local_status.pr_number is not None:
        pr_payload = get_pr(repo_root, local_status.pr_number)
        pr_head_sha = _optional_payload_str(pr_payload.get("headRefOid"))
    current_revision_sha = pr_head_sha or _current_issue_revision_sha(local_status)
    review_path = write_review_result(
        paths.review_dir,
        issue_number=issue_number,
        branch=local_status.branch,
        verification_status=str(verification_status) if verification_status else None,
        implementation_path=str(implementation_path) if implementation_path else None,
        worktree_path=local_status.worktree_path,
        pr_number=local_status.pr_number,
        issue_title=str(local_status.extra.get("issue_title", "")) or None,
        context_path=str(local_status.extra.get("context_path", "")) or None,
        issue_url=str(local_status.extra.get("issue_url", "")) or None,
        base_ref=repo_config.base_ref,
    )
    review_payload = read_json_file(review_path)
    findings_path = None
    findings_status = None
    findings_count = None
    if review_payload["status"] == "findings":
        findings_path = _write_review_findings(
            paths,
            issue_number=issue_number,
            pr_number=local_status.pr_number,
            review_path=review_path,
            review_payload=review_payload,
        )
        findings_status = "findings"
        findings_count = len(review_payload["findings"])
        next_status = "review-fix-loop"
    else:
        findings_status = "clean"
        findings_count = 0
        next_status = "pr-open" if local_status.pr_number is not None else "ready-for-pr"
    findings_fingerprint = None
    repeated_findings = False
    if findings_status == "findings":
        findings_fingerprint = _findings_fingerprint(
            {
                "status": findings_status,
                "findings": review_payload.get("findings", []),
            }
        )
        repeated_findings = _is_repeated_finding_set(local_status, findings_fingerprint)
        if repeated_findings:
            next_status = "blocked"
    updated = IssueStatus(
        issue_number=local_status.issue_number,
        status=next_status,
        branch=local_status.branch,
        worktree_path=local_status.worktree_path,
        pr_number=local_status.pr_number,
        last_updated_utc=utc_now_iso(),
        extra={
            **local_status.extra,
            "review_path": str(review_path),
            "review_status": review_payload["status"],
            "review_finding_count": len(review_payload["findings"]),
            "reviewed_implementation_path": (
                str(implementation_path) if implementation_path else None
            ),
            "reviewed_head_sha": current_revision_sha,
            "reviewed_pr_head_sha": pr_head_sha,
            "pr_head_sha": pr_head_sha or local_status.extra.get("pr_head_sha"),
            "findings_path": str(findings_path) if findings_path else None,
            "findings_status": findings_status,
            "findings_count": findings_count,
            "findings_fingerprint": findings_fingerprint,
            "findings_source_implementation_path": (
                str(implementation_path) if findings_path and implementation_path else None
            ),
            "findings_source_head_sha": (
                current_revision_sha if findings_path else None
            ),
            "needs_post_conflict_review": False,
            "review_is_stale": False,
            "findings_are_stale": False,
            "error": (
                "review findings repeated unchanged after a new implementation pass"
                if repeated_findings
                else None
            ),
        },
    )
    write_issue_status(paths, updated)
    print(
        json.dumps(
            {
                "issue_number": issue_number,
                "status": updated.status,
                "review_status": review_payload["status"],
                "review_path": str(review_path),
                "finding_count": len(review_payload["findings"]),
                "repeated_findings": repeated_findings,
            },
            indent=2,
        )
    )
    return 0 if review_payload["status"] == "clean" else 1


def _write_review_findings(
    paths,
    *,
    issue_number: int,
    pr_number: int | None,
    review_path: Path,
    review_payload: dict[str, object],
) -> Path:
    payload = {
        "issue_number": issue_number,
        "pr_number": pr_number,
        "status": review_payload.get("status"),
        "findings": review_payload.get("findings", []),
        "ci": None,
        "pr_discussion": None,
        "review_comments": None,
        "review_path": str(review_path),
        "source": "review-agent",
        "written_at_utc": utc_now_iso(),
    }
    return write_json_file(paths.findings_dir / f"{issue_number}.json", payload)


def _handle_revise(paths, issue_number: int) -> int:
    local_status = try_load_issue_status(paths, issue_number)
    if local_status is None or not local_status.branch:
        print(
            json.dumps(
                {
                    "error": "issue is not ready for revise phase",
                    "issue_number": issue_number,
                },
                indent=2,
            )
        )
        return 1

    findings_path = local_status.extra.get("findings_path")
    if not findings_path:
        print(
            json.dumps(
                {
                    "error": "no findings artifact exists for this issue",
                    "issue_number": issue_number,
                },
                indent=2,
            )
        )
        return 1

    findings_payload = read_json_file(Path(str(findings_path)))
    revision_path = write_revision_plan(
        paths.revision_dir,
        issue_number=issue_number,
        branch=local_status.branch,
        findings_payload=findings_payload,
    )
    updated = IssueStatus(
        issue_number=local_status.issue_number,
        status="revising",
        branch=local_status.branch,
        worktree_path=local_status.worktree_path,
        pr_number=local_status.pr_number,
        last_updated_utc=utc_now_iso(),
        extra={
            **local_status.extra,
            "revision_path": str(revision_path),
        },
    )
    write_issue_status(paths, updated)
    revision_payload = read_json_file(revision_path)
    print(
        json.dumps(
            {
                "issue_number": issue_number,
                "status": updated.status,
                "revision_path": str(revision_path),
                "next_actions": revision_payload.get("next_actions", []),
            },
            indent=2,
        )
    )
    return 0


def _handle_resolve_conflicts(
    repo_root: Path,
    paths,
    issue_policy: IssuePolicy,
    repo_config: RepoConfig,
    issue_number: int,
) -> int:
    local_status = try_load_issue_status(paths, issue_number)
    if (
        local_status is None
        or not local_status.branch
        or not local_status.worktree_path
        or local_status.pr_number is None
    ):
        print(
            json.dumps(
                {
                    "error": "issue is not ready for conflict resolution",
                    "issue_number": issue_number,
                },
                indent=2,
            )
        )
        return 1

    fetch_base_ref(repo_root, repo_config)
    conflict_path = resolve_pr_conflicts(
        conflict_dir=paths.conflict_dir,
        issue_number=issue_number,
        pr_number=local_status.pr_number,
        branch=local_status.branch,
        worktree_path=local_status.worktree_path,
        issue_title=str(local_status.extra.get("issue_title", f"Issue {issue_number}")),
        issue_url=_optional_payload_str(local_status.extra.get("issue_url")),
        context_path=_optional_payload_str(local_status.extra.get("context_path")),
        base_ref=repo_config.base_ref,
    )
    conflict_payload = read_json_file(conflict_path)

    if conflict_payload.get("status") == "blocked":
        blocked = _block_issue(
            repo_root,
            paths,
            issue_policy,
            local_status,
            reason=str(
                conflict_payload.get("blocked_reason")
                or "Sandcastle could not resolve the merge conflict safely."
            ),
            next_action=(
                "Resolve the conflict manually on the existing issue branch, then resume Sandcastle."
            ),
        )
        print(
            json.dumps(
                {
                    "issue_number": issue_number,
                    "status": blocked.status,
                    "conflict_path": str(conflict_path),
                    "reason": blocked.extra.get("blocked_reason"),
                },
                indent=2,
            )
        )
        return 1

    if conflict_payload.get("status") != "resolved":
        updated = IssueStatus(
            issue_number=local_status.issue_number,
            status="resolving-conflicts",
            branch=local_status.branch,
            worktree_path=local_status.worktree_path,
            pr_number=local_status.pr_number,
            last_updated_utc=utc_now_iso(),
            extra={
                **local_status.extra,
                "conflict_path": str(conflict_path),
                "conflict_status": str(conflict_payload.get("status")),
            },
        )
        write_issue_status(paths, updated)
        print(
            json.dumps(
                {
                    "issue_number": issue_number,
                    "status": updated.status,
                    "conflict_path": str(conflict_path),
                    "conflict_status": conflict_payload.get("status"),
                },
                indent=2,
            )
        )
        return 1

    resolution_kind = str(conflict_payload.get("resolution_kind", "resolved"))
    if resolution_kind != "no-op":
        push_branch(repo_root, repo_config.remote, local_status.branch)

    post_merge_head_sha = _optional_payload_str(conflict_payload.get("post_merge_head_sha"))
    updated = IssueStatus(
        issue_number=local_status.issue_number,
        status="pr-open",
        branch=local_status.branch,
        worktree_path=local_status.worktree_path,
        pr_number=local_status.pr_number,
        last_updated_utc=utc_now_iso(),
        extra={
            **local_status.extra,
            "conflict_path": str(conflict_path),
            "conflict_status": str(conflict_payload.get("status")),
            "conflict_resolution_kind": resolution_kind,
            "conflict_resolution_head_sha": post_merge_head_sha,
            "needs_post_conflict_verification": resolution_kind != "no-op",
            "needs_post_conflict_review": resolution_kind != "no-op",
            "verification_status": None if resolution_kind != "no-op" else local_status.extra.get("verification_status"),
            "verification_path": None if resolution_kind != "no-op" else local_status.extra.get("verification_path"),
            "review_path": None if resolution_kind != "no-op" else local_status.extra.get("review_path"),
            "review_status": None if resolution_kind != "no-op" else local_status.extra.get("review_status"),
            "review_finding_count": None if resolution_kind != "no-op" else local_status.extra.get("review_finding_count"),
            "findings_path": None if resolution_kind != "no-op" else local_status.extra.get("findings_path"),
            "findings_status": None if resolution_kind != "no-op" else local_status.extra.get("findings_status"),
            "findings_count": None if resolution_kind != "no-op" else local_status.extra.get("findings_count"),
            "revision_path": None if resolution_kind != "no-op" else local_status.extra.get("revision_path"),
            "verified_implementation_path": None if resolution_kind != "no-op" else local_status.extra.get("verified_implementation_path"),
            "verified_head_sha": None if resolution_kind != "no-op" else local_status.extra.get("verified_head_sha"),
            "reviewed_implementation_path": None if resolution_kind != "no-op" else local_status.extra.get("reviewed_implementation_path"),
            "reviewed_head_sha": None if resolution_kind != "no-op" else local_status.extra.get("reviewed_head_sha"),
            "findings_source_implementation_path": None if resolution_kind != "no-op" else local_status.extra.get("findings_source_implementation_path"),
            "findings_source_head_sha": None if resolution_kind != "no-op" else local_status.extra.get("findings_source_head_sha"),
            "pr_head_sha": post_merge_head_sha or local_status.extra.get("pr_head_sha"),
            "verification_is_stale": None if resolution_kind != "no-op" else local_status.extra.get("verification_is_stale"),
            "review_is_stale": None if resolution_kind != "no-op" else local_status.extra.get("review_is_stale"),
            "findings_are_stale": None if resolution_kind != "no-op" else local_status.extra.get("findings_are_stale"),
            "auto_ready_status": None if resolution_kind != "no-op" else local_status.extra.get("auto_ready_status"),
            "auto_ready_reason": None if resolution_kind != "no-op" else local_status.extra.get("auto_ready_reason"),
            "auto_merge_status": None if resolution_kind != "no-op" else local_status.extra.get("auto_merge_status"),
            "auto_merge_reason": None if resolution_kind != "no-op" else local_status.extra.get("auto_merge_reason"),
            "auto_merge_requested_head_sha": None if resolution_kind != "no-op" else local_status.extra.get("auto_merge_requested_head_sha"),
        },
    )
    write_issue_status(paths, updated)
    print(
        json.dumps(
            {
                "issue_number": issue_number,
                "status": updated.status,
                "conflict_path": str(conflict_path),
                "resolution_kind": resolution_kind,
                "needs_post_conflict_verification": updated.extra.get(
                    "needs_post_conflict_verification"
                ),
            },
            indent=2,
        )
    )
    return 0


def _handle_sync_pr(
    repo_root: Path, paths, repo_config: RepoConfig, issue_number: int
) -> int:
    local_status = try_load_issue_status(paths, issue_number)
    if local_status is None:
        print(
            json.dumps(
                {
                    "error": "no local state exists for this issue",
                    "issue_number": issue_number,
                },
                indent=2,
            )
        )
        return 1
    if local_status.pr_number is None:
        print(
            json.dumps(
                {
                    "error": "issue has no pull request recorded",
                    "issue_number": issue_number,
                },
                indent=2,
            )
        )
        return 1

    updated = _sync_pr_state(repo_root, paths, repo_config, local_status)
    print(
        json.dumps(
            {
                "issue_number": issue_number,
                "status": updated.status,
                "operator_state": _operator_state(updated),
                "pr_number": updated.pr_number,
                "pr_state": updated.extra.get("pr_state"),
                "is_draft": updated.extra.get("pr_is_draft"),
                "merged_at": updated.extra.get("pr_merged_at"),
                "findings_count": updated.extra.get("findings_count"),
            },
            indent=2,
        )
    )
    return 0


def _handle_wait_ci(
    repo_root: Path,
    paths,
    repo_config: RepoConfig,
    issue_number: int,
    timeout_seconds: int,
    poll_interval_seconds: int,
) -> int:
    local_status = try_load_issue_status(paths, issue_number)
    if local_status is None:
        print(
            json.dumps(
                {
                    "error": "no local state exists for this issue",
                    "issue_number": issue_number,
                },
                indent=2,
            )
        )
        return 1
    if local_status.pr_number is None:
        print(
            json.dumps(
                {
                    "error": "issue has no pull request recorded",
                    "issue_number": issue_number,
                },
                indent=2,
            )
        )
        return 1

    ci_path = wait_for_pr_checks(
        repo_root=repo_root,
        ci_dir=paths.ci_dir,
        issue_number=issue_number,
        pr_number=local_status.pr_number,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )
    ci_payload = read_json_file(ci_path)
    pr_payload = get_pr(repo_root, local_status.pr_number)
    current_revision_sha = _optional_payload_str(
        pr_payload.get("headRefOid")
    ) or _current_issue_revision_sha(local_status)
    verification_payload = None
    verification_path = local_status.extra.get("verification_path")
    if verification_path:
        verification_payload = read_json_file(Path(str(verification_path)))
    (
        verification_is_stale,
        review_is_stale,
        findings_are_stale,
    ) = _current_gate_freshness(
        local_status, current_revision_sha=current_revision_sha
    )
    findings_source_implementation_path, findings_source_head_sha = (
        _findings_source_metadata_for_update(
            local_status,
            current_revision_sha=current_revision_sha,
            findings_are_stale=findings_are_stale,
        )
    )

    findings_path = collect_findings(
        repo_root=repo_root,
        repo_name=repo_config.repo,
        findings_dir=paths.findings_dir,
        issue_number=issue_number,
        pr_number=local_status.pr_number,
        verification_payload=(
            None if verification_is_stale else verification_payload
        ),
        ci_payload=ci_payload,
    )
    findings_payload = read_json_file(findings_path)
    ci_status = str(ci_payload.get("status", ""))
    if verification_is_stale or review_is_stale or findings_are_stale:
        next_status = "pr-open"
    elif ci_status == "failed":
        next_status = "review-fix-loop"
    elif ci_status == "passed" and findings_payload.get("status") == "clean":
        next_status = _human_wait_status(pr_payload)
    else:
        next_status = "pr-open"

    updated = IssueStatus(
        issue_number=local_status.issue_number,
        status=next_status,
        branch=local_status.branch,
        worktree_path=local_status.worktree_path,
        pr_number=local_status.pr_number,
        last_updated_utc=utc_now_iso(),
        extra={
            **local_status.extra,
            "ci_path": str(ci_path),
            "ci_status": ci_status,
            "ci_check_count": len(ci_payload.get("checks", [])),
            "ci_failed_count": len(ci_payload.get("failed_checks", [])),
            "findings_path": str(findings_path),
            "findings_status": findings_payload.get("status"),
            "findings_count": len(findings_payload.get("findings", [])),
            "findings_source_implementation_path": findings_source_implementation_path,
            "findings_source_head_sha": findings_source_head_sha,
            "pr_head_sha": current_revision_sha,
            "verification_is_stale": verification_is_stale,
            "review_is_stale": review_is_stale,
            "findings_are_stale": findings_are_stale,
        },
    )
    write_issue_status(paths, updated)
    print(
        json.dumps(
            {
                "issue_number": issue_number,
                "status": updated.status,
                "operator_state": _operator_state(updated),
                "ci_status": ci_status,
                "ci_path": str(ci_path),
                "failed_checks": len(ci_payload.get("failed_checks", [])),
                "findings_count": len(findings_payload.get("findings", [])),
            },
            indent=2,
        )
    )
    return 0 if ci_status == "passed" else 1


def _handle_probe_ci(
    repo_root: Path,
    paths,
    repo_config: RepoConfig,
    issue_number: int,
) -> int:
    local_status = try_load_issue_status(paths, issue_number)
    if local_status is None or local_status.pr_number is None:
        print(
            json.dumps(
                {
                    "error": "issue is not ready for CI probing",
                    "issue_number": issue_number,
                },
                indent=2,
            )
        )
        return 1

    ci_path = snapshot_pr_checks(
        repo_root=repo_root,
        ci_dir=paths.ci_dir,
        issue_number=issue_number,
        pr_number=local_status.pr_number,
    )
    ci_payload = read_json_file(ci_path)
    pr_payload = get_pr(repo_root, local_status.pr_number)
    current_revision_sha = _optional_payload_str(
        pr_payload.get("headRefOid")
    ) or _current_issue_revision_sha(local_status)
    verification_payload = None
    verification_path = local_status.extra.get("verification_path")
    if verification_path:
        verification_payload = read_json_file(Path(str(verification_path)))
    (
        verification_is_stale,
        review_is_stale,
        findings_are_stale,
    ) = _current_gate_freshness(
        local_status, current_revision_sha=current_revision_sha
    )
    findings_source_implementation_path, findings_source_head_sha = (
        _findings_source_metadata_for_update(
            local_status,
            current_revision_sha=current_revision_sha,
            findings_are_stale=findings_are_stale,
        )
    )

    findings_path = collect_findings(
        repo_root=repo_root,
        repo_name=repo_config.repo,
        findings_dir=paths.findings_dir,
        issue_number=issue_number,
        pr_number=local_status.pr_number,
        verification_payload=(
            None if verification_is_stale else verification_payload
        ),
        ci_payload=ci_payload,
    )
    findings_payload = read_json_file(findings_path)
    ci_status = str(ci_payload.get("status", ""))
    if verification_is_stale or review_is_stale or findings_are_stale:
        next_status = "pr-open"
    elif ci_status == "failed":
        next_status = "review-fix-loop"
    elif ci_status == "passed" and findings_payload.get("status") == "clean":
        next_status = _human_wait_status(pr_payload)
    else:
        next_status = "pr-open"

    updated = IssueStatus(
        issue_number=local_status.issue_number,
        status=next_status,
        branch=local_status.branch,
        worktree_path=local_status.worktree_path,
        pr_number=local_status.pr_number,
        last_updated_utc=utc_now_iso(),
        extra={
            **local_status.extra,
            "ci_path": str(ci_path),
            "ci_status": ci_status,
            "ci_check_count": len(ci_payload.get("checks", [])),
            "ci_failed_count": len(ci_payload.get("failed_checks", [])),
            "findings_path": str(findings_path),
            "findings_status": findings_payload.get("status"),
            "findings_count": len(findings_payload.get("findings", [])),
            "findings_source_implementation_path": findings_source_implementation_path,
            "findings_source_head_sha": findings_source_head_sha,
            "pr_head_sha": current_revision_sha,
            "verification_is_stale": verification_is_stale,
            "review_is_stale": review_is_stale,
            "findings_are_stale": findings_are_stale,
        },
    )
    write_issue_status(paths, updated)
    print(
        json.dumps(
            {
                "issue_number": issue_number,
                "status": updated.status,
                "operator_state": _operator_state(updated),
                "ci_status": ci_status,
                "ci_path": str(ci_path),
                "failed_checks": len(ci_payload.get("failed_checks", [])),
                "findings_count": len(findings_payload.get("findings", [])),
            },
            indent=2,
        )
    )
    return 0


def _implementation_path(local_status: IssueStatus) -> str | None:
    value = local_status.extra.get("implementation_path")
    return str(value) if value else None


def _current_issue_revision_sha(local_status: IssueStatus) -> str | None:
    pr_head_sha = _optional_payload_str(local_status.extra.get("pr_head_sha"))
    if pr_head_sha is not None:
        return pr_head_sha
    return _optional_payload_str(local_status.extra.get("implementation_head_sha"))


def _artifact_revision_is_stale(
    recorded_revision_sha: object, current_revision_sha: str | None
) -> bool:
    if current_revision_sha is None:
        return False
    return _optional_payload_str(recorded_revision_sha) != current_revision_sha


def _verification_is_stale(
    local_status: IssueStatus, *, current_revision_sha: str | None = None
) -> bool:
    implementation_path = _implementation_path(local_status)
    if implementation_path is None:
        return False
    verified_implementation_path = local_status.extra.get("verified_implementation_path")
    if str(verified_implementation_path or "") != implementation_path:
        return True
    return _artifact_revision_is_stale(
        local_status.extra.get("verified_head_sha"),
        current_revision_sha or _current_issue_revision_sha(local_status),
    )


def _review_is_stale(
    local_status: IssueStatus, *, current_revision_sha: str | None = None
) -> bool:
    implementation_path = _implementation_path(local_status)
    if implementation_path is None:
        return False
    reviewed_implementation_path = local_status.extra.get("reviewed_implementation_path")
    if str(reviewed_implementation_path or "") != implementation_path:
        return True
    reviewed_head_sha = local_status.extra.get("reviewed_head_sha")
    if reviewed_head_sha is None:
        reviewed_head_sha = local_status.extra.get("reviewed_pr_head_sha")
    return _artifact_revision_is_stale(
        reviewed_head_sha,
        current_revision_sha or _current_issue_revision_sha(local_status),
    )


def _findings_are_stale(
    local_status: IssueStatus, *, current_revision_sha: str | None = None
) -> bool:
    implementation_path = _implementation_path(local_status)
    if implementation_path is None:
        return False
    findings_source_path = local_status.extra.get("findings_source_implementation_path")
    if str(findings_source_path or "") != implementation_path:
        return True
    return _artifact_revision_is_stale(
        local_status.extra.get("findings_source_head_sha"),
        current_revision_sha or _current_issue_revision_sha(local_status),
    )


def _current_gate_freshness(
    local_status: IssueStatus, *, current_revision_sha: str | None = None
) -> tuple[bool, bool, bool]:
    verification_is_stale = _verification_is_stale(
        local_status, current_revision_sha=current_revision_sha
    )
    review_is_stale = _review_is_stale(
        local_status, current_revision_sha=current_revision_sha
    )
    findings_are_stale = _findings_are_stale(
        local_status, current_revision_sha=current_revision_sha
    )
    if verification_is_stale or review_is_stale:
        findings_are_stale = True
    return verification_is_stale, review_is_stale, findings_are_stale


def _findings_source_metadata_for_update(
    local_status: IssueStatus,
    *,
    current_revision_sha: str | None,
    findings_are_stale: bool,
) -> tuple[object, object]:
    if findings_are_stale:
        return (
            local_status.extra.get("findings_source_implementation_path"),
            local_status.extra.get("findings_source_head_sha"),
        )
    return (
        local_status.extra.get("implementation_path"),
        current_revision_sha,
    )


def _stale_gate_reason(
    *,
    verification_is_stale: bool,
    review_is_stale: bool,
    findings_are_stale: bool,
) -> str:
    stale_gates: list[str] = []
    if verification_is_stale:
        stale_gates.append("verification")
    if review_is_stale:
        stale_gates.append("review")
    if findings_are_stale:
        stale_gates.append("findings")
    gates = ", ".join(stale_gates) or "quality gates"
    return f"the latest {gates} results no longer match the current branch head"


def _record_resume_priority(
    paths, local_status: IssueStatus, backlog: BacklogSelection
) -> IssueStatus:
    next_issue = backlog.selected_issue
    if next_issue is None:
        return local_status

    reason = "existing branch regained priority ahead of fresh backlog pickup"
    updated = IssueStatus(
        issue_number=local_status.issue_number,
        status=local_status.status,
        branch=local_status.branch,
        worktree_path=local_status.worktree_path,
        pr_number=local_status.pr_number,
        last_updated_utc=utc_now_iso(),
        extra={
            **local_status.extra,
            "resume_priority_reason": reason,
            "resumed_ahead_of_issue_number": next_issue.number,
            "resumed_ahead_of_issue_title": next_issue.title,
            "resume_priority_recorded_at": utc_now_iso(),
        },
    )
    write_issue_status(paths, updated)
    return updated


def _resume_sync_required(local_status: IssueStatus) -> bool:
    if not local_status.branch or not local_status.worktree_path:
        return False
    return local_status.status not in {"claimed", "branch-created", "merged", "blocked"}


def _clear_ready_state_after_resume_sync(
    local_status: IssueStatus, *, resolution_kind: str
) -> dict[str, object]:
    if resolution_kind == "no-op":
        return {}
    if local_status.status in {"review-fix-loop", "revising", "implementing"}:
        return {}
    return {
        "verification_status": None,
        "verification_path": None,
        "verified_implementation_path": None,
        "verified_head_sha": None,
        "review_path": None,
        "review_status": None,
        "review_finding_count": None,
        "reviewed_implementation_path": None,
        "reviewed_head_sha": None,
        "findings_source_implementation_path": None,
        "findings_source_head_sha": None,
        "pr_head_sha": None,
        "verification_is_stale": None,
        "review_is_stale": None,
        "findings_are_stale": None,
        "auto_ready_status": None,
        "auto_ready_reason": None,
        "auto_merge_status": None,
        "auto_merge_reason": None,
        "auto_merge_requested_head_sha": None,
    }


def _handle_resume_sync(
    repo_root: Path,
    paths,
    issue_policy: IssuePolicy,
    repo_config: RepoConfig,
    local_status: IssueStatus,
) -> int | None:
    if not _resume_sync_required(local_status):
        return None

    fetch_base_ref(repo_root, repo_config)
    behind_count = branch_behind_count(
        repo_root, local_status.branch or "", repo_config.base_ref
    )
    base_sha = ref_sha(repo_root, repo_config.base_ref)
    if behind_count < 1:
        return None

    conflict_path = resolve_pr_conflicts(
        conflict_dir=paths.conflict_dir,
        issue_number=local_status.issue_number,
        pr_number=local_status.pr_number,
        branch=local_status.branch or "",
        worktree_path=local_status.worktree_path or "",
        issue_title=str(
            local_status.extra.get(
                "issue_title", f"Issue {local_status.issue_number}"
            )
        ),
        issue_url=_optional_payload_str(local_status.extra.get("issue_url")),
        context_path=_optional_payload_str(local_status.extra.get("context_path")),
        base_ref=repo_config.base_ref,
    )
    conflict_payload = read_json_file(conflict_path)
    if conflict_payload.get("status") == "blocked":
        blocked = _block_issue(
            repo_root,
            paths,
            issue_policy,
            local_status,
            reason=str(
                conflict_payload.get("blocked_reason")
                or "Sandcastle could not sync the issue branch with fresh main safely."
            ),
            next_action=(
                "Resolve the merge-from-main conflict manually on the existing issue branch, then resume Sandcastle."
            ),
        )
        print(
            json.dumps(
                {
                    "issue_number": local_status.issue_number,
                    "status": blocked.status,
                    "resume_sync_path": str(conflict_path),
                    "reason": blocked.extra.get("blocked_reason"),
                },
                indent=2,
            )
        )
        return 1

    if conflict_payload.get("status") != "resolved":
        raise RuntimeError(
            "resume sync returned an unexpected status: "
            + str(conflict_payload.get("status"))
        )

    resolution_kind = str(conflict_payload.get("resolution_kind", "resolved"))
    if resolution_kind != "no-op" and (
        local_status.pr_number is not None or bool(local_status.extra.get("branch_pushed"))
    ):
        push_branch(repo_root, repo_config.remote, local_status.branch or "")

    updated = IssueStatus(
        issue_number=local_status.issue_number,
        status=local_status.status,
        branch=local_status.branch,
        worktree_path=local_status.worktree_path,
        pr_number=local_status.pr_number,
        last_updated_utc=utc_now_iso(),
        extra={
            **local_status.extra,
            **_clear_ready_state_after_resume_sync(
                local_status, resolution_kind=resolution_kind
            ),
            "resume_sync_status": "resolved",
            "resume_sync_resolution_kind": resolution_kind,
            "resume_sync_path": str(conflict_path),
            "resume_sync_commits_behind": behind_count,
            "resume_sync_base_ref": repo_config.base_ref,
            "resume_sync_base_sha": base_sha,
            "resume_sync_completed_at": utc_now_iso(),
        },
    )
    write_issue_status(paths, updated)
    print(
        json.dumps(
            {
                "issue_number": local_status.issue_number,
                "status": updated.status,
                "resume_sync_status": updated.extra.get("resume_sync_status"),
                "resolution_kind": resolution_kind,
                "resume_sync_path": str(conflict_path),
                "commits_behind": behind_count,
            },
            indent=2,
        )
    )
    return 0


def _advance_active_issue(
    repo_root: Path,
    paths,
    issue_policy: IssuePolicy,
    repo_config: RepoConfig,
    gates_config: GatesConfig,
    local_status: IssueStatus,
) -> int:
    if local_status.status == "merged":
        return _handle_cleanup(repo_root, paths, repo_config, local_status.issue_number)

    if local_status.status == "resolving-conflicts":
        return _handle_resolve_conflicts(
            repo_root, paths, issue_policy, repo_config, local_status.issue_number
        )

    resume_sync = _handle_resume_sync(
        repo_root, paths, issue_policy, repo_config, local_status
    )
    if resume_sync is not None:
        return resume_sync

    if local_status.status == "review-fix-loop":
        return _handle_revise(paths, local_status.issue_number)

    if local_status.status == "revising":
        return _handle_implement(
            repo_root, paths, repo_config, gates_config, local_status.issue_number
        )

    if local_status.pr_number is None:
        if local_status.extra.get("context_loaded") is not True:
            return _handle_load_context(
                repo_root, paths, issue_policy, local_status.issue_number
            )
        if local_status.extra.get("implementation_path") is None:
            return _handle_implement(
                repo_root, paths, repo_config, gates_config, local_status.issue_number
            )
        verification_status = str(local_status.extra.get("verification_status", "") or "")
        if _verification_is_stale(local_status):
            return _handle_verify(
                paths, issue_policy, gates_config, local_status.issue_number
            )
        if verification_status == "failed":
            if _findings_are_stale(local_status):
                return _handle_collect_findings(
                    paths, repo_config, local_status.issue_number
                )
            return _handle_revise(paths, local_status.issue_number)
        return _handle_resume(
            repo_root, paths, issue_policy, repo_config, local_status.issue_number
        )

    if local_status.status in {
        "pr-open",
        "waiting-human-review",
        "approved-awaiting-merge",
        "ready-for-human",
        "ready-for-pr",
        "implementing",
    }:
        if bool(local_status.extra.get("needs_post_conflict_verification")):
            return _handle_verify(
                paths, issue_policy, gates_config, local_status.issue_number
            )
        verification_status = str(local_status.extra.get("verification_status", "") or "")
        if _verification_is_stale(local_status):
            return _handle_verify(
                paths, issue_policy, gates_config, local_status.issue_number
            )
        if verification_status == "failed":
            if _findings_are_stale(local_status):
                return _handle_collect_findings(
                    paths, repo_config, local_status.issue_number
                )
            return _handle_revise(paths, local_status.issue_number)
        if bool(local_status.extra.get("needs_post_conflict_review")):
            return _handle_review(
                paths, issue_policy, repo_config, local_status.issue_number
            )
        if _review_is_stale(local_status):
            return _handle_review(
                paths, issue_policy, repo_config, local_status.issue_number
            )
        ci_status = str(local_status.extra.get("ci_status", "") or "")
        findings_status = str(local_status.extra.get("findings_status", "") or "")
        if (
            local_status.status == "pr-open"
            and (ci_status == "failed" or findings_status == "findings")
        ):
            return _handle_wait_ci(
                repo_root,
                paths,
                repo_config,
                local_status.issue_number,
                900,
                15,
            )
        return _handle_sync_pr(repo_root, paths, repo_config, local_status.issue_number)

    if local_status.status in {"claimed", "branch-created"}:
        return _handle_resume(
            repo_root, paths, issue_policy, repo_config, local_status.issue_number
        )

    print(
        json.dumps(
            {
                "error": "auto-step does not know how to advance this state",
                "issue_number": local_status.issue_number,
                "status": local_status.status,
            },
            indent=2,
        )
    )
    return 1


def _handle_resume(
    repo_root: Path,
    paths,
    issue_policy: IssuePolicy,
    repo_config: RepoConfig,
    issue_number: int,
) -> int:
    local_status = try_load_issue_status(paths, issue_number)
    if local_status is None:
        print(
            json.dumps(
                {
                    "error": "no local state exists for this issue",
                    "issue_number": issue_number,
                },
                indent=2,
            )
        )
        return 1

    issue = get_issue(repo_root, issue_number)
    decision = evaluate_issue(issue, issue_policy)
    if not decision.eligible:
        print(
            json.dumps(
                {
                    "error": "issue is no longer eligible for Sandcastle pickup",
                    "issue_number": issue.number,
                    "reasons": list(decision.reasons),
                },
                indent=2,
            )
        )
        return 1

    local_status, resumed_from_block = _resume_blocked_issue_if_supported(local_status)
    if resumed_from_block:
        write_issue_status(paths, local_status)

    if local_status.status not in RESUMABLE_ISSUE_STATUSES:
        print(
            json.dumps(
                {
                    "error": "resume is not implemented for the current local state",
                    "issue_number": issue.number,
                    "local_state": _serialize_local_state(local_status),
                },
                indent=2,
            )
        )
        return 1

    if not local_status.branch:
        print(
            json.dumps(
                {
                    "error": "local state is missing the issue branch",
                    "issue_number": issue.number,
                },
                indent=2,
            )
        )
        return 1

    pr_number, pr_url = find_pr_for_head(repo_root, local_status.branch)
    if pr_number is not None:
        pr_payload = get_pr(repo_root, pr_number)
        try:
            ensure_pull_request_assignee(
                repo_root,
                pr_number=pr_number,
                assignee_username=repo_config.github.assignee_username,
                current_assignees=_connection_logins(pr_payload.get("assignees")),
            )
        except RuntimeError as error:
            return _handle_pull_request_assignment_failure(
                repo_root,
                paths,
                issue_policy,
                local_status,
                pr_number=pr_number,
                pr_url=pr_url,
                error=error,
            )

        updated = IssueStatus(
            issue_number=issue.number,
            status="pr-open",
            branch=local_status.branch,
            worktree_path=local_status.worktree_path,
            pr_number=pr_number,
            last_updated_utc=utc_now_iso(),
            extra={
                **local_status.extra,
                "pr_url": pr_url,
                "branch_pushed": True,
            },
        )
        write_issue_status(paths, updated)
        print(
            json.dumps(
                {
                    "issue_number": issue.number,
                    "status": updated.status,
                    "branch": updated.branch,
                    "pr_number": pr_number,
                    "pr_url": pr_url,
                },
                indent=2,
            )
        )
        return 0

    ahead_count = branch_ahead_count(
        repo_root, repo_config.base_ref, local_status.branch
    )
    if ahead_count < 1:
        print(
            json.dumps(
                {
                    "issue_number": issue.number,
                    "status": local_status.status,
                    "branch": local_status.branch,
                    "message": "waiting for the first real commit before draft PR creation",
                    "ahead_count": ahead_count,
                },
                indent=2,
            )
        )
        return 0

    issue_title = str(local_status.extra.get("issue_title", issue.title))
    issue_context = fetch_issue_context(repo_root, issue.number)
    assignee_username = (repo_config.github.assignee_username or "").strip()
    if not assignee_username:
        return _handle_pull_request_assignment_failure(
            repo_root,
            paths,
            issue_policy,
            local_status,
            pr_number=None,
            pr_url=None,
            error=RuntimeError(
                "GitHub assignee username is required before Sandcastle can create and assign a pull request."
            ),
        )

    push_branch(repo_root, repo_config.remote, local_status.branch)
    pr_body = _render_pr_body(
        paths,
        issue.number,
        issue_title,
        local_status.branch,
        issue_context.get("body", ""),
    )
    pr_title = f"Issue #{issue.number}: {issue_title}"
    pr_number, pr_url = create_pr(
        repo_root,
        base_branch=repo_config.default_branch,
        head_branch=local_status.branch,
        title=pr_title,
        body=pr_body,
        draft=repo_config.github.draft_pr_by_default,
    )
    try:
        ensure_pull_request_assignee(
            repo_root,
            pr_number=pr_number,
            assignee_username=assignee_username,
            current_assignees=(),
        )
    except RuntimeError as error:
        return _handle_pull_request_assignment_failure(
            repo_root,
            paths,
            issue_policy,
            local_status,
            pr_number=pr_number,
            pr_url=pr_url,
            error=error,
        )

    updated = IssueStatus(
        issue_number=issue.number,
        status="pr-open",
        branch=local_status.branch,
        worktree_path=local_status.worktree_path,
        pr_number=pr_number,
        last_updated_utc=utc_now_iso(),
        extra={
            **local_status.extra,
            "pr_url": pr_url,
            "branch_pushed": True,
        },
    )
    write_issue_status(paths, updated)
    print(
        json.dumps(
            {
                "issue_number": issue.number,
                "status": updated.status,
                "branch": updated.branch,
                "pr_number": pr_number,
                "pr_url": pr_url,
            },
            indent=2,
        )
    )
    return 0


def _resume_blocked_issue_if_supported(
    local_status: IssueStatus,
) -> tuple[IssueStatus, bool]:
    if local_status.status != "blocked":
        return local_status, False

    resume_kind = _optional_payload_str(local_status.extra.get("blocked_resume_kind"))
    if resume_kind != BLOCKED_RESUME_KIND_PULL_REQUEST_ASSIGNMENT:
        return local_status, False

    resume_status = _optional_payload_str(
        local_status.extra.get("blocked_resume_status")
    )
    if resume_status not in RESUMABLE_ISSUE_STATUSES:
        return local_status, False

    extra = dict(local_status.extra)
    last_blocked_reason = extra.pop("blocked_reason", None)
    last_blocked_next_action = extra.pop("blocked_next_action", None)
    extra.pop("handoff_note_path", None)
    extra.pop("blocked_resume_kind", None)
    extra.pop("blocked_resume_status", None)
    if last_blocked_reason is not None:
        extra["last_blocked_reason"] = last_blocked_reason
    if last_blocked_next_action is not None:
        extra["last_blocked_next_action"] = last_blocked_next_action
    extra["last_blocked_resume_kind"] = resume_kind
    extra["last_blocked_resume_status"] = resume_status
    extra["last_blocked_resumed_at"] = utc_now_iso()
    return (
        IssueStatus(
            issue_number=local_status.issue_number,
            status=resume_status,
            branch=local_status.branch,
            worktree_path=local_status.worktree_path,
            pr_number=local_status.pr_number,
            last_updated_utc=utc_now_iso(),
            extra=extra,
        ),
        True,
    )


def _handle_run_issue(
    repo_root: Path,
    paths,
    issue_policy: IssuePolicy,
    repo_config: RepoConfig,
    issue_number: int,
) -> int:
    existing = try_load_issue_status(paths, issue_number)
    if existing is not None:
        print(
            json.dumps(
                {
                    "error": "local state already exists for this issue; use resume once implemented",
                    "local_state": _serialize_local_state(existing),
                },
                indent=2,
            )
        )
        return 1

    issue = get_issue(repo_root, issue_number)
    decision = evaluate_issue(issue, issue_policy)
    if not decision.eligible:
        print(
            json.dumps(
                {
                    "error": "issue is not eligible for Sandcastle pickup",
                    "issue_number": issue.number,
                    "reasons": list(decision.reasons),
                },
                indent=2,
            )
        )
        return 1

    claimed_by = get_current_login(repo_root)
    issue_status = IssueStatus(
        issue_number=issue.number,
        status="claiming",
        last_updated_utc=utc_now_iso(),
        extra={
            "issue_title": issue.title,
            "claimed_by": claimed_by,
            "issue_url": issue.url,
        },
    )
    write_issue_status(paths, issue_status)

    try:
        _claim_issue(
            repo_root,
            paths,
            issue_policy,
            repo_config,
            issue,
            claimed_by,
        )
        issue_status = IssueStatus(
            issue_number=issue.number,
            status="claimed",
            last_updated_utc=utc_now_iso(),
            extra={
                "issue_title": issue.title,
                "claimed_by": claimed_by,
                "issue_url": issue.url,
            },
        )
        write_issue_status(paths, issue_status)

        fetch_base_ref(repo_root, repo_config)
        branch_name, worktree_path = create_issue_worktree(
            repo_root, repo_config, issue.number, issue.title
        )
        issue_status = IssueStatus(
            issue_number=issue.number,
            status="branch-created",
            branch=branch_name,
            worktree_path=worktree_path,
            last_updated_utc=utc_now_iso(),
            extra={
                "issue_title": issue.title,
                "claimed_by": claimed_by,
                "issue_url": issue.url,
            },
        )
        write_issue_status(paths, issue_status)
    except Exception as error:
        blocked_status = IssueStatus(
            issue_number=issue.number,
            status="blocked",
            branch=issue_status.branch,
            worktree_path=issue_status.worktree_path,
            last_updated_utc=utc_now_iso(),
            extra={
                "issue_title": issue.title,
                "claimed_by": claimed_by,
                "issue_url": issue.url,
                "error": str(error),
            },
        )
        write_issue_status(paths, blocked_status)
        if _is_failed_startup_cleanup_candidate(repo_root, repo_config, blocked_status):
            return _cleanup_failed_startup_issue(
                repo_root, paths, repo_config, blocked_status
            )
        print(
            json.dumps(
                {
                    "error": "run-issue failed",
                    "issue_number": issue.number,
                    "details": str(error),
                    "local_state": _serialize_local_state(blocked_status),
                },
                indent=2,
            )
        )
        return 1

    print(
        json.dumps(
            {
                "issue_number": issue.number,
                "status": issue_status.status,
                "branch": issue_status.branch,
                "worktree_path": issue_status.worktree_path,
            },
            indent=2,
        )
    )
    return 0


def _handle_verify(
    paths, issue_policy: IssuePolicy, gates_config: GatesConfig, issue_number: int
) -> int:
    local_status = try_load_issue_status(paths, issue_number)
    if local_status is None or not local_status.worktree_path:
        print(
            json.dumps(
                {
                    "error": "issue has no local worktree state",
                    "issue_number": issue_number,
                },
                indent=2,
            )
        )
        return 1

    verification_path = run_verification(
        worktree_path=Path(local_status.worktree_path),
        verification_dir=paths.verification_dir,
        issue_number=issue_number,
        gates=gates_config,
    )
    verification_payload = read_json_file(verification_path)
    updated = IssueStatus(
        issue_number=local_status.issue_number,
        status=local_status.status,
        branch=local_status.branch,
        worktree_path=local_status.worktree_path,
        pr_number=local_status.pr_number,
        last_updated_utc=utc_now_iso(),
        extra={
            **local_status.extra,
            "verification_status": verification_payload["status"],
            "verification_path": str(verification_path),
            "verified_implementation_path": local_status.extra.get(
                "implementation_path"
            ),
            "verified_head_sha": _current_issue_revision_sha(local_status),
            "needs_post_conflict_verification": False,
            "verification_is_stale": False,
        },
    )
    write_issue_status(paths, updated)
    print(
        json.dumps(
            {
                "issue_number": issue_number,
                "status": verification_payload["status"],
                "verification_path": str(verification_path),
                "steps": [
                    {
                        "name": step["name"],
                        "returncode": step["returncode"],
                    }
                    for step in verification_payload["steps"]
                ],
            },
            indent=2,
        )
    )
    return 0 if verification_payload["status"] == "passed" else 1


def _sync_pr_state(
    repo_root: Path, paths, repo_config: RepoConfig, local_status: IssueStatus
) -> IssueStatus:
    local_status = _hydrate_cleanup_state(repo_root, local_status)
    if local_status.pr_number is None:
        raise RuntimeError("cannot sync a PR-less issue state")

    pr_payload = get_pr(repo_root, local_status.pr_number)
    merged_at = _optional_payload_str(pr_payload.get("mergedAt"))
    pr_state = _optional_payload_str(pr_payload.get("state"))
    pr_is_draft = bool(pr_payload.get("isDraft"))
    pr_review_decision = _optional_payload_str(pr_payload.get("reviewDecision"))
    pr_merge_state_status = _optional_payload_str(pr_payload.get("mergeStateStatus"))
    pr_mergeable = _optional_payload_str(pr_payload.get("mergeable"))
    pr_head_sha = _optional_payload_str(pr_payload.get("headRefOid"))
    pr_check_count = len(get_pr_check_rollup(repo_root, local_status.pr_number))
    findings_count = local_status.extra.get("findings_count", 0)
    findings_status = local_status.extra.get("findings_status")
    findings_path = local_status.extra.get("findings_path")
    ci_path_object = snapshot_pr_checks(
        repo_root=repo_root,
        ci_dir=paths.ci_dir,
        issue_number=local_status.issue_number,
        pr_number=local_status.pr_number,
    )
    ci_payload = read_json_file(ci_path_object)
    ci_status = str(ci_payload.get("status", "")) if ci_payload is not None else None
    auto_ready_status = _optional_payload_str(local_status.extra.get("auto_ready_status"))
    auto_ready_reason = _optional_payload_str(local_status.extra.get("auto_ready_reason"))
    auto_merge_status = _optional_payload_str(local_status.extra.get("auto_merge_status"))
    auto_merge_reason = _optional_payload_str(local_status.extra.get("auto_merge_reason"))
    main_health = _load_main_health(paths)
    next_status = local_status.status
    current_revision_sha = pr_head_sha or _current_issue_revision_sha(local_status)
    (
        verification_is_stale,
        review_is_stale,
        findings_are_stale,
    ) = _current_gate_freshness(
        local_status, current_revision_sha=current_revision_sha
    )
    findings_source_implementation_path, findings_source_head_sha = (
        _findings_source_metadata_for_update(
            local_status,
            current_revision_sha=current_revision_sha,
            findings_are_stale=findings_are_stale,
        )
    )

    if merged_at is not None:
        next_status = "merged"
    else:
        verification_payload = None
        verification_path = local_status.extra.get("verification_path")
        if verification_path:
            verification_payload = read_json_file(Path(str(verification_path)))

        findings_path_object = collect_findings(
            repo_root=repo_root,
            repo_name=repo_config.repo,
            findings_dir=paths.findings_dir,
            issue_number=local_status.issue_number,
            pr_number=local_status.pr_number,
            verification_payload=(
                None if verification_is_stale else verification_payload
            ),
            ci_payload=ci_payload,
        )
        findings_payload = read_json_file(findings_path_object)
        findings_count = len(findings_payload.get("findings", []))
        findings_status = findings_payload.get("status")
        findings_path = str(findings_path_object)
        if _pr_needs_conflict_resolution(pr_payload):
            next_status = "resolving-conflicts"
        elif verification_is_stale or review_is_stale or findings_are_stale:
            next_status = "pr-open"
            if repo_config.github.auto_merge_prs:
                auto_merge_status = "waiting"
                auto_merge_reason = _stale_gate_reason(
                    verification_is_stale=verification_is_stale,
                    review_is_stale=review_is_stale,
                    findings_are_stale=findings_are_stale,
                )
        elif ci_status == "failed":
            next_status = "review-fix-loop"
        elif findings_status == "findings":
            next_status = "review-fix-loop"
        elif ci_status == "passed":
            (
                pr_payload,
                auto_ready_status,
                auto_ready_reason,
            ) = _attempt_pr_auto_ready(repo_root, repo_config, local_status, pr_payload)
            merged_at = _optional_payload_str(pr_payload.get("mergedAt"))
            pr_state = _optional_payload_str(pr_payload.get("state"))
            pr_is_draft = bool(pr_payload.get("isDraft"))
            pr_review_decision = _optional_payload_str(pr_payload.get("reviewDecision"))
            pr_merge_state_status = _optional_payload_str(
                pr_payload.get("mergeStateStatus")
            )
            pr_mergeable = _optional_payload_str(pr_payload.get("mergeable"))
            pr_head_sha = _optional_payload_str(pr_payload.get("headRefOid"))
            pr_check_count = len(get_pr_check_rollup(repo_root, local_status.pr_number))

            if merged_at is not None:
                next_status = "merged"
            else:
                if not bool(main_health.get("healthy")):
                    auto_merge_status = "waiting"
                    auto_merge_reason = (
                        "main is unhealthy, so Sandcastle is suspending new merges until default-branch health recovers"
                    )
                    next_status = _human_wait_status(pr_payload)
                else:
                    (
                        pr_payload,
                        auto_merge_status,
                        auto_merge_reason,
                    ) = _attempt_pr_auto_merge(
                        repo_root,
                        repo_config,
                        local_status,
                        pr_payload,
                        ci_status,
                        findings_status,
                    )
                    merged_at = _optional_payload_str(pr_payload.get("mergedAt"))
                    pr_state = _optional_payload_str(pr_payload.get("state"))
                    pr_is_draft = bool(pr_payload.get("isDraft"))
                    pr_review_decision = _optional_payload_str(
                        pr_payload.get("reviewDecision")
                    )
                    pr_merge_state_status = _optional_payload_str(
                        pr_payload.get("mergeStateStatus")
                    )
                    pr_mergeable = _optional_payload_str(pr_payload.get("mergeable"))
                    pr_head_sha = _optional_payload_str(pr_payload.get("headRefOid"))
                    pr_check_count = len(
                        get_pr_check_rollup(repo_root, local_status.pr_number)
                    )
                    next_status = (
                        "merged"
                        if merged_at is not None
                        else _human_wait_status(pr_payload)
                    )
        else:
            next_status = "pr-open"

    updated = IssueStatus(
        issue_number=local_status.issue_number,
        status=next_status,
        branch=local_status.branch,
        worktree_path=local_status.worktree_path,
        pr_number=local_status.pr_number,
        last_updated_utc=utc_now_iso(),
        extra={
            **local_status.extra,
            "pr_state": pr_state,
            "pr_is_draft": pr_is_draft,
            "pr_url": _optional_payload_str(pr_payload.get("url"))
            or local_status.extra.get("pr_url"),
            "pr_head_branch": _optional_payload_str(pr_payload.get("headRefName")),
            "pr_head_sha": pr_head_sha,
            "pr_base_branch": _optional_payload_str(pr_payload.get("baseRefName")),
            "pr_review_decision": pr_review_decision,
            "pr_merge_state_status": pr_merge_state_status,
            "pr_mergeable": pr_mergeable,
            "pr_merged_at": merged_at,
            "pr_check_count": pr_check_count,
            "ci_path": str(ci_path_object),
            "ci_status": ci_status,
            "needs_post_conflict_verification": False
            if next_status == "resolving-conflicts"
            else local_status.extra.get("needs_post_conflict_verification"),
            "needs_post_conflict_review": False
            if next_status == "resolving-conflicts"
            else local_status.extra.get("needs_post_conflict_review"),
            "findings_path": findings_path,
            "findings_status": findings_status,
            "findings_count": findings_count,
            "findings_source_implementation_path": findings_source_implementation_path,
            "findings_source_head_sha": findings_source_head_sha,
            "verification_is_stale": verification_is_stale,
            "review_is_stale": review_is_stale,
            "findings_are_stale": findings_are_stale,
            "auto_ready_status": auto_ready_status,
            "auto_ready_reason": auto_ready_reason,
            "auto_merge_status": auto_merge_status,
            "auto_merge_reason": auto_merge_reason,
            "auto_merge_requested_head_sha": (
                pr_head_sha if auto_merge_status == "requested" else None
            ),
            "pr_last_synced_at": utc_now_iso(),
        },
    )
    write_issue_status(paths, updated)
    return updated


def _load_issue_policy(path: Path) -> IssuePolicy:
    payload = read_json_file(path)
    claim_payload = payload["claim"]
    return IssuePolicy(
        required_labels=tuple(payload["required_labels"]),
        forbidden_labels=tuple(payload["forbidden_labels"]),
        require_open_state=bool(payload["require_open_state"]),
        require_unassigned=bool(payload["require_unassigned"]),
        allow_batches=bool(payload["allow_batches"]),
        claim=ClaimPolicy(
            assign_to_me=bool(claim_payload["assign_to_me"]),
            add_comment=bool(claim_payload["add_comment"]),
            comment_template=str(claim_payload["comment_template"]),
            overwrite_existing_assignees=bool(
                claim_payload.get("overwrite_existing_assignees", False)
            ),
        ),
        pull_order=PullOrderPolicy(**payload["pull_order"]),
        handoff=HandoffPolicy(**payload["handoff"]),
        completion=CompletionPolicy(**payload["completion"]),
    )


def _load_repo_config(path: Path) -> RepoConfig:
    payload = read_json_file(path)
    return RepoConfig(
        repo=str(payload["repo"]),
        remote=str(payload["remote"]),
        default_branch=str(payload["default_branch"]),
        base_ref=str(payload["base_ref"]),
        branch_prefix=str(payload["branch_prefix"]),
        branch_name_pattern=str(payload["branch_name_pattern"]),
        worktree=WorktreePolicy(**payload["worktree"]),
        scheduler=SchedulerPolicy(
            mode=str(payload["scheduler"]["mode"]),
            max_active_issues=int(payload["scheduler"]["max_active_issues"]),
            max_waiting_for_human=int(payload["scheduler"]["max_waiting_for_human"]),
            lease_timeout_seconds=int(payload["scheduler"]["lease_timeout_seconds"]),
            lease_refresh_interval_seconds=int(
                payload["scheduler"]["lease_refresh_interval_seconds"]
            ),
            lease_lock_timeout_seconds=int(
                payload["scheduler"]["lease_lock_timeout_seconds"]
            ),
        ),
        implementation=ImplementationPolicy(
            enabled=bool(payload["implementation"]["enabled"]),
            command=tuple(payload["implementation"]["command"]),
        ),
        cleanup=CleanupPolicy(**payload["cleanup"]),
        github=GithubPolicy(
            issues_enabled=bool(payload["github"]["issues_enabled"]),
            pull_requests_enabled=bool(payload["github"]["pull_requests_enabled"]),
            draft_pr_by_default=bool(payload["github"]["draft_pr_by_default"]),
            auto_ready_pr_after_checks=bool(
                payload["github"]["auto_ready_pr_after_checks"]
            ),
            auto_merge_prs=bool(payload["github"]["auto_merge_prs"]),
            merge_method=str(payload["github"]["merge_method"]),
            assignee_username=_optional_payload_str(
                payload["github"].get("assignee_username")
            ),
        ),
    )


def _attempt_pr_auto_ready(
    repo_root: Path,
    repo_config: RepoConfig,
    local_status: IssueStatus,
    pr_payload: dict[str, object],
) -> tuple[dict[str, object], str | None, str | None]:
    if not repo_config.github.auto_ready_pr_after_checks:
        return pr_payload, "disabled", None
    if not bool(pr_payload.get("isDraft")):
        return pr_payload, "not-needed", None

    error = mark_pr_ready(repo_root, local_status.pr_number or 0)
    if error is not None:
        return pr_payload, "failed", error
    refreshed = get_pr(repo_root, local_status.pr_number or 0)
    return refreshed, "ready", None


def _attempt_pr_auto_merge(
    repo_root: Path,
    repo_config: RepoConfig,
    local_status: IssueStatus,
    pr_payload: dict[str, object],
    ci_status: str | None,
    findings_status: object,
) -> tuple[dict[str, object], str | None, str | None]:
    if not repo_config.github.auto_merge_prs:
        return pr_payload, "disabled", None

    blocked_reason = _pr_auto_merge_block_reason(
        pr_payload=pr_payload,
        ci_status=ci_status,
        findings_status=findings_status,
    )
    if blocked_reason is not None:
        return pr_payload, "waiting", blocked_reason

    pr_number = local_status.pr_number or 0
    head_sha = _optional_payload_str(pr_payload.get("headRefOid"))
    if not head_sha:
        return pr_payload, "waiting", "pull request head SHA is missing"

    if (
        local_status.extra.get("auto_merge_status") == "requested"
        and local_status.extra.get("auto_merge_requested_head_sha") == head_sha
    ):
        return pr_payload, "requested", _optional_payload_str(
            local_status.extra.get("auto_merge_reason")
        )

    error = merge_pr(
        repo_root,
        pr_number,
        merge_method=repo_config.github.merge_method,
        head_sha=head_sha,
    )
    if error is not None:
        return pr_payload, "failed", error

    refreshed = get_pr(repo_root, pr_number)
    if refreshed.get("mergedAt") is not None:
        return refreshed, "merged", None
    return (
        refreshed,
        "requested",
        "GitHub accepted the merge request, but the PR is still open while merge finalization completes.",
    )


def _pr_needs_conflict_resolution(pr_payload: dict[str, object]) -> bool:
    mergeable = _optional_payload_str(pr_payload.get("mergeable"))
    if mergeable is not None and mergeable.upper() == "CONFLICTING":
        return True

    merge_state_status = _optional_payload_str(pr_payload.get("mergeStateStatus"))
    return merge_state_status in {"BEHIND", "DIRTY", "UNKNOWN"}


def _pr_auto_merge_block_reason(
    *, pr_payload: dict[str, object], ci_status: str | None, findings_status: object
) -> str | None:
    if _optional_payload_str(pr_payload.get("state")) != "OPEN":
        return "pull request is no longer open"
    if bool(pr_payload.get("isDraft")):
        return "pull request is still draft"
    if ci_status != "passed":
        return "required CI checks have not passed yet"
    if findings_status != "clean":
        return "actionable findings are still present"

    review_decision = _optional_payload_str(pr_payload.get("reviewDecision"))
    if review_decision == "CHANGES_REQUESTED":
        return "a reviewer has requested changes"
    if review_decision == "REVIEW_REQUIRED":
        return "repository review policy still requires approval"

    mergeable = _optional_payload_str(pr_payload.get("mergeable"))
    if mergeable is not None and mergeable.upper() != "MERGEABLE":
        return f"GitHub reports the pull request is {mergeable.lower()}"

    merge_state_status = _optional_payload_str(pr_payload.get("mergeStateStatus"))
    if merge_state_status is not None and merge_state_status != "CLEAN":
        return f"GitHub merge state is {merge_state_status.lower()}"

    return None


def _connection_logins(value: object) -> tuple[str, ...]:
    if isinstance(value, dict):
        items = value.get("nodes")
    else:
        items = value
    if not isinstance(items, list):
        return ()
    return tuple(
        login
        for login in (
            _optional_payload_str(_optional_payload_mapping(item).get("login"))
            for item in items
        )
        if login
    )


def _load_gates_config(path: Path) -> GatesConfig:
    payload = read_json_file(path)
    return GatesConfig(
        must_pass_before_pr=bool(payload["must_pass_before_pr"]),
        rerun_after_review_fixes=bool(payload["rerun_after_review_fixes"]),
        rerun_after_conflict_resolution=bool(
            payload["rerun_after_conflict_resolution"]
        ),
        verification_order=tuple(payload["verification_order"]),
        format=GateCommand(
            enabled=bool(payload["format"]["enabled"]),
            command=tuple(payload["format"]["command"]),
        ),
        tests=TestsPolicy(
            targeted_first=bool(payload["tests"]["targeted_first"]),
            default_command=tuple(payload["tests"]["default_command"]),
            escalate_to_full_suite_when=tuple(
                payload["tests"]["escalate_to_full_suite_when"]
            ),
        ),
        review=ReviewPolicy(**payload["review"]),
        branch_policy=BranchPolicy(**payload["branch_policy"]),
        block_on=BlockOnPolicy(**payload["block_on"]),
    )


def _claim_issue(
    repo_root: Path,
    paths,
    issue_policy: IssuePolicy,
    repo_config: RepoConfig,
    issue: IssueCandidate,
    claimed_by: str,
) -> None:
    if issue_policy.claim.assign_to_me:
        ensure_issue_assignee(
            repo_root,
            issue_number=issue.number,
            assignee_username=repo_config.github.assignee_username,
            current_assignees=issue.assignees,
            overwrite_existing_assignees=issue_policy.claim.overwrite_existing_assignees,
        )

    if issue_policy.claim.add_comment:
        template_path = paths.templates_dir / issue_policy.claim.comment_template
        body = render_template(
            template_path,
            {
                "issue_number": issue.number,
                "issue_title": issue.title,
                "claimed_by": claimed_by,
            },
        )
        comment_on_issue(repo_root, issue.number, body)


def _write_completion_comment(
    repo_root: Path, paths, issue_number: int, issue_status: IssueStatus
) -> None:
    body = _render_completion_comment(paths, issue_number, issue_status)
    comment_on_issue(repo_root, issue_number, body)


def _render_no_op_comment(
    paths,
    issue_number: int,
    *,
    branch: str | None,
    worktree_path: str | None,
    cleanup_actions: list[str],
) -> str:
    template_path = paths.templates_dir / "issue-comment-no-op.md"
    return render_template(
        template_path,
        {
            "issue_number": issue_number,
            "branch": branch or "(none)",
            "worktree_path": worktree_path or "(none)",
            "cleanup_summary": _format_cleanup_summary(
                cleanup_actions,
                default="- no local cleanup was required",
            ),
        },
    )


def _block_issue(
    repo_root: Path,
    paths,
    issue_policy: IssuePolicy,
    local_status: IssueStatus,
    *,
    reason: str,
    next_action: str,
) -> IssueStatus:
    handoff_note_path = None
    if local_status.worktree_path and issue_policy.handoff.write_local_note:
        note_path = (
            Path(local_status.worktree_path)
            / ".sandcastle-agent"
            / f"issue-{local_status.issue_number}"
            / "handoff-note.md"
        )
        note_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.write_text(
            render_template(
                paths.templates_dir / issue_policy.handoff.local_note_template,
                {
                    "issue_number": local_status.issue_number,
                    "issue_title": local_status.extra.get(
                        "issue_title", f"Issue {local_status.issue_number}"
                    ),
                    "reason": reason,
                    "branch": local_status.branch or "(none)",
                    "worktree_path": local_status.worktree_path or "(none)",
                    "next_action": next_action,
                },
            ),
            encoding="utf-8",
        )
        handoff_note_path = str(note_path)

    body = render_template(
        paths.templates_dir / issue_policy.handoff.comment_template,
        {
            "issue_number": local_status.issue_number,
            "issue_title": local_status.extra.get(
                "issue_title", f"Issue {local_status.issue_number}"
            ),
            "reason": reason,
            "branch": local_status.branch or "(none)",
            "worktree_path": local_status.worktree_path or "(none)",
            "next_action": next_action,
        },
    )
    comment_on_issue(repo_root, local_status.issue_number, body)

    updated = IssueStatus(
        issue_number=local_status.issue_number,
        status="blocked",
        branch=local_status.branch,
        worktree_path=local_status.worktree_path,
        pr_number=local_status.pr_number,
        last_updated_utc=utc_now_iso(),
        extra={
            **local_status.extra,
            "blocked_reason": reason,
            "blocked_next_action": next_action,
            "handoff_note_path": handoff_note_path,
            "block_scope": BLOCK_SCOPE_ISSUE,
        },
    )
    write_issue_status(paths, updated)
    return updated


def _handle_pull_request_assignment_failure(
    repo_root: Path,
    paths,
    issue_policy: IssuePolicy,
    local_status: IssueStatus,
    *,
    pr_number: int | None,
    pr_url: str | None,
    error: RuntimeError,
) -> int:
    blocking_state = IssueStatus(
        issue_number=local_status.issue_number,
        status=local_status.status,
        branch=local_status.branch,
        worktree_path=local_status.worktree_path,
        pr_number=pr_number,
        last_updated_utc=utc_now_iso(),
        extra={
            **local_status.extra,
            "pr_url": pr_url,
            "branch_pushed": True,
            "blocked_resume_kind": BLOCKED_RESUME_KIND_PULL_REQUEST_ASSIGNMENT,
            "blocked_resume_status": local_status.status,
        },
    )
    blocked = _block_issue(
        repo_root,
        paths,
        issue_policy,
        blocking_state,
        reason=(
            f"Sandcastle could not complete GitHub pull-request assignment: {error}"
        ),
        next_action=(
            "Fix the configured GitHub assignee or repository permissions, then rerun "
            f"`python -m sandcastle.runner.main resume {local_status.issue_number}`."
        ),
    )
    print(
        json.dumps(
            {
                "issue_number": local_status.issue_number,
                "status": blocked.status,
                "pr_number": pr_number,
                "pr_url": pr_url,
                "error": str(error),
            },
            indent=2,
        )
    )
    return 1


def _render_completion_comment(paths, issue_number: int, issue_status: IssueStatus) -> str:
    template_path = paths.templates_dir / "issue-comment-complete.md"
    return render_template(
        template_path,
        {
            "issue_number": issue_number,
            "pr_number": issue_status.pr_number or "(none)",
            "branch": issue_status.extra.get("closed_branch")
            or issue_status.branch
            or "(none)",
            "status": issue_status.status,
            "cleanup_summary": _format_cleanup_summary(
                issue_status.extra.get("cleanup_actions", [])
            ),
        },
    )


def _hydrate_cleanup_state(repo_root: Path, local_status: IssueStatus) -> IssueStatus:
    branch = local_status.branch or _optional_payload_str(
        local_status.extra.get("closed_branch")
    )
    worktree_path = local_status.worktree_path or _optional_payload_str(
        local_status.extra.get("closed_worktree_path")
    )
    pr_number = local_status.pr_number
    pr_url = _optional_payload_str(local_status.extra.get("pr_url"))

    if pr_number is None and branch:
        recovered_pr_number, recovered_pr_url = find_pr_for_head(repo_root, branch)
        if recovered_pr_number is not None:
            pr_number = recovered_pr_number
            if recovered_pr_url is not None:
                pr_url = recovered_pr_url

    return IssueStatus(
        issue_number=local_status.issue_number,
        status=local_status.status,
        branch=branch,
        worktree_path=worktree_path,
        pr_number=pr_number,
        last_updated_utc=local_status.last_updated_utc,
        extra={
            **local_status.extra,
            "pr_url": pr_url or local_status.extra.get("pr_url"),
        },
    )


def _existing_cleanup_actions(local_status: IssueStatus) -> list[str]:
    raw_actions = local_status.extra.get("cleanup_actions", [])
    if not isinstance(raw_actions, list):
        return []
    return [str(action) for action in raw_actions]


def _append_cleanup_action(actions: list[str], action: str) -> None:
    if action not in actions:
        actions.append(action)


def _is_already_absent_remote_branch_error(error: RuntimeError) -> bool:
    message = str(error)
    return (
        (
            "remote rejected" in message
            and "unable to resolve reference" in message
        )
        or "remote ref does not exist" in message
    )


def _format_cleanup_summary(
    actions: object, *, default: str = "- merged PR recorded"
) -> str:
    if not isinstance(actions, list) or not actions:
        return default
    return "\n".join(f"- {action}" for action in actions)


def _implementation_optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _implementation_changed_files(
    implementation_payload: dict[str, object],
) -> list[str]:
    raw_files = implementation_payload.get("changed_files", [])
    if not isinstance(raw_files, list):
        return []
    changed_files: list[str] = []
    for item in raw_files:
        if not isinstance(item, str):
            continue
        normalized = item.replace("\\", "/").strip()
        if normalized:
            changed_files.append(normalized)
    return sorted(dict.fromkeys(changed_files))


def _meaningful_implementation_changed_files(
    implementation_payload: dict[str, object],
) -> list[str]:
    return [
        path
        for path in _implementation_changed_files(implementation_payload)
        if not _is_implementation_runtime_path(path)
    ]


def _meaningful_worktree_changed_files(worktree_path: str) -> list[str]:
    return [
        path
        for path in (
            item.replace("\\", "/").strip()
            for item in list_worktree_changed_files(worktree_path)
        )
        if path and not _is_implementation_runtime_path(path)
    ]


def _is_implementation_runtime_path(path: str) -> bool:
    normalized = path.replace("\\", "/").strip()
    return any(
        normalized == prefix.rstrip("/") or normalized.startswith(prefix)
        for prefix in IMPLEMENTATION_RUNTIME_PATH_PREFIXES
    )


def _implementation_diff_summary(implementation_payload: dict[str, object]) -> str | None:
    value = implementation_payload.get("diff_summary")
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _implementation_commit_message(issue_number: int, implementation_mode: str) -> str:
    if implementation_mode == "review-fix":
        return f"Sandcastle issue #{issue_number}: apply review fixes"
    return f"Sandcastle issue #{issue_number}: implement issue changes"


def _findings_fingerprint(findings_payload: dict[str, object]) -> str | None:
    findings = findings_payload.get("findings", [])
    if not isinstance(findings, list) or not findings:
        return None
    normalized: list[dict[str, str]] = []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        normalized.append(
            {
                "source": str(finding.get("source", "")),
                "kind": str(finding.get("kind", "")),
                "summary": str(finding.get("summary", "")),
                "details": str(finding.get("details", "")),
                "path": str(finding.get("path", "")),
            }
        )
    if not normalized:
        return None
    return json.dumps(normalized, sort_keys=True)


def _is_repeated_finding_set(local_status: IssueStatus, findings_fingerprint: str | None) -> bool:
    if not findings_fingerprint:
        return False
    previous_fingerprint = _implementation_optional_str(
        local_status.extra.get("findings_fingerprint")
    )
    previous_source = _implementation_optional_str(
        local_status.extra.get("findings_source_implementation_path")
    )
    current_source = _implementation_optional_str(local_status.extra.get("implementation_path"))
    if not previous_fingerprint or not previous_source or not current_source:
        return False
    if previous_source == current_source:
        return False
    return previous_fingerprint == findings_fingerprint


def _implementation_failure_reason(implementation_payload: dict[str, object]) -> str:
    stderr = str(implementation_payload.get("stderr", "") or "").strip()
    if stderr:
        return stderr
    returncode = implementation_payload.get("returncode")
    if returncode is not None:
        return f"implementation command failed with exit code {returncode}"
    return "implementation command failed before Sandcastle could continue"


def _can_force_remove_worktree(changes: list[str]) -> bool:
    if not changes:
        return False
    for line in changes:
        if not line.startswith("?? "):
            return False
        path = line[3:].replace("\\", "/").strip()
        if not (
            path.startswith("sandcastle/state/")
            or _is_implementation_runtime_path(path)
        ):
            return False
    return True


def _complete_no_op_issue(
    *,
    repo_root: Path,
    paths,
    repo_config: RepoConfig,
    local_status: IssueStatus,
    implementation_path: Path,
    implementation_payload: dict[str, object],
    implementation_mode: str,
    findings_path: object,
    revision_path: object,
    review_fix_attempt_count: int,
) -> IssueStatus:
    cleanup_actions = _existing_cleanup_actions(local_status)
    no_op_reason = "implementation command exited 0 but produced no meaningful code changes"
    original_branch = local_status.branch
    original_worktree_path = local_status.worktree_path
    branch = original_branch
    worktree_path = original_worktree_path
    removed_runtime_paths: list[str] = []

    meaningful_worktree_changes = (
        _meaningful_worktree_changed_files(original_worktree_path)
        if original_worktree_path
        else []
    )
    branch_has_local_commits = False
    if original_branch and branch_exists(repo_root, original_branch):
        branch_has_local_commits = (
            branch_ahead_count(repo_root, repo_config.base_ref, original_branch) > 0
        )

    if branch_has_local_commits:
        _append_cleanup_action(
            cleanup_actions,
            f"kept local branch {original_branch} because it is ahead of {repo_config.base_ref}",
        )
    if meaningful_worktree_changes and original_worktree_path:
        _append_cleanup_action(
            cleanup_actions,
            f"kept worktree {original_worktree_path} because it still has non-runtime local changes",
        )

    if not branch_has_local_commits and not meaningful_worktree_changes:
        worktree_path = _cleanup_no_op_worktree(
            repo_root,
            repo_config,
            original_worktree_path,
            cleanup_actions,
        )
        branch = _cleanup_no_op_branch(repo_root, original_branch, cleanup_actions)
        if (
            repo_config.cleanup.delete_remote_branch_after_failed_startup
            and original_branch
        ):
            if branch is not None:
                _append_cleanup_action(
                    cleanup_actions,
                    "kept remote branch "
                    f"{repo_config.remote}/{original_branch} because local branch cleanup did not complete safely",
                )
            elif not remote_branch_exists(repo_root, repo_config.remote, original_branch):
                _append_cleanup_action(
                    cleanup_actions,
                    f"remote branch {repo_config.remote}/{original_branch} was already absent",
                )
            else:
                try:
                    delete_remote_branch(repo_root, repo_config.remote, original_branch)
                    _append_cleanup_action(
                        cleanup_actions,
                        f"deleted remote branch {repo_config.remote}/{original_branch}",
                    )
                except RuntimeError as error:
                    if _is_already_absent_remote_branch_error(error):
                        _append_cleanup_action(
                            cleanup_actions,
                            f"remote branch {repo_config.remote}/{original_branch} was already absent",
                        )
                    else:
                        _append_cleanup_action(
                            cleanup_actions,
                            "kept remote branch "
                            f"{repo_config.remote}/{original_branch} because cleanup could not delete it safely ({error})",
                        )
        removed_runtime_paths = _delete_no_op_runtime_artifacts(
            paths, local_status.issue_number
        )
        if removed_runtime_paths:
            _append_cleanup_action(
                cleanup_actions,
                f"removed {len(removed_runtime_paths)} transient runtime file(s)",
            )

    comment_on_issue(
        repo_root,
        local_status.issue_number,
        _render_no_op_comment(
            paths,
            local_status.issue_number,
            branch=original_branch,
            worktree_path=original_worktree_path,
            cleanup_actions=cleanup_actions,
        ),
    )

    updated = IssueStatus(
        issue_number=local_status.issue_number,
        status="no-op",
        branch=branch,
        worktree_path=worktree_path,
        pr_number=None,
        last_updated_utc=utc_now_iso(),
        extra={
            **local_status.extra,
            "implementation_path": str(implementation_path),
            "implementation_status": "no-op",
            "implementation_returncode": implementation_payload.get("returncode"),
            "implementation_mode": implementation_mode,
            "implementation_changed_files": _implementation_changed_files(
                implementation_payload
            ),
            "implementation_diff_summary": _implementation_diff_summary(
                implementation_payload
            ),
            "implementation_pre_command_head_sha": _implementation_optional_str(
                implementation_payload.get("pre_command_head_sha")
            ),
            "implementation_post_command_head_sha": _implementation_optional_str(
                implementation_payload.get("post_command_head_sha")
            ),
            "implementation_head_sha": None,
            "implementation_runner_committed": False,
            "implementation_runner_pushed": False,
            "implementation_input_findings_path": (
                str(findings_path) if findings_path else None
            ),
            "implementation_input_revision_path": (
                str(revision_path) if revision_path else None
            ),
            "review_fix_attempt_count": review_fix_attempt_count,
            "cleanup_actions": cleanup_actions,
            "no_op_reason": no_op_reason,
            "no_op_branch": original_branch,
            "no_op_worktree_path": original_worktree_path,
            "no_op_removed_runtime_paths": removed_runtime_paths,
            "issue_closed": False,
            "verification_status": None,
            "verification_path": None,
            "verified_implementation_path": None,
            "verified_head_sha": None,
            "review_path": None,
            "review_status": None,
            "review_finding_count": None,
            "reviewed_implementation_path": None,
            "reviewed_head_sha": None,
            "findings_path": None,
            "findings_status": None,
            "findings_count": None,
            "findings_source_implementation_path": None,
            "findings_source_head_sha": None,
            "revision_path": None,
            "verification_is_stale": None,
            "review_is_stale": None,
            "findings_are_stale": None,
            "auto_ready_status": None,
            "auto_ready_reason": None,
            "auto_merge_status": None,
            "auto_merge_reason": None,
            "auto_merge_requested_head_sha": None,
            "error": None,
        },
    )
    write_issue_status(paths, updated)
    return updated


def _cleanup_no_op_worktree(
    repo_root: Path,
    repo_config: RepoConfig,
    worktree_path: str | None,
    cleanup_actions: list[str],
) -> str | None:
    if not worktree_path:
        return None

    worktree = Path(worktree_path)
    if not worktree.exists():
        _append_cleanup_action(
            cleanup_actions, f"worktree {worktree_path} was already absent"
        )
        return None

    try:
        remove_worktree(repo_root, worktree_path)
        _append_cleanup_action(cleanup_actions, f"removed worktree {worktree_path}")
        return None
    except RuntimeError as error:
        if not worktree_is_registered(repo_root, worktree_path):
            remove_orphaned_worktree_directory(
                repo_root,
                repo_config.worktree.root,
                worktree_path,
            )
            _append_cleanup_action(
                cleanup_actions,
                f"removed orphaned managed worktree directory {worktree_path} after git had already deregistered it",
            )
            return None

        worktree_changes = list_worktree_changes(worktree_path)
        if _can_force_remove_worktree(worktree_changes):
            force_remove_worktree(repo_root, worktree_path)
            _append_cleanup_action(
                cleanup_actions,
                f"force-removed worktree {worktree_path} because it only contained Sandcastle runtime artifacts",
            )
            return None

        _append_cleanup_action(
            cleanup_actions,
            f"kept worktree {worktree_path} because cleanup could not remove it safely ({error})",
        )
        return worktree_path


def _cleanup_no_op_branch(
    repo_root: Path,
    branch: str | None,
    cleanup_actions: list[str],
) -> str | None:
    if not branch:
        return None
    if not branch_exists(repo_root, branch):
        _append_cleanup_action(
            cleanup_actions, f"local branch {branch} was already absent"
        )
        return None

    try:
        delete_local_branch(repo_root, branch)
        _append_cleanup_action(cleanup_actions, f"deleted local branch {branch}")
        return None
    except RuntimeError as error:
        _append_cleanup_action(
            cleanup_actions,
            f"kept local branch {branch} because cleanup could not delete it ({error})",
        )
        return branch


def _delete_no_op_runtime_artifacts(paths, issue_number: int) -> list[str]:
    issue_name = f"{issue_number}.json"
    candidates = (
        paths.ci_dir / issue_name,
        paths.verification_dir / issue_name,
        paths.conflict_dir / issue_name,
        paths.review_dir / issue_name,
        paths.findings_dir / issue_name,
        paths.revision_dir / issue_name,
        paths.leases_dir / issue_name,
        paths.leases_dir / f"{issue_number}.lock",
    )
    removed: list[str] = []
    for path in candidates:
        if not path.exists():
            continue
        path.unlink(missing_ok=True)
        removed.append(str(path))
    return removed


def _is_failed_startup_cleanup_candidate(
    repo_root: Path, repo_config: RepoConfig, local_status: IssueStatus
) -> bool:
    if local_status.pr_number is not None:
        return False
    if local_status.status != "blocked":
        return False
    branch = local_status.branch
    if branch and branch_exists(repo_root, branch):
        if branch_ahead_count(repo_root, repo_config.base_ref, branch) > 0:
            return False
    return True


def _cleanup_failed_startup_issue(
    repo_root: Path, paths, repo_config: RepoConfig, local_status: IssueStatus
) -> int:
    cleanup_actions = _existing_cleanup_actions(local_status)
    branch = local_status.branch
    worktree_path = local_status.worktree_path

    if repo_config.cleanup.remove_worktree_after_failed_startup and worktree_path:
        worktree = Path(worktree_path)
        if not worktree.exists():
            _append_cleanup_action(
                cleanup_actions, f"worktree {worktree_path} was already absent"
            )
        else:
            try:
                remove_worktree(repo_root, worktree_path)
                _append_cleanup_action(
                    cleanup_actions, f"removed worktree {worktree_path}"
                )
            except RuntimeError as error:
                if not worktree_is_registered(repo_root, worktree_path):
                    remove_orphaned_worktree_directory(
                        repo_root,
                        repo_config.worktree.root,
                        worktree_path,
                    )
                    _append_cleanup_action(
                        cleanup_actions,
                        f"removed orphaned managed worktree directory {worktree_path} after git had already deregistered it",
                    )
                else:
                    worktree_changes = list_worktree_changes(worktree_path)
                    if _can_force_remove_worktree(worktree_changes):
                        force_remove_worktree(repo_root, worktree_path)
                        _append_cleanup_action(
                            cleanup_actions,
                            f"force-removed worktree {worktree_path} after failed startup because it only contained Sandcastle runtime artifacts",
                        )
                    else:
                        print(
                            json.dumps(
                                {
                                    "error": "failed-startup cleanup could not remove the worktree safely",
                                    "issue_number": local_status.issue_number,
                                    "worktree_path": worktree_path,
                                    "details": str(error),
                                },
                                indent=2,
                            )
                        )
                        return 1

    if repo_config.cleanup.delete_local_branch_after_failed_startup and branch:
        if not branch_exists(repo_root, branch):
            _append_cleanup_action(
                cleanup_actions, f"local branch {branch} was already absent"
            )
        else:
            try:
                delete_local_branch(repo_root, branch)
                _append_cleanup_action(cleanup_actions, f"deleted local branch {branch}")
            except RuntimeError:
                try:
                    force_delete_local_branch(repo_root, branch)
                    _append_cleanup_action(
                        cleanup_actions,
                        f"force-deleted local branch {branch} after failed startup",
                    )
                except RuntimeError as error:
                    print(
                        json.dumps(
                            {
                                "error": "failed-startup cleanup could not delete the local branch",
                                "issue_number": local_status.issue_number,
                                "branch": branch,
                                "details": str(error),
                            },
                            indent=2,
                        )
                    )
                    return 1

    if repo_config.cleanup.delete_remote_branch_after_failed_startup and branch:
        if not remote_branch_exists(repo_root, repo_config.remote, branch):
            _append_cleanup_action(
                cleanup_actions,
                f"remote branch {repo_config.remote}/{branch} was already absent",
            )
        else:
            try:
                delete_remote_branch(repo_root, repo_config.remote, branch)
                _append_cleanup_action(
                    cleanup_actions,
                    f"deleted remote branch {repo_config.remote}/{branch}",
                )
            except RuntimeError as error:
                if _is_already_absent_remote_branch_error(error):
                    _append_cleanup_action(
                        cleanup_actions,
                        f"remote branch {repo_config.remote}/{branch} was already absent",
                    )
                else:
                    print(
                        json.dumps(
                            {
                                "error": "failed-startup cleanup could not delete the remote branch",
                                "issue_number": local_status.issue_number,
                                "branch": branch,
                                "details": str(error),
                            },
                            indent=2,
                        )
                    )
                    return 1

    removed_runtime_paths: list[str] = []
    if repo_config.cleanup.delete_runtime_state_after_failed_startup:
        removed_runtime_paths = [
            str(path) for path in delete_issue_runtime_state(paths, local_status.issue_number)
        ]
        if removed_runtime_paths:
            _append_cleanup_action(
                cleanup_actions,
                f"removed {len(removed_runtime_paths)} runtime state file(s)",
            )

    print(
        json.dumps(
            {
                "issue_number": local_status.issue_number,
                "status": "failed-startup-cleaned",
                "cleanup_actions": cleanup_actions,
                "removed_runtime_paths": removed_runtime_paths,
            },
            indent=2,
        )
    )
    return 0


def _list_blocked_issues(paths) -> list[IssueStatus]:
    blocked = [status for status in list_issue_statuses(paths) if status.status == "blocked"]
    blocked.sort(
        key=lambda status: (
            status.last_updated_utc or "",
            status.issue_number,
        )
    )
    return blocked


def _blocked_signatures(issue_statuses: list[IssueStatus]) -> tuple[tuple[object, ...], ...]:
    blocked = [status for status in issue_statuses if status.status == "blocked"]
    return tuple(
        (
            status.issue_number,
            status.branch,
            status.worktree_path,
            status.pr_number,
            status.last_updated_utc,
            json.dumps(status.extra, sort_keys=True),
        )
        for status in blocked
    )


def _render_pr_body(
    paths, issue_number: int, issue_title: str, branch: str, issue_body: str
) -> str:
    template_path = paths.templates_dir / "pr-body.md"
    summary_points = _pick_points(
        _extract_section_bullets(issue_body, "## Goal"),
        (
            "Updates the Sandcastle workflow for this issue.",
            "Uses the issue branch and PR lifecycle flow defined by Sandcastle.",
            "Keeps the change scoped to the issue requirements.",
        ),
    )
    validation_points = _pick_points(
        _extract_section_bullets(issue_body, "## Done when"),
        (
            "A fresh Sandcastle issue branch and draft PR are created.",
            "The branch carries only the issue-scoped Sandcastle changes.",
            "The resulting PR body reflects the issue requirements accurately.",
        ),
    )
    return render_template(
        template_path,
        {
            "issue_number": issue_number,
            "issue_title": issue_title,
            "branch": branch,
            "summary_point_1": summary_points[0],
            "summary_point_2": summary_points[1],
            "summary_point_3": summary_points[2],
            "validation_point_1": validation_points[0],
            "validation_point_2": validation_points[1],
            "validation_point_3": validation_points[2],
            "note_point_1": "Generated by Sandcastle from the current issue context rather than a hardcoded milestone summary.",
            "note_point_2": "Sandcastle may mark this PR ready and merge it automatically once checks and merge policy are satisfied.",
        },
    )


def _extract_section_bullets(markdown: str, heading: str) -> list[str]:
    lines = markdown.splitlines()
    in_section = False
    bullets: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped == heading:
            in_section = True
            continue
        if in_section and stripped.startswith("## "):
            break
        if in_section and stripped.startswith("- "):
            bullets.append(stripped[2:].strip())

    return bullets


def _pick_points(
    candidates: list[str], fallback: tuple[str, str, str]
) -> tuple[str, str, str]:
    selected = list(candidates[:3])
    while len(selected) < 3:
        selected.append(fallback[len(selected)])
    return selected[0], selected[1], selected[2]


def _optional_payload_str(value: object) -> str | None:
    return str(value) if value is not None else None


def _optional_payload_mapping(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return value
    return {}


def _active_signature(
    local_status: IssueStatus,
) -> tuple[
    int,
    str,
    int | None,
    bool,
    str | None,
    str | None,
    str | None,
    str | None,
] :
    return (
        local_status.issue_number,
        local_status.status,
        local_status.pr_number,
        local_status.extra.get("context_loaded") is True,
        _optional_payload_str(local_status.extra.get("implementation_path")),
        _optional_payload_str(local_status.extra.get("implementation_status")),
        _optional_payload_str(local_status.extra.get("ci_status")),
        _optional_payload_str(local_status.extra.get("findings_status")),
    )


def _active_signatures(
    active_statuses: list[IssueStatus],
) -> tuple[
    tuple[int, str, int | None, bool, str | None, str | None, str | None, str | None],
    ...,
]:
    return tuple(_active_signature(status) for status in active_statuses)


def _serialize_active_issue(local_status: IssueStatus) -> dict[str, object]:
    lease = active_issue_lease(
        build_paths(repo_root_from_file(__file__)), local_status.issue_number
    )
    operator_state = _operator_state(local_status)
    return {
        "issue_number": local_status.issue_number,
        "status": local_status.status,
        "operator_state": operator_state,
        "operator_wait_reason": _operator_wait_reason(local_status),
        "pr_number": local_status.pr_number,
        "lease_owner": None if lease is None else lease.get("owner_id"),
        "lease_expires_at": None if lease is None else lease.get("expires_at"),
        "consumes_active_slot": _consumes_active_slot(local_status),
    }


def _serialize_active_issues(active_statuses: list[IssueStatus]) -> list[dict[str, object]]:
    return [_serialize_active_issue(status) for status in active_statuses]


def _scheduler_snapshot(
    active_statuses: list[IssueStatus],
    backlog: BacklogSelection,
    scheduler: SchedulerPolicy,
    *,
    blocked_issue_count: int = 0,
) -> dict[str, object]:
    active_issue_count = _count_active_work(active_statuses)
    waiting_issue_count = _count_waiting_work(active_statuses)
    at_active_cap = active_issue_count >= scheduler.max_active_issues
    at_waiting_cap = waiting_issue_count >= scheduler.max_waiting_for_human
    blocked_by_serial_active_slot = _is_serial_active_slot(scheduler) and bool(
        active_issue_count
    )
    capacity_available = (
        not blocked_by_serial_active_slot and not at_active_cap and not at_waiting_cap
    )
    return {
        "scheduler_mode": scheduler.mode,
        "active_issue_count": active_issue_count,
        "waiting_issue_count": waiting_issue_count,
        "waiting_for_human_count": waiting_issue_count,
        "total_tracked_issue_count": len(active_statuses),
        "max_active_issues": scheduler.max_active_issues,
        "max_waiting_for_human": scheduler.max_waiting_for_human,
        "at_active_issue_cap": at_active_cap,
        "at_waiting_issue_cap": at_waiting_cap,
        "at_waiting_for_human_cap": at_waiting_cap,
        "blocked_by_serial_active_slot": blocked_by_serial_active_slot,
        "capacity_available_for_new_issue": capacity_available,
        "blocked_issue_count": blocked_issue_count,
        "operator_state_counts": {
            "active": _count_operator_state(active_statuses, "active"),
            "waiting": _count_operator_state(active_statuses, "waiting"),
            "blocked": blocked_issue_count,
            "done": 0,
        },
        "active_issues": _serialize_active_issues(active_statuses),
        **_serialize_backlog_selection(backlog),
    }


def _waiting_reason(
    active_statuses: list[IssueStatus],
    backlog: BacklogSelection,
    scheduler: SchedulerPolicy,
    *,
    blocked_issue_count: int = 0,
) -> str:
    next_issue = backlog.selected_issue
    if not active_statuses:
        if next_issue is None:
            if blocked_issue_count:
                return (
                    "no eligible open issue is available for pickup; "
                    f"{blocked_issue_count} blocked issue(s) remain handed off for human follow-up"
                )
            return (
                "all eligible open issues are currently held by label policy"
                if backlog.held_issue_numbers
                else "no eligible open issue is available for pickup"
            )
        if backlog.reordered_issue_numbers:
            return "label policy reordered backlog pickup ahead of older eligible issues"
        return "eligible issue is available for pickup on the next poll"

    if _is_serial_active_slot(scheduler) and _count_active_work(active_statuses) > 0:
        return (
            "waiting because the serial active coding slot is already in use"
        )

    waiting_issue_count = _count_waiting_work(active_statuses)
    if waiting_issue_count >= scheduler.max_waiting_for_human:
        return "waiting because the passive waiting lane is already at capacity"
    if _count_active_work(active_statuses) >= scheduler.max_active_issues:
        return "waiting because the active issue set is already at the WIP cap"

    first = active_statuses[0]
    if _operator_state(first) == "waiting":
        return _operator_wait_reason(first) or (
            f"waiting while issue {first.issue_number} remains {first.status}"
        )
    if first.status == "implementing":
        return "waiting for an implementation commit or PR creation conditions"
    return f"waiting for the next poll while issue {first.issue_number} remains {first.status}"


def _pick_next_issue(repo_root: Path, paths, issue_policy: IssuePolicy):
    return _select_backlog_issue(repo_root, paths, issue_policy).selected_issue


def _select_backlog_issue(
    repo_root: Path, paths, issue_policy: IssuePolicy
) -> BacklogSelection:
    limit = max(issue_policy.pull_order.max_issues_per_run, 25)
    statuses = {status.issue_number: status for status in list_issue_statuses(paths)}
    issues = list_open_issues(repo_root, limit=limit)
    eligible: list[IssueCandidate] = []
    held: list[int] = []
    for issue in issues:
        if issue.number in statuses:
            continue

        decision = evaluate_issue(issue, issue_policy)
        if decision.eligible:
            eligible.append(issue)
            continue

        if is_held_only_by_label_policy(issue, issue_policy):
            held.append(issue.number)

    candidates: list[tuple[int, int, str, IssueCandidate]] = []
    for issue in eligible:
        policy = _issue_pickup_policy(issue, issue_policy.pull_order)
        if policy["held"]:
            continue
        candidates.append(
            (
                int(policy["priority_rank"]),
                issue.number,
                str(policy["priority"]),
                issue,
            )
        )

    candidates.sort(key=lambda item: (item[0], item[1]))
    if not candidates:
        return BacklogSelection(
            selected_issue=None,
            held_issue_numbers=tuple(sorted(held)),
        )

    selected_rank, _, selected_priority, selected_issue = candidates[0]
    reordered_issue_numbers = tuple(
        issue.number
        for rank, _, _, issue in candidates[1:]
        if issue.number < selected_issue.number and rank > selected_rank
    )
    return BacklogSelection(
        selected_issue=selected_issue,
        selected_priority=selected_priority,
        held_issue_numbers=tuple(sorted(held)),
        reordered_issue_numbers=reordered_issue_numbers,
    )


def _issue_pickup_policy(
    issue, pull_order: PullOrderPolicy
) -> dict[str, object]:
    labels = set(issue.labels)
    hold = bool(pull_order.hold_label and pull_order.hold_label in labels)
    high = bool(
        pull_order.priority_high_label and pull_order.priority_high_label in labels
    )
    low = bool(
        pull_order.priority_low_label and pull_order.priority_low_label in labels
    )

    if high and not low:
        return {"held": hold, "priority": "high", "priority_rank": 0}
    if low and not high:
        return {"held": hold, "priority": "low", "priority_rank": 2}
    return {"held": hold, "priority": "normal", "priority_rank": 1}


def _serialize_backlog_selection(backlog: BacklogSelection) -> dict[str, object]:
    return {
        "next_issue_number": (
            None if backlog.selected_issue is None else backlog.selected_issue.number
        ),
        "next_issue_priority": backlog.selected_priority,
        "held_issue_numbers": list(backlog.held_issue_numbers),
        "reordered_issue_numbers": list(backlog.reordered_issue_numbers),
    }


def _serialize_dependency_status(
    decision: EligibilityDecision,
) -> dict[str, object]:
    return {
        "source": decision.dependency_source,
        "blocking_issue_numbers": list(decision.blocking_issue_numbers),
        "dependency_error": decision.dependency_error,
    }


def _list_active_issues(paths) -> list[IssueStatus]:
    active_statuses = [
        status
        for status in list_issue_statuses(paths)
        if status.status not in DONE_STATUSES and status.status != "blocked"
    ]
    active_statuses.sort(
        key=lambda status: (
            status.last_updated_utc or "",
            status.issue_number,
        )
    )
    return active_statuses


def _select_next_actionable_issue(active_statuses: list[IssueStatus]) -> IssueStatus | None:
    ordered = _ordered_actionable_issues(active_statuses)
    if not ordered:
        return None
    return ordered[0]


def _ordered_actionable_issues(active_statuses: list[IssueStatus]) -> list[IssueStatus]:
    actionable = [
        status for status in active_statuses if _action_priority(status) is not None
    ]
    actionable.sort(
        key=lambda status: (
            _action_priority(status),
            status.last_updated_utc or "",
            status.issue_number,
        )
    )
    return actionable


def _select_next_monitorable_issue(active_statuses: list[IssueStatus]) -> IssueStatus | None:
    monitorable = [
        status for status in active_statuses if _needs_background_observation(status)
    ]
    if not monitorable:
        return None
    monitorable.sort(
        key=lambda status: (
            status.last_updated_utc or "",
            status.issue_number,
        )
    )
    return monitorable[0]


def _can_pick_new_issue(
    active_statuses: list[IssueStatus], scheduler: SchedulerPolicy
) -> bool:
    if _is_serial_active_slot(scheduler):
        return (
            _count_active_work(active_statuses) == 0
            and _count_waiting_work(active_statuses)
            < scheduler.max_waiting_for_human
        )
    if _count_active_work(active_statuses) >= scheduler.max_active_issues:
        return False
    return _count_waiting_work(active_statuses) < scheduler.max_waiting_for_human


def _is_serial_active_slot(scheduler: SchedulerPolicy) -> bool:
    return scheduler.mode in {"serial-merge-first", "serial-active-slot"}


def _count_waiting_work(active_statuses: list[IssueStatus]) -> int:
    return sum(
        1 for status in active_statuses if _operator_state(status) == "waiting"
    )


def _count_active_work(active_statuses: list[IssueStatus]) -> int:
    return sum(1 for status in active_statuses if _consumes_active_slot(status))


def _count_operator_state(
    active_statuses: list[IssueStatus], operator_state: str
) -> int:
    return sum(1 for status in active_statuses if _operator_state(status) == operator_state)


def _blocked_issue_scope(local_status: IssueStatus) -> str:
    scope = _optional_payload_str(local_status.extra.get("block_scope"))
    if scope == BLOCK_SCOPE_QUEUE or bool(local_status.extra.get("queue_blocking")):
        return BLOCK_SCOPE_QUEUE
    return BLOCK_SCOPE_ISSUE


def _consumes_active_slot(local_status: IssueStatus) -> bool:
    return local_status.status in {"implementing", "revising", "resolving-conflicts"}


def _is_waiting_for_human_status(status: str) -> bool:
    return status in HUMAN_WAIT_STATUSES


def _human_wait_status(pr_payload: dict[str, object]) -> str:
    if bool(pr_payload.get("isDraft")):
        return "waiting-human-review"
    review_decision = _optional_payload_str(pr_payload.get("reviewDecision"))
    if review_decision == "APPROVED":
        return "approved-awaiting-merge"
    return "waiting-human-review"


def _pr_requires_follow_up(local_status: IssueStatus) -> bool:
    ci_status = str(local_status.extra.get("ci_status", "") or "")
    findings_status = str(local_status.extra.get("findings_status", "") or "")
    return (
        ci_status == "failed"
        or findings_status == "findings"
        or bool(local_status.extra.get("verification_is_stale"))
        or bool(local_status.extra.get("review_is_stale"))
        or bool(local_status.extra.get("findings_are_stale"))
    )


def _action_priority(local_status: IssueStatus) -> int | None:
    if local_status.status == "merged":
        return 0
    if local_status.status == "resolving-conflicts":
        return 1
    if local_status.status in {"review-fix-loop", "revising"}:
        return 2
    if local_status.status == "pr-open":
        if _pr_requires_follow_up(local_status):
            return 3
        return None
    if local_status.status in {"implementing", "ready-for-pr"}:
        return 4
    if local_status.status in {"branch-created", "claimed"}:
        return 5
    if local_status.status == POST_MERGE_HEALTH_STATUS:
        return None
    if _is_waiting_for_human_status(local_status.status):
        return None
    return 10


def _needs_background_observation(local_status: IssueStatus) -> bool:
    if local_status.status == POST_MERGE_HEALTH_STATUS:
        return True
    if local_status.pr_number is None:
        return False
    if _is_waiting_for_human_status(local_status.status):
        return True
    if local_status.status != "pr-open":
        return False
    return _operator_state(local_status) == "waiting"


def _refresh_issue_observation(
    repo_root: Path,
    paths,
    repo_config: RepoConfig,
    local_status: IssueStatus,
) -> str:
    if local_status.status == POST_MERGE_HEALTH_STATUS:
        result = _handle_post_merge_health(
            repo_root, paths, repo_config, local_status.issue_number
        )
        reloaded = try_load_issue_status(paths, local_status.issue_number)
        if result == 0 and reloaded is not None:
            return reloaded.status
        return local_status.status
    if _is_waiting_for_human_status(local_status.status):
        result = _handle_sync_pr(repo_root, paths, repo_config, local_status.issue_number)
        reloaded = try_load_issue_status(paths, local_status.issue_number)
        if result == 0 and reloaded is not None:
            return reloaded.status
        return local_status.status
    if local_status.status == "pr-open":
        result = _handle_probe_ci(repo_root, paths, repo_config, local_status.issue_number)
        reloaded = try_load_issue_status(paths, local_status.issue_number)
        if result == 0 and reloaded is not None:
            return reloaded.status
        return local_status.status
    return local_status.status


def _status_priority(local_status: IssueStatus) -> int:
    priorities = {
        "merged": 0,
        "resolving-conflicts": 1,
        "review-fix-loop": 2,
        "revising": 3,
        "pr-open": 4,
        "implementing": 5,
        "branch-created": 6,
        "claimed": 7,
        "ready-for-pr": 8,
        "waiting-human-review": 9,
        "approved-awaiting-merge": 10,
        "ready-for-human": 11,
        POST_MERGE_HEALTH_STATUS: 12,
    }
    return priorities.get(local_status.status, 50)


def _load_ci_payload(local_status: IssueStatus) -> dict[str, object] | None:
    ci_path = local_status.extra.get("ci_path")
    if not ci_path:
        return None
    path = Path(str(ci_path))
    if not path.exists():
        return None
    return read_json_file(path)


def _serialize_local_state(local_status: IssueStatus) -> dict[str, object]:
    return {
        "status": local_status.status,
        "operator_state": _operator_state(local_status),
        "operator_wait_reason": _operator_wait_reason(local_status),
        "branch": local_status.branch,
        "worktree_path": local_status.worktree_path,
        "pr_number": local_status.pr_number,
        "last_updated_utc": local_status.last_updated_utc,
        "extra": local_status.extra,
    }


def _operator_state(local_status: IssueStatus) -> str:
    if local_status.status == "blocked":
        return "blocked"
    if local_status.status in DONE_STATUSES:
        return "done"
    if local_status.status == POST_MERGE_HEALTH_STATUS:
        return "waiting"
    if local_status.status == "pr-open":
        if _pr_requires_follow_up(local_status):
            return "active"
        return "waiting"
    if _is_waiting_for_human_status(local_status.status):
        return "waiting"
    if local_status.status in ACTIVE_DETAIL_STATUSES:
        return "active"
    return "active"


def _operator_wait_reason(local_status: IssueStatus) -> str | None:
    if _operator_state(local_status) != "waiting":
        return None
    if local_status.status == POST_MERGE_HEALTH_STATUS:
        return "waiting for post-merge main health verification"
    if local_status.status == "approved-awaiting-merge":
        return "waiting for merge after approval"
    if _is_waiting_for_human_status(local_status.status):
        return "waiting for human review or merge"
    if local_status.status == "pr-open":
        return "waiting for PR CI or merge-state changes"
    return f"waiting while detailed state remains {local_status.status}"


if __name__ == "__main__":
    sys.exit(main())
