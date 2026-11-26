from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sandcastle.runner.main import _handle_auto_step
from sandcastle.runner.models import (
    BacklogSelection,
    ClaimPolicy,
    CleanupPolicy,
    CompletionPolicy,
    GithubPolicy,
    HandoffPolicy,
    ImplementationPolicy,
    IssuePolicy,
    IssueStatus,
    PullOrderPolicy,
    RepoConfig,
    SandcastlePaths,
    SchedulerPolicy,
    WorktreePolicy,
)
from sandcastle.runner.state_store import try_load_issue_status, write_issue_status


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


class ResumeSyncTests(unittest.TestCase):
    def test_auto_step_syncs_existing_branch_before_resumed_active_work(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = _paths(root)
            worktree_path = root / ".sandcastle-worktrees" / "issue-289"
            worktree_path.mkdir(parents=True)
            conflict_path = paths.conflict_dir / "289.json"
            conflict_path.parent.mkdir(parents=True, exist_ok=True)
            conflict_path.write_text(
                json.dumps({"status": "resolved", "resolution_kind": "clean-merge"}) + "\n",
                encoding="utf-8",
            )
            write_issue_status(
                paths,
                IssueStatus(
                    issue_number=289,
                    status="review-fix-loop",
                    branch="sandcastle/issue-289",
                    worktree_path=str(worktree_path),
                    pr_number=17,
                    extra={"issue_title": "Resume sync"},
                ),
            )
            output = io.StringIO()

            with patch(
                "sandcastle.runner.main.branch_behind_count", return_value=2
            ):
                with patch("sandcastle.runner.main.ref_sha", return_value="abc123"):
                    with patch("sandcastle.runner.main.resolve_pr_conflicts", return_value=conflict_path):
                        with patch("sandcastle.runner.main.push_branch") as push_branch:
                            with patch("sandcastle.runner.main.fetch_base_ref"):
                                with patch("sandcastle.runner.main._select_backlog_issue", return_value=BacklogSelection(selected_issue=None)):
                                    with patch("sandcastle.runner.main._handle_revise") as revise:
                                        with redirect_stdout(output):
                                            exit_code = _handle_auto_step(
                                                root,
                                                paths,
                                                _issue_policy(),
                                                _repo_config(),
                                                SimpleNamespace(),
                                            )

            self.assertEqual(exit_code, 0)
            revise.assert_not_called()
            push_branch.assert_called_once_with(root, "origin", "sandcastle/issue-289")
            persisted = try_load_issue_status(paths, 289)
            self.assertIsNotNone(persisted)
            assert persisted is not None
            self.assertEqual(persisted.status, "review-fix-loop")
            self.assertEqual(persisted.extra["resume_sync_status"], "resolved")
            self.assertEqual(persisted.extra["resume_sync_resolution_kind"], "clean-merge")
            self.assertEqual(persisted.extra["resume_sync_base_sha"], "abc123")

    def test_auto_step_blocks_when_resume_sync_is_not_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = _paths(root)
            worktree_path = root / ".sandcastle-worktrees" / "issue-289"
            worktree_path.mkdir(parents=True)
            conflict_path = paths.conflict_dir / "289.json"
            conflict_path.parent.mkdir(parents=True, exist_ok=True)
            conflict_path.write_text(
                json.dumps({"status": "blocked", "blocked_reason": "semantic conflict"}) + "\n",
                encoding="utf-8",
            )
            local_status = IssueStatus(
                issue_number=289,
                status="review-fix-loop",
                branch="sandcastle/issue-289",
                worktree_path=str(worktree_path),
                pr_number=17,
                extra={"issue_title": "Resume sync"},
            )
            write_issue_status(paths, local_status)
            blocked = IssueStatus(
                issue_number=289,
                status="blocked",
                branch=local_status.branch,
                worktree_path=local_status.worktree_path,
                pr_number=local_status.pr_number,
                extra={"blocked_reason": "semantic conflict"},
            )

            with patch("sandcastle.runner.main.branch_behind_count", return_value=1):
                with patch("sandcastle.runner.main.ref_sha", return_value="abc123"):
                    with patch("sandcastle.runner.main.resolve_pr_conflicts", return_value=conflict_path):
                        with patch("sandcastle.runner.main.fetch_base_ref"):
                            with patch("sandcastle.runner.main._select_backlog_issue", return_value=BacklogSelection(selected_issue=None)):
                                with patch("sandcastle.runner.main._block_issue", return_value=blocked) as block_issue:
                                    with patch("sandcastle.runner.main._handle_revise") as revise:
                                        exit_code = _handle_auto_step(
                                            root,
                                            paths,
                                            _issue_policy(),
                                            _repo_config(),
                                            SimpleNamespace(),
                                        )

            self.assertEqual(exit_code, 1)
            revise.assert_not_called()
            block_issue.assert_called_once()


if __name__ == "__main__":
    unittest.main()
