from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from sandcastle.runner.main import (
    POST_MERGE_HEALTH_STATUS,
    _handle_auto_step,
    _handle_cleanup,
    _handle_post_merge_health,
    _handle_sync_pr,
)
from sandcastle.runner.models import (
    BacklogSelection,
    BlockOnPolicy,
    BranchPolicy,
    CleanupPolicy,
    ClaimPolicy,
    CompletionPolicy,
    GateCommand,
    GatesConfig,
    GithubPolicy,
    HandoffPolicy,
    IssueCandidate,
    IssuePolicy,
    IssueStatus,
    ImplementationPolicy,
    PullOrderPolicy,
    RepoConfig,
    ReviewPolicy,
    SandcastlePaths,
    SchedulerPolicy,
    TestsPolicy,
    WorktreePolicy,
)
from sandcastle.runner.state_store import (
    read_json_file,
    try_load_issue_status,
    write_issue_status,
    write_json_file,
)


def _paths(root: Path) -> SandcastlePaths:
    state_dir = root / "state"
    return SandcastlePaths(
        root=root / "sandcastle",
        config_dir=root / "sandcastle" / "config",
        templates_dir=root / "sandcastle" / "templates",
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


def _issue_policy() -> IssuePolicy:
    return IssuePolicy(
        required_labels=("ready-for-agent", "type:dev"),
        forbidden_labels=("needs-info", "ready-for-human", "wontfix", "type:infra"),
        require_open_state=True,
        require_unassigned=False,
        allow_batches=False,
        claim=ClaimPolicy(
            assign_to_me=True,
            add_comment=True,
            comment_template="issue-comment-claimed.md",
        ),
        pull_order=PullOrderPolicy(
            strategy="oldest-first",
            max_issues_per_run=1,
            hold_label="sandcastle:hold",
            priority_high_label="sandcastle:priority-high",
            priority_low_label="sandcastle:priority-low",
        ),
        handoff=HandoffPolicy(
            comment_template="issue-comment-blocked.md",
            write_local_note=True,
            local_note_template="handoff-note.md",
        ),
        completion=CompletionPolicy(comment_template="issue-comment-complete.md"),
    )


def _repo_config() -> RepoConfig:
    return RepoConfig(
        repo="example-maintainer/example-project",
        remote="origin",
        default_branch="main",
        base_ref="origin/main",
        branch_prefix="sandcastle",
        branch_name_pattern="sandcastle/issue-{issue_number}-{slug}",
        worktree=WorktreePolicy(
            enabled=True,
            root=".sandcastle-worktrees",
            path_pattern=".sandcastle-worktrees/issue-{issue_number}",
        ),
        scheduler=SchedulerPolicy(
            mode="serial-active-slot",
            max_active_issues=3,
            max_waiting_for_human=3,
            lease_timeout_seconds=1800,
            lease_refresh_interval_seconds=60,
            lease_lock_timeout_seconds=30,
        ),
        implementation=ImplementationPolicy(enabled=True, command=("python",)),
        cleanup=CleanupPolicy(
            close_issue_after_merge=True,
            remove_worktree_after_merge=True,
            delete_local_branch_after_merge=True,
            delete_remote_branch_after_merge=True,
            remove_worktree_after_failed_startup=True,
            delete_local_branch_after_failed_startup=True,
            delete_remote_branch_after_failed_startup=True,
            delete_runtime_state_after_failed_startup=True,
            remove_worktree_after_block=False,
            delete_local_branch_after_block=False,
        ),
        github=GithubPolicy(
            issues_enabled=True,
            pull_requests_enabled=True,
            draft_pr_by_default=True,
            auto_ready_pr_after_checks=True,
            auto_merge_prs=True,
            merge_method="merge",
            assignee_username="example-maintainer",
        ),
    )


def _gates_config() -> GatesConfig:
    return GatesConfig(
        must_pass_before_pr=True,
        rerun_after_review_fixes=True,
        rerun_after_conflict_resolution=True,
        verification_order=("format", "tests", "review"),
        format=GateCommand(enabled=True, command=("python", "-m", "ruff", "check")),
        tests=TestsPolicy(
            targeted_first=True,
            default_command=("python", "-m", "pytest"),
            escalate_to_full_suite_when=("integration",),
        ),
        review=ReviewPolicy(
            required=True,
            must_be_clean_before_ready_for_human=True,
            max_revision_loops=3,
        ),
        branch_policy=BranchPolicy(
            one_issue_one_branch_until_merge=True,
            new_branch_from_fresh_main_after_merge=True,
        ),
        block_on=BlockOnPolicy(
            format_failure=True,
            test_failure=True,
            unresolved_review_findings=True,
            unsafe_conflict_resolution=True,
        ),
    )


def _eligible_issue(issue_number: int, title: str) -> IssueCandidate:
    return IssueCandidate(
        number=issue_number,
        title=title,
        state="OPEN",
        labels=("ready-for-agent", "type:dev"),
        assignees=(),
        url=f"https://example.test/issues/{issue_number}",
    )


def _open_pr_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "number": 17,
        "url": "https://example.test/pr/17",
        "state": "OPEN",
        "isDraft": False,
        "mergedAt": None,
        "headRefName": "sandcastle/issue-306-main-health",
        "headRefOid": "abc123",
        "baseRefName": "main",
        "reviewDecision": "APPROVED",
        "mergeStateStatus": "CLEAN",
        "mergeable": "MERGEABLE",
    }
    payload.update(overrides)
    return payload


