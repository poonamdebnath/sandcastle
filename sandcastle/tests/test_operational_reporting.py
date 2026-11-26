import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from sandcastle.runner.main import _handle_morning_handover, _handle_report
from sandcastle.runner.models import (
    CleanupPolicy,
    GithubPolicy,
    ImplementationPolicy,
    IssueStatus,
    RepoConfig,
    SandcastlePaths,
    SchedulerPolicy,
    WorktreePolicy,
)
from sandcastle.runner.state_store import write_issue_status, write_json_file


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


class OperationalReportingTests(unittest.TestCase):
    def test_report_command_persists_queue_report_and_handover(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = _paths(root)
            write_issue_status(
                paths,
                IssueStatus(
                    issue_number=307,
                    status="implementing",
                    branch="sandcastle/issue-307-reporting",
                    worktree_path="C:/worktrees/issue-307",
                    extra={
                        "issue_title": "Operational reporting",
                        "verification_status": "passed",
                        "review_status": "clean",
                    },
                    last_updated_utc="2026-10-27T05:00:00+00:00",
                ),
            )
            write_issue_status(
                paths,
                IssueStatus(
                    issue_number=308,
                    status="waiting-human-review",
                    branch="sandcastle/issue-308-docs",
                    worktree_path="C:/worktrees/issue-308",
                    pr_number=18,
                    extra={
                        "issue_title": "Architecture guide",
                        "pr_url": "https://example.test/pr/18",
                        "ci_status": "passed",
                        "findings_status": "clean",
                    },
                    last_updated_utc="2026-10-27T05:10:00+00:00",
                ),
            )
            write_issue_status(
                paths,
                IssueStatus(
                    issue_number=309,
                    status="blocked",
                    branch="sandcastle/issue-309-runbook",
                    worktree_path="C:/worktrees/issue-309",
                    pr_number=19,
                    extra={
                        "issue_title": "Runbook",
                        "blocked_reason": "review findings conflict with the issue intent",
                        "blocked_next_action": "Clarify whether the review finding or the issue should win.",
                    },
                    last_updated_utc="2026-10-27T05:20:00+00:00",
                ),
            )
            write_issue_status(
                paths,
                IssueStatus(
                    issue_number=306,
                    status="closed",
                    branch=None,
                    worktree_path=None,
                    pr_number=17,
                    extra={
                        "issue_title": "Main health and incidents",
                        "pr_url": "https://example.test/pr/17",
                        "pr_merged_at": "2026-10-27T04:55:00+00:00",
                        "cleanup_completed": True,
                        "closed_branch": "sandcastle/issue-306-main-health",
                    },
                    last_updated_utc="2026-10-27T04:58:00+00:00",
                ),
            )

            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = _handle_report(paths, _repo_config())

            self.assertEqual(exit_code, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["queue_health"]["status"], "attention-needed")
            self.assertEqual(payload["queue_health"]["blocked_issue_count"], 1)
            self.assertIn(
                "unrelated eligible work can still continue",
                payload["queue_health"]["summary"],
            )
            self.assertEqual(payload["current_issue"]["issue_number"], 307)
            self.assertEqual(
                payload["current_issue"]["active_agent_role"], "development-agent"
            )
            self.assertIn(
                "queue work continues on issue #307",
                payload["morning_handover"]["headline"],
            )
            self.assertEqual(payload["last_successful_merge"]["issue_number"], 306)
            self.assertEqual(payload["pending_approvals"][0]["issue_number"], 308)
            self.assertIn(
                "Clarify whether the review finding or the issue should win.",
                [item["next_action"] for item in payload["required_human_actions"]],
            )

            persisted_report = json.loads(
                (paths.state_dir / "operator-report.json").read_text(encoding="utf-8")
            )
            persisted_handover = json.loads(
                (paths.state_dir / "morning-handover.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                persisted_report["current_issue"]["issue_number"],
                payload["current_issue"]["issue_number"],
            )
            self.assertEqual(
                persisted_handover["headline"],
                payload["morning_handover"]["headline"],
            )

    def test_report_command_highlights_unhealthy_main_and_open_incidents(self) -> None:
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
            write_json_file(
                paths.state_dir / "incidents.json",
                {
                    "incidents": [
                        {
                            "id": "main-ci-401",
                            "status": "open",
                            "summary": "Main CI failed after the latest merge.",
                            "issue_number": 401,
                            "requires_human_action": True,
                            "next_action": "Investigate the failing pipeline and decide whether to revert.",
                        }
                    ]
                },
            )

            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = _handle_report(paths, _repo_config())

            self.assertEqual(exit_code, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["queue_health"]["status"], "unhealthy-main")
            self.assertFalse(payload["main_health"]["healthy"])
            self.assertEqual(payload["open_incidents"][0]["id"], "main-ci-401")
            self.assertIn(
                "Investigate the failed main build before allowing more merges.",
                [item["next_action"] for item in payload["required_human_actions"]],
            )

    def test_morning_handover_is_restart_safe_from_persisted_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = _paths(root)
            write_issue_status(
                paths,
                IssueStatus(
                    issue_number=307,
                    status="blocked",
                    branch="sandcastle/issue-307-reporting",
                    worktree_path="C:/worktrees/issue-307",
                    extra={
                        "issue_title": "Operational reporting",
                        "blocked_reason": "cleanup retry is still pending",
                        "blocked_next_action": "Retry cleanup and verify the remote branch deletion.",
                    },
                    last_updated_utc="2026-10-27T06:00:00+00:00",
                ),
            )
            write_issue_status(
                paths,
                IssueStatus(
                    issue_number=306,
                    status="closed",
                    pr_number=17,
                    extra={
                        "issue_title": "Main health and incidents",
                        "pr_url": "https://example.test/pr/17",
                        "pr_merged_at": "2026-10-27T05:55:00+00:00",
                        "cleanup_completed": True,
                    },
                    last_updated_utc="2026-10-27T05:58:00+00:00",
                ),
            )

            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = _handle_morning_handover(paths, _repo_config())

            self.assertEqual(exit_code, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["queue_health"]["status"], "attention-needed")
            self.assertEqual(payload["last_successful_merge"]["issue_number"], 306)
            self.assertEqual(payload["blocked_issues"][0]["issue_number"], 307)
            self.assertIn(
                "unrelated eligible work can continue",
                payload["headline"],
            )
            self.assertEqual(
                payload["required_human_actions"][0]["next_action"],
                "Retry cleanup and verify the remote branch deletion.",
            )


if __name__ == "__main__":
    unittest.main()