class MainHealthTests(unittest.TestCase):
    def test_cleanup_waits_for_post_merge_health_before_closing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = _paths(root)
            worktree_path = root / ".sandcastle-worktrees" / "issue-306"
            worktree_path.mkdir(parents=True)
            merged_status = IssueStatus(
                issue_number=306,
                status="merged",
                branch="sandcastle/issue-306-main-health",
                worktree_path=str(worktree_path),
                pr_number=17,
                extra={
                    "issue_title": "Main health and incidents",
                    "pr_url": "https://example.test/pr/17",
                    "pr_merged_at": "2026-10-27T04:55:00+00:00",
                    "pr_head_sha": "abc123",
                },
            )
            write_issue_status(paths, merged_status)

            with patch(
                "sandcastle.runner.main._sync_pr_state",
                return_value=merged_status,
            ):
                with patch("sandcastle.runner.main.fetch_base_ref"):
                    with patch("sandcastle.runner.main.remove_worktree"):
                        with patch(
                            "sandcastle.runner.main.branch_exists", return_value=True
                        ):
                            with patch(
                                "sandcastle.runner.main.delete_local_branch"
                            ):
                                with patch(
                                    "sandcastle.runner.main.remote_branch_exists",
                                    return_value=True,
                                ):
                                    with patch(
                                        "sandcastle.runner.main.delete_remote_branch"
                                    ):
                                        with patch(
                                            "sandcastle.runner.main.get_issue"
                                        ) as get_issue:
                                            exit_code = _handle_cleanup(
                                                root, paths, _repo_config(), 306
                                            )
            persisted = try_load_issue_status(paths, 306)

        self.assertEqual(exit_code, 0)
        get_issue.assert_not_called()
        self.assertIsNotNone(persisted)
        assert persisted is not None
        self.assertEqual(persisted.status, POST_MERGE_HEALTH_STATUS)
        self.assertEqual(
            persisted.extra["completion_state"], "awaiting-post-merge-health"
        )
        self.assertFalse(persisted.extra["completion_ready"])

    def test_post_merge_failure_creates_one_incident_without_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = _paths(root)
            write_issue_status(
                paths,
                IssueStatus(
                    issue_number=306,
                    status=POST_MERGE_HEALTH_STATUS,
                    pr_number=17,
                    extra={
                        "issue_title": "Main health and incidents",
                        "cleanup_completed": True,
                        "pr_head_sha": "merge-sha-1",
                    },
                ),
            )
            failed_health_path = write_json_file(
                paths.ci_dir / "306-post-merge.json",
                {
                    "issue_number": 306,
                    "branch_name": "main",
                    "head_sha": "main-head-1",
                    "status": "failed",
                    "checks": [{"name": "tests", "status": "COMPLETED"}],
                    "failed_checks": [{"name": "tests", "log_excerpt": "boom"}],
                    "completed": True,
                    "saw_checks": True,
                },
            )

            with patch(
                "sandcastle.runner.main.snapshot_main_checks",
                return_value=failed_health_path,
            ):
                first_exit_code = _handle_post_merge_health(
                    root, paths, _repo_config(), 306
                )
                second_exit_code = _handle_post_merge_health(
                    root, paths, _repo_config(), 306
                )

            incidents = read_json_file(paths.state_dir / "incidents.json")["incidents"]
            main_health = read_json_file(paths.state_dir / "main-health.json")
            persisted = try_load_issue_status(paths, 306)

        self.assertEqual(first_exit_code, 0)
        self.assertEqual(second_exit_code, 0)
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0]["status"], "open")
        self.assertEqual(incidents[0]["issue_number"], 306)
        self.assertEqual(main_health["status"], "unhealthy")
        self.assertIsNotNone(persisted)
        assert persisted is not None
        self.assertEqual(persisted.status, POST_MERGE_HEALTH_STATUS)
        self.assertEqual(persisted.extra["post_merge_incident_id"], incidents[0]["id"])

    def test_post_merge_success_resolves_incident_and_closes_issue(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = _paths(root)
            write_issue_status(
                paths,
                IssueStatus(
                    issue_number=306,
                    status=POST_MERGE_HEALTH_STATUS,
                    pr_number=17,
                    extra={
                        "issue_title": "Main health and incidents",
                        "cleanup_completed": True,
                        "cleanup_actions": ["removed worktree C:/worktrees/issue-306"],
                        "post_merge_incident_id": "main-health-issue-306",
                    },
                ),
            )
            write_json_file(
                paths.state_dir / "incidents.json",
                {
                    "incidents": [
                        {
                            "id": "main-health-issue-306",
                            "status": "open",
                            "summary": "Main health failed after merge of issue #306.",
                            "issue_number": 306,
                            "pr_number": 17,
                            "requires_human_action": True,
                            "next_action": "Investigate the failing main build before allowing more merges.",
                        }
                    ]
                },
            )
            passed_health_path = write_json_file(
                paths.ci_dir / "306-post-merge.json",
                {
                    "issue_number": 306,
                    "branch_name": "main",
                    "head_sha": "main-head-2",
                    "status": "passed",
                    "checks": [{"name": "tests", "status": "COMPLETED"}],
                    "failed_checks": [],
                    "completed": True,
                    "saw_checks": True,
                },
            )

            with patch(
                "sandcastle.runner.main.snapshot_main_checks",
                return_value=passed_health_path,
            ):
                with patch(
                    "sandcastle.runner.main.get_issue",
                    return_value=_eligible_issue(306, "Main health and incidents"),
                ):
                    with patch(
                        "sandcastle.runner.main._render_completion_comment",
                        return_value="done",
                    ):
                        with patch("sandcastle.runner.main.close_issue") as close_issue:
                            exit_code = _handle_post_merge_health(
                                root, paths, _repo_config(), 306
                            )

            incidents = read_json_file(paths.state_dir / "incidents.json")["incidents"]
            main_health = read_json_file(paths.state_dir / "main-health.json")
            persisted = try_load_issue_status(paths, 306)

        self.assertEqual(exit_code, 0)
        close_issue.assert_called_once()
        self.assertEqual(main_health["status"], "healthy")
        self.assertEqual(incidents[0]["status"], "resolved")
        self.assertIsNotNone(persisted)
        assert persisted is not None
        self.assertEqual(persisted.status, "closed")
        self.assertTrue(persisted.extra["completion_ready"])

    def test_auto_step_does_not_pick_new_issue_while_main_is_unhealthy(self) -> None:
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = _paths(root)
            write_json_file(
                paths.state_dir / "main-health.json",
                {
                    "status": "unhealthy",
                    "reason": "post-merge CI failed on main",
                    "incident_issue_number": 401,
                    "next_action": "Investigate the failed main build before allowing more merges.",
                },
            )

            with patch(
                "sandcastle.runner.main._select_backlog_issue",
                return_value=BacklogSelection(
                    selected_issue=_eligible_issue(307, "Fresh backlog work")
                ),
            ):
                with patch("sandcastle.runner.main._handle_run_issue") as run_issue:
                    with redirect_stdout(output):
                        exit_code = _handle_auto_step(
                            root,
                            paths,
                            _issue_policy(),
                            _repo_config(),
                            _gates_config(),
                        )

        self.assertEqual(exit_code, 0)
        run_issue.assert_not_called()
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["status"], "idle")
        self.assertIn("main is unhealthy", payload["message"])

    def test_auto_step_keeps_monitoring_post_merge_health_while_main_is_unhealthy(self) -> None:
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = _paths(root)
            write_json_file(
                paths.state_dir / "main-health.json",
                {
                    "status": "unhealthy",
                    "reason": "post-merge CI failed on main",
                    "incident_issue_number": 306,
                    "next_action": "Investigate the failed main build before allowing more merges.",
                },
            )
            write_issue_status(
                paths,
                IssueStatus(
                    issue_number=306,
                    status=POST_MERGE_HEALTH_STATUS,
                    pr_number=17,
                    extra={"issue_title": "Main health and incidents"},
                ),
            )
            write_issue_status(
                paths,
                IssueStatus(
                    issue_number=307,
                    status="implementing",
                    branch="sandcastle/issue-307-other-work",
                    worktree_path="C:/worktrees/issue-307",
                    extra={"issue_title": "Other work"},
                ),
            )

            def observe_main(*_args: object, **_kwargs: object) -> int:
                write_issue_status(
                    paths,
                    IssueStatus(
                        issue_number=306,
                        status="closed",
                        pr_number=17,
                        extra={"issue_title": "Main health and incidents"},
                    ),
                )
                return 0

            with patch(
                "sandcastle.runner.main._handle_post_merge_health",
                side_effect=observe_main,
            ):
                with patch(
                    "sandcastle.runner.main._select_backlog_issue",
                    return_value=BacklogSelection(selected_issue=None),
                ):
                    with patch(
                        "sandcastle.runner.main._advance_active_issue"
                    ) as advance_active_issue:
                        with redirect_stdout(output):
                            exit_code = _handle_auto_step(
                                root,
                                paths,
                                _issue_policy(),
                                _repo_config(),
                                _gates_config(),
                            )

        self.assertEqual(exit_code, 0)
        advance_active_issue.assert_not_called()
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["chosen_issue_number"], 306)
        self.assertEqual(payload["status"], "closed")

    def test_sync_pr_does_not_auto_merge_while_main_is_unhealthy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = _paths(root)
            write_json_file(
                paths.state_dir / "main-health.json",
                {
                    "status": "unhealthy",
                    "reason": "post-merge CI failed on main",
                    "incident_issue_number": 401,
                    "next_action": "Investigate the failed main build before allowing more merges.",
                },
            )
            write_issue_status(
                paths,
                IssueStatus(
                    issue_number=306,
                    status="waiting-human-review",
                    branch="sandcastle/issue-306-main-health",
                    worktree_path="C:/worktrees/issue-306",
                    pr_number=17,
                    extra={
                        "issue_title": "Main health and incidents",
                        "ci_status": "passed",
                        "findings_status": "clean",
                        "implementation_path": "C:/state/implementation/306.json",
                        "implementation_head_sha": "abc123",
                        "verified_implementation_path": "C:/state/implementation/306.json",
                        "verified_head_sha": "abc123",
                        "reviewed_implementation_path": "C:/state/implementation/306.json",
                        "reviewed_head_sha": "abc123",
                        "findings_source_implementation_path": "C:/state/implementation/306.json",
                        "findings_source_head_sha": "abc123",
                        "pr_head_sha": "abc123",
                    },
                ),
            )

            with patch(
                "sandcastle.runner.main.snapshot_pr_checks",
                side_effect=lambda **kwargs: write_json_file(
                    kwargs["ci_dir"] / f"{kwargs['issue_number']}.json",
                    {
                        "issue_number": kwargs["issue_number"],
                        "pr_number": kwargs["pr_number"],
                        "status": "passed",
                        "checks": [],
                        "failed_checks": [],
                    },
                ),
            ):
                with patch(
                    "sandcastle.runner.main.collect_findings",
                    side_effect=lambda **kwargs: write_json_file(
                        kwargs["findings_dir"] / f"{kwargs['issue_number']}.json",
                        {
                            "issue_number": kwargs["issue_number"],
                            "pr_number": kwargs["pr_number"],
                            "status": "clean",
                            "findings": [],
                        },
                    ),
                ):
                    with patch(
                        "sandcastle.runner.main.get_pr",
                        return_value=_open_pr_payload(),
                    ):
                        with patch(
                            "sandcastle.runner.main.get_pr_check_rollup",
                            return_value=[],
                        ):
                            with patch("sandcastle.runner.main.merge_pr") as merge_pr:
                                exit_code = _handle_sync_pr(
                                    root,
                                    paths,
                                    _repo_config(),
                                    306,
                                )
            persisted = try_load_issue_status(paths, 306)

        self.assertEqual(exit_code, 0)
        merge_pr.assert_not_called()
        self.assertIsNotNone(persisted)
        assert persisted is not None
        self.assertEqual(persisted.status, "approved-awaiting-merge")
        self.assertEqual(persisted.extra["auto_merge_status"], "waiting")
        self.assertIn("main is unhealthy", str(persisted.extra["auto_merge_reason"]))


if __name__ == "__main__":
    unittest.main()
